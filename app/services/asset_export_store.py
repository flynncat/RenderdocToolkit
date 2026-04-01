from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4

from app.config import EXPORT_JOB_ROOT


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class AssetExportStore:
    def __init__(self, job_root: Path | None = None) -> None:
        self.job_root = job_root or EXPORT_JOB_ROOT
        self.job_root.mkdir(parents=True, exist_ok=True)

    def create_job(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        job_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
        job_dir = self.job_root / job_id
        for child in (
            job_dir / "inputs",
            job_dir / "exports" / "csv",
            job_dir / "exports" / "models",
            job_dir / "exports" / "shaders",
            job_dir / "exports" / "textures",
            job_dir / "artifacts",
        ):
            child.mkdir(parents=True, exist_ok=True)

        metadata = {
            "job_id": job_id,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "status": "created",
            "input": payload,
            "progress": {
                "stage": "created",
                "message": "",
                "current": 0,
                "total": 0,
            },
            "artifacts": {
                "capture_file": "",
                "job_log": "artifacts/job.log",
                "manifest_json": "artifacts/manifest.json",
                "mapping_json": "artifacts/mapping.json",
            },
            "result": {
                "selected_passes": [],
                "csv_files": [],
                "model_files": [],
                "shader_files": [],
                "texture_files": [],
                "failed_items": [],
            },
        }
        self._write_json(job_dir / "metadata.json", metadata)
        self._write_json(job_dir / "artifacts" / "manifest.json", {"items": []})
        self._write_json(job_dir / "artifacts" / "mapping.json", {})
        return metadata

    def list_jobs(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for job_dir in sorted(self.job_root.iterdir(), reverse=True):
            if not job_dir.is_dir():
                continue
            metadata_file = job_dir / "metadata.json"
            if metadata_file.exists():
                items.append(self._read_json(metadata_file))
        items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return items

    def load_metadata(self, job_id: str) -> Dict[str, Any]:
        return self._read_json(self._job_dir(job_id) / "metadata.json")

    def update_metadata(self, job_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        metadata = self.load_metadata(job_id)
        metadata = self._deep_merge(metadata, patch)
        metadata["updated_at"] = _now_iso()
        self._write_json(self._job_dir(job_id) / "metadata.json", metadata)
        return metadata

    def save_input_file(self, job_id: str, filename: str, content: bytes) -> str:
        path = self._job_dir(job_id) / "inputs" / filename
        path.write_bytes(content)
        return str(path)

    def write_text_artifact(self, job_id: str, relative_path: str, content: str) -> str:
        path = self._job_dir(job_id) / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return str(path)

    def write_json_artifact(self, job_id: str, relative_path: str, payload: Any) -> str:
        path = self._job_dir(job_id) / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json(path, payload)
        return str(path)

    def get_job_detail(self, job_id: str) -> Dict[str, Any]:
        metadata = self.load_metadata(job_id)
        log_path = self._job_dir(job_id) / "artifacts" / "job.log"
        manifest_path = self._job_dir(job_id) / "artifacts" / "manifest.json"
        mapping_path = self._job_dir(job_id) / "artifacts" / "mapping.json"
        return {
            "metadata": metadata,
            "job_log": log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else "",
            "manifest": self._read_json(manifest_path) if manifest_path.exists() else {"items": []},
            "mapping": self._read_json(mapping_path) if mapping_path.exists() else {},
        }

    def job_path(self, job_id: str) -> Path:
        return self._job_dir(job_id)

    def _job_dir(self, job_id: str) -> Path:
        path = self.job_root / job_id
        if not path.exists():
            raise FileNotFoundError(f"asset export job not found: {job_id}")
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
                base[key] = AssetExportStore._deep_merge(base[key], value)
            else:
                base[key] = value
        return base
