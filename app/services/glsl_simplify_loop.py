"""Simplification loop: iteratively apply GLSL transforms, verify each step.

All RenderDoc replay operations run in isolated subprocesses by default
to prevent GPU driver crashes from killing the main service.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.glsl_simplifier import GlslSimplifier, SimplifyResult
from app.services.shader_verify_service import ShaderVerifyService, VerifyResult


@dataclass
class _PrecomputedCandidate:
    step_idx: int
    levels: str
    source: str
    line_count: int
    is_duplicate: bool


@dataclass
class StepLog:
    step: int
    levels: str
    lines_before: int
    lines_after: int
    ssim: float
    passed: bool
    compile_ok: bool
    compile_errors: str = ""
    action: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "levels": self.levels,
            "lines_before": self.lines_before,
            "lines_after": self.lines_after,
            "ssim": self.ssim,
            "passed": self.passed,
            "compile_ok": self.compile_ok,
            "compile_errors": self.compile_errors,
            "action": self.action,
        }


@dataclass
class LoopResult:
    original_source: str
    simplified_source: str
    original_line_count: int
    simplified_line_count: int
    total_steps: int
    accepted_steps: int
    rejected_steps: int
    final_ssim: float
    steps: List[StepLog] = field(default_factory=list)
    simplify_details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "original_line_count": self.original_line_count,
            "simplified_line_count": self.simplified_line_count,
            "reduction_pct": round(
                (1 - self.simplified_line_count / max(self.original_line_count, 1)) * 100, 1
            ),
            "total_steps": self.total_steps,
            "accepted_steps": self.accepted_steps,
            "rejected_steps": self.rejected_steps,
            "final_ssim": self.final_ssim,
            "steps": [s.to_dict() for s in self.steps],
            "simplify_details": self.simplify_details,
        }


SIMPLIFY_PASSES = [
    "L0",
    "L0,L1",
    "L0,L1,L2",
    "L0,L1,L2,L3",
    "L0,L1,L2,L3,L4",
]


class GlslSimplifyLoop:
    """Iteratively simplify GLSL, verifying each level via ShaderVerifyService."""

    def __init__(
        self,
        simplifier: GlslSimplifier | None = None,
        verify_service: ShaderVerifyService | None = None,
    ):
        self.simplifier = simplifier or GlslSimplifier()
        self.verify_service = verify_service or ShaderVerifyService()

    def run(
        self,
        *,
        capture_path: str | Path,
        eid: int,
        original_glsl: str,
        shader_params_json: str = "",
        output_dir: str | Path,
        stage: str = "ps",
        ssim_threshold: float = 0.98,
        use_subprocess: bool = True,
    ) -> LoopResult:
        capture_path = Path(capture_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        original_lines = len(original_glsl.splitlines()) if original_glsl.strip() else 0

        candidates: List[_PrecomputedCandidate] = []
        last_simplify: Optional[SimplifyResult] = None
        seen_sources: set[str] = {original_glsl.strip()}

        for step_idx, levels in enumerate(SIMPLIFY_PASSES):
            candidate = self.simplifier.simplify(
                original_glsl,
                shader_params_json=shader_params_json,
                levels=levels,
            )
            last_simplify = candidate
            stripped = candidate.simplified_source.strip()
            is_dup = stripped in seen_sources
            seen_sources.add(stripped)
            candidates.append(_PrecomputedCandidate(
                step_idx=step_idx,
                levels=levels,
                source=candidate.simplified_source,
                line_count=candidate.simplified_line_count,
                is_duplicate=is_dup,
            ))

        need_verify = [c for c in candidates if not c.is_duplicate]

        if need_verify and use_subprocess:
            variants = [c.source for c in need_verify]
            verify_results = self.verify_service.verify_multiple_subprocess(
                capture_path=capture_path,
                eid=eid,
                stage=stage,
                variants=variants,
                output_dir=output_dir,
                ssim_threshold=ssim_threshold,
            )
            vr_map: Dict[int, VerifyResult] = {}
            for i, c in enumerate(need_verify):
                vr_map[c.step_idx] = verify_results[i]
        else:
            vr_map = {}
            for c in need_verify:
                step_dir = output_dir / f"step_{c.step_idx}"
                vr_map[c.step_idx] = self.verify_service.verify_shader_equivalence(
                    capture_path=capture_path,
                    eid=eid,
                    stage=stage,
                    original_source=original_glsl,
                    modified_source=c.source,
                    output_dir=step_dir,
                    ssim_threshold=ssim_threshold,
                    use_subprocess=False,
                )

        current_source = original_glsl
        steps: List[StepLog] = []
        accepted = 0
        rejected = 0
        final_ssim = 0.0

        for c in candidates:
            if c.is_duplicate:
                steps.append(StepLog(
                    step=c.step_idx,
                    levels=c.levels,
                    lines_before=len(current_source.splitlines()),
                    lines_after=c.line_count,
                    ssim=final_ssim,
                    passed=True,
                    compile_ok=True,
                    action="skip_no_change",
                ))
                continue

            vr = vr_map.get(c.step_idx)
            if vr is None:
                steps.append(StepLog(
                    step=c.step_idx, levels=c.levels,
                    lines_before=len(current_source.splitlines()),
                    lines_after=c.line_count, ssim=0.0,
                    passed=False, compile_ok=False,
                    compile_errors="", action="missing_result",
                ))
                rejected += 1
                continue

            log = StepLog(
                step=c.step_idx,
                levels=c.levels,
                lines_before=len(current_source.splitlines()),
                lines_after=c.line_count,
                ssim=vr.ssim,
                passed=vr.passed,
                compile_ok=vr.compile_ok,
                compile_errors=vr.compile_errors,
            )

            if vr.passed:
                current_source = c.source
                final_ssim = vr.ssim
                accepted += 1
                log.action = "accepted"
            elif not vr.compile_ok:
                rejected += 1
                log.action = "rejected_compile_fail"
            else:
                rejected += 1
                log.action = "rejected_ssim_below_threshold"

            steps.append(log)

        log_path = output_dir / "simplify_log.json"
        result = LoopResult(
            original_source=original_glsl,
            simplified_source=current_source,
            original_line_count=original_lines,
            simplified_line_count=len(current_source.splitlines()),
            total_steps=len(steps),
            accepted_steps=accepted,
            rejected_steps=rejected,
            final_ssim=final_ssim,
            steps=steps,
            simplify_details=last_simplify.to_dict() if last_simplify else None,
        )
        log_path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return result
