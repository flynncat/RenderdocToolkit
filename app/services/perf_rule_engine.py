"""Deterministic rule engine that turns a perf job's row table into a list of
prioritised ``Finding`` objects.

Design philosophy:
- 100 % deterministic.  No LLM, no probabilistic scoring.  Every rule maps
  ``(row | rows | overview)`` -> 0..N findings.
- Rules are small, independent ``if`` blocks for easy human review.  No DSL.
- All numeric thresholds are top-level constants so they can be tuned without
  hunting through code.
- Each ``Finding`` carries an ``affected`` list of ``{capture_name, eid,
  pass_name}`` dicts so downstream report renderers can build deep links
  back into the perf table (``#perf-row-{eid}``).
- Findings stay strictly evidence-only: there are no recommendation
  templates or "expected gain" estimates - that copy was found to be
  generic and unhelpful in practice and has been removed.

The engine currently ships 10 rules (R001-R008, R010, R014).  Five additional
rule IDs (R009/R011/R012/R013/R015) are reserved in comments and will be
implemented after M3 (pipeline-state extras + malioc) lands.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional  # noqa: F401


# ---------------------------------------------------------------------------
# Tunable thresholds (single source of truth)
# ---------------------------------------------------------------------------

# R001: per-pixel multiplier of PS invocations vs covered area
OVERDRAW_FILL_TO_COVERAGE_RATIO = 3.0
OVERDRAW_MIN_PS_INVOCATIONS = 1_000_000

# R002: dense full-screen pass with heavy ALU
FULLSCREEN_COVERAGE_PERCENT = 80.0
FULLSCREEN_PS_INSTRUCTION_COUNT = 200

# R003: dense full-screen pass with high texture bandwidth
FULLSCREEN_BANDWIDTH_TEXTURE_MB = 4.0
FULLSCREEN_BANDWIDTH_TEXTURE_COUNT = 4

# R004: translucency pass overdraw
# Pass-label aliases.  The perf service emits both UE-style names
# (Translucency, ShadowDepths, PostProcessing) and shorter generic names
# (Translucent, Shadow, PostProcess) produced by the render-state
# heuristic.  Rules need to recognise both styles.
TRANSLUCENCY_PASS_NAMES = {"Translucency", "Translucent", "Additive"}
SHADOW_PASS_NAMES = {"ShadowDepths", "Shadow"}
POST_PROCESSING_PASS_NAMES = {"PostProcessing", "PostProcess"}
TRANSLUCENCY_OVERDRAW_PS_INVOCATIONS = 1_000_000

# R005/R006: pass-level percentage thresholds
SHADOW_PASS_PERCENT = 20.0
POST_PROCESSING_PASS_PERCENT = 25.0

# R007: top-K ALU-heavy shader outliers within the frame
SHADER_ALU_TOP_PERCENT = 0.05
SHADER_ALU_MIN_PS_INVOCATIONS = 100_000

# R008: huge textures bound by visually-small draws
HUGE_TEXTURE_MB = 16.0
HUGE_TEXTURE_LOW_COVERAGE_PERCENT = 5.0

# R010: micro-triangle waste
HIGH_TRI_LOW_PIXEL_TRIANGLES = 10_000
HIGH_TRI_LOW_PIXEL_COVERAGE_PERCENT = 1.0

# R014: per-pass unique-texture explosion
UNIQUE_TEXTURE_EXPLOSION_PER_PASS = 50


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    rule_id: str
    category: str               # overdraw | fillrate_alu | fillrate_bw | shader | texture | geometry | rt | other
    severity: str               # high | med | low
    scope: str                  # draw | pass | shader | frame
    title: str                  # short human-readable title (evidence summary)
    affected: List[Dict[str, Any]] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)
    report_anchor: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "severity": self.severity,
            "scope": self.scope,
            "title": self.title,
            "affected": list(self.affected),
            "evidence": dict(self.evidence),
            "report_anchor": self.report_anchor,
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class PerfRuleEngine:
    """Runs all enabled rules and returns a severity-sorted finding list."""

    def analyze(
        self,
        *,
        rows: List[Mapping[str, Any]],
        overview: Mapping[str, Any],
        pass_chart: List[Mapping[str, Any]],
        capture_info: Mapping[str, Any],
        capture_name: str = "",
    ) -> List[Finding]:
        rows = list(rows or [])
        pass_chart = list(pass_chart or [])

        findings: List[Finding] = []
        findings.extend(self._r001_overdraw_heavy(rows, capture_name))
        findings.extend(self._r002_fullscreen_heavy_ps(rows, capture_name))
        findings.extend(self._r003_fullscreen_bandwidth(rows, capture_name))
        findings.extend(self._r004_translucency_overdraw(rows, capture_name))
        findings.extend(self._r005_shadow_pass_too_heavy(pass_chart, rows, capture_name))
        findings.extend(self._r006_post_processing_heavy(pass_chart, rows, capture_name))
        findings.extend(self._r007_shader_alu_outlier(rows, capture_name))
        findings.extend(self._r008_huge_texture_low_use(rows, capture_name))
        findings.extend(self._r010_high_tri_low_pixel(rows, capture_name))
        findings.extend(self._r014_unique_texture_explosion(rows, capture_name))

        # NOTE: R009/R011/R012/R013/R015 are deferred until M3 lands the
        # supporting fields (state_switch_count, msaa_samples,
        # early_z_reject_rate, malioc cycles).

        for finding in findings:
            finding.report_anchor = self._build_anchor(finding)
        return self._sort_findings(findings)

    # -----------------------------------------------------------------------
    # Rule implementations.
    # Each returns a list of Findings (possibly empty).  Keep them small and
    # readable - one rule per method.
    # -----------------------------------------------------------------------

    def _r001_overdraw_heavy(self, rows, capture_name) -> List[Finding]:
        matched = []
        for row in rows:
            ps_inv = int(row.get("ps_invocations") or 0)
            coverage_px = max(int(row.get("coverage_pixels_estimate") or 0), 1)
            if ps_inv < OVERDRAW_MIN_PS_INVOCATIONS:
                continue
            ratio = ps_inv / coverage_px
            if ratio < OVERDRAW_FILL_TO_COVERAGE_RATIO:
                continue
            matched.append((row, ratio))
        if not matched:
            return []
        matched.sort(key=lambda item: item[1], reverse=True)
        affected = [_affected_entry(row, capture_name) for row, _ in matched[:5]]
        evidence_row, evidence_ratio = matched[0]
        return [Finding(
            rule_id="R001_overdraw_heavy",
            category="overdraw",
            severity="high",
            scope="draw",
            title="过绘制严重 (PS 调用远大于覆盖像素)",
            affected=affected,
            evidence={
                "matched_draws": len(matched),
                "worst_eid": _stringify(evidence_row.get("eid")),
                "worst_pass": _stringify(evidence_row.get("pass_name")),
                "worst_ps_invocations": int(evidence_row.get("ps_invocations") or 0),
                "worst_coverage_pixels": int(evidence_row.get("coverage_pixels_estimate") or 0),
                "worst_fill_to_coverage_ratio": round(evidence_ratio, 3),
                "threshold_ratio": OVERDRAW_FILL_TO_COVERAGE_RATIO,
            },
        )]

    def _r002_fullscreen_heavy_ps(self, rows, capture_name) -> List[Finding]:
        matched = [
            row for row in rows
            if float(row.get("screen_coverage_percent") or 0.0) >= FULLSCREEN_COVERAGE_PERCENT
            and int(row.get("ps_instruction_count") or 0) >= FULLSCREEN_PS_INSTRUCTION_COUNT
        ]
        if not matched:
            return []
        matched.sort(
            key=lambda row: int(row.get("ps_instruction_count") or 0)
            * int(row.get("ps_invocations") or 1),
            reverse=True,
        )
        evidence_row = matched[0]
        return [Finding(
            rule_id="R002_fullscreen_heavy_ps",
            category="fillrate_alu",
            severity="high",
            scope="draw",
            title="全屏覆盖 + 重 PS 指令 (ALU 填充率瓶颈)",
            affected=[_affected_entry(row, capture_name) for row in matched[:5]],
            evidence={
                "matched_draws": len(matched),
                "worst_eid": _stringify(evidence_row.get("eid")),
                "worst_ps_instruction_count": int(evidence_row.get("ps_instruction_count") or 0),
                "worst_ps_invocations": int(evidence_row.get("ps_invocations") or 0),
                "worst_coverage_percent": float(evidence_row.get("screen_coverage_percent") or 0.0),
                "threshold_ps_instruction_count": FULLSCREEN_PS_INSTRUCTION_COUNT,
                "threshold_coverage_percent": FULLSCREEN_COVERAGE_PERCENT,
            },
        )]

    def _r003_fullscreen_bandwidth(self, rows, capture_name) -> List[Finding]:
        matched = [
            row for row in rows
            if float(row.get("screen_coverage_percent") or 0.0) >= FULLSCREEN_COVERAGE_PERCENT
            and float(row.get("texture_total_mb") or 0.0) >= FULLSCREEN_BANDWIDTH_TEXTURE_MB
            and int(row.get("texture_count") or 0) >= FULLSCREEN_BANDWIDTH_TEXTURE_COUNT
        ]
        if not matched:
            return []
        matched.sort(
            key=lambda row: float(row.get("texture_total_mb") or 0.0)
            * int(row.get("ps_invocations") or 1),
            reverse=True,
        )
        evidence_row = matched[0]
        return [Finding(
            rule_id="R003_fullscreen_bandwidth",
            category="fillrate_bw",
            severity="high",
            scope="draw",
            title="全屏覆盖 + 大体量贴图 (带宽填充率瓶颈)",
            affected=[_affected_entry(row, capture_name) for row in matched[:5]],
            evidence={
                "matched_draws": len(matched),
                "worst_eid": _stringify(evidence_row.get("eid")),
                "worst_texture_total_mb": float(evidence_row.get("texture_total_mb") or 0.0),
                "worst_texture_count": int(evidence_row.get("texture_count") or 0),
                "worst_ps_invocations": int(evidence_row.get("ps_invocations") or 0),
                "threshold_texture_mb": FULLSCREEN_BANDWIDTH_TEXTURE_MB,
                "threshold_texture_count": FULLSCREEN_BANDWIDTH_TEXTURE_COUNT,
            },
        )]

    def _r004_translucency_overdraw(self, rows, capture_name) -> List[Finding]:
        matched = [
            row for row in rows
            if _stringify(row.get("scene_pass")) in TRANSLUCENCY_PASS_NAMES
            and int(row.get("ps_invocations") or 0) >= TRANSLUCENCY_OVERDRAW_PS_INVOCATIONS
        ]
        if not matched:
            return []
        matched.sort(key=lambda row: int(row.get("ps_invocations") or 0), reverse=True)
        evidence_row = matched[0]
        return [Finding(
            rule_id="R004_translucency_overdraw",
            category="overdraw",
            severity="high",
            scope="draw",
            title="Translucency Pass 过绘制",
            affected=[_affected_entry(row, capture_name) for row in matched[:5]],
            evidence={
                "matched_draws": len(matched),
                "worst_eid": _stringify(evidence_row.get("eid")),
                "worst_pass_name": _stringify(evidence_row.get("pass_name")),
                "worst_ps_invocations": int(evidence_row.get("ps_invocations") or 0),
                "worst_instances": int(evidence_row.get("instances") or 0),
                "worst_triangles": int(evidence_row.get("triangles") or 0),
                "threshold_ps_invocations": TRANSLUCENCY_OVERDRAW_PS_INVOCATIONS,
            },
        )]

    def _r005_shadow_pass_too_heavy(self, pass_chart, rows, capture_name) -> List[Finding]:
        chart_item = None
        for name in SHADOW_PASS_NAMES:
            chart_item = _find_pass(pass_chart, name)
            if chart_item is not None:
                break
        if chart_item is None:
            return []
        percent = float(chart_item.get("percent") or 0.0)
        if percent < SHADOW_PASS_PERCENT:
            return []
        affected = sorted(
            [row for row in rows if _stringify(row.get("scene_pass")) in SHADOW_PASS_NAMES],
            key=lambda row: int(row.get("triangles") or 0),
            reverse=True,
        )[:5]
        return [Finding(
            rule_id="R005_shadow_pass_too_heavy",
            category="rt",
            severity="med",
            scope="pass",
            title="ShadowDepths Pass 占比偏高",
            affected=[_affected_entry(row, capture_name) for row in affected],
            evidence={
                "pass_percent": percent,
                "pass_gpu_duration_ms": float(chart_item.get("gpu_duration_ms") or 0.0),
                "pass_draw_count": int(chart_item.get("draw_count") or 0),
                "pass_total_triangles": int(chart_item.get("triangles") or 0),
                "threshold_percent": SHADOW_PASS_PERCENT,
            },
        )]

    def _r006_post_processing_heavy(self, pass_chart, rows, capture_name) -> List[Finding]:
        chart_item = None
        for name in POST_PROCESSING_PASS_NAMES:
            chart_item = _find_pass(pass_chart, name)
            if chart_item is not None:
                break
        if chart_item is None:
            return []
        percent = float(chart_item.get("percent") or 0.0)
        if percent < POST_PROCESSING_PASS_PERCENT:
            return []
        affected = sorted(
            [row for row in rows if _stringify(row.get("scene_pass")) in POST_PROCESSING_PASS_NAMES],
            key=lambda row: float(row.get("gpu_duration_ms") or 0.0),
            reverse=True,
        )[:5]
        return [Finding(
            rule_id="R006_post_processing_heavy",
            category="fillrate_alu",
            severity="med",
            scope="pass",
            title="PostProcessing Pass 占比偏高",
            affected=[_affected_entry(row, capture_name) for row in affected],
            evidence={
                "pass_percent": percent,
                "pass_gpu_duration_ms": float(chart_item.get("gpu_duration_ms") or 0.0),
                "pass_draw_count": int(chart_item.get("draw_count") or 0),
                "threshold_percent": POST_PROCESSING_PASS_PERCENT,
            },
        )]

    def _r007_shader_alu_outlier(self, rows, capture_name) -> List[Finding]:
        candidates = [
            row for row in rows
            if int(row.get("ps_invocations") or 0) >= SHADER_ALU_MIN_PS_INVOCATIONS
            and int(row.get("ps_instruction_count") or 0) > 0
        ]
        if not candidates:
            return []
        instructions = sorted(
            (int(row.get("ps_instruction_count") or 0) for row in candidates),
            reverse=True,
        )
        if not instructions:
            return []
        top_n = max(1, int(len(instructions) * SHADER_ALU_TOP_PERCENT))
        threshold = instructions[min(top_n, len(instructions)) - 1]
        matched = [
            row for row in candidates
            if int(row.get("ps_instruction_count") or 0) >= threshold
        ]
        if not matched:
            return []
        matched.sort(
            key=lambda row: int(row.get("ps_instruction_count") or 0)
            * int(row.get("ps_invocations") or 1),
            reverse=True,
        )
        evidence_row = matched[0]
        return [Finding(
            rule_id="R007_shader_alu_outlier",
            category="shader",
            severity="high",
            scope="shader",
            title="个别 PS 着色器指令数明显偏多",
            affected=[_affected_entry(row, capture_name) for row in matched[:5]],
            evidence={
                "matched_draws": len(matched),
                "threshold_top_percent": SHADER_ALU_TOP_PERCENT,
                "threshold_ps_instruction_count": threshold,
                "worst_eid": _stringify(evidence_row.get("eid")),
                "worst_ps_instruction_count": int(evidence_row.get("ps_instruction_count") or 0),
                "worst_ps_invocations": int(evidence_row.get("ps_invocations") or 0),
                "worst_shader_id": _stringify(
                    (evidence_row.get("shader_ids") or {}).get("ps")
                    if isinstance(evidence_row.get("shader_ids"), Mapping) else ""
                ),
            },
        )]

    def _r008_huge_texture_low_use(self, rows, capture_name) -> List[Finding]:
        matched = [
            row for row in rows
            if float(row.get("texture_total_mb") or 0.0) >= HUGE_TEXTURE_MB
            and float(row.get("screen_coverage_percent") or 0.0) < HUGE_TEXTURE_LOW_COVERAGE_PERCENT
        ]
        if not matched:
            return []
        matched.sort(key=lambda row: float(row.get("texture_total_mb") or 0.0), reverse=True)
        evidence_row = matched[0]
        return [Finding(
            rule_id="R008_huge_texture_low_use",
            category="texture",
            severity="med",
            scope="draw",
            title="大体量贴图但屏幕占用很小 (资源浪费)",
            affected=[_affected_entry(row, capture_name) for row in matched[:5]],
            evidence={
                "matched_draws": len(matched),
                "worst_eid": _stringify(evidence_row.get("eid")),
                "worst_texture_total_mb": float(evidence_row.get("texture_total_mb") or 0.0),
                "worst_coverage_percent": float(evidence_row.get("screen_coverage_percent") or 0.0),
                "threshold_texture_mb": HUGE_TEXTURE_MB,
                "threshold_coverage_percent": HUGE_TEXTURE_LOW_COVERAGE_PERCENT,
            },
        )]

    def _r010_high_tri_low_pixel(self, rows, capture_name) -> List[Finding]:
        matched = [
            row for row in rows
            if int(row.get("triangles") or 0) >= HIGH_TRI_LOW_PIXEL_TRIANGLES
            and float(row.get("screen_coverage_percent") or 0.0) < HIGH_TRI_LOW_PIXEL_COVERAGE_PERCENT
        ]
        if not matched:
            return []
        matched.sort(key=lambda row: int(row.get("triangles") or 0), reverse=True)
        evidence_row = matched[0]
        return [Finding(
            rule_id="R010_high_tri_low_pixel",
            category="geometry",
            severity="med",
            scope="draw",
            title="高三角面但像素覆盖极低 (微小三角形浪费)",
            affected=[_affected_entry(row, capture_name) for row in matched[:5]],
            evidence={
                "matched_draws": len(matched),
                "worst_eid": _stringify(evidence_row.get("eid")),
                "worst_triangles": int(evidence_row.get("triangles") or 0),
                "worst_coverage_percent": float(evidence_row.get("screen_coverage_percent") or 0.0),
                "threshold_triangles": HIGH_TRI_LOW_PIXEL_TRIANGLES,
                "threshold_coverage_percent": HIGH_TRI_LOW_PIXEL_COVERAGE_PERCENT,
            },
        )]

    def _r014_unique_texture_explosion(self, rows, capture_name) -> List[Finding]:
        # Group rows by scene_pass and dedup textures by resource_id.
        per_pass_unique: Dict[str, set] = {}
        per_pass_eids: Dict[str, List[Mapping[str, Any]]] = {}
        for row in rows:
            pass_name = _stringify(row.get("scene_pass")) or "Other"
            tex_set = per_pass_unique.setdefault(pass_name, set())
            for item in row.get("texture_summary_items") or []:
                if not isinstance(item, Mapping):
                    continue
                res_id = _stringify(item.get("resource_id") or item.get("res_id"))
                if res_id:
                    tex_set.add(res_id)
            per_pass_eids.setdefault(pass_name, []).append(row)

        findings = []
        for pass_name, tex_set in per_pass_unique.items():
            if len(tex_set) < UNIQUE_TEXTURE_EXPLOSION_PER_PASS:
                continue
            affected_rows = sorted(
                per_pass_eids.get(pass_name) or [],
                key=lambda row: int(row.get("texture_count") or 0),
                reverse=True,
            )[:5]
            findings.append(Finding(
                rule_id="R014_unique_texture_explosion",
                category="texture",
                severity="med",
                scope="pass",
                title=f"`{pass_name}` Pass 贴图种类爆炸 (>= {UNIQUE_TEXTURE_EXPLOSION_PER_PASS})",
                affected=[_affected_entry(row, capture_name) for row in affected_rows],
                evidence={
                    "scene_pass": pass_name,
                    "unique_texture_count": len(tex_set),
                    "threshold": UNIQUE_TEXTURE_EXPLOSION_PER_PASS,
                    "draw_count_in_pass": len(per_pass_eids.get(pass_name) or []),
                },
            ))
        return findings

    # -----------------------------------------------------------------------

    def _build_anchor(self, finding: Finding) -> str:
        first_eid = ""
        if finding.affected:
            first_eid = _stringify(finding.affected[0].get("eid"))
        slug = finding.rule_id.lower().replace("_", "-")
        if first_eid:
            return f"finding-{slug}-{first_eid}"
        return f"finding-{slug}"

    def _sort_findings(self, findings: List[Finding]) -> List[Finding]:
        order = {"high": 0, "med": 1, "low": 2}
        return sorted(
            findings,
            key=lambda f: (order.get(f.severity, 99), f.rule_id),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stringify(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _affected_entry(row: Mapping[str, Any], capture_name: str) -> Dict[str, Any]:
    return {
        "capture_name": capture_name,
        "eid": _stringify(row.get("eid")),
        "pass_name": _stringify(row.get("pass_name")),
        "scene_pass": _stringify(row.get("scene_pass")),
        "gpu_duration_ms": float(row.get("gpu_duration_ms") or 0.0),
        "ps_invocations": int(row.get("ps_invocations") or 0),
        "triangles": int(row.get("triangles") or 0),
    }


def _find_pass(pass_chart, name: str) -> Optional[Mapping[str, Any]]:
    for item in pass_chart:
        if isinstance(item, Mapping) and _stringify(item.get("name")) == name:
            return item
    return None
