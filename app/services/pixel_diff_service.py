"""Pixel-level image comparison service (SSIM, PSNR, RMSE, diff heatmap)."""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
from PIL import Image


@dataclass
class DiffResult:
    ssim: float
    psnr: float
    rmse: float
    max_pixel_error: float
    pass_threshold: bool
    diff_image_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _ssim_single_channel(a: np.ndarray, b: np.ndarray) -> float:
    """Compute SSIM for a single-channel pair using the standard formula."""
    C1 = (0.01 * 255) ** 2
    C2 = (0.03 * 255) ** 2

    a = a.astype(np.float64)
    b = b.astype(np.float64)

    mu_a = a.mean()
    mu_b = b.mean()
    sigma_a_sq = a.var()
    sigma_b_sq = b.var()
    sigma_ab = ((a - mu_a) * (b - mu_b)).mean()

    num = (2 * mu_a * mu_b + C1) * (2 * sigma_ab + C2)
    den = (mu_a ** 2 + mu_b ** 2 + C1) * (sigma_a_sq + sigma_b_sq + C2)
    return float(num / den)


def _compute_ssim(a: np.ndarray, b: np.ndarray) -> float:
    """Mean SSIM across all shared channels."""
    if a.ndim == 2:
        return _ssim_single_channel(a, b)
    channels = min(a.shape[2], b.shape[2])
    return float(np.mean([_ssim_single_channel(a[:, :, c], b[:, :, c]) for c in range(channels)]))


def _compute_psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    if mse == 0:
        return float("inf")
    return 10.0 * math.log10(255.0 ** 2 / mse)


def _compute_rmse(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.sqrt(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)))


def _generate_diff_heatmap(a: np.ndarray, b: np.ndarray, output_path: Path) -> None:
    """Write a per-pixel absolute-difference heatmap (amplified for visibility)."""
    diff = np.abs(a.astype(np.float64) - b.astype(np.float64))
    if diff.ndim == 3:
        diff = diff.mean(axis=2)
    diff_max = diff.max()
    if diff_max > 0:
        diff = diff / diff_max * 255.0
    diff = diff.astype(np.uint8)

    heatmap = np.zeros((*diff.shape, 3), dtype=np.uint8)
    heatmap[:, :, 0] = diff
    heatmap[:, :, 1] = np.clip(255 - diff * 2, 0, 255).astype(np.uint8)
    heatmap[:, :, 2] = 0

    Image.fromarray(heatmap).save(str(output_path))


class PixelDiffService:
    DEFAULT_SSIM_THRESHOLD = 0.98

    def compare(
        self,
        baseline_path: str | Path,
        candidate_path: str | Path,
        output_dir: str | Path | None = None,
        ssim_threshold: float | None = None,
    ) -> DiffResult:
        baseline_path = Path(baseline_path)
        candidate_path = Path(candidate_path)
        if not baseline_path.exists():
            raise FileNotFoundError(f"Baseline not found: {baseline_path}")
        if not candidate_path.exists():
            raise FileNotFoundError(f"Candidate not found: {candidate_path}")

        img_a = np.array(Image.open(str(baseline_path)).convert("RGB"))
        img_b_raw = Image.open(str(candidate_path)).convert("RGB")

        if img_b_raw.size != (img_a.shape[1], img_a.shape[0]):
            img_b_raw = img_b_raw.resize((img_a.shape[1], img_a.shape[0]), Image.LANCZOS)
        img_b = np.array(img_b_raw)

        threshold = ssim_threshold if ssim_threshold is not None else self.DEFAULT_SSIM_THRESHOLD
        ssim_val = _compute_ssim(img_a, img_b)
        psnr_val = _compute_psnr(img_a, img_b)
        rmse_val = _compute_rmse(img_a, img_b)
        max_err = float(np.max(np.abs(img_a.astype(np.float64) - img_b.astype(np.float64))))

        diff_image_path: Optional[str] = None
        if output_dir is not None:
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            diff_png = out / "diff_heatmap.png"
            _generate_diff_heatmap(img_a, img_b, diff_png)
            diff_image_path = str(diff_png)

        return DiffResult(
            ssim=round(ssim_val, 6),
            psnr=round(psnr_val, 4) if not math.isinf(psnr_val) else float("inf"),
            rmse=round(rmse_val, 6),
            max_pixel_error=round(max_err, 2),
            pass_threshold=ssim_val >= threshold,
            diff_image_path=diff_image_path,
        )
