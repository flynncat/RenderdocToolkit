from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4

from app.config import PERF_SESSION_ROOT


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class RenderdocPerfStore:
    def __init__(self, session_root: Path | None = None) -> None:
        self.session_root = session_root or PERF_SESSION_ROOT
        self.session_root.mkdir(parents=True, exist_ok=True)

    def create_job(self, title: str) -> Dict[str, Any]:
        job_id = f"perf-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
        job_dir = self.session_root / job_id
        for child in (
            job_dir / "inputs",
            job_dir / "artifacts" / "previews",
        ):
            child.mkdir(parents=True, exist_ok=True)

        metadata = {
            "job_id": job_id,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "status": "created",
            "title": title,
            "inputs": {},
            "summary": {},
            "artifacts": {
                "analysis_json": "artifacts/perf_analysis.json",
                "run_log": "artifacts/perf_run_log.txt",
            },
        }
        self._write_json(job_dir / "metadata.json", metadata)
        self._write_json(job_dir / "artifacts" / "perf_analysis.json", {})
        (job_dir / "artifacts" / "perf_run_log.txt").write_text("", encoding="utf-8")
        return metadata

    def list_jobs(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for job_dir in sorted(self.session_root.iterdir(), reverse=True):
            if not job_dir.is_dir():
                continue
            metadata_file = job_dir / "metadata.json"
            if metadata_file.exists():
                items.append(self._read_json(metadata_file))
        items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return items

    def load_metadata(self, job_id: str) -> Dict[str, Any]:
        return self._read_json(self.job_path(job_id) / "metadata.json")

    def update_metadata(self, job_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        metadata = self.load_metadata(job_id)
        metadata = self._deep_merge(metadata, patch)
        metadata["updated_at"] = _now_iso()
        self._write_json(self.job_path(job_id) / "metadata.json", metadata)
        return metadata

    def save_input_file(self, job_id: str, filename: str, content: bytes) -> Path:
        path = self.job_path(job_id) / "inputs" / filename
        path.write_bytes(content)
        return path

    def write_json_artifact(self, job_id: str, relative_path: str, payload: Any) -> Path:
        path = self.job_path(job_id) / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json(path, payload)
        return path

    def write_text_artifact(self, job_id: str, relative_path: str, content: str) -> Path:
        path = self.job_path(job_id) / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def get_job_detail(self, job_id: str) -> Dict[str, Any]:
        job_dir = self.job_path(job_id)
        metadata = self.load_metadata(job_id)
        analysis_path = job_dir / "artifacts" / "perf_analysis.json"
        run_log_path = job_dir / "artifacts" / "perf_run_log.txt"
        return {
            "metadata": metadata,
            "analysis": self._read_json(analysis_path) if analysis_path.exists() else {},
            "run_log": run_log_path.read_text(encoding="utf-8", errors="replace") if run_log_path.exists() else "",
        }

    def job_path(self, job_id: str) -> Path:
        path = self.session_root / job_id
        if not path.exists():
            raise FileNotFoundError(f"performance job not found: {job_id}")
        return path

    @staticmethod
    def _read_json(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                base[key] = RenderdocPerfStore._deep_merge(base[key], value)
            else:
                base[key] = value
        return base
