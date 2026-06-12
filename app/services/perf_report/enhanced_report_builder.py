"""Enhanced performance report builder.

Orchestrates the sub-analyzers in this package and renders a Markdown report
structured like the reference report ``DefaultPosition_性能分析报告.md``:

  一、核心结论 (TL;DR)
  二、按一级/二级分类的耗时分布
  三、Top 10 热点 Drawcall
  四、Shader Mali 编译器分析
  五、纹理/带宽问题
  六、场景内容问题
  七、其他可优化点
  八、优化优先级建议
  九、参考热点 EID
  附：分析所用工具

It consumes the *existing* ``perf_analysis.json`` (written by
``app/services/renderdoc_perf_service.py``) and, when present, the sibling
``findings.json``.  It does NOT modify any existing module.

SKELETON STATUS
---------------
The orchestration + Markdown layout are complete and runnable on a real
``perf_analysis.json``.  Sections that depend on data not yet persisted
(Mali shader source, friendly texture names, real-device timings) render
explicit "待接入" placeholders rather than fake data.

TODO(integration): wire this builder into the perf pipeline so an enhanced
report is produced automatically.  The natural hook is
``RenderdocPerfService._generate_report_artifacts`` (write
``artifacts/perf_report_enhanced.md``) plus a download route in
``app/main.py`` mirroring ``GET /api/renderdoc-perf/jobs/{job_id}/report``.
Both touch existing files and are out of scope for this skeleton phase.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from app.services.perf_report.drawcall_classifier import DrawcallClassifier
from app.services.perf_report.models import (
    CategoryBreakdownEntry,
    EnhancedReportData,
    ShaderMaliMetrics,
)
from app.services.perf_report.insight_engine import InsightEngine
from app.services.perf_report.mali_shader_analyzer import MaliShaderAnalyzer
from app.services.perf_report.optimization_planner import OptimizationPlanner
from app.services.perf_report.texture_auditor import TextureAuditor


# Frame-time budget reference for the FPS verdict in the TL;DR.
_TARGET_FRAME_MS_30FPS = 1000.0 / 30.0


class EnhancedReportBuilder:
    def __init__(
        self,
        *,
        classifier: Optional[DrawcallClassifier] = None,
        mali_analyzer: Optional[MaliShaderAnalyzer] = None,
        texture_auditor: Optional[TextureAuditor] = None,
        planner: Optional[OptimizationPlanner] = None,
        insight_engine: Optional[InsightEngine] = None,
    ) -> None:
        self._classifier = classifier or DrawcallClassifier.from_config()
        self._mali = mali_analyzer or MaliShaderAnalyzer()
        self._texture_auditor = texture_auditor or TextureAuditor()
        self._planner = planner or OptimizationPlanner()
        self._insight = insight_engine or InsightEngine()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def build(
        self,
        analysis_json_path: str | Path,
        *,
        findings_json_path: Optional[str | Path] = None,
    ) -> str:
        """Build the enhanced report Markdown from a ``perf_analysis.json``."""
        data = self.assemble(analysis_json_path, findings_json_path=findings_json_path)
        return self.render_markdown(data)

    def build_from_analysis(
        self,
        analysis: Mapping[str, Any],
        findings: Optional[List[Mapping[str, Any]]] = None,
    ) -> str:
        """Build the Markdown directly from an in-memory analysis dict.

        Used by the perf pipeline so it can render the enhanced report without
        round-tripping through disk.
        """
        data = self.assemble_from_analysis(analysis, findings or [])
        return self.render_markdown(data)

    def build_html_from_analysis(
        self,
        analysis: Mapping[str, Any],
        findings: Optional[List[Mapping[str, Any]]] = None,
    ) -> str:
        data = self.assemble_from_analysis(analysis, findings or [])
        return self.render_html(data)

    def assemble(
        self,
        analysis_json_path: str | Path,
        *,
        findings_json_path: Optional[str | Path] = None,
    ) -> EnhancedReportData:
        analysis = self._load_json(analysis_json_path) or {}
        findings = self._load_findings(analysis_json_path, findings_json_path)
        return self.assemble_from_analysis(analysis, findings)

    def assemble_from_analysis(
        self,
        analysis: Mapping[str, Any],
        findings: List[Mapping[str, Any]],
    ) -> EnhancedReportData:
        analysis = dict(analysis or {})
        findings = list(findings or [])

        rows: List[Mapping[str, Any]] = list(analysis.get("rows") or [])
        overview = dict(analysis.get("overview") or {})
        pass_chart = list(analysis.get("pass_chart") or [])

        classifications = {c.eid: c for c in self._classifier.classify_rows(rows)}
        texture_audit = self._texture_auditor.audit(analysis)

        # Prefer the real Mali metrics the perf pipeline now persists in
        # ``analysis["shader_mali_metrics"]``.  Fall back to "unavailable"
        # placeholders (one per unique shader id) when they are absent, so the
        # report still renders the table shape and a clear note.
        shader_metrics = self._resolve_shader_metrics(analysis, rows)

        total_ms = float(overview.get("total_gpu_duration_ms") or 0.0)

        # Data-driven insight (works without pipeline-stat counters): this is
        # what fills the reference-style optimisation table + scene/other
        # sections, since the counter-based findings are mostly inert here.
        insight = self._insight.analyze(
            rows,
            shader_metrics=shader_metrics,
            classifications=classifications,
            texture_audit=texture_audit,
            total_gpu_ms=total_ms,
        )
        # Prefer the insight items; if it produced nothing, fall back to the
        # findings-driven planner so the section is never silently empty.
        optimization_items = insight.optimization_items or self._planner.plan(
            findings,
            shader_metrics=shader_metrics,
            pass_chart=pass_chart,
        )

        features = analysis.get("analysis_features") or {}
        counters_available = bool(features.get("counters_available", True))

        data = EnhancedReportData(
            capture_name=str(analysis.get("capture_name") or "未命名 capture"),
            capture_path=str(analysis.get("capture_path") or ""),
            total_gpu_duration_ms=total_ms,
            estimated_fps=(1000.0 / total_ms) if total_ms > 0 else 0.0,
            draw_count=int(overview.get("draw_count") or len(rows)),
            unique_texture_count=len(texture_audit),
            unique_shader_count=len(shader_metrics),
            category_breakdown_level1=self._aggregate_categories(
                rows, classifications, level="level1"
            ),
            category_breakdown_class=self._aggregate_categories(
                rows, classifications, level="class"
            ),
            top_hotspots=self._top_hotspots(rows, classifications),
            shader_metrics=shader_metrics,
            texture_audit=texture_audit,
            optimization_items=optimization_items,
            reference_eids=self._reference_eids(findings),
            bottleneck_summary=insight.bottleneck_summary,
            scene_content_notes=insight.scene_content_notes,
            other_notes=insight.other_notes,
            hotspot_problems=insight.hotspot_problems,
            counters_available=counters_available,
            analysis=dict(analysis),
        )
        return data

    # ------------------------------------------------------------------
    # Aggregation helpers
    # ------------------------------------------------------------------
    def _aggregate_categories(
        self,
        rows: List[Mapping[str, Any]],
        classifications: Dict[str, Any],
        *,
        level: str,
    ) -> List[CategoryBreakdownEntry]:
        bucket: Dict[str, CategoryBreakdownEntry] = defaultdict(
            lambda: CategoryBreakdownEntry(level=level)
        )
        total_ms = 0.0
        for row in rows:
            eid = str(row.get("eid") or "")
            cls = classifications.get(eid)
            if cls is None:
                key = "其他" if level == "level1" else "unknown"
            else:
                key = cls.level1 if level == "level1" else cls.class_id
            ms = float(row.get("gpu_duration_ms") or 0.0)
            total_ms += ms
            entry = bucket[key]
            entry.name = key
            entry.draw_count += 1
            entry.batch_count += 1
            entry.gpu_duration_ms += ms

        entries = list(bucket.values())
        denom = total_ms or 1.0
        for entry in entries:
            entry.gpu_duration_ms = round(entry.gpu_duration_ms, 3)
            entry.percent = round(entry.gpu_duration_ms / denom * 100.0, 1)
        entries.sort(key=lambda e: e.gpu_duration_ms, reverse=True)
        return entries

    def _top_hotspots(
        self,
        rows: List[Mapping[str, Any]],
        classifications: Dict[str, Any],
        *,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        ordered = sorted(
            rows,
            key=lambda r: (
                float(r.get("gpu_duration_ms") or 0.0),
                float(r.get("stable_sort_score") or 0.0),
            ),
            reverse=True,
        )
        hotspots: List[Dict[str, Any]] = []
        for row in ordered[:limit]:
            eid = str(row.get("eid") or "")
            cls = classifications.get(eid)
            hotspots.append(
                {
                    "eid": eid,
                    "gpu_duration_ms": round(float(row.get("gpu_duration_ms") or 0.0), 3),
                    "level1": getattr(cls, "level1", "其他"),
                    "class_id": getattr(cls, "class_id", "unknown"),
                    "pass_name": str(row.get("pass_name") or ""),
                    "triangles": int(row.get("triangles") or 0),
                    "instruction_total": int(row.get("instruction_total") or 0),
                    "ps_instruction_count": int(row.get("ps_instruction_count") or 0),
                    "ps_invocations": int(row.get("ps_invocations") or 0),
                    "screen_coverage_percent": float(row.get("screen_coverage_percent") or 0.0),
                }
            )
        return hotspots

    def _resolve_shader_metrics(
        self, analysis: Mapping[str, Any], rows: List[Mapping[str, Any]]
    ) -> List[ShaderMaliMetrics]:
        raw = analysis.get("shader_mali_metrics") or []
        metrics: List[ShaderMaliMetrics] = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            metrics.append(
                ShaderMaliMetrics(
                    shader_id=str(item.get("shader_id") or ""),
                    stage=str(item.get("stage") or ""),
                    work_registers=int(item.get("work_registers") or 0),
                    uniform_registers=int(item.get("uniform_registers") or 0),
                    alu_cycles=float(item.get("alu_cycles") or 0.0),
                    ls_cycles=float(item.get("ls_cycles") or 0.0),
                    varying_cycles=float(item.get("varying_cycles") or 0.0),
                    texture_cycles=float(item.get("texture_cycles") or 0.0),
                    arithmetic_16bit=float(item.get("arithmetic_16bit") or 0.0),
                    bound_unit=str(item.get("bound_unit") or ""),
                    register_spill=bool(item.get("register_spill")),
                    available=bool(item.get("available")),
                    note=str(item.get("note") or ""),
                )
            )
        if metrics:
            return metrics
        return self._collect_placeholder_shader_metrics(rows)

    def _collect_placeholder_shader_metrics(
        self, rows: List[Mapping[str, Any]]
    ) -> List[ShaderMaliMetrics]:
        seen: Dict[str, ShaderMaliMetrics] = {}
        for row in rows:
            shader_ids = row.get("shader_ids") or {}
            if not isinstance(shader_ids, dict):
                continue
            for stage, shader_id in shader_ids.items():
                sid = str(shader_id or "").strip()
                if not sid or sid.endswith("::0") or sid in seen:
                    continue
                # analyze("") -> available=False placeholder with note.
                seen[sid] = self._mali.analyze("", stage, shader_id=sid)
        return list(seen.values())

    def _reference_eids(self, findings: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        refs: List[Dict[str, Any]] = []
        for finding in findings or []:
            for entry in finding.get("affected") or []:
                eid = str(entry.get("eid") or "").strip()
                if eid:
                    refs.append(
                        {
                            "eid": eid,
                            "rule_id": str(finding.get("rule_id") or ""),
                            "title": str(finding.get("title") or ""),
                        }
                    )
        return refs

    # ------------------------------------------------------------------
    # Markdown rendering
    # ------------------------------------------------------------------
    def render_markdown(self, data: EnhancedReportData) -> str:
        lines: List[str] = []
        lines.append(f"# {data.capture_name} 性能回访分析报告")
        lines.append("")
        lines.append(f"> 数据来源: `{data.capture_path or '(未知路径)'}`")
        lines.append(
            "> 说明: 本报告基于现有桌面回放的 perf_analysis.json 生成。"
            "真机回放（adb + RenderDoc 远程，Mali 硬件计数器）数据更准确，"
            "属后续接入项。"
        )
        lines.append("")
        lines.append("---")
        lines.append("")

        lines.extend(self._section_tldr(data))
        lines.extend(self._section_category_breakdown(data))
        lines.extend(self._section_top_hotspots(data))
        lines.extend(self._section_mali(data))
        lines.extend(self._section_textures(data))
        lines.extend(self._section_scene_content(data))
        lines.extend(self._section_other(data))
        lines.extend(self._section_optimization(data))
        lines.extend(self._section_reference_eids(data))
        lines.extend(self._section_tools(data))

        return "\n".join(lines) + "\n"

    def render_html(self, data: EnhancedReportData) -> str:
        """Render a self-contained HTML document from the report Markdown."""
        md = self.render_markdown(data)
        try:
            import markdown as _markdown  # local import: optional dependency

            body = _markdown.markdown(
                md, extensions=["tables", "fenced_code"], output_format="html5"
            )
        except Exception:
            import html as _html

            body = f"<pre>{_html.escape(md)}</pre>"
        title = data.capture_name or "性能回访分析报告"
        return (
            "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"UTF-8\">\n"
            f"<title>增强性能报告 - {title}</title>\n"
            "<style>body{font-family:'Segoe UI',Arial,sans-serif;background:#0f1115;"
            "color:#e6e6e6;padding:16px 24px;line-height:1.55;}"
            "h1,h2,h3{color:#fff;}h1{border-bottom:1px solid #272b33;padding-bottom:8px;}"
            "table{border-collapse:collapse;margin:12px 0;}"
            "th,td{border:1px solid #272b33;padding:6px 10px;font-size:13px;text-align:left;}"
            "th{background:#171a20;}code{background:#171a20;padding:1px 4px;border-radius:3px;}"
            "blockquote{border-left:3px solid #2f81f7;background:#171a20;padding:8px 12px;"
            "color:#a7b0bf;}</style>\n</head>\n<body>\n"
            + body
            + "\n</body>\n</html>\n"
        )

    def _section_tldr(self, data: EnhancedReportData) -> List[str]:
        fps = data.estimated_fps
        verdict = "✅ 达标" if data.total_gpu_duration_ms <= _TARGET_FRAME_MS_30FPS else "⛔ 不达标 (目标 33ms / 30FPS)"
        lines = [
            "## 一、核心结论（TL;DR）",
            "",
            "| 指标 | 数据 | 评估 |",
            "|---|---|---|",
            f"| 整帧 GPU 时间 | {data.total_gpu_duration_ms:.2f} ms | ≈ {fps:.0f} FPS · {verdict} |",
            f"| Drawcall 总数 | {data.draw_count} | - |",
            f"| 唯一纹理 | {data.unique_texture_count} | - |",
            f"| 唯一 Shader | {data.unique_shader_count} | - |",
            "",
        ]
        if data.bottleneck_summary:
            lines.append(f"**瓶颈定位**：{data.bottleneck_summary}")
            lines.append("")
        if not data.counters_available:
            lines.append(
                "> 注：本次为桌面回放，GPU 管线统计计数器（PS 调用 / 覆盖率 / 顶点 / 图元）不可用，"
                "已自动改用 GPU 耗时、指令数、Mali 静态分析等真实有效数据进行展示、排序与诊断。"
            )
            lines.append("")
        return lines

    def _section_category_breakdown(self, data: EnhancedReportData) -> List[str]:
        lines = ["## 二、按一级分类的耗时分布", ""]
        if not data.category_breakdown_level1:
            lines.append("> 暂无分类数据。")
            lines.append("")
        else:
            lines.append("| 一级分类 | Draw 数 | GPU 耗时 (ms) | 占比 |")
            lines.append("|---|---:|---:|---:|")
            for e in data.category_breakdown_level1:
                lines.append(
                    f"| {e.name} | {e.draw_count} | {e.gpu_duration_ms:.2f} | {e.percent:.1f}% |"
                )
            lines.append("")
            lines.append("### 2.1 进一步细分（按 class）")
            lines.append("")
            lines.append("| 二级分类 | Draw 数 | GPU 耗时 (ms) | 占比 |")
            lines.append("|---|---:|---:|---:|")
            for e in data.category_breakdown_class:
                lines.append(
                    f"| `{e.name}` | {e.draw_count} | {e.gpu_duration_ms:.2f} | {e.percent:.1f}% |"
                )
            lines.append("")
            lines.append(
                "> 分类依据 `classifier_rules.json` 的业务关键词；命中质量取决于项目的 marker / mesh / material 命名。"
            )
            lines.append("")
        return lines

    def _section_top_hotspots(self, data: EnhancedReportData) -> List[str]:
        lines = ["## 三、Top 10 热点 Drawcall", ""]
        if not data.top_hotspots:
            lines.append("> 暂无热点数据。")
            lines.append("")
            return lines
        # Drop the all-zero counter columns (PS 调用 / 覆盖%) when the backend
        # didn't provide them; lead with the real signals instead.
        lines.append("| # | EID | 耗时 (ms) | 一级分类 | class | Marker | 三角面 | 总指令 | 关键问题 |")
        lines.append("|---:|---|---:|---|---|---|---:|---:|---|")
        for idx, h in enumerate(data.top_hotspots, start=1):
            eid = str(h.get("eid") or "")
            problem = data.hotspot_problems.get(eid, "-")
            lines.append(
                f"| {idx} | {eid} | {h['gpu_duration_ms']:.3f} | {h['level1']} | `{h['class_id']}` "
                f"| {_md_inline(h['pass_name'])} | {h['triangles']:,} | {int(h.get('instruction_total') or 0):,} "
                f"| {_md_inline(problem)} |"
            )
        lines.append("")
        return lines

    def _section_mali(self, data: EnhancedReportData) -> List[str]:
        lines = ["## 四、Shader Mali 编译器分析", ""]
        available = [m for m in data.shader_metrics if m.available]
        lines.append("| Shader | Stage | Work Reg | ALU | LS | Varying | Tex | Bound | 评估 |")
        lines.append("|---|---|---:|---:|---:|---:|---:|---|---|")
        if available:
            for m in available:
                verdict = "⛔ register spill" if m.register_spill else "-"
                lines.append(
                    f"| `{m.shader_id}` | {m.stage} | {m.work_registers} | {m.alu_cycles:.2f} "
                    f"| {m.ls_cycles:.2f} | {m.varying_cycles:.2f} | {m.texture_cycles:.2f} "
                    f"| {m.bound_unit or '-'} | {verdict} |"
                )
        else:
            lines.append(
                "| _(待接入)_ | - | - | - | - | - | - | - | 需 shader GLSL 源 + malioc |"
            )
        lines.append("")
        if available:
            lines.append(
                f"> 已用 Mali Offline Compiler (malioc) 对 {len(available)} 个具备 GLSL 源的 shader 做静态分析。"
                f"ALU/LS/Varying/Tex 为各单元周期数，Bound 为瓶颈单元；register spill 表示 Work Reg 过高需拆分。"
            )
        else:
            note = data.shader_metrics[0].note if data.shader_metrics else ""
            lines.append(
                f"> Mali 分析需要逐 shader 的 GLSL 源（从 replay 的 `DisassembleShader` 导出）"
                f"与 malioc 可用。当前: {note or 'shader 源待接入'}。"
            )
        lines.append("")
        return lines

    def _section_textures(self, data: EnhancedReportData) -> List[str]:
        lines = ["## 五、纹理/带宽问题", "", "### 5.1 大尺寸纹理", ""]
        large = [t for t in data.texture_audit if t.is_large]
        if not large:
            lines.append("> 未发现 >=2048 的大尺寸纹理（或纹理数据不可用）。")
            lines.append("")
        else:
            lines.append("| 纹理 | 分辨率 | 格式 | 大小 (MB) | 被引用 draw 数 | 建议 |")
            lines.append("|---|---|---|---:|---:|---|")
            for t in large[:20]:
                lines.append(
                    f"| `{_md_inline(t.name)}` | {t.width}x{t.height} | {t.format} "
                    f"| {t.byte_size_mb:.2f} | {t.used_by_draw_count} | {_md_inline(t.suggestion)} |"
                )
            lines.append("")
            lines.append(
                "> 纹理友好名称（如 T_FC_Skybox_Day）需 replay 的资源名映射，属后续接入项；当前以 resource_id 显示。"
            )
            lines.append("")
        return lines

    def _section_scene_content(self, data: EnhancedReportData) -> List[str]:
        lines = ["## 六、场景内容问题", ""]
        notes = list(data.scene_content_notes)
        if data.category_breakdown_class:
            top = data.category_breakdown_class[0]
            lines.append(
                f"- 当前最重业务 class 为 `{top.name}`，占帧 {top.percent:.1f}% "
                f"（{top.gpu_duration_ms:.2f} ms / {top.draw_count} draw）。"
            )
        for note in notes:
            lines.append(f"- {_md_inline(note)}")
        if not notes and not data.category_breakdown_class:
            lines.append("- 未发现明显的场景内容类问题。")
        lines.append("")
        return lines

    def _section_other(self, data: EnhancedReportData) -> List[str]:
        lines = ["## 七、其他可优化点", ""]
        if data.other_notes:
            for note in data.other_notes:
                lines.append(f"- {_md_inline(note)}")
        else:
            lines.append("- 未发现其他明显可优化点。")
        lines.append("")
        return lines

    def _section_optimization(self, data: EnhancedReportData) -> List[str]:
        lines = ["## 八、优化优先级建议", ""]
        if not data.optimization_items:
            lines.append("> 暂未触发优化项（findings 为空或阈值偏严）。")
            lines.append("")
            return lines
        lines.append("| 优先级 | 优化项 | 预期收益 (ms) | 改动量 | 关联 EID |")
        lines.append("|---|---|---|---|---|")
        for item in data.optimization_items:
            gain = (
                f"-{item.expected_gain_ms_low:.1f} 至 -{item.expected_gain_ms_high:.1f}"
                if item.expected_gain_ms_high > 0
                else "待估"
            )
            eids = "、".join(item.related_eids[:5]) or "-"
            lines.append(
                f"| {item.priority} | {_md_inline(item.title)} | {gain} | {item.effort} | {eids} |"
            )
        low, high = self._planner.total_expected_gain_ms(data.optimization_items)
        lines.append("")
        lines.append(f"> 总预期收益（粗估）: -{low:.1f} 至 -{high:.1f} ms。收益为基于实测 GPU 耗时的启发式估算，需实测校准。")
        lines.append("")
        # Detailed rationale per item (the "why").
        details = [item for item in data.optimization_items if item.rationale]
        if details:
            lines.append("### 8.1 详细依据")
            lines.append("")
            for item in details:
                lines.append(f"- **[{item.priority}] {_md_inline(item.title)}** — {_md_inline(item.rationale)}")
            lines.append("")
        return lines

    def _section_reference_eids(self, data: EnhancedReportData) -> List[str]:
        lines = ["## 九、参考热点 EID（用于复现 / 进一步定位）", ""]
        if not data.reference_eids:
            lines.append("> 无关联 EID。")
            lines.append("")
            return lines
        lines.append("| EID | 关联规则 | 说明 |")
        lines.append("|---|---|---|")
        seen = set()
        for ref in data.reference_eids:
            key = (ref["eid"], ref["rule_id"])
            if key in seen:
                continue
            seen.add(key)
            lines.append(
                f"| {ref['eid']} | `{ref['rule_id']}` | {_md_inline(ref['title'])} |"
            )
        lines.append("")
        return lines

    def _section_tools(self, data: EnhancedReportData) -> List[str]:
        mali_ok = "✅" if self._mali.is_available() else "❌ 未找到"
        return [
            "## 附：分析所用工具",
            "",
            "- RenderDoc 桌面回放（perf_analysis.json 来源）",
            f"- Mali Offline Compiler (malioc): {mali_ok}",
            "- drawcall 业务分类器（classifier_rules.json）",
            "- 真机回放（adb 远程，Mali 硬件计数器）: _(待接入，可显著提升数据准确性)_",
            "",
        ]

    # ------------------------------------------------------------------
    # IO helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _load_json(path: str | Path) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return None

    def _load_findings(
        self,
        analysis_json_path: str | Path,
        findings_json_path: Optional[str | Path],
    ) -> List[Dict[str, Any]]:
        if findings_json_path is not None:
            loaded = self._load_json(findings_json_path)
        else:
            sibling = Path(analysis_json_path).parent / "findings.json"
            loaded = self._load_json(sibling)
        if isinstance(loaded, list):
            return [item for item in loaded if isinstance(item, dict)]
        return []


def _md_inline(value: Any) -> str:
    if value is None:
        return "-"
    text = str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip() or "-"
