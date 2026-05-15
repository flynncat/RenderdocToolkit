"""Replay backend abstraction for perf/cmp analysis.

We support two backends today:

- ``QRenderdocScriptBackend`` (preferred when a usable ``qrenderdoc.exe`` is
  available).  Spawns ``qrenderdoc.exe --python <worker>`` so that the
  RenderDoc-replay capable Python interpreter that ships *inside qrenderdoc*
  is used for the heavy lifting.  This sidesteps the Python ABI mismatch
  between our packaged Python (3.13) and any RenderDoc fork that was built
  for Python 3.6 (or any other version).

- ``XmlFallbackBackend`` is the previous behaviour, used as a last resort.
  It only relies on ``renderdoccmd convert -c zip.xml`` plus our own ASTC
  decoder, so it works without GPU replay but cannot recover render-target
  contents or stream-loaded texture pixels.

The selection happens in :func:`select_replay_backend`.  Callers receive a
:class:`ReplayResult` dataclass that captures the manifest and any errors,
so they can integrate the data into the existing perf/cmp pipelines or
silently fall back to the XML path.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import app.config as app_config
from app.services.subprocess_utils import hidden_subprocess_kwargs

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ReplayResult:
    """What a replay backend returns to its caller.

    Even when ``ok`` is False, callers may inspect ``stderr_tail`` /
    ``error`` to decide whether to surface the failure or silently degrade.
    """

    ok: bool = False
    backend: str = ""
    output_dir: Optional[Path] = None
    manifest: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    stderr_tail: str = ""
    duration_seconds: float = 0.0

    def png_for_draw_rt(self, eid: int) -> Optional[Path]:
        if not (self.ok and self.manifest and self.output_dir):
            return None
        for d in self.manifest.get("draws", []):
            if int(d.get("eid", -1)) == int(eid):
                png = d.get("rt_png")
                if png:
                    p = self.output_dir / png
                    if p.exists() and p.stat().st_size > 0:
                        return p
        return None

    def textures_index(self) -> Dict[str, Path]:
        """Return ``{resource_id: png_path}`` for every dumped texture."""
        if not (self.ok and self.manifest and self.output_dir):
            return {}
        out: Dict[str, Path] = {}
        for t in self.manifest.get("textures", []):
            png = t.get("png")
            if not png:
                continue
            p = self.output_dir / png
            if p.exists() and p.stat().st_size > 0:
                out[str(t.get("resource_id"))] = p
        for d in self.manifest.get("draws", []):
            for t in d.get("textures", []) or []:
                png = t.get("png")
                if not png:
                    continue
                p = self.output_dir / png
                if p.exists() and p.stat().st_size > 0:
                    out.setdefault(str(t.get("resource_id")), p)
        return out

    def draws_index(self) -> Dict[int, Dict[str, Any]]:
        if not (self.ok and self.manifest):
            return {}
        return {int(d["eid"]): d for d in self.manifest.get("draws", []) if "eid" in d}


# ---------------------------------------------------------------------------
# qrenderdoc-based backend
# ---------------------------------------------------------------------------

def find_qrenderdoc(renderdoc_dir: str) -> Optional[Path]:
    """Return the path to ``qrenderdoc.exe`` inside *renderdoc_dir* if any.

    On non-Windows we'd look for ``qrenderdoc`` (no extension) instead.  The
    file existing is sufficient — we don't try to inspect the build's
    Python version here because Plan B1 explicitly uses qrenderdoc's
    *own* interpreter regardless of version.
    """
    if not renderdoc_dir:
        return None
    base = Path(renderdoc_dir)
    if not base.is_dir():
        return None
    candidates = [
        base / "qrenderdoc.exe",
        base / "qrenderdoc",
    ]
    for c in candidates:
        if c.exists() and c.is_file():
            return c
    return None


def find_worker_script() -> Path:
    """Locate ``qr_replay_worker.py`` for both source and PyInstaller layouts."""
    candidates: List[Path] = []
    # Source-layout: alongside rdc_compare_ultimate.py
    src_root = app_config.RENDERDOC_CMP_ROOT / "qr_replay_worker.py"
    candidates.append(src_root)

    # Frozen PyInstaller layout: _internal/external_tools/renderdoccmp/...
    if getattr(sys, "frozen", False):
        frozen_root = Path(sys.executable).parent / "_internal" / "external_tools" / "renderdoccmp" / "qr_replay_worker.py"
        candidates.append(frozen_root)

    for c in candidates:
        if c.exists():
            return c.resolve()
    # Last-ditch: source tree relative to this file
    return (Path(__file__).resolve().parents[2]
            / "external_tools" / "renderdoccmp" / "qr_replay_worker.py")


class QRenderdocScriptBackend:
    """Run our worker via ``qrenderdoc.exe --python``.

    The qrenderdoc executable in the user-selected RenderDoc directory must
    be present, but we don't otherwise probe it: any RenderDoc fork that
    embeds Python and exposes ``import renderdoc`` plus the documented
    ``pyrenderdoc`` global will work.  When that turns out not to be true
    (worker writes a manifest with ``ok=False`` or doesn't write one at
    all), the caller can fall back to the XML path.
    """

    name = "qrenderdoc_script"

    def __init__(self, qrenderdoc_path: Path, worker_script: Path,
                 timeout_seconds: int = 900) -> None:
        self.qrenderdoc_path = qrenderdoc_path
        self.worker_script = worker_script
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_renderdoc_dir(
        cls,
        renderdoc_dir: str,
        timeout_seconds: int = 900,
    ) -> Optional["QRenderdocScriptBackend"]:
        qr = find_qrenderdoc(renderdoc_dir)
        if qr is None:
            return None
        worker = find_worker_script()
        if not worker.exists():
            log.warning("qr_replay_worker.py not found at %s", worker)
            return None
        return cls(qr, worker, timeout_seconds=timeout_seconds)

    def is_available(self) -> bool:
        return self.qrenderdoc_path.exists() and self.worker_script.exists()

    # ------------------------------------------------------------------
    # Public entry-point
    # ------------------------------------------------------------------

    def run(
        self,
        capture_path: Path,
        output_dir: Path,
        mode: str = "perf",
        max_draws: int = 200,
        event_ids: Optional[List[int]] = None,
        max_extra_textures: int = 0,
    ) -> ReplayResult:
        """Drive a replay job.  Always returns a :class:`ReplayResult`.

        *output_dir* is created if missing.  All PNGs and ``manifest.json``
        end up inside it.  The caller owns cleanup.

        - ``mode="perf"`` runs the per-draw loop (RT + bound textures for
          each event in *event_ids* or the top-N hottest).
        - ``mode="cmp"`` skips per-draw work entirely and only dumps the
          global texture roster.  Use *max_extra_textures* to cap how many
          textures get exported as PNG.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        job_spec = {
            "capture": str(Path(capture_path).resolve()),
            "output_dir": str(output_dir.resolve()),
            "mode": mode,
            "max_draws": int(max_draws),
            "event_ids": list(event_ids or []),
            "max_extra_textures": int(max_extra_textures),
        }

        # Write the job-spec JSON via a temp file inside ``output_dir`` so the
        # path itself contains no Chinese characters (the file is in an
        # ASCII-only tool-managed directory we control).
        job_path = output_dir / "job_spec.json"
        try:
            job_path.write_text(json.dumps(job_spec, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        except OSError as exc:
            return ReplayResult(
                ok=False, backend=self.name, error=f"job-spec write failed: {exc}",
                output_dir=output_dir,
            )

        env = os.environ.copy()
        env["QR_JOB_JSON_PATH"] = str(job_path)
        env["QR_JOB_OUTPUT_DIR"] = str(output_dir)
        # qrenderdoc's analytics popup is a real risk in headless mode —
        # disable.
        env.setdefault("RENDERDOC_DISABLE_ANALYTICS", "1")
        # Inherit but cleanse PYTHONPATH so we don't accidentally feed our
        # tool's 3.13 packages into the embedded 3.6 interpreter.
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)

        cmd = [
            str(self.qrenderdoc_path),
            "--python", str(self.worker_script),
        ]

        log.info("Launching qrenderdoc replay: %s", " ".join(cmd))
        t0 = time.time()
        # Critical: ``hidden_subprocess_kwargs`` sets STARTF_USESHOWWINDOW +
        # SW_HIDE which interferes with qrenderdoc's OpenGL output texture
        # readback — overlay/wireframe textures come back empty.  We need
        # CREATE_NO_WINDOW alone (no console for the subprocess) without
        # the wShowWindow override.  qrenderdoc itself never shows a UI
        # because our worker calls ``sys.exit(0)`` before the main window
        # is opened.
        kwargs = {}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                timeout=self.timeout_seconds,
                cwd=str(self.qrenderdoc_path.parent),
                **kwargs,
            )
        except subprocess.TimeoutExpired as exc:
            return ReplayResult(
                ok=False, backend=self.name,
                error=f"qrenderdoc timed out after {self.timeout_seconds}s",
                duration_seconds=time.time() - t0,
                stderr_tail=(exc.stderr or b"").decode("utf-8", errors="replace")[-2000:]
                if exc.stderr else "",
                output_dir=output_dir,
            )
        except Exception as exc:
            return ReplayResult(
                ok=False, backend=self.name,
                error=f"qrenderdoc spawn failed: {exc}",
                duration_seconds=time.time() - t0,
                output_dir=output_dir,
            )

        duration = time.time() - t0
        stderr_tail = ""
        if proc.stderr:
            stderr_tail = proc.stderr.decode("utf-8", errors="replace")[-2000:]

        manifest_path = output_dir / "manifest.json"
        if not manifest_path.exists():
            return ReplayResult(
                ok=False, backend=self.name,
                error=f"qrenderdoc exited rc={proc.returncode} but produced no manifest.json",
                duration_seconds=duration, stderr_tail=stderr_tail,
                output_dir=output_dir,
            )

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return ReplayResult(
                ok=False, backend=self.name,
                error=f"manifest.json unreadable: {exc}",
                duration_seconds=duration, stderr_tail=stderr_tail,
                output_dir=output_dir,
            )

        if not manifest.get("ok"):
            return ReplayResult(
                ok=False, backend=self.name,
                error=manifest.get("error") or "worker reported ok=false",
                duration_seconds=duration, stderr_tail=stderr_tail,
                manifest=manifest, output_dir=output_dir,
            )

        log.info(
            "qrenderdoc replay OK in %.1fs (rdoc %s, %d draws, %d textures)",
            duration,
            manifest.get("renderdoc_version", "?"),
            len(manifest.get("draws", [])),
            len(manifest.get("textures", [])),
        )
        return ReplayResult(
            ok=True, backend=self.name,
            manifest=manifest, output_dir=output_dir,
            duration_seconds=duration, stderr_tail=stderr_tail,
        )


# ---------------------------------------------------------------------------
# Selector
# ---------------------------------------------------------------------------

def select_replay_backend(
    renderdoc_dir: str,
    timeout_seconds: int = 900,
) -> Optional[QRenderdocScriptBackend]:
    """Return a usable :class:`QRenderdocScriptBackend` for the given
    directory, or ``None`` if no qrenderdoc is available (caller should
    fall back to the XML path)."""
    backend = QRenderdocScriptBackend.from_renderdoc_dir(
        renderdoc_dir, timeout_seconds=timeout_seconds,
    )
    if backend and backend.is_available():
        return backend
    return None


def describe_backend(renderdoc_dir: str) -> Dict[str, Any]:
    """Diagnostic info for the health page."""
    info: Dict[str, Any] = {
        "renderdoc_dir": renderdoc_dir,
        "qrenderdoc_path": "",
        "worker_script": "",
        "available": False,
    }
    qr = find_qrenderdoc(renderdoc_dir)
    if qr:
        info["qrenderdoc_path"] = str(qr)
    worker = find_worker_script()
    if worker.exists():
        info["worker_script"] = str(worker)
    info["available"] = bool(qr and worker.exists())
    return info
