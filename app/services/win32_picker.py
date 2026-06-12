"""Native Windows file/folder picker built on the modern Vista+ Common Item
Dialog (``IFileOpenDialog``) via ``ctypes``.

Why not ``tkinter`` / ``GetOpenFileNameW`` / ``SHBrowseForFolderW``?
    - ``tkinter`` is intentionally excluded from the packaged build.
    - The legacy ``comdlg32`` / ``SHBrowseForFolder`` APIs are fragile when
      invoked from an arbitrary server worker thread (wrong COM apartment /
      missing message pump) and were silently returning an empty path inside
      the frozen exe.

``IFileOpenDialog`` is the exact dialog Windows Explorer uses (folder tree on
the left, address bar, search box). With ``FOS_PICKFOLDERS`` the *same* modern
dialog is used to pick a folder, so the file and folder pickers look and behave
identically -- which is what the user asked for.

The dialog is always run on a freshly spawned thread that initialises COM as a
single-threaded apartment (STA), guaranteeing a correct environment regardless
of which thread FastAPI/anyio hands us.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import POINTER, byref, c_void_p, c_uint, c_int, c_wchar_p, wintypes
import platform

_IS_WINDOWS = platform.system().lower().startswith("win")

# --- COM / shell constants --------------------------------------------------
_S_OK = 0
_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1

# IFileDialog options (FILEOPENDIALOGOPTIONS)
_FOS_PICKFOLDERS = 0x00000020
_FOS_FORCEFILESYSTEM = 0x00000040
_FOS_FILEMUSTEXIST = 0x00001000
_FOS_PATHMUSTEXIST = 0x00000800

_SIGDN_FILESYSPATH = 0x80058000

# HRESULT for a user-cancelled dialog: HRESULT_FROM_WIN32(ERROR_CANCELLED)
_ERROR_CANCELLED_HRESULT = -2147023673  # 0x800704C7 as signed 32-bit

# {DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}
_CLSID_FileOpenDialog = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
# {D57C7288-D4AD-4768-BE02-9D969532D960}
_IID_IFileOpenDialog = "{D57C7288-D4AD-4768-BE02-9D969532D960}"


if _IS_WINDOWS:
    _ole32 = ctypes.windll.ole32

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class _COMDLG_FILTERSPEC(ctypes.Structure):
        _fields_ = [("pszName", c_wchar_p), ("pszSpec", c_wchar_p)]

    def _guid(text: str) -> "_GUID":
        g = _GUID()
        # CLSIDFromString parses both CLSID and IID textual forms.
        ctypes.oledll.ole32.CLSIDFromString(text, byref(g))
        return g

    def _vtbl(ptr: c_void_p, index: int, proto):
        """Return a callable for the COM method at *index* in *ptr*'s vtable."""
        vtable = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))
        return proto(vtable[0][index])

    # vtable method prototypes (first arg is always the ``this`` pointer)
    _Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)
    _Show = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, wintypes.HWND)
    _SetOptions = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, wintypes.DWORD)
    _GetOptions = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(wintypes.DWORD))
    _SetTitle = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, c_wchar_p)
    _SetFileTypes = ctypes.WINFUNCTYPE(
        ctypes.c_long, c_void_p, c_uint, POINTER(_COMDLG_FILTERSPEC)
    )
    _GetResult = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(c_void_p))
    _GetDisplayName = ctypes.WINFUNCTYPE(
        ctypes.c_long, c_void_p, c_int, POINTER(c_wchar_p)
    )

    # IFileDialog vtable indices
    _IDX_RELEASE = 2
    _IDX_SHOW = 3
    _IDX_SETFILETYPES = 4
    _IDX_SETOPTIONS = 9
    _IDX_GETOPTIONS = 10
    _IDX_SETTITLE = 17
    _IDX_GETRESULT = 20
    # IShellItem vtable indices
    _IDX_SI_RELEASE = 2
    _IDX_SI_GETDISPLAYNAME = 5


def _run_dialog(mode: str, exts: str, title: str) -> str:
    if not _IS_WINDOWS:
        return ""

    result: dict[str, str] = {"path": ""}

    def _worker() -> None:
        hr = _ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
        com_ready = hr in (0, 1)  # S_OK or S_FALSE
        dialog = c_void_p()
        try:
            clsid = _guid(_CLSID_FileOpenDialog)
            iid = _guid(_IID_IFileOpenDialog)
            hr = _ole32.CoCreateInstance(
                byref(clsid), None, _CLSCTX_INPROC_SERVER, byref(iid), byref(dialog)
            )
            if hr != _S_OK or not dialog:
                return

            # Read current options then add ours.
            opts = wintypes.DWORD(0)
            _vtbl(dialog, _IDX_GETOPTIONS, _GetOptions)(dialog, byref(opts))
            new_opts = opts.value | _FOS_FORCEFILESYSTEM
            if mode == "dir":
                new_opts |= _FOS_PICKFOLDERS
            else:
                new_opts |= _FOS_FILEMUSTEXIST | _FOS_PATHMUSTEXIST
            _vtbl(dialog, _IDX_SETOPTIONS, _SetOptions)(dialog, new_opts)

            if title:
                _vtbl(dialog, _IDX_SETTITLE, _SetTitle)(dialog, title)

            # File-type filter (file mode only).
            specs = None
            if mode != "dir" and exts:
                suffixes = [s.strip() for s in exts.split(",") if s.strip()]
                entries = []
                for s in suffixes:
                    label = f"{s.lstrip('.').upper()} 文件 (*{s})"
                    entries.append((label, f"*{s}"))
                entries.append(("所有文件 (*.*)", "*.*"))
                specs = (_COMDLG_FILTERSPEC * len(entries))()
                for i, (name, spec) in enumerate(entries):
                    specs[i].pszName = name
                    specs[i].pszSpec = spec
                _vtbl(dialog, _IDX_SETFILETYPES, _SetFileTypes)(
                    dialog, len(entries), specs
                )

            # Pick an owner window so the dialog reliably surfaces in the
            # foreground instead of opening *behind* the browser.  The browser
            # is the foreground window at click time, so prefer it; fall back to
            # our own console window, then to no owner.
            user32 = ctypes.windll.user32
            try:
                # ASFW_ANY (-1): relax the foreground-lock so a background
                # process (this server) is allowed to bring a window to front.
                user32.AllowSetForegroundWindow(-1)
            except Exception:
                pass
            owner = (
                user32.GetForegroundWindow()
                or ctypes.windll.kernel32.GetConsoleWindow()
                or 0
            )
            hr = _vtbl(dialog, _IDX_SHOW, _Show)(dialog, owner)
            if hr != _S_OK:
                return  # cancelled or error

            item = c_void_p()
            hr = _vtbl(dialog, _IDX_GETRESULT, _GetResult)(dialog, byref(item))
            if hr != _S_OK or not item:
                return
            try:
                name_ptr = c_wchar_p()
                hr = _vtbl(item, _IDX_SI_GETDISPLAYNAME, _GetDisplayName)(
                    item, _SIGDN_FILESYSPATH, byref(name_ptr)
                )
                if hr == _S_OK and name_ptr.value:
                    result["path"] = name_ptr.value
                    _ole32.CoTaskMemFree(name_ptr)
            finally:
                _vtbl(item, _IDX_SI_RELEASE, _Release)(item)
        finally:
            if dialog:
                _vtbl(dialog, _IDX_RELEASE, _Release)(dialog)
            if com_ready:
                _ole32.CoUninitialize()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    return result["path"]


def pick_directory_win32(title: str = "选择文件夹") -> str:
    """Open the modern Windows folder picker and return the chosen path."""
    return _run_dialog("dir", "", title)


def pick_file_win32(title: str = "选择文件", filetypes=None) -> str:
    """Open the modern Windows file picker and return the chosen path.

    *filetypes* is accepted for backwards compatibility; the extension filter is
    derived from the ``exts`` passed by the caller instead.
    """
    exts = ""
    if filetypes:
        parts = []
        for _name, pattern in filetypes:
            # pattern like ``*.rdc`` -> ``.rdc``
            cleaned = pattern.replace("*", "").strip()
            if cleaned and cleaned != ".":
                parts.append(cleaned)
        exts = ",".join(parts)
    return _run_dialog("file", exts, title)


def pick_file_win32_exts(title: str = "选择文件", exts: str = "") -> str:
    """Open the modern Windows file picker, filtering by comma-separated exts."""
    return _run_dialog("file", exts, title)
