"""Self-test for PixelDiffService — no RenderDoc required.

Creates synthetic test images and validates SSIM/PSNR behaviour.
"""
from __future__ import annotations

import sys
import math
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.pixel_diff_service import PixelDiffService


def _make_test_images(out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.RandomState(42)

    base = rng.randint(0, 256, (256, 256, 3), dtype=np.uint8)
    Image.fromarray(base, "RGB").save(str(out_dir / "base.png"))

    Image.fromarray(base.copy(), "RGB").save(str(out_dir / "identical.png"))

    noisy = base.astype(np.int16) + rng.randint(-30, 31, base.shape, dtype=np.int16)
    noisy = np.clip(noisy, 0, 255).astype(np.uint8)
    Image.fromarray(noisy, "RGB").save(str(out_dir / "noisy.png"))

    tinted = base.copy().astype(np.float64)
    tinted[:, :, 0] = np.clip(tinted[:, :, 0] * 1.3, 0, 255)
    tinted[:, :, 2] = np.clip(tinted[:, :, 2] * 0.7, 0, 255)
    Image.fromarray(tinted.astype(np.uint8), "RGB").save(str(out_dir / "tinted.png"))


def main():
    out_dir = Path("scripts/_test_pixel_diff")
    _make_test_images(out_dir)

    svc = PixelDiffService()
    passed = 0
    total = 0

    # Test 1: identical images
    total += 1
    r = svc.compare(out_dir / "base.png", out_dir / "identical.png", out_dir / "diff_identical")
    if r.ssim == 1.0 and math.isinf(r.psnr) and r.pass_threshold:
        print(f"[PASS] Identical: ssim={r.ssim}, psnr={r.psnr}, pass={r.pass_threshold}")
        passed += 1
    else:
        print(f"[FAIL] Identical: ssim={r.ssim}, psnr={r.psnr}, pass={r.pass_threshold}")

    # Test 2: noisy image should fail threshold
    total += 1
    r = svc.compare(out_dir / "base.png", out_dir / "noisy.png", out_dir / "diff_noisy")
    if r.ssim < 0.98 and not r.pass_threshold:
        print(f"[PASS] Noisy: ssim={r.ssim:.4f}, psnr={r.psnr:.2f}, pass={r.pass_threshold}")
        passed += 1
    else:
        print(f"[FAIL] Noisy: ssim={r.ssim:.4f}, psnr={r.psnr:.2f}, pass={r.pass_threshold}")

    # Test 3: tinted image should fail
    total += 1
    r = svc.compare(out_dir / "base.png", out_dir / "tinted.png", out_dir / "diff_tinted")
    if r.ssim < 0.98 and not r.pass_threshold:
        print(f"[PASS] Tinted: ssim={r.ssim:.4f}, psnr={r.psnr:.2f}, pass={r.pass_threshold}")
        passed += 1
    else:
        print(f"[FAIL] Tinted: ssim={r.ssim:.4f}, psnr={r.psnr:.2f}, pass={r.pass_threshold}")

    # Test 4: diff heatmap file exists
    total += 1
    heatmap = out_dir / "diff_noisy" / "diff_heatmap.png"
    if heatmap.exists() and heatmap.stat().st_size > 0:
        print(f"[PASS] Diff heatmap exists: {heatmap}")
        passed += 1
    else:
        print(f"[FAIL] Diff heatmap missing: {heatmap}")

    print(f"\n{passed}/{total} tests passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
