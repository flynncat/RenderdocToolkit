from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from app.config import SESSION_ROOT


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class SessionStore:
    def __init__(self, session_root: Path | None = None) -> None:
        self.session_root = session_root or SESSION_ROOT
        self.session_root.mkdir(parents=True, exist_ok=True)

    def create_session(
        self,
        pass_name: str,
        issue: str,
        eid_before: str = "",
        eid_after: str = "",
    ) -> Dict[str, Any]:
        session_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
        session_dir = self.session_root / session_id
        inputs_dir = session_dir / "inputs"
        analysis_dir = session_dir / "analysis"
        inputs_dir.mkdir(parents=True, exist_ok=True)
        analysis_dir.mkdir(parents=True, exist_ok=True)

        metadata = {
            "session_id": session_id,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "status": "created",
            "inputs": {
                "before_file": "",
                "after_file": "",
                "pass_name": pass_name,
                "issue": issue,
                "eid_before": eid_before,
                "eid_after": eid_after,
            },
            "artifacts": {
                "analysis_md": "analysis/analysis.md",
                "analysis_json": "analysis/analysis.json",
                "raw_diff": "analysis/raw_diff.txt",
                "run_log": "analysis/run_log.txt",
            },
            "summary": {
                "title": issue[:40] or "RenderDoc 分析",
                "top_cause": "",
                "confidence": "",
            },
        }
        self._write_json(session_dir / "metadata.json", metadata)
        self._write_json(session_dir / "chat_history.json", [])
        return metadata

    def save_input_file(self, session_id: str, filename: str, content: bytes) -> str:
        session_dir = self._session_dir(session_id)
        inputs_dir = session_dir / "inputs"
        inputs_dir.mkdir(parents=True, exist_ok=True)
        dest = inputs_dir / filename
        dest.write_bytes(content)
        return str(dest)

    def update_metadata(self, session_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        metadata = self.load_metadata(session_id)
        metadata = self._deep_merge(metadata, patch)
        metadata["updated_at"] = _now_iso()
        self._write_json(self._session_dir(session_id) / "metadata.json", metadata)
        return metadata

    def append_chat(self, session_id: str, role: str, content: str, sources: Optional[List[str]] = None) -> None:
        chat = self.load_chat(session_id)
        item = {
            "role": role,
            "content": content,
            "created_at": _now_iso(),
        }
        if sources:
            item["sources"] = sources
        chat.append(item)
        self._write_json(self._session_dir(session_id) / "chat_history.json", chat)
        self.update_metadata(session_id, {})

    def load_metadata(self, session_id: str) -> Dict[str, Any]:
        return self._read_json(self._session_dir(session_id) / "metadata.json")

    def load_chat(self, session_id: str) -> List[Dict[str, Any]]:
        return self._read_json(self._session_dir(session_id) / "chat_history.json")

    def load_analysis_json(self, session_id: str) -> Optional[Dict[str, Any]]:
        path = self._session_dir(session_id) / "analysis" / "analysis.json"
        if not path.exists():
            return None
        return self._read_json(path)

    def load_analysis_markdown(self, session_id: str) -> str:
        path = self._session_dir(session_id) / "analysis" / "analysis.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def load_eid_deep_dive_json(self, session_id: str) -> Optional[Dict[str, Any]]:
        path = self._session_dir(session_id) / "analysis" / "eid_deep_dive.json"
        if not path.exists():
            return None
        return self._read_json(path)

    def load_eid_deep_dive_markdown(self, session_id: str) -> str:
        path = self._session_dir(session_id) / "analysis" / "eid_deep_dive.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def load_ue_scan_json(self, session_id: str) -> Optional[Dict[str, Any]]:
        path = self._session_dir(session_id) / "analysis" / "ue_scan.json"
        if not path.exists():
            return None
        return self._read_json(path)

    def load_ue_scan_markdown(self, session_id: str) -> str:
        path = self._session_dir(session_id) / "analysis" / "ue_scan.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def list_sessions(self) -> List[Dict[str, Any]]:
        items = []
        for session_dir in sorted(self.session_root.iterdir(), reverse=True):
            if not session_dir.is_dir():
                continue
            metadata_file = session_dir / "metadata.json"
            if not metadata_file.exists():
                continue
            items.append(self._read_json(metadata_file))
        items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return items

    def get_session_detail(self, session_id: str) -> Dict[str, Any]:
        metadata = self.load_metadata(session_id)
        return {
            "metadata": metadata,
            "analysis_markdown": self.load_analysis_markdown(session_id),
            "analysis_json": self.load_analysis_json(session_id),
            "eid_deep_dive_markdown": self.load_eid_deep_dive_markdown(session_id),
            "eid_deep_dive_json": self.load_eid_deep_dive_json(session_id),
            "ue_scan_markdown": self.load_ue_scan_markdown(session_id),
            "ue_scan_json": self.load_ue_scan_json(session_id),
            "chat_history": self.load_chat(session_id),
        }

    def _session_dir(self, session_id: str) -> Path:
        path = self.session_root / session_id
        if not path.exists():
            raise FileNotFoundError(f"session not found: {session_id}")
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
                base[key] = SessionStore._deep_merge(base[key], value)
            else:
                base[key] = value
        return base
