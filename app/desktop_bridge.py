from __future__ import annotations

import os
from pathlib import Path


class DesktopBridge:
    """JS API bridge exposed to pywebview.

    File/directory pickers use ``window.create_file_dialog()`` from
    pywebview instead of tkinter, because pywebview JS-API callbacks
    run on a worker thread and tkinter requires the main thread.
    """

    def __init__(self) -> None:
        self._last_dir = str(Path.home())

    @property
    def _window(self):
        import webview
        return webview.windows[0] if webview.windows else None

    def pick_rdc_file(self) -> str:
        return self._pick_file(("RenderDoc Capture (*.rdc)",))

    def pick_csv_file(self) -> str:
        return self._pick_file(("CSV File (*.csv)",))

    def pick_csv_files(self) -> str:
        import webview
        window = self._window
        if not window:
            return ""
        result = window.create_file_dialog(
            webview.OPEN_DIALOG,
            directory=self._last_dir,
            allow_multiple=True,
            file_types=("CSV File (*.csv)", "All files (*.*)"),
        )
        if not result:
            return ""
        picked = [str(p) for p in result if p]
        if picked:
            self._last_dir = str(Path(picked[0]).parent)
        return "\n".join(picked)

    def pick_any_file(self) -> str:
        return self._pick_file(("All files (*.*)",))

    def pick_directory(self) -> str:
        import webview
        window = self._window
        if not window:
            return ""
        result = window.create_file_dialog(
            webview.FOLDER_DIALOG,
            directory=self._last_dir,
        )
        if result and result[0]:
            self._last_dir = str(result[0])
            return str(result[0])
        return ""

    def reveal_path(self, path: str) -> bool:
        target = (path or "").strip()
        if not target:
            return False
        try:
            target_path = Path(target).expanduser()
            if target_path.is_file():
                target_path = target_path.parent
            elif not target_path.exists() and target_path.parent.exists():
                target_path = target_path.parent
            os.startfile(str(target_path))
            return True
        except OSError:
            return False

    def _pick_file(self, file_types: tuple[str, ...]) -> str:
        import webview
        window = self._window
        if not window:
            return ""
        result = window.create_file_dialog(
            webview.OPEN_DIALOG,
            directory=self._last_dir,
            file_types=(*file_types, "All files (*.*)"),
        )
        if result and result[0]:
            self._last_dir = str(Path(result[0]).parent)
            return str(result[0])
        return ""
