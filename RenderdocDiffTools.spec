# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ["app.services.win32_picker"]
for package_name in ("fastapi", "starlette", "uvicorn", "httpx", "anyio", "jinja2", "markdown"):
    hiddenimports += collect_submodules(package_name)

datas = [
    ("app/templates", "app/templates"),
    ("app/static", "app/static"),
    (".cursor/skills/renderdoc-compare-diagnose", ".cursor/skills/renderdoc-compare-diagnose"),
    ("docs", "docs"),
    ("external_tools/renderdoccmp", "external_tools/renderdoccmp"),
]

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Web-only build: no desktop window backend, so keep the heavy Qt /
        # webview stacks out of the package entirely (saves ~100MB+).
        "webview",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "tkinter",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RenderdocDiffTools",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # Web-only build: show a console window so a double-click gives visible
    # feedback (prints the URL + lets the user Ctrl+C to stop the service),
    # instead of the previous silent background process.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="RenderdocDiffTools",
)
