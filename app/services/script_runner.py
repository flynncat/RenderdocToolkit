from __future__ import annotations

import contextlib
import io
import os
import runpy
import sys
import threading
from pathlib import Path
from typing import List, Tuple


_SCRIPT_RUN_LOCK = threading.Lock()


def run_python_script_inproc(script_path: Path, argv: List[str], cwd: Path | None = None) -> Tuple[int, str]:
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    old_argv = sys.argv[:]
    old_cwd = Path.cwd()

    with _SCRIPT_RUN_LOCK:
        try:
            sys.argv = [str(script_path), *argv]
            if cwd is not None:
                os.chdir(str(cwd))
            with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
                try:
                    runpy.run_path(str(script_path), run_name="__main__")
                    exit_code = 0
                except SystemExit as exc:
                    code = exc.code
                    if isinstance(code, int):
                        exit_code = code
                    elif code in (None, ""):
                        exit_code = 0
                    else:
                        print(code, file=stderr_buffer)
                        exit_code = 1
                except Exception:
                    import traceback

                    traceback.print_exc(file=stderr_buffer)
                    exit_code = 1
        finally:
            sys.argv = old_argv
            os.chdir(str(old_cwd))

    output = stdout_buffer.getvalue()
    err = stderr_buffer.getvalue()
    combined = output + (("\n" + err) if err else "")
    return exit_code, combined.strip()
