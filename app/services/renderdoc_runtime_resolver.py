"""Shared resolver that turns a user-provided RenderDoc directory into a
concrete runtime context (Python module path, CLI executable path, etc.).

Both the ``性能`` and ``性能 Diff`` tabs use this so that "which RenderDoc
version to use" is decided in exactly one place with one fallback chain.
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import app.config as app_config


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
    """

    task_dir = (task_renderdoc_dir or "").strip()

    if task_dir:
        resolved = Path(task_dir).expanduser().resolve()
        if resolved.is_dir():
            return RenderdocRuntimeContext(
                renderdoc_dir=str(resolved),
                renderdoc_python_path=str(resolved),
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
