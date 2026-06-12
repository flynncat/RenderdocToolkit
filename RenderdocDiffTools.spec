# -*- mode: python ; coding: utf-8 -*-

import os

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ["app.services.win32_picker"]
for package_name in (
    "fastapi",
    "starlette",
    "uvicorn",
    "httpx",
    "anyio",
    "jinja2",
    "markdown",
    "app.services.perf_report",
):
    hiddenimports += collect_submodules(package_name)


def _collect_cmp_datas():
    """Bundle ``external_tools/renderdoccmp`` but skip the heavy RenderDoc CLI.

    ``tools/renderdoc`` (renderdoc.dll + renderdoccmd.exe, ~24.5MB) is no
    longer shipped: the app auto-detects a system-installed RenderDoc or uses
    a user-provided path (see ``renderdoc_runtime_resolver``).  malioc /
    astcenc and the python scripts are still bundled.
    """
    root = "external_tools/renderdoccmp"
    skip = os.path.normpath(os.path.join("tools", "renderdoc"))
    entries = []
    for dirpath, _dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        rel = "" if rel == "." else os.path.normpath(rel)
        if rel == skip or rel.startswith(skip + os.sep):
            continue
        if "__pycache__" in rel.split(os.sep):
            continue
        for fn in filenames:
            if fn.endswith(".pyc"):
                continue
            src = os.path.join(dirpath, fn)
            dest = os.path.join(root, rel) if rel else root
            entries.append((src, dest))
    return entries


datas = [
    ("app/templates", "app/templates"),
    ("app/static", "app/static"),
    (".cursor/skills/renderdoc-compare-diagnose", ".cursor/skills/renderdoc-compare-diagnose"),
    ("docs", "docs"),
    ("app/services/perf_report/classifier_rules.example.json", "app/services/perf_report"),
] + _collect_cmp_datas()

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
