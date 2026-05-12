"""Shader equivalence verification: replace → re-render → pixel diff.

All RenderDoc operations run in an **isolated subprocess** by default so that
GPU driver crashes never take down the main service process.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.pixel_diff_service import DiffResult, PixelDiffService


def _scripts_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "scripts"
    return Path(__file__).resolve().parents[1].parent / "scripts"

_WORKER_SCRIPT = _scripts_dir() / "rdc_probe_worker.py"
_BATCH_WORKER_SCRIPT = _scripts_dir() / "rdc_verify_batch_worker.py"
_WORKER_TIMEOUT = 120


@dataclass
class VerifyResult:
    passed: bool
    compile_ok: bool
    compile_errors: str = ""
    ssim: float = 0.0
    psnr: float = 0.0
    rmse: float = 0.0
    max_pixel_error: float = 0.0
    baseline_path: Optional[str] = None
    candidate_path: Optional[str] = None
    diff_image_path: Optional[str] = None
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BatchVerifyItem:
    eid: int
    stage: str
    modified_source: str


@dataclass
class BatchVerifyResult:
    items: List[Dict[str, Any]] = field(default_factory=list)
    total: int = 0
    passed_count: int = 0
    failed_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ShaderVerifyService:
    def __init__(self, pixel_diff: PixelDiffService | None = None):
        self.pixel_diff = pixel_diff or PixelDiffService()

    def verify_shader_equivalence(
        self,
        *,
        capture_path: str | Path,
        eid: int,
        stage: str,
        original_source: str,
        modified_source: str,
        output_dir: str | Path,
        ssim_threshold: float | None = None,
        use_subprocess: bool = True,
    ) -> VerifyResult:
        """Full pipeline: baseline screenshot → shader replace → screenshot → pixel diff.

        When *use_subprocess* is True (default), the RenderDoc replay runs in a
        child process so that GPU driver crashes cannot kill the main service.
        """
        if use_subprocess:
            return self._verify_via_subprocess(
                capture_path=Path(capture_path),
                eid=eid,
                stage=stage,
                current_source=original_source,
                modified_source=modified_source,
                output_dir=Path(output_dir),
                ssim_threshold=ssim_threshold,
            )
        return self._verify_in_process(
            capture_path=Path(capture_path),
            eid=eid,
            stage=stage,
            original_source=original_source,
            modified_source=modified_source,
            output_dir=Path(output_dir),
            ssim_threshold=ssim_threshold,
        )

    def _verify_via_subprocess(
        self,
        *,
        capture_path: Path,
        eid: int,
        stage: str,
        current_source: str,
        modified_source: str,
        output_dir: Path,
        ssim_threshold: float | None,
    ) -> VerifyResult:
        """Run shader verification in a subprocess to isolate RenderDoc crashes."""
        output_dir.mkdir(parents=True, exist_ok=True)
        orig_path = output_dir / "original.glsl"
        mod_path = output_dir / "modified.glsl"
        orig_path.write_text(current_source, encoding="utf-8")
        mod_path.write_text(modified_source, encoding="utf-8")

        cmd = [
            sys.executable, str(_WORKER_SCRIPT),
            str(capture_path), str(eid), stage,
            str(orig_path), str(mod_path), str(output_dir),
        ]

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=_WORKER_TIMEOUT,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return VerifyResult(
                passed=False, compile_ok=False,
                error=f"RenderDoc worker timed out ({_WORKER_TIMEOUT}s)",
            )
        except Exception as exc:
            return VerifyResult(
                passed=False, compile_ok=False,
                error=f"Worker launch error: {exc}",
            )

        result_file = output_dir / "result.json"
        if not result_file.exists():
            stderr_snippet = (proc.stderr or "")[:500]
            return VerifyResult(
                passed=False, compile_ok=False,
                error=f"Worker crashed (exit={proc.returncode}): {stderr_snippet}",
            )

        try:
            data = json.loads(result_file.read_text(encoding="utf-8"))
        except Exception as exc:
            return VerifyResult(
                passed=False, compile_ok=False,
                error=f"Worker result.json unreadable: {exc}",
            )

        if data.get("error"):
            return VerifyResult(
                passed=False,
                compile_ok=data.get("compile_ok", False),
                error=data["error"],
            )

        if not data.get("compile_ok"):
            return VerifyResult(
                passed=False, compile_ok=False,
                compile_errors=data.get("compile_errors", ""),
            )

        baseline_path = output_dir / "baseline.png"
        candidate_path = output_dir / "candidate.png"
        if not baseline_path.exists() or not candidate_path.exists():
            return VerifyResult(
                passed=False, compile_ok=True,
                compile_errors=data.get("compile_errors", ""),
                error="Screenshots not produced by worker",
            )

        diff = self.pixel_diff.compare(
            baseline_path, candidate_path,
            output_dir=output_dir,
            ssim_threshold=ssim_threshold,
        )

        return VerifyResult(
            passed=diff.pass_threshold,
            compile_ok=True,
            compile_errors=data.get("compile_errors", ""),
            ssim=diff.ssim,
            psnr=diff.psnr,
            rmse=diff.rmse,
            max_pixel_error=diff.max_pixel_error,
            baseline_path=str(baseline_path),
            candidate_path=str(candidate_path),
            diff_image_path=diff.diff_image_path,
        )

    def _verify_in_process(
        self,
        *,
        capture_path: Path,
        eid: int,
        stage: str,
        original_source: str,
        modified_source: str,
        output_dir: Path,
        ssim_threshold: float | None,
    ) -> VerifyResult:
        """In-process verification — only use when caller accepts crash risk."""
        from app.services.renderdoc_direct_replay import RenderdocDirectReplay

        output_dir.mkdir(parents=True, exist_ok=True)
        baseline_path = output_dir / "baseline.png"
        candidate_path = output_dir / "candidate.png"

        try:
            with RenderdocDirectReplay(capture_path) as replay:
                bl = replay.capture_original_draw(eid=eid, output_path=baseline_path)
                if bl is None:
                    return VerifyResult(
                        passed=False,
                        compile_ok=False,
                        error="Failed to capture baseline render target",
                    )

                result = replay.replace_shader_and_capture(
                    eid=eid,
                    stage=stage,
                    modified_source=modified_source,
                    output_path=candidate_path,
                )

                if not result.get("compile_ok"):
                    return VerifyResult(
                        passed=False,
                        compile_ok=False,
                        compile_errors=result.get("compile_errors", ""),
                        baseline_path=str(baseline_path) if baseline_path.exists() else None,
                    )

                if not result.get("success"):
                    return VerifyResult(
                        passed=False,
                        compile_ok=True,
                        compile_errors=result.get("compile_errors", ""),
                        error="Shader compiled but render target capture failed",
                        baseline_path=str(baseline_path) if baseline_path.exists() else None,
                    )

        except Exception as exc:
            return VerifyResult(
                passed=False,
                compile_ok=False,
                error=f"Replay error: {exc}",
            )

        diff = self.pixel_diff.compare(
            baseline_path,
            candidate_path,
            output_dir=output_dir,
            ssim_threshold=ssim_threshold,
        )

        return VerifyResult(
            passed=diff.pass_threshold,
            compile_ok=True,
            compile_errors=result.get("compile_errors", ""),
            ssim=diff.ssim,
            psnr=diff.psnr,
            rmse=diff.rmse,
            max_pixel_error=diff.max_pixel_error,
            baseline_path=str(baseline_path),
            candidate_path=str(candidate_path),
            diff_image_path=diff.diff_image_path,
        )

    def verify_multiple_subprocess(
        self,
        *,
        capture_path: str | Path,
        eid: int,
        stage: str,
        variants: List[str],
        output_dir: str | Path,
        ssim_threshold: float | None = None,
    ) -> List[VerifyResult]:
        """Verify multiple GLSL variants in ONE subprocess (single capture open).

        *variants* is a list of GLSL source strings.  The subprocess opens the
        capture once, takes a shared baseline screenshot, then iterates over
        each variant doing build→replace→screenshot→cleanup.

        Returns a list of ``VerifyResult`` in the same order as *variants*.
        """
        capture_path = Path(capture_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        batch_dir = output_dir / "_batch_verify"
        batch_dir.mkdir(parents=True, exist_ok=True)

        manifest = []
        for idx, glsl_source in enumerate(variants):
            glsl_path = batch_dir / f"variant_{idx:03d}.glsl"
            glsl_path.write_text(glsl_source, encoding="utf-8")
            manifest.append({"index": idx, "glsl_path": str(glsl_path)})

        manifest_path = batch_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        timeout = max(len(variants) * 15 + 120, _WORKER_TIMEOUT)
        cmd = [
            sys.executable, str(_BATCH_WORKER_SCRIPT),
            str(capture_path), str(eid), stage,
            str(manifest_path), str(batch_dir),
        ]

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return [VerifyResult(passed=False, compile_ok=False,
                                 error=f"Batch worker timed out ({timeout}s)")
                    for _ in variants]
        except Exception as exc:
            return [VerifyResult(passed=False, compile_ok=False,
                                 error=f"Worker launch error: {exc}")
                    for _ in variants]

        results_file = batch_dir / "batch_verify_results.json"
        if not results_file.exists():
            stderr_snippet = (proc.stderr or "")[:500]
            return [VerifyResult(passed=False, compile_ok=False,
                                 error=f"Worker crashed (exit={proc.returncode}): {stderr_snippet}")
                    for _ in variants]

        try:
            data = json.loads(results_file.read_text(encoding="utf-8"))
        except Exception as exc:
            return [VerifyResult(passed=False, compile_ok=False,
                                 error=f"Result JSON unreadable: {exc}")
                    for _ in variants]

        result_map: Dict[int, Dict] = {item["index"]: item for item in data}
        baseline_path = batch_dir / "baseline.png"
        out: List[VerifyResult] = []

        for idx in range(len(variants)):
            item = result_map.get(idx)
            if item is None:
                out.append(VerifyResult(passed=False, compile_ok=False,
                                        error="Missing result from worker"))
                continue

            if item.get("error"):
                out.append(VerifyResult(passed=False,
                                        compile_ok=item.get("compile_ok", False),
                                        error=item["error"]))
                continue

            if not item.get("compile_ok"):
                out.append(VerifyResult(passed=False, compile_ok=False,
                                        compile_errors=item.get("compile_errors", "")))
                continue

            candidate_path = Path(item.get("candidate_path", ""))
            if not baseline_path.exists() or not candidate_path.exists():
                out.append(VerifyResult(passed=False, compile_ok=True,
                                        compile_errors=item.get("compile_errors", ""),
                                        error="Screenshots not produced"))
                continue

            diff = self.pixel_diff.compare(
                baseline_path, candidate_path,
                output_dir=output_dir / f"diff_{idx:03d}",
                ssim_threshold=ssim_threshold,
            )

            out.append(VerifyResult(
                passed=diff.pass_threshold,
                compile_ok=True,
                compile_errors=item.get("compile_errors", ""),
                ssim=diff.ssim,
                psnr=diff.psnr,
                rmse=diff.rmse,
                max_pixel_error=diff.max_pixel_error,
                baseline_path=str(baseline_path),
                candidate_path=str(candidate_path),
                diff_image_path=diff.diff_image_path,
            ))

        return out

    def verify_batch(
        self,
        *,
        capture_path: str | Path,
        items: List[BatchVerifyItem],
        output_root: str | Path,
        ssim_threshold: float | None = None,
        use_subprocess: bool = True,
    ) -> BatchVerifyResult:
        """Verify multiple EID/shader pairs."""
        capture_path = Path(capture_path)
        output_root = Path(output_root)
        output_root.mkdir(parents=True, exist_ok=True)

        batch = BatchVerifyResult(total=len(items))
        for idx, item in enumerate(items):
            item_dir = output_root / f"eid_{item.eid}_{item.stage}_{idx}"
            result = self.verify_shader_equivalence(
                capture_path=capture_path,
                eid=item.eid,
                stage=item.stage,
                original_source="",
                modified_source=item.modified_source,
                output_dir=item_dir,
                ssim_threshold=ssim_threshold,
                use_subprocess=use_subprocess,
            )
            entry = result.to_dict()
            entry["eid"] = item.eid
            entry["stage"] = item.stage
            batch.items.append(entry)
            if result.passed:
                batch.passed_count += 1
            else:
                batch.failed_count += 1

        return batch

    @staticmethod
    def persist_result(result: VerifyResult | BatchVerifyResult, output_dir: str | Path) -> Path:
        """Write the verification result as JSON."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        out = output_dir / "verify_result.json"
        out.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return out
