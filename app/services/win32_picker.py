import ctypes
from ctypes import wintypes
import platform

# Only define structures on Windows
is_windows = platform.system().lower().startswith("win")

if is_windows:
    # --- Structures for Folder Picker (SHBrowseForFolderW) ---
    class BROWSEINFO(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPCWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", ctypes.c_void_p),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int)
        ]

    # --- Structures for File Picker (GetOpenFileNameW) ---
    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", ctypes.c_void_p),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", ctypes.c_void_p),
            ("dwReserved", ctypes.c_void_p),
            ("FlagsEx", wintypes.DWORD),
        ]

    # Flags
    BIF_RETURNONLYFSDIRS = 0x0001
    BIF_NEWDIALOGSTYLE = 0x0040
    
    OFN_FILEMUSTEXIST = 0x00001000
    OFN_PATHMUSTEXIST = 0x00000800


def pick_directory_win32(title="选择文件夹") -> str:
    """Open Windows native folder selection dialog using ctypes."""
    if not is_windows:
        return ""
        
    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    user32 = ctypes.windll.user32
    
    # Initialize COM
    ole32.CoInitialize(None)
    
    try:
        bi = BROWSEINFO()
        # Use active window or desktop window as parent
        bi.hwndOwner = user32.GetActiveWindow() or user32.GetDesktopWindow()
        bi.pidlRoot = None
        bi.pszDisplayName = None
        bi.lpszTitle = title
        bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE
        bi.lpfn = None
        bi.lParam = 0
        bi.iImage = 0
        
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return ""
            
        path_buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        success = shell32.SHGetPathFromIDListW(pidl, path_buf)
        
        ole32.CoTaskMemFree(pidl)
        
        if success:
            return path_buf.value
        return ""
    except Exception as exc:
        print(f"pick_directory_win32 error: {exc}")
        return ""
    finally:
        ole32.CoUninitialize()


def pick_file_win32(title="选择文件", filetypes=None) -> str:
    """Open Windows native file selection dialog using ctypes."""
    if not is_windows:
        return ""
        
    comdlg32 = ctypes.windll.comdlg32
    user32 = ctypes.windll.user32
    
    # Format filter string: e.g. "RenderDoc Files (*.rdc)\0*.rdc\0All Files (*.*)\0*.*\0\0"
    filter_str = ""
    if filetypes:
        for name, pattern in filetypes:
            filter_str += f"{name}\0{pattern}\0"
    filter_str += "All Files (*.*)\0*.*\0\0"
    
    # Create buffer for the selected file path
    buffer_size = 65536  # 64KB to handle long paths
    file_buf = ctypes.create_unicode_buffer(buffer_size)
    
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = user32.GetActiveWindow() or user32.GetDesktopWindow()
    ofn.lpstrFilter = filter_str
    ofn.lpstrFile = ctypes.cast(file_buf, wintypes.LPWSTR)
    ofn.nMaxFile = buffer_size
    ofn.lpstrTitle = title
    ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST
    
    if comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
        return file_buf.value
    return ""
