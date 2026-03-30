# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
for package_name in ("fastapi", "starlette", "uvicorn", "httpx", "anyio", "jinja2", "webview"):
    hiddenimports += collect_submodules(package_name)

datas = [
    ("app/templates", "app/templates"),
    ("app/static", "app/static"),
    (".cursor/skills/renderdoc-compare-diagnose", ".cursor/skills/renderdoc-compare-diagnose"),
    ("docs", "docs"),
    (r"G:\UGit\renderdoc_cmp\renderdoccmp", "external_tools/renderdoccmp"),
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
    excludes=[],
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
    console=False,
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
