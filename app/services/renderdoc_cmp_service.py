from __future__ import annotations

import json
import multiprocessing
import shutil
import subprocess
import sys
from datetime import datetime
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from app.config import CMP_SESSION_ROOT, RENDERDOC_CMP_ROOT, RENDERDOC_CMP_SCRIPT
from app.services.script_runner import run_python_script_inproc, run_python_script_inproc_to_file
from app.services.subprocess_utils import hidden_subprocess_kwargs


# Hard cap on cmp script wall-clock time.  The two-capture cmp pipeline does
# heavy texture decoding + per-shader malioc analysis and can legitimately run
# for 10-20 minutes on large UE captures.  We give it 45 minutes max so a
# truly stuck child gets killed instead of blocking the UI forever.
_CMP_SUBPROCESS_TIMEOUT_SECONDS = 45 * 60


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
        from app.services.renderdoc_runtime_resolver import resolve_renderdoc_runtime
        rd_ctx = resolve_renderdoc_runtime(renderdoc_dir)
        effective_renderdoc_dir = rd_ctx.renderdoc_dir if rd_ctx.renderdoc_dir else renderdoc_dir.strip()

        if not self.cmp_script.exists():
            raise FileNotFoundError(f"renderdoc_cmp script not found: {self.cmp_script}")

        job_dir = self._job_dir(job_id)
        work_dir = job_dir / "workdir"
        run_log = job_dir / "report" / "cmp_run_log.txt"
        script_args = [str(base_file), str(new_file)]
        if strict_mode:
            script_args.append("--strict")
        if effective_renderdoc_dir:
            script_args.extend(["--renderdoc", effective_renderdoc_dir])
        if malioc_path.strip():
            script_args.extend(["--malioc", malioc_path.strip()])
        if verbose:
            script_args.append("--verbose")

        # In frozen builds (portable .exe) we MUST run the cmp script in a
        # separate process — it does heavy texture decoding and Mali compiler
        # invocations that can OOM on large UE captures.  Running it in-proc
        # would kill the entire web server (and thus the desktop window).
        if getattr(sys, "frozen", False):
            returncode = self._run_compare_subprocess(script_args, work_dir, run_log)
            combined_output = run_log.read_text(encoding="utf-8", errors="replace") if run_log.exists() else ""
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
                **hidden_subprocess_kwargs(),
            )
            returncode = proc.returncode
            combined_output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
            run_log.write_text(combined_output, encoding="utf-8", errors="replace")
        if returncode != 0:
            tail = combined_output.strip()[-4000:] if combined_output else ""
            raise RuntimeError(tail or f"renderdoc_cmp 执行失败 (exit_code={returncode})")

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
                    "renderdoc_dir_requested": renderdoc_dir.strip(),
                    "renderdoc_dir_resolved": rd_ctx.renderdoc_dir,
                    "renderdoc_cmd_path": rd_ctx.renderdoc_cmd_path,
                    "renderdoc_source": rd_ctx.source,
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

    def _run_compare_subprocess(
        self,
        script_args: List[str],
        work_dir: Path,
        run_log: Path,
    ) -> int:
        """Run the cmp script inside an isolated child process.

        Streams its stdout/stderr live to *run_log*.  If the child crashes
        (OOM, segfault from a native extension, etc.) the parent service
        survives and reports a useful error instead of dying silently.
        """
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=False)
        process = ctx.Process(
            target=_cmp_worker_entry,
            args=(
                str(self.cmp_script),
                list(script_args),
                str(work_dir),
                str(run_log),
                child_conn,
            ),
            daemon=False,
        )
        process.start()
        child_conn.close()
        process.join(timeout=_CMP_SUBPROCESS_TIMEOUT_SECONDS)

        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            self._append_to_log(
                run_log,
                f"\n[cmp_service] subprocess timed out after "
                f"{_CMP_SUBPROCESS_TIMEOUT_SECONDS}s and was terminated.\n",
            )
            raise RuntimeError(
                f"cmp 子进程执行超时（>{_CMP_SUBPROCESS_TIMEOUT_SECONDS // 60} 分钟），已强制终止。"
            )

        result: Dict[str, Any] = {}
        if parent_conn.poll():
            try:
                result = parent_conn.recv()
            except EOFError:
                result = {}
        parent_conn.close()

        if process.exitcode not in (0, None):
            self._append_to_log(
                run_log,
                f"\n[cmp_service] subprocess exited with code "
                f"{process.exitcode} (likely OOM, segfault, or killed).\n",
            )
            raise RuntimeError(
                f"cmp 子进程异常退出 (exit_code={process.exitcode})，"
                f"可能由于内存不足或本地工具崩溃。"
                f"详见日志：{run_log}"
            )

        return int(result.get("rc", 1))

    @staticmethod
    def _append_to_log(log_path: Path, text: str) -> None:
        try:
            with log_path.open("a", encoding="utf-8", errors="replace") as f:
                f.write(text)
        except OSError:
            pass


def _cmp_worker_entry(
    script_path: str,
    argv: List[str],
    work_dir: str,
    output_file: str,
    conn: Connection,
) -> None:
    """Subprocess worker: run the cmp script with output streamed to a file.

    Lives in module scope so it is picklable for ``multiprocessing.spawn``.
    """
    out_path = Path(output_file)
    try:
        rc = run_python_script_inproc_to_file(
            Path(script_path),
            argv,
            out_path,
            cwd=Path(work_dir),
        )
        conn.send({"ok": True, "rc": rc})
    except Exception as exc:
        import traceback
        try:
            with out_path.open("a", encoding="utf-8", errors="replace") as f:
                f.write(f"\n[cmp_worker] uncaught {type(exc).__name__}: {exc}\n")
                traceback.print_exc(file=f)
        except OSError:
            pass
        try:
            conn.send({"ok": False, "rc": -1, "error": str(exc)})
        except OSError:
            pass
    finally:
        try:
            conn.close()
        except OSError:
            pass
