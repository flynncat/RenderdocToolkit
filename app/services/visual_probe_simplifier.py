"""Visual probe simplifier: verify code removal by hot-swapping shaders
in RenderDoc and comparing rendered output.

This implements the user's manual debugging workflow in an automated way:
  1. Parse the shader into removable candidates (uniform, if-block, statement)
  2. For each candidate, hot-swap the modified shader via RenderDoc replay
  3. Compare the screenshot against the original using SSIM/PSNR
  4. Accept or reject the removal based on pixel-level equivalence
  5. Accumulate all accepted removals to produce a maximally simplified shader

Three probe levels run in order:
  Level A — Uniform contribution probing (replace each uniform with default)
  Level B — Control-flow block probing (remove if-blocks that don't write outputs)
  Level C — Statement-level probing (remove or default individual statements)
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import subprocess
import sys

from app.services.glsl_code_analyzer import GlslCodeAnalyzer, RemovalCandidate
from app.services.glsl_simplifier import GlslSimplifier
from app.services.llm_shader_simplifier import LlmShaderSimplifier
from app.services.shader_verify_service import ShaderVerifyService, VerifyResult


@dataclass
class ProbeStep:
    index: int
    kind: str
    label: str
    description: str
    line_range: tuple
    accepted: bool
    ssim: float = 0.0
    psnr: float = 0.0
    compile_ok: bool = True
    compile_errors: str = ""
    error: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class VisualProbeResult:
    original_source: str
    static_simplified_source: str
    final_source: str
    original_lines: int
    static_simplified_lines: int
    final_lines: int
    total_probes: int
    accepted_probes: int
    rejected_probes: int
    compile_failed_probes: int
    probe_steps: List[ProbeStep] = field(default_factory=list)
    elapsed_total_ms: int = 0
    mode: str = "full"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "original_lines": self.original_lines,
            "static_simplified_lines": self.static_simplified_lines,
            "final_lines": self.final_lines,
            "reduction_total_pct": round(
                (1 - self.final_lines / max(self.original_lines, 1)) * 100, 1
            ),
            "reduction_visual_pct": round(
                (1 - self.final_lines / max(self.static_simplified_lines, 1)) * 100, 1
            ),
            "total_probes": self.total_probes,
            "accepted_probes": self.accepted_probes,
            "rejected_probes": self.rejected_probes,
            "compile_failed_probes": self.compile_failed_probes,
            "probe_steps": [s.to_dict() for s in self.probe_steps],
            "elapsed_total_ms": self.elapsed_total_ms,
            "mode": self.mode,
        }


class VisualProbeSimplifier:
    """Automated visual-probe shader simplification engine."""

    def __init__(
        self,
        verify_service: ShaderVerifyService | None = None,
        simplifier: GlslSimplifier | None = None,
        analyzer: GlslCodeAnalyzer | None = None,
        llm_simplifier: LlmShaderSimplifier | None = None,
    ):
        self.verify_service = verify_service or ShaderVerifyService()
        self.simplifier = simplifier or GlslSimplifier()
        self.analyzer = analyzer or GlslCodeAnalyzer()
        self.llm_simplifier = llm_simplifier

    def run(
        self,
        *,
        capture_path: str | Path,
        eid: int,
        original_glsl: str,
        shader_params_json: str = "",
        output_dir: str | Path,
        stage: str = "ps",
        ssim_threshold: float = 0.995,
        max_probes: int = 200,
        use_subprocess: bool = True,
        compile_only: bool = False,
        use_llm: bool = False,
    ) -> VisualProbeResult:
        """Full pipeline: static simplify → analyze → probe each candidate → return result.

        When *use_subprocess* is True (default), each RenderDoc replay runs in an
        isolated subprocess so that GPU driver crashes don't kill the main process.

        When *compile_only* is True, probes are verified only by compilation
        (no rendering/SSIM comparison). Useful when the capture GPU differs from
        the replay GPU, making rendered output unreliable.
        """
        t0 = time.time()
        capture_path = Path(capture_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        static_result = self.simplifier.simplify(
            original_glsl,
            shader_params_json=shader_params_json,
            levels="L0,L1,L2,L3,L4",
        )
        current_source = static_result.simplified_source
        static_lines = static_result.simplified_line_count

        (output_dir / "static_simplified.glsl").write_text(
            current_source, encoding="utf-8",
        )

        candidates = self.analyzer.analyze(current_source)

        llm_result = None
        if use_llm and self.llm_simplifier and self.llm_simplifier.is_available():
            llm_result = self.llm_simplifier.generate_candidates(current_source)
            if llm_result.candidates:
                seen_ranges = {c.line_range for c in candidates}
                for lc in llm_result.candidates:
                    if lc.line_range not in seen_ranges:
                        candidates.append(lc)

            llm_log = output_dir / "llm_simplify.json"
            llm_log.write_text(
                json.dumps(llm_result.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        candidates.sort(key=lambda c: _candidate_priority(c))

        if len(candidates) > max_probes:
            candidates = candidates[:max_probes]

        steps: List[ProbeStep] = []
        accepted = 0
        rejected = 0
        compile_failed = 0

        if compile_only and use_subprocess and candidates:
            batch_results = self._batch_compile_check(
                capture_path, eid, stage, candidates, output_dir,
            )

            for idx, candidate in enumerate(candidates):
                vr = batch_results.get(idx, VerifyResult(passed=False, compile_ok=False, error="missing"))
                step = ProbeStep(
                    index=idx,
                    kind=candidate.kind,
                    label=candidate.label,
                    description=candidate.description,
                    line_range=candidate.line_range,
                    accepted=vr.passed,
                    ssim=vr.ssim,
                    compile_ok=vr.compile_ok,
                    compile_errors=vr.compile_errors,
                    error=vr.error,
                    elapsed_ms=0,
                )
                steps.append(step)

                if vr.passed:
                    current_source = candidate.modified_source
                    accepted += 1
                elif not vr.compile_ok:
                    compile_failed += 1
                else:
                    rejected += 1

        else:
            for idx, candidate in enumerate(candidates):
                step_dir = output_dir / f"probe_{idx:03d}_{candidate.kind}"
                t_step = time.time()

                try:
                    vr = self.verify_service.verify_shader_equivalence(
                        capture_path=capture_path,
                        eid=eid,
                        stage=stage,
                        original_source=current_source,
                        modified_source=candidate.modified_source,
                        output_dir=step_dir,
                        ssim_threshold=ssim_threshold,
                        use_subprocess=use_subprocess,
                    )
                except Exception as exc:
                    step = ProbeStep(
                        index=idx,
                        kind=candidate.kind,
                        label=candidate.label,
                        description=candidate.description,
                        line_range=candidate.line_range,
                        accepted=False,
                        error=str(exc),
                        elapsed_ms=int((time.time() - t_step) * 1000),
                    )
                    steps.append(step)
                    rejected += 1
                    continue

                step = ProbeStep(
                    index=idx,
                    kind=candidate.kind,
                    label=candidate.label,
                    description=candidate.description,
                    line_range=candidate.line_range,
                    accepted=vr.passed,
                    ssim=vr.ssim,
                    psnr=vr.psnr,
                    compile_ok=vr.compile_ok,
                    compile_errors=vr.compile_errors,
                    error=vr.error,
                    elapsed_ms=int((time.time() - t_step) * 1000),
                )
                steps.append(step)

                if vr.passed:
                    current_source = candidate.modified_source
                    accepted += 1

                    remaining_candidates = candidates[idx + 1:]
                    new_candidates = self.analyzer.analyze(current_source)
                    new_candidates.sort(key=lambda c: _candidate_priority(c))

                    def _find_matching(old_c: RemovalCandidate,
                                       new_list: List[RemovalCandidate]) -> Optional[RemovalCandidate]:
                        for nc in new_list:
                            if nc.kind == old_c.kind and nc.label == old_c.label:
                                return nc
                        return None

                    updated = []
                    for old_c in remaining_candidates:
                        match = _find_matching(old_c, new_candidates)
                        if match is not None:
                            updated.append(match)
                    candidates = candidates[:idx + 1] + updated
                    if len(candidates) > idx + 1 + max_probes:
                        candidates = candidates[:idx + 1 + max_probes]
                elif not vr.compile_ok:
                    compile_failed += 1
                else:
                    rejected += 1

        current_source = self._clean_probe_markers(current_source)

        (output_dir / "final_simplified.glsl").write_text(
            current_source, encoding="utf-8",
        )

        result = VisualProbeResult(
            original_source=original_glsl,
            static_simplified_source=static_result.simplified_source,
            final_source=current_source,
            original_lines=len(original_glsl.splitlines()),
            static_simplified_lines=static_lines,
            final_lines=len(current_source.splitlines()),
            total_probes=len(steps),
            accepted_probes=accepted,
            rejected_probes=rejected,
            compile_failed_probes=compile_failed,
            probe_steps=steps,
            elapsed_total_ms=int((time.time() - t0) * 1000),
            mode="full",
        )

        log_path = output_dir / "visual_probe_log.json"
        log_path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        return result

    def run_offline(
        self,
        *,
        original_glsl: str,
        shader_params_json: str = "",
        output_dir: str | Path,
        use_llm: bool = False,
    ) -> VisualProbeResult:
        """Offline mode: only static simplification + candidate analysis (no RenderDoc).

        Returns the list of candidates that *would* be tested, for reporting.
        """
        t0 = time.time()
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        static_result = self.simplifier.simplify(
            original_glsl,
            shader_params_json=shader_params_json,
            levels="L0,L1,L2,L3,L4",
        )
        current_source = static_result.simplified_source
        static_lines = static_result.simplified_line_count

        candidates = self.analyzer.analyze(current_source)

        if use_llm and self.llm_simplifier and self.llm_simplifier.is_available():
            llm_result = self.llm_simplifier.generate_candidates(current_source)
            if llm_result.candidates:
                seen_ranges = {c.line_range for c in candidates}
                for lc in llm_result.candidates:
                    if lc.line_range not in seen_ranges:
                        candidates.append(lc)
            llm_log = output_dir / "llm_simplify.json"
            llm_log.write_text(
                json.dumps(llm_result.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        candidates.sort(key=lambda c: _candidate_priority(c))

        steps: List[ProbeStep] = []
        for idx, c in enumerate(candidates):
            steps.append(ProbeStep(
                index=idx,
                kind=c.kind,
                label=c.label,
                description=c.description,
                line_range=c.line_range,
                accepted=False,
                error="offline_mode: 需要 .rdc 文件进行验证",
            ))

        (output_dir / "static_simplified.glsl").write_text(
            current_source, encoding="utf-8",
        )

        result = VisualProbeResult(
            original_source=original_glsl,
            static_simplified_source=current_source,
            final_source=current_source,
            original_lines=len(original_glsl.splitlines()),
            static_simplified_lines=static_lines,
            final_lines=static_lines,
            total_probes=len(steps),
            accepted_probes=0,
            rejected_probes=0,
            compile_failed_probes=0,
            probe_steps=steps,
            elapsed_total_ms=int((time.time() - t0) * 1000),
            mode="offline",
        )

        log_path = output_dir / "visual_probe_log.json"
        log_path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return result

    def _batch_compile_check(
        self,
        capture_path: Path,
        eid: int,
        stage: str,
        candidates: List[RemovalCandidate],
        output_dir: Path,
    ) -> Dict[int, VerifyResult]:
        """Batch compile all candidates in a single subprocess (much faster)."""
        batch_dir = output_dir / "_batch_compile"
        batch_dir.mkdir(parents=True, exist_ok=True)

        manifest = []
        for idx, c in enumerate(candidates):
            glsl_path = batch_dir / f"probe_{idx:03d}.glsl"
            glsl_path.write_text(c.modified_source, encoding="utf-8")
            manifest.append({"index": idx, "glsl_path": str(glsl_path)})

        manifest_path = batch_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        worker = Path(__file__).resolve().parents[1].parent / "scripts" / "rdc_batch_compile_worker.py"
        cmd = [
            sys.executable, str(worker),
            str(capture_path), str(eid), stage,
            str(manifest_path), str(batch_dir),
        ]

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=max(len(candidates) * 5, 60),
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return {i: VerifyResult(passed=False, compile_ok=False, error="Batch compile timed out")
                    for i in range(len(candidates))}
        except Exception as exc:
            return {i: VerifyResult(passed=False, compile_ok=False, error=f"Worker error: {exc}")
                    for i in range(len(candidates))}

        results_file = batch_dir / "batch_results.json"
        if not results_file.exists():
            return {i: VerifyResult(passed=False, compile_ok=False,
                                    error=f"Worker crashed (exit={proc.returncode})")
                    for i in range(len(candidates))}

        data = json.loads(results_file.read_text(encoding="utf-8"))
        result_map: Dict[int, VerifyResult] = {}
        for item in data:
            idx = item["index"]
            ok = item.get("compile_ok", False)
            result_map[idx] = VerifyResult(
                passed=ok,
                compile_ok=ok,
                compile_errors=item.get("compile_errors", ""),
                error=item.get("error", ""),
                ssim=1.0 if ok else 0.0,
            )
        return result_map

    @staticmethod
    def _clean_probe_markers(source: str) -> str:
        import re
        source = re.sub(r"/\*\s*probed-out\s*\*/\s*", "", source)
        lines = source.split("\n")
        cleaned = [line for line in lines if line.strip()]
        return "\n".join(cleaned)


def _candidate_priority(c: RemovalCandidate) -> tuple:
    """Sort order: preprocessor first, then uniform, sampler, if branches, statements."""
    order = {
        "preprocessor_define": 0,
        "preprocessor_define_inline": 0,
        "preprocessor_extension": 0,
        "preprocessor_keep_if": 1,
        "preprocessor_keep_else": 1,
        "preprocessor_remove_block": 1,
        "uniform": 2,
        "sampler_fetch": 3,
        "if_block": 4,
        "if_branch_keep_if": 5,
        "if_branch_keep_else": 5,
        "statement": 6,
        "statement_default": 7,
        "llm_remove": 8,
        "llm_replace": 8,
        "llm_branch": 8,
    }
    return (order.get(c.kind, 9), c.line_range[0])
