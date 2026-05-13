"""Lazy per-draw texture thumbnail generator for the XML fallback analysis.

Used when the standard RenderDoc Python replay API is unavailable (custom or
older RenderDoc builds).  Instead of rendering a per-draw wireframe overlay
— which requires GPU replay — we surface the dominant bound texture for each
draw as a visual hint.  This gives users *some* per-draw imagery in the perf
table even though the genuine wireframe overlay is impossible.

The companion ``capture.zip`` produced by ``renderdoccmd convert -c zip.xml``
contains raw resource buffers keyed by zero-padded resource ID.  We extract
the buffer, decode it, and write a PNG into the job's preview directory.

Supported decodes:
- ASTC compressed (4x4, 6x6, 8x8 — common in UE mobile)
- Raw RGBA8 / RGB8
- Anything PIL can open natively (PNG/JPG embedded blobs)
"""

from __future__ import annotations

import logging
import struct
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Optional, Tuple

try:
    from PIL import Image
    _PIL_OK = True
except ImportError:
    Image = None  # type: ignore
    _PIL_OK = False

log = logging.getLogger(__name__)


# ASTC block size -> (block_x, block_y, is_srgb).  Only the most common
# formats used in UE mobile captures.
_ASTC_FORMATS: dict[str, tuple[int, int, bool]] = {
    "GL_COMPRESSED_RGBA_ASTC_4x4": (4, 4, False),
    "GL_COMPRESSED_SRGB8_ALPHA8_ASTC_4x4": (4, 4, True),
    "GL_COMPRESSED_RGBA_ASTC_6x6": (6, 6, False),
    "GL_COMPRESSED_SRGB8_ALPHA8_ASTC_6x6": (6, 6, True),
    "GL_COMPRESSED_RGBA_ASTC_8x8": (8, 8, False),
    "GL_COMPRESSED_SRGB8_ALPHA8_ASTC_8x8": (8, 8, True),
    "GL_COMPRESSED_RGBA_ASTC_5x5": (5, 5, False),
    "GL_COMPRESSED_SRGB8_ALPHA8_ASTC_5x5": (5, 5, True),
    "GL_COMPRESSED_RGBA_ASTC_10x10": (10, 10, False),
    "GL_COMPRESSED_SRGB8_ALPHA8_ASTC_10x10": (10, 10, True),
}


def is_supported() -> bool:
    """Whether thumbnail generation is supported in this environment."""
    return _PIL_OK


def generate_thumbnail(
    zip_path: Path,
    resource_id: str,
    width: int,
    height: int,
    fmt: str,
    output_png: Path,
    astcenc_path: Optional[Path] = None,
    max_size: int = 192,
) -> bool:
    """Extract and decode a texture from *zip_path* to a PNG thumbnail.

    Returns *True* on success.  Errors are logged but never raised — the
    caller treats absence of a PNG as "preview unavailable".
    """
    if not _PIL_OK:
        log.debug("PIL unavailable; cannot generate texture thumbnail")
        return False
    if not resource_id:
        return False
    try:
        resource_name = _format_resource_name(resource_id)
        if not zip_path.exists():
            log.debug("Texture zip missing: %s", zip_path)
            return False
        with zipfile.ZipFile(zip_path, "r") as zf:
            if resource_name not in zf.namelist():
                # The texture may have been allocated but never uploaded — that's normal.
                return False
            data = zf.read(resource_name)

        img = _decode_to_image(data, width, height, fmt, astcenc_path)
        if img is None:
            return False

        img.thumbnail((max_size, max_size), Image.LANCZOS)
        output_png.parent.mkdir(parents=True, exist_ok=True)
        img.save(output_png, format="PNG", optimize=True)
        return output_png.exists() and output_png.stat().st_size > 64
    except Exception as exc:
        log.debug("Thumbnail generation failed for res=%s fmt=%s: %s",
                  resource_id, fmt, exc)
        return False


def _format_resource_name(resource_id: str) -> str:
    try:
        return f"{int(resource_id):06d}"
    except ValueError:
        return resource_id


def _decode_to_image(
    data: bytes,
    width: int,
    height: int,
    fmt: str,
    astcenc_path: Optional[Path],
):
    """Decode *data* into a PIL Image, dispatching by format."""
    if not data:
        return None
    if fmt in _ASTC_FORMATS:
        block_x, block_y, is_srgb = _ASTC_FORMATS[fmt]
        return _decode_astc(data, width, height, block_x, block_y, is_srgb, astcenc_path)

    fmt_upper = (fmt or "").upper()
    # Raw RGBA8 / SRGB8_ALPHA8
    if width > 0 and height > 0 and (
        "RGBA8" in fmt_upper or "RGBA" == fmt_upper or "SRGB8_ALPHA8" in fmt_upper
    ):
        expected = width * height * 4
        if len(data) >= expected:
            try:
                return Image.frombytes("RGBA", (width, height), data[:expected])
            except Exception:
                return None
    # Raw RGB8
    if width > 0 and height > 0 and ("RGB8" in fmt_upper or "SRGB8" in fmt_upper):
        expected = width * height * 3
        if len(data) >= expected:
            try:
                return Image.frombytes("RGB", (width, height), data[:expected])
            except Exception:
                return None

    # Last resort: maybe it's a regular PNG/JPG/BMP blob
    try:
        import io
        return Image.open(io.BytesIO(data))
    except Exception:
        return None


def _decode_astc(
    data: bytes,
    width: int,
    height: int,
    block_x: int,
    block_y: int,
    is_srgb: bool,
    astcenc_path: Optional[Path],
):
    """Decode ASTC via the bundled ``astcenc`` binary."""
    if astcenc_path is None or not Path(astcenc_path).exists():
        log.debug("astcenc not found, cannot decode ASTC texture")
        return None
    try:
        astc_header = _create_astc_header(width, height, block_x, block_y)
        with tempfile.NamedTemporaryFile(suffix=".astc", delete=False) as f:
            f.write(astc_header)
            f.write(data)
            astc_file = f.name
        png_file = astc_file + ".png"
        decode_flag = "-ds" if is_srgb else "-dl"

        kwargs: dict = {}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        if hasattr(subprocess, "STARTUPINFO"):
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs["startupinfo"] = si

        result = subprocess.run(
            [str(astcenc_path), decode_flag, astc_file, png_file],
            capture_output=True, timeout=15, **kwargs,
        )
        try:
            Path(astc_file).unlink()
        except OSError:
            pass

        if result.returncode != 0 or not Path(png_file).exists():
            log.debug("astcenc returned %d, no PNG output", result.returncode)
            return None
        img = Image.open(png_file).copy()
        try:
            Path(png_file).unlink()
        except OSError:
            pass
        return img
    except Exception as exc:
        log.debug("ASTC decode failed: %s", exc)
        return None


def _create_astc_header(width: int, height: int, block_x: int, block_y: int) -> bytes:
    """Build the 16-byte ASTC file header so astcenc accepts our buffer."""
    return struct.pack(
        "<I3B3B3B3B",
        0x5CA1AB13,  # magic
        block_x, block_y, 1,
        width & 0xFF, (width >> 8) & 0xFF, (width >> 16) & 0xFF,
        height & 0xFF, (height >> 8) & 0xFF, (height >> 16) & 0xFF,
        1, 0, 0,  # depth = 1
    )


def find_astcenc() -> Optional[Path]:
    """Locate the bundled ``astcenc`` binary."""
    import platform
    import sys

    base_paths: list[Path] = []
    if getattr(sys, "frozen", False):
        base_paths.append(
            Path(sys.executable).parent / "_internal" / "external_tools" / "renderdoccmp" / "tools" / "astcenc"
        )
    base_paths.append(
        Path(__file__).resolve().parents[2] / "external_tools" / "renderdoccmp" / "tools" / "astcenc"
    )

    system = platform.system()
    if system == "Windows":
        subdir = "windows"
        exe_names = ("astcenc-sse4.1.exe", "astcenc-sse2.exe", "astcenc-avx2.exe", "astcenc.exe")
    elif system == "Darwin":
        subdir = "macos"
        exe_names = ("astcenc-sse4.1", "astcenc-avx2", "astcenc")
    else:
        subdir = "linux"
        exe_names = ("astcenc-sse4.1", "astcenc-avx2", "astcenc")

    for base in base_paths:
        if not base.exists():
            continue
        platform_dir = base / subdir
        if platform_dir.exists():
            for name in exe_names:
                candidate = platform_dir / name
                if candidate.exists():
                    return candidate
        # Fall back to recursive search in case layout changes
        for name in exe_names:
            for candidate in base.rglob(name):
                return candidate

    return None
