"""Enhanced performance-report package.

Adds the analysis layers the reference report needs on top of the existing
``perf_analysis.json`` produced by ``app/services/renderdoc_perf_service.py``:

- :class:`MaliShaderAnalyzer`   - per-shader Mali Offline Compiler metrics
- :class:`DrawcallClassifier`   - business-semantic (农作物/宠物/...) taxonomy
- :class:`TextureAuditor`       - frame-wide unique-texture audit
- :class:`OptimizationPlanner`  - P0-P3 优化优先级 + 预期收益
- :class:`EnhancedReportBuilder`- orchestrates the above into Markdown

This package is self-contained and does not modify any existing module.  See
``EnhancedReportBuilder`` for the (deferred) pipeline-integration TODOs.
"""
from __future__ import annotations

from app.services.perf_report.drawcall_classifier import DrawcallClassifier
from app.services.perf_report.enhanced_report_builder import EnhancedReportBuilder
from app.services.perf_report.insight_engine import InsightEngine, InsightResult
from app.services.perf_report.mali_shader_analyzer import MaliShaderAnalyzer
from app.services.perf_report.models import (
    BusinessCategory,
    CategoryBreakdownEntry,
    DrawClassification,
    EnhancedReportData,
    OptimizationItem,
    ShaderMaliMetrics,
    TextureAuditEntry,
)
from app.services.perf_report.optimization_planner import OptimizationPlanner
from app.services.perf_report.texture_auditor import TextureAuditor

__all__ = [
    "BusinessCategory",
    "CategoryBreakdownEntry",
    "DrawClassification",
    "DrawcallClassifier",
    "EnhancedReportBuilder",
    "EnhancedReportData",
    "InsightEngine",
    "InsightResult",
    "MaliShaderAnalyzer",
    "OptimizationItem",
    "OptimizationPlanner",
    "ShaderMaliMetrics",
    "TextureAuditEntry",
    "TextureAuditor",
]
