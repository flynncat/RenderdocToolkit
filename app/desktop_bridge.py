from __future__ import annotations

import os
import urllib.request
from pathlib import Path
from typing import Any


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

    def save_artifact_from_url(self, url: str, suggested_name: str) -> dict[str, Any]:
        """Pop a system Save dialog, fetch ``url`` and write bytes to the
        chosen path.

        Returns a dict that the JS side inspects:
        - ``{"path": str, "size": int}`` on success
        - ``{"cancelled": True}`` if the user cancelled the dialog
        - ``{"error": str}`` if anything went wrong

        Background: pywebview's WebView2 backend on Windows does not honour
        ``<a download href="blob:...">.click()`` reliably (the click is
        silently swallowed because WebView2 doesn't surface download
        navigations to host apps without explicit handler wiring). So we
        route downloads through this bridge, which uses a native Save dialog
        + a plain ``urllib`` fetch against the locally-running FastAPI
        server.
        """
        import webview
        window = self._window
        if not window:
            return {"error": "桌面窗口未就绪"}

        suggested = (suggested_name or "perf_artifact").strip() or "perf_artifact"
        ext = Path(suggested).suffix.lstrip(".").lower()
        # File-type filter must reflect the extension hint so the dialog
        # defaults to a sensible filter.  Adding "All files" keeps the user
        # in charge if they want to rename to something else.
        filter_map = {
            "zip": ("ZIP archive (*.zip)", "All files (*.*)"),
            "html": ("HTML page (*.html)", "All files (*.*)"),
            "md": ("Markdown (*.md)", "All files (*.*)"),
            "csv": ("CSV file (*.csv)", "All files (*.*)"),
            "tsv": ("TSV file (*.tsv)", "All files (*.*)"),
            "json": ("JSON file (*.json)", "All files (*.*)"),
        }
        file_types = filter_map.get(ext, ("All files (*.*)",))

        try:
            result = window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=self._last_dir,
                save_filename=suggested,
                file_types=file_types,
            )
        except Exception as exc:  # pragma: no cover (dialog-side errors are platform-specific)
            return {"error": f"打开保存对话框失败: {exc}"}

        if not result:
            return {"cancelled": True}
        target = result if isinstance(result, str) else result[0]
        if not target:
            return {"cancelled": True}
        self._last_dir = str(Path(target).parent)

        absolute_url = self._normalise_url(url)
        if not absolute_url:
            return {"error": "URL 不合法或未指定 RENDERDOC_WEBUI_PORT"}

        try:
            with urllib.request.urlopen(absolute_url, timeout=120) as response:
                data = response.read()
            Path(target).write_bytes(data)
        except Exception as exc:
            return {"error": f"下载或写入失败: {exc}"}

        return {"path": str(target), "size": len(data)}

    @staticmethod
    def _normalise_url(url: str) -> str:
        value = (url or "").strip()
        if not value:
            return ""
        if value.startswith("http://") or value.startswith("https://"):
            return value
        if not value.startswith("/"):
            value = "/" + value
        port = os.environ.get("RENDERDOC_WEBUI_PORT", "").strip()
        if not port:
            return ""
        return f"http://127.0.0.1:{port}{value}"

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
