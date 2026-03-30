from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from app.config import CMP_SESSION_ROOT, RENDERDOC_CMP_ROOT, RENDERDOC_CMP_SCRIPT
from app.services.script_runner import run_python_script_inproc


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class RenderdocCmpService:
    def __init__(self, cmp_root: Path | None = None, cmp_script: Path | None = None) -> None:
        self.cmp_root = (cmp_root or RENDERDOC_CMP_ROOT).resolve()
        self.cmp_script = (cmp_script or RENDERDOC_CMP_SCRIPT).resolve()
        self.session_root = CMP_SESSION_ROOT
        self.session_root.mkdir(parents=True, exist_ok=True)

    def create_job(self, title: str = "RenderDoc CMP") -> Dict[str, Any]:
        job_id = f"cmp-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
        job_dir = self.session_root / job_id
        inputs_dir = job_dir / "inputs"
        work_dir = job_dir / "workdir"
        report_dir = job_dir / "report"
        for path in (inputs_dir, work_dir, report_dir):
            path.mkdir(parents=True, exist_ok=True)

        metadata = {
            "job_id": job_id,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "status": "created",
            "title": title,
            "inputs": {},
            "artifacts": {},
        }
        (job_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return metadata

    def save_input_file(self, job_id: str, name: str, content: bytes) -> Path:
        dest = self._job_dir(job_id) / "inputs" / name
        dest.write_bytes(content)
        return dest

    def update_metadata(self, job_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        path = self._job_dir(job_id) / "metadata.json"
        metadata = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        metadata = self._deep_merge(metadata, patch)
        metadata["updated_at"] = _now_iso()
        path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return metadata

    def run_compare(
        self,
        job_id: str,
        base_file: Path,
        new_file: Path,
        strict_mode: bool = False,
        renderdoc_dir: str = "",
        malioc_path: str = "",
        verbose: bool = False,
    ) -> Dict[str, Any]:
        if not self.cmp_script.exists():
            raise FileNotFoundError(f"renderdoc_cmp script not found: {self.cmp_script}")

        job_dir = self._job_dir(job_id)
        work_dir = job_dir / "workdir"
        run_log = job_dir / "report" / "cmp_run_log.txt"
        script_args = [str(base_file), str(new_file)]
        if strict_mode:
            script_args.append("--strict")
        if renderdoc_dir.strip():
            script_args.extend(["--renderdoc", renderdoc_dir.strip()])
        if malioc_path.strip():
            script_args.extend(["--malioc", malioc_path.strip()])
        if verbose:
            script_args.append("--verbose")

        if getattr(sys, "frozen", False):
            returncode, combined_output = run_python_script_inproc(self.cmp_script, script_args, cwd=work_dir)
        else:
            cmd = [sys.executable, str(self.cmp_script), *script_args]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(work_dir),
                shell=False,
            )
            returncode = proc.returncode
            combined_output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        run_log.write_text(combined_output, encoding="utf-8", errors="replace")
        if returncode != 0:
            raise RuntimeError(combined_output.strip() or "renderdoc_cmp 执行失败")

        self._assert_supported_output(combined_output)

        generated_dir = work_dir / "output" / "rdc_comparison_output"
        if not generated_dir.exists():
            raise RuntimeError(f"未找到比较结果目录: {generated_dir}")

        target_report_dir = job_dir / "report" / "cmp_output"
        if target_report_dir.exists():
            shutil.rmtree(target_report_dir)
        shutil.copytree(generated_dir, target_report_dir)

        html_path = target_report_dir / "comparison_report.html"
        if not html_path.exists():
            raise RuntimeError("未生成 comparison_report.html")

        metadata = self.update_metadata(
            job_id,
            {
                "status": "completed",
                "inputs": {
                    "base_file": self._path_ref(job_dir, base_file),
                    "new_file": self._path_ref(job_dir, new_file),
                    "strict_mode": strict_mode,
                    "renderdoc_dir": renderdoc_dir.strip(),
                    "malioc_path": malioc_path.strip(),
                },
                "artifacts": {
                    "report_html": "report/cmp_output/comparison_report.html",
                    "report_dir": "report/cmp_output",
                    "run_log": "report/cmp_run_log.txt",
                },
            },
        )
        return {
            "metadata": metadata,
            "report_url": f"/cmp-session-files/{job_id}/report/cmp_output/comparison_report.html",
            "run_log": combined_output,
        }

    @staticmethod
    def _assert_supported_output(run_log: str) -> None:
        lower = run_log.lower()
        if "driver: d3d11" in lower and "[opengl mode]" in lower:
            raise RuntimeError(
                "renderdoc_cmp 当前脚本把 D3D11 capture 错误地按 OpenGL 路径解析，"
                "会导致 draw call / shader / texture 统计失真。当前版本不适用于这类 D3D11 .rdc。"
            )
        if "found 0 draw calls" in lower and ("driver: d3d11" in lower or "driver: d3d12" in lower):
            raise RuntimeError(
                "renderdoc_cmp 对当前 Direct3D capture 未正确解析出 draw calls，"
                "结果不可用。建议暂时只用于其明确支持的 capture 类型，或后续对脚本做 D3D 分支适配。"
            )

    def list_jobs(self) -> list[dict]:
        jobs = []
        for job_dir in sorted(self.session_root.iterdir(), reverse=True):
            if not job_dir.is_dir():
                continue
            metadata_file = job_dir / "metadata.json"
            if not metadata_file.exists():
                continue
            jobs.append(json.loads(metadata_file.read_text(encoding="utf-8", errors="replace")))
        jobs.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return jobs

    def get_job_detail(self, job_id: str) -> Dict[str, Any]:
        job_dir = self._job_dir(job_id)
        metadata = json.loads((job_dir / "metadata.json").read_text(encoding="utf-8", errors="replace"))
        run_log_path = job_dir / "report" / "cmp_run_log.txt"
        return {
            "metadata": metadata,
            "report_url": (
                f"/cmp-session-files/{job_id}/report/cmp_output/comparison_report.html"
                if (job_dir / "report" / "cmp_output" / "comparison_report.html").exists()
                else ""
            ),
            "run_log": run_log_path.read_text(encoding="utf-8", errors="replace") if run_log_path.exists() else "",
        }

    def _job_dir(self, job_id: str) -> Path:
        path = self.session_root / job_id
        if not path.exists():
            raise FileNotFoundError(f"cmp job not found: {job_id}")
        return path

    @staticmethod
    def _path_ref(job_dir: Path, path: Path) -> str:
        try:
            return str(path.relative_to(job_dir)).replace("\\", "/")
        except ValueError:
            return str(path)

    @staticmethod
    def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                base[key] = RenderdocCmpService._deep_merge(base[key], value)
            else:
                base[key] = value
        return base
