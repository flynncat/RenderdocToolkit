"""Verify that files required by the portable (PyInstaller) build exist in the repo.

Run from repo root::

    python scripts/verify_bundled_assets.py

Exit code 0 = all critical assets present with plausible sizes.
Exit code 1 = missing or suspiciously small (likely LFS pointer stub).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (relative path, minimum bytes).  LFS pointer stubs are ~120-140 bytes.
CRITICAL_ASSETS: list[tuple[str, int]] = [
    ("external_tools/renderdoccmp/rdc_compare_ultimate.py", 10_000),
    ("external_tools/renderdoccmp/qr_replay_worker.py", 1_000),
    ("external_tools/renderdoccmp/tools/babylon.js", 100),
    # NOTE: The RenderDoc CLI (tools/renderdoc/windows/*) is intentionally NOT
    # bundled anymore - the portable package relies on a system-installed
    # RenderDoc (auto-detected) or a user-provided path, which keeps the
    # package ~24MB smaller.  See renderdoc_runtime_resolver.
    ("external_tools/renderdoccmp/tools/astcenc/windows/astcenc-sse4.1.exe", 100_000),
    ("external_tools/renderdoccmp/tools/mali_offline_compiler/windows/malioc.exe", 10_000),
    (
        "external_tools/renderdoccmp/tools/mali_offline_compiler/windows/external/glslang.exe",
        10_000,
    ),
    (
        "external_tools/renderdoccmp/tools/mali_offline_compiler/windows/graphics/Mali-Gxx_r51p0-00rel0.dll",
        1_000,
    ),
    (
        "external_tools/renderdoccmp/tools/mali_offline_compiler/windows/graphics/Mali-Gxx_r55p0-00rel0.dll",
        1_000,
    ),
    (
        "external_tools/renderdoccmp/tools/mali_offline_compiler/windows/graphics/Mali-T600_r23p0-00rel0.dll",
        1_000,
    ),
    (".cursor/skills/renderdoc-compare-diagnose/scripts/compare_pass_issue.py", 500),
    ("docs/images/overview-home.png", 1_000),
    ("docs/images/cmp-report.png", 100),
    ("docs/images/asset-export.png", 100),
    ("app/templates/index.html", 1_000),
    ("app/static/app.js", 1_000),
    ("app/static/app.css", 500),
    ("app/services/win32_picker.py", 200),
]

LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"


def _check(path: Path, min_bytes: int) -> str | None:
    if not path.is_file():
        return f"MISSING: {path.relative_to(ROOT).as_posix()}"
    size = path.stat().st_size
    if size < min_bytes:
        return (
            f"TOO SMALL ({size} B, need >={min_bytes} B): "
            f"{path.relative_to(ROOT).as_posix()} — likely Git LFS pointer stub"
        )
    head = path.read_bytes()[:64]
    if head.startswith(LFS_POINTER_PREFIX):
        return f"LFS POINTER (not real file): {path.relative_to(ROOT).as_posix()}"
    return None


def main() -> int:
    errors: list[str] = []
    for rel, min_bytes in CRITICAL_ASSETS:
        err = _check(ROOT / rel, min_bytes)
        if err:
            errors.append(err)
    if errors:
        print("Bundled asset verification FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        print(
            "\nFix: ensure these paths are committed as normal git blobs "
            "(see .gitattributes) and not listed in .gitignore.",
            file=sys.stderr,
        )
        return 1
    print(f"OK: {len(CRITICAL_ASSETS)} critical bundled assets verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
