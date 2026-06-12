"""Data models for the enhanced performance report.

These dataclasses describe the *extra* information the enhanced report needs
on top of what the existing ``perf_analysis.json`` already provides (see
``app/services/renderdoc_perf_service.py`` -> ``_build_rows`` for the source
row schema).  They are intentionally plain and JSON-serialisable so the
enhanced report layer can be wired into the existing artifact pipeline later
without dragging in RenderDoc runtime types.

This module is part of the *skeleton* delivery: structures are defined, but
the producers (``mali_shader_analyzer`` / ``drawcall_classifier`` /
``texture_auditor`` / ``optimization_planner``) return placeholder values
until the follow-up implementation phase fills them in.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


# Mali GPU work-register ceiling.  Fragment shaders at/above this trigger
# register spilling (data evicted to memory), which collapses occupancy and
# tanks performance.  Mirrors the threshold used in the reference report's
# shader table (e.g. program 40494 at 64 regs flagged as "spill 严重").
MALI_WORK_REGISTER_SPILL_THRESHOLD = 64


@dataclass
class ShaderMaliMetrics:
    """Per-shader Mali Offline Compiler result.

    Field names follow ``malioc`` output (see ``rdc_compare_ultimate.py``
    ``ShaderComplexity`` and its parsing regexes for the source of truth).
    """

    shader_id: str = ""
    stage: str = ""  # "vs" | "fs"/"ps"
    work_registers: int = 0
    uniform_registers: int = 0
    alu_cycles: float = 0.0
    ls_cycles: float = 0.0
    varying_cycles: float = 0.0
    texture_cycles: float = 0.0
    arithmetic_16bit: float = 0.0
    bound_unit: str = ""  # "A" | "LS" | "V" | "T" - the bottleneck unit
    register_spill: bool = False
    # True when the metrics could not be produced (no malioc / no shader
    # source / parse failure) so the report can render a clear "N/A" instead
    # of silently showing zeros.
    available: bool = False
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BusinessCategory:
    """A single business-classification rule entry.

    Maps engine artefact names (markers / mesh / material / shader names) to
    a two-level taxonomy mirroring the reference report:
      - ``level1``: 一级分类 (农作物 / 宠物 / 场景装饰物 / 其他)
      - ``class_id``: 二级 class (crop / crop_special / env_terrain / ...)
    """

    level1: str = "其他"
    class_id: str = "unknown"
    keywords: List[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DrawClassification:
    """Classification result attached to one draw row."""

    eid: str = ""
    level1: str = "其他"
    class_id: str = "unknown"
    matched_keyword: str = ""
    matched_source: str = ""  # which field matched: pass_name | breadcrumb | shader

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TextureAuditEntry:
    """One unique texture aggregated across the whole frame."""

    resource_id: str = ""
    name: str = ""
    width: int = 0
    height: int = 0
    format: str = ""
    byte_size_mb: float = 0.0
    used_by_draw_count: int = 0
    is_large: bool = False  # width or height >= LARGE_TEXTURE_EDGE
    suggestion: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Textures with either edge >= this are flagged as "大尺寸纹理" in the audit
# (matches the reference report calling out 4096 / 2048 textures).
LARGE_TEXTURE_EDGE = 2048


@dataclass
class OptimizationItem:
    """One row in the optimisation-priority table (报告第八节)."""

    priority: str = "P2"  # P0 | P1 | P2 | P3
    title: str = ""
    rationale: str = ""
    expected_gain_ms_low: float = 0.0
    expected_gain_ms_high: float = 0.0
    effort: str = "中"  # 低 | 中 | 高
    related_eids: List[str] = field(default_factory=list)
    related_rule_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CategoryBreakdownEntry:
    """Aggregated GPU time per business category (报告第二节)."""

    name: str = ""
    level: str = "level1"  # level1 | class
    draw_count: int = 0
    batch_count: int = 0
    gpu_duration_ms: float = 0.0
    percent: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EnhancedReportData:
    """Everything the report builder needs, assembled from sub-analyzers.

    Kept separate from the rendered Markdown so callers can also serialise it
    to JSON for downstream tooling.
    """

    capture_name: str = ""
    capture_path: str = ""
    # TL;DR / overview numbers (derived from analysis["overview"] + capture_info)
    total_gpu_duration_ms: float = 0.0
    estimated_fps: float = 0.0
    draw_count: int = 0
    unique_texture_count: int = 0
    unique_shader_count: int = 0

    category_breakdown_level1: List[CategoryBreakdownEntry] = field(default_factory=list)
    category_breakdown_class: List[CategoryBreakdownEntry] = field(default_factory=list)
    top_hotspots: List[Dict[str, Any]] = field(default_factory=list)
    shader_metrics: List[ShaderMaliMetrics] = field(default_factory=list)
    texture_audit: List[TextureAuditEntry] = field(default_factory=list)
    optimization_items: List[OptimizationItem] = field(default_factory=list)
    reference_eids: List[Dict[str, Any]] = field(default_factory=list)

    # Data-driven insight text (from InsightEngine), used to fill report
    # sections 三/六/七 and the TL;DR bottleneck line.
    bottleneck_summary: str = ""
    scene_content_notes: List[str] = field(default_factory=list)
    other_notes: List[str] = field(default_factory=list)
    hotspot_problems: Dict[str, str] = field(default_factory=dict)
    # Whether the replay backend provided pipeline-statistics counters.  When
    # False, the report hides the all-zero columns (PS 调用 / 覆盖率 / ...).
    counters_available: bool = True

    # Raw passthrough so the builder can still reach less-structured data.
    analysis: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "capture_name": self.capture_name,
            "capture_path": self.capture_path,
            "total_gpu_duration_ms": self.total_gpu_duration_ms,
            "estimated_fps": self.estimated_fps,
            "draw_count": self.draw_count,
            "unique_texture_count": self.unique_texture_count,
            "unique_shader_count": self.unique_shader_count,
            "category_breakdown_level1": [e.to_dict() for e in self.category_breakdown_level1],
            "category_breakdown_class": [e.to_dict() for e in self.category_breakdown_class],
            "top_hotspots": list(self.top_hotspots),
            "shader_metrics": [m.to_dict() for m in self.shader_metrics],
            "texture_audit": [t.to_dict() for t in self.texture_audit],
            "optimization_items": [o.to_dict() for o in self.optimization_items],
            "reference_eids": list(self.reference_eids),
            "bottleneck_summary": self.bottleneck_summary,
            "scene_content_notes": list(self.scene_content_notes),
            "other_notes": list(self.other_notes),
            "hotspot_problems": dict(self.hotspot_problems),
            "counters_available": self.counters_available,
        }
