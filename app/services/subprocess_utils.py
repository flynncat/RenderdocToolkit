from __future__ import annotations

import subprocess
from typing import Any, Dict


def hidden_subprocess_kwargs() -> Dict[str, Any]:
    """Subprocess kwargs that fully hide the child's console on Windows.

    The combination of ``CREATE_NO_WINDOW`` plus an explicit
    ``wShowWindow = SW_HIDE`` ``STARTUPINFO`` is needed because some children
    can still pop a window if only ``CREATE_NO_WINDOW`` is set without the
    matching ``STARTUPINFO`` show-window override.
    """
    kwargs: Dict[str, Any] = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    if hasattr(subprocess, "STARTUPINFO") and hasattr(subprocess, "STARTF_USESHOWWINDOW"):
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = startupinfo
    return kwargs
