from __future__ import annotations

import os
from pathlib import Path
from tkinter import Tk, filedialog
from typing import Optional


class DesktopBridge:
    def __init__(self) -> None:
        self._last_dir = str(Path.home())

    def pick_rdc_file(self) -> str:
        return self._pick_file("选择 RenderDoc Capture", [("RenderDoc Capture", "*.rdc"), ("All files", "*.*")])

    def pick_csv_file(self) -> str:
        return self._pick_file("选择 CSV 文件", [("CSV File", "*.csv"), ("All files", "*.*")])

    def pick_csv_files(self) -> str:
        root = self._create_root()
        try:
            values = filedialog.askopenfilenames(
                title="选择一个或多个 CSV 文件",
                initialdir=self._last_dir or str(Path.home()),
                filetypes=[("CSV File", "*.csv"), ("All files", "*.*")],
            )
        finally:
            root.destroy()
        picked = [str(value) for value in values if value]
        if picked:
            self._last_dir = str(Path(picked[0]).parent)
        return "\n".join(picked)

    def pick_any_file(self) -> str:
        return self._pick_file("选择文件", [("All files", "*.*")])

    def pick_directory(self) -> str:
        root = self._create_root()
        try:
            value = filedialog.askdirectory(title="选择目录", initialdir=self._last_dir or str(Path.home()))
        finally:
            root.destroy()
        if value:
            self._last_dir = value
        return value or ""

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

    def _pick_file(self, title: str, filetypes: list[tuple[str, str]]) -> str:
        root = self._create_root()
        try:
            value = filedialog.askopenfilename(title=title, initialdir=self._last_dir or str(Path.home()), filetypes=filetypes)
        finally:
            root.destroy()
        if value:
            self._last_dir = str(Path(value).parent)
        return value or ""

    @staticmethod
    def _create_root() -> Tk:
        root = Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        return root
