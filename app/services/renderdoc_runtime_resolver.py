"""Shared resolver that turns a user-provided RenderDoc directory into a
concrete runtime context (Python module path, CLI executable path, etc.).

Both the ``性能`` and ``性能 Diff`` tabs use this so that "which RenderDoc
version to use" is decided in exactly one place with one fallback chain.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import app.config as app_config

log = logging.getLogger(__name__)

# Magic bytes produced by older / custom RenderDoc builds that the *current*
# standard v1.41 ``renderdoc`` Python module cannot read.
# ``CODR`` is the on-disk representation of the ``RDOC`` magic (little-endian
# uint32 ``0x52444F43``), used by RenderDoc <= v1.36 and many custom forks.
_FOREIGN_MAGICS: set[bytes] = {
    b"\x43\x4f\x44\x52",  # CODR / RDOC (v1.36 and custom builds)
}


@dataclass
class RenderdocRuntimeContext:
    renderdoc_dir: str = ""
    renderdoc_python_path: str = ""
    renderdoc_cmd_path: str = ""
    source: str = ""


def resolve_renderdoc_runtime(
    task_renderdoc_dir: str = "",
) -> RenderdocRuntimeContext:
    """Resolve the RenderDoc runtime context with a unified fallback chain:

    1. *task_renderdoc_dir* – per-task override from the UI form.
    2. Global ``RENDERDOC_PYTHON_PATH`` from settings / env.
    3. Bundled ``renderdoccmd`` shipped with the cmp tool, or system PATH.

    When *task_renderdoc_dir* is given but doesn't contain a usable
    ``renderdoc.pyd``, the resolver will look for one provided by the
    ``rdc-cli`` package or on the system so that the Python replay API
    still has *a* module to work with for standard-format captures.
    """

    task_dir = (task_renderdoc_dir or "").strip()

    if task_dir:
        resolved = Path(task_dir).expanduser().resolve()
        if resolved.is_dir():
            python_path = _find_usable_python_path(resolved) or str(resolved)
            return RenderdocRuntimeContext(
                renderdoc_dir=str(resolved),
                renderdoc_python_path=python_path,
                renderdoc_cmd_path=_find_renderdoccmd_in(resolved),
                source="task_override",
            )

    global_python_path = (app_config.RENDERDOC_PYTHON_PATH or "").strip()
    if global_python_path:
        resolved = Path(global_python_path).expanduser().resolve()
        parent = resolved if resolved.is_dir() else resolved.parent
        return RenderdocRuntimeContext(
            renderdoc_dir=str(parent),
            renderdoc_python_path=str(resolved),
            renderdoc_cmd_path=_find_renderdoccmd_in(parent),
            source="global_settings",
        )

    bundled = _find_bundled_renderdoccmd()
    system_cmd = _find_system_renderdoccmd()
    cmd_path = bundled or system_cmd or ""
    cmd_dir = str(Path(cmd_path).parent) if cmd_path else ""
    return RenderdocRuntimeContext(
        renderdoc_dir=cmd_dir,
        renderdoc_python_path="",
        renderdoc_cmd_path=cmd_path,
        source="bundled" if bundled else ("path" if system_cmd else "none"),
    )


# ---------------------------------------------------------------------------
# Capture format helpers
# ---------------------------------------------------------------------------

def capture_needs_foreign_renderdoc(capture_path: str | Path) -> bool:
    """Return *True* when *capture_path* uses a file format that the
    standard ``renderdoc`` Python module cannot open (e.g. older/custom builds
    with the ``RDOC``/``CODR`` magic).
    """
    p = Path(capture_path)
    if not p.is_file():
        return False
    try:
        with p.open("rb") as fh:
            magic = fh.read(4)
        return magic in _FOREIGN_MAGICS
    except OSError:
        return False


def convert_capture_to_xml(
    capture_path: str | Path,
    renderdoc_cmd_path: str,
    output_dir: str | Path,
) -> Optional[Path]:
    """Use a (possibly foreign) ``renderdoccmd convert`` to dump the capture
    as XML.  Returns the path to the XML file on success, ``None`` otherwise.
    """
    capture_path = Path(capture_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    xml_path = output_dir / f"{capture_path.stem}.xml"
    cmd = [
        renderdoc_cmd_path,
        "convert",
        "-f", str(capture_path),
        "-o", str(xml_path),
        "-c", "xml",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode == 0 and xml_path.exists() and xml_path.stat().st_size > 100:
            log.info("Converted capture to XML: %s (%d bytes)", xml_path, xml_path.stat().st_size)
            return xml_path
        log.warning("renderdoccmd convert failed (rc=%d): %s", proc.returncode, proc.stderr[:500])
    except Exception as exc:
        log.warning("renderdoccmd convert error: %s", exc)
    return None


def resolve_renderdoc_metadata(
    task_renderdoc_dir: str = "",
) -> dict:
    """Return a dict suitable for embedding in job ``metadata.inputs``."""
    ctx = resolve_renderdoc_runtime(task_renderdoc_dir)
    return {
        "renderdoc_dir_requested": (task_renderdoc_dir or "").strip(),
        "renderdoc_dir_resolved": ctx.renderdoc_dir,
        "renderdoc_python_path": ctx.renderdoc_python_path,
        "renderdoc_cmd_path": ctx.renderdoc_cmd_path,
        "renderdoc_source": ctx.source,
    }


def _find_usable_python_path(directory: Path) -> str:
    """Return a directory containing a ``renderdoc.pyd`` (or ``.so``)
    importable from the *current* Python interpreter.

    Search order:
    1. *directory* itself (ideal when user supplies a full RenderDoc install)
    2. ``rdc-cli`` package's local module cache (``%LOCALAPPDATA%/rdc/renderdoc``)
    3. Sibling of the system ``renderdoccmd`` on PATH
    """
    is_windows = platform.system() == "Windows"
    ext = ".pyd" if is_windows else ".so"
    if (directory / f"renderdoc{ext}").is_file():
        return str(directory)

    # rdc-cli local install (Windows-specific)
    if is_windows:
        local = Path(os.environ.get("LOCALAPPDATA", "")) / "rdc" / "renderdoc"
        if (local / f"renderdoc{ext}").is_file():
            return str(local)

    cmd = shutil.which("renderdoccmd")
    if cmd:
        sibling = Path(cmd).resolve().parent
        if (sibling / f"renderdoc{ext}").is_file():
            return str(sibling)

    return ""


def _find_renderdoccmd_in(directory: Path) -> str:
    is_windows = platform.system() == "Windows"
    exe_name = "renderdoccmd.exe" if is_windows else "renderdoccmd"
    candidate = directory / exe_name
    if candidate.exists():
        return str(candidate)
    return ""


def _find_bundled_renderdoccmd() -> str:
    is_windows = platform.system() == "Windows"
    platform_dir = "windows" if is_windows else "linux"
    exe_name = "renderdoccmd.exe" if is_windows else "renderdoccmd"
    bundled = app_config.RENDERDOC_CMP_ROOT / "tools" / "renderdoc" / platform_dir / exe_name
    if bundled.exists():
        return str(bundled)
    return ""


def _find_system_renderdoccmd() -> str:
    result = shutil.which("renderdoccmd")
    return result or ""
