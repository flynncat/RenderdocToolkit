"""Build the human-readable performance report.

Output structure (pyramid):
1. Header + health badge + 5 key numbers
2. Executive summary: 1-line per finding, hyperlinked to the detailed section
3. Detailed findings: one per finding, with role-targeted suggestions
4. Raw data links: CSV / TSV attachments
5. Frame fingerprint: passes & hottest draws table for context

Each finding line carries TWO link types so users can jump bidirectionally:
- ``[EID 528 @ capture](#perf-row-528)`` -> highlights the row in the perf
  table (handled by ``app.js``)
- ``[详细 ->](#finding-...)`` -> jumps within the report panel itself

HTML rendering uses ``markdown`` (already added to requirements) with the
``tables`` + ``fenced_code`` extensions.  Anchor ``id`` attributes are injected
manually for finding sections because ``markdown.extensions.toc`` would mangle
the slug format we need.
"""
from __future__ import annotations

import html as html_module
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from app.services.perf_rule_engine import Finding


SEVERITY_LABEL = {
    "high": "高",
    "med": "中",
    "low": "低",
}

SEVERITY_ICON = {
    "high": "🔴",
    "med": "🟡",
    "low": "🟢",
}


@dataclass
class BuiltReport:
    md: str
    html: str

    def to_dict(self) -> Dict[str, str]:
        return {"md": self.md, "html": self.html}


class PerfReportBuilder:
    """Render a Markdown + HTML report from analysis + findings."""

    def build(
        self,
        analysis: Mapping[str, Any],
        findings: Sequence[Finding | Mapping[str, Any]],
        *,
        capture_name: str = "",
        job_id: str = "",
        exports_relpath: str = "exports",
    ) -> BuiltReport:
        capture_name = capture_name or str(analysis.get("capture_name") or "未命名 capture")
        capture_path = str(analysis.get("capture_path") or "")
        overview = dict(analysis.get("overview") or {})
        capture_info = dict(analysis.get("capture_info") or {})
        pass_chart = list(analysis.get("pass_chart") or [])
        rows = list(analysis.get("rows") or [])

        normalized_findings: List[Dict[str, Any]] = [
            f.to_dict() if isinstance(f, Finding) else dict(f) for f in findings
        ]
        severity_counts = _count_by_severity(normalized_findings)
        generated_at = datetime.now().isoformat(timespec="seconds")

        md = self._build_markdown(
            capture_name=capture_name,
            capture_path=capture_path,
            job_id=job_id,
            generated_at=generated_at,
            overview=overview,
            capture_info=capture_info,
            findings=normalized_findings,
            severity_counts=severity_counts,
            pass_chart=pass_chart,
            rows=rows,
            exports_relpath=exports_relpath,
        )
        # Standalone HTML always embeds the full perf-results table so users
        # who open the downloaded file in a browser can click any EID anchor
        # and have it jump to the corresponding row in the same document.
        embedded_table_html = self._render_embedded_perf_table(rows)
        html = self._render_html(
            md,
            capture_name=capture_name,
            severity_counts=severity_counts,
            extra_body_html=embedded_table_html,
        )
        return BuiltReport(md=md, html=html)

    # -----------------------------------------------------------------------
    # Markdown
    # -----------------------------------------------------------------------

    def _build_markdown(
        self,
        *,
        capture_name: str,
        capture_path: str,
        job_id: str,
        generated_at: str,
        overview: Mapping[str, Any],
        capture_info: Mapping[str, Any],
        findings: List[Dict[str, Any]],
        severity_counts: Dict[str, int],
        pass_chart: List[Mapping[str, Any]],
        rows: List[Mapping[str, Any]],
        exports_relpath: str,
    ) -> str:
        lines: List[str] = []
        lines.append(f"# 性能分析报告 — {capture_name}")
        lines.append("")
        lines.append(f"> 抓帧文件: `{capture_path or '(未知路径)'}`")
        lines.append(f"> 任务 ID: `{job_id or '(unknown)'}` · 生成时间: `{generated_at}`")
        badge_parts = []
        for sev in ("high", "med", "low"):
            badge_parts.append(f"{SEVERITY_ICON[sev]} {SEVERITY_LABEL[sev]} {severity_counts.get(sev, 0)}")
        lines.append(f"> 健康卡: {' · '.join(badge_parts)}")
        lines.append("")

        # ---- Section 1: Executive summary ----
        lines.append("## 1. 摘要")
        lines.append("")
        lines.append("| 项目 | 数值 |")
        lines.append("|---|---|")
        lines.append(f"| 驱动 / 后端 | {capture_info.get('driver_name') or '-'} |")
        lines.append(f"| Draw 总数 | {int(overview.get('draw_count') or 0)} |")
        lines.append(f"| 帧总 GPU 耗时 | {float(overview.get('total_gpu_duration_ms') or 0.0):.3f} ms |")
        lines.append(f"| 总三角面数 | {int(overview.get('total_triangles') or 0):,} |")
        lines.append(f"| 总指令数 (估算) | {int(overview.get('total_instruction_count') or 0):,} |")
        lines.append(f"| 总绑定贴图 (含重复) | {float(overview.get('total_texture_mb') or 0.0):.2f} MB |")
        lines.append("")

        # ---- Section 2: Findings summary table ----
        lines.append("## 2. 风险卡片 (5 秒摘要)")
        lines.append("")
        if not findings:
            lines.append("> 暂未触发任何性能规则。可能是阈值偏严格，或者抓帧整体很健康。")
            lines.append("")
        else:
            lines.append("| # | 严重度 | 类别 | 一句话 | 受影响 |")
            lines.append("|---|---|---|---|---|")
            for idx, finding in enumerate(findings, start=1):
                sev = finding.get("severity", "low")
                category = finding.get("category", "")
                title = finding.get("title") or finding.get("rule_id", "")
                affected = finding.get("affected") or []
                anchor = finding.get("report_anchor") or ""
                one_line = self._one_line_summary(finding)
                affected_link = self._affected_link(affected, capture_name)
                detail_link = f"[详细 ->](#{anchor})" if anchor else ""
                lines.append(
                    f"| {idx} | {SEVERITY_ICON.get(sev, '')} {SEVERITY_LABEL.get(sev, sev)} "
                    f"| {_md_inline(category)} "
                    f"| {_md_inline(one_line)} "
                    f"| {affected_link} {detail_link} |"
                )
            lines.append("")

        # ---- Section 3: Detailed findings ----
        lines.append("## 3. 详细发现")
        lines.append("")
        if not findings:
            lines.append("_(无)_")
            lines.append("")
        else:
            for idx, finding in enumerate(findings, start=1):
                lines.extend(
                    self._finding_block(idx, finding, capture_name=capture_name)
                )
                lines.append("")

        # ---- Section 4: Raw data ----
        lines.append("## 4. 原始数据")
        lines.append("")
        rel = exports_relpath.rstrip("/")
        lines.append(
            f"- [overview.csv]({rel}/overview.csv) · "
            f"[draws.csv]({rel}/draws.csv) · "
            f"[passes.csv]({rel}/passes.csv) · "
            f"[textures.csv]({rel}/textures.csv) · "
            f"[shaders.csv]({rel}/shaders.csv) · "
            f"[findings.csv]({rel}/findings.csv)"
        )
        lines.append(f"- 剪贴板友好版 (TSV)：[draws.tsv]({rel}/draws.tsv)")
        lines.append("")

        # ---- Section 5: Frame fingerprint ----
        lines.append("## 5. 帧指纹 (供对照参考)")
        lines.append("")
        if pass_chart:
            lines.append("### 5.1 Pass 占比")
            lines.append("")
            lines.append("| Pass | GPU ms | Draw 数 | 三角面 | 占比 |")
            lines.append("|---|---|---|---|---|")
            for item in pass_chart:
                lines.append(
                    f"| {_md_inline(item.get('name'))} "
                    f"| {float(item.get('gpu_duration_ms') or 0.0):.3f} "
                    f"| {int(item.get('draw_count') or 0)} "
                    f"| {int(item.get('triangles') or 0):,} "
                    f"| {float(item.get('percent') or 0.0):.2f}% |"
                )
            lines.append("")

        if rows:
            for sub_idx, variant in enumerate(self._top_variants(rows), start=2):
                top = sorted(rows, key=variant["sort_key"], reverse=True)[:10]
                if not top:
                    continue
                lines.append(
                    f"### 5.{sub_idx} Top 10 最值得优化的 Draw (按 {variant['label']} 排序)"
                )
                lines.append("")
                lines.append(
                    "| EID | 渲染分类 | 来源 | Pass marker | GPU ms | PS 调用 | 三角面 | 贴图 MB | 覆盖% | 稳定得分 |"
                )
                lines.append("|---|---|---|---|---|---|---|---|---|---|")
                for row in top:
                    eid = _stringify(row.get("eid"))
                    eid_link = f"[EID {eid}](#perf-row-{eid})" if eid else "-"
                    decided_by = _stringify(row.get("scene_pass_decided_by")) or "-"
                    source_label = {
                        "marker": "marker",
                        "marker_raw": "marker(raw)",
                        "render_state": "状态推断",
                        "fallback": "未识别",
                        "-": "-",
                    }.get(decided_by, decided_by)
                    lines.append(
                        f"| {eid_link} "
                        f"| {_md_inline(row.get('scene_pass'))} "
                        f"| {source_label} "
                        f"| {_md_inline(row.get('pass_name'))} "
                        f"| {float(row.get('gpu_duration_ms') or 0.0):.3f} "
                        f"| {int(row.get('ps_invocations') or 0):,} "
                        f"| {int(row.get('triangles') or 0):,} "
                        f"| {float(row.get('texture_total_mb') or 0.0):.2f} "
                        f"| {float(row.get('screen_coverage_percent') or 0.0):.2f}% "
                        f"| {float(row.get('stable_sort_score') or 0.0):.2f} |"
                    )
                lines.append("")

        return "\n".join(lines) + "\n"

    def _finding_block(
        self,
        idx: int,
        finding: Dict[str, Any],
        *,
        capture_name: str,
    ) -> List[str]:
        rule_id = finding.get("rule_id", "")
        anchor = finding.get("report_anchor") or ""
        sev = finding.get("severity", "low")
        title = finding.get("title") or rule_id
        affected = finding.get("affected") or []
        evidence = finding.get("evidence") or {}

        block: List[str] = []
        if anchor:
            block.append(f'<a id="{anchor}"></a>')
        block.append(
            f"### 风险 #{idx} · {SEVERITY_ICON.get(sev, '')} {SEVERITY_LABEL.get(sev, sev)} · {title}"
        )
        block.append("")
        block.append(f"- **规则**: `{rule_id}` · **类别**: `{finding.get('category', '')}` · **作用域**: `{finding.get('scope', '')}`")
        block.append(f"- **定位**: {self._affected_link(affected, capture_name, max_n=8)}")

        if evidence:
            block.append("- **证据**:")
            for k, v in evidence.items():
                block.append(f"  - `{k}` = `{_format_value(v)}`")
        return block

    @staticmethod
    def _top_variants(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        """Build the list of Top-10 variants for section 5.

        Always returns at least 3 variants (PS invocations / triangles /
        texture MB).  The GPU-time variant is included as the leading variant
        when any row has a non-zero ``gpu_duration_ms``; otherwise it is
        replaced by a composite "估算开销" variant so XML-fallback captures
        still get a sensible default.
        """
        has_gpu_time = any(float(r.get("gpu_duration_ms") or 0.0) > 0.0 for r in rows)
        if has_gpu_time:
            primary = {
                "label": "GPU 耗时",
                "sort_key": lambda r: (
                    float(r.get("gpu_duration_ms") or 0.0),
                    int(r.get("ps_invocations") or 0),
                    float(r.get("stable_sort_score") or 0.0),
                ),
            }
        else:
            primary = {
                "label": "估算开销 (PS 工作量 + 带宽)",
                "sort_key": lambda r: (
                    int(r.get("ps_invocations") or 0)
                    * max(int(r.get("ps_instruction_count") or 0), 1)
                    + float(r.get("texture_total_mb") or 0.0)
                    * int(r.get("ps_invocations") or 0)
                    / 1_000_000.0,
                    float(r.get("stable_sort_score") or 0.0),
                ),
            }
        # Prefer PS instruction count for the second variant - it answers
        # "which draws are doing the most pixel work per pixel?" much more
        # directly than raw invocation counts.  We only fall back to PS
        # invocations when the disassembly-based instruction count is
        # entirely unavailable (e.g. very old captures or unsupported
        # backends), so this variant degrades gracefully instead of
        # disappearing.
        has_ps_instructions = any(int(r.get("ps_instruction_count") or 0) > 0 for r in rows)
        if has_ps_instructions:
            ps_variant = {
                "label": "PS 指令数",
                "sort_key": lambda r: (
                    int(r.get("ps_instruction_count") or 0),
                    int(r.get("ps_invocations") or 0),
                    float(r.get("stable_sort_score") or 0.0),
                ),
            }
        else:
            ps_variant = {
                "label": "PS 调用数",
                "sort_key": lambda r: (
                    int(r.get("ps_invocations") or 0),
                    int(r.get("ps_instruction_count") or 0),
                    float(r.get("stable_sort_score") or 0.0),
                ),
            }
        return [
            primary,
            ps_variant,
            {
                "label": "三角面数",
                "sort_key": lambda r: (
                    int(r.get("triangles") or 0),
                    int(r.get("vertices_read") or 0),
                    float(r.get("stable_sort_score") or 0.0),
                ),
            },
            {
                "label": "贴图 MB",
                "sort_key": lambda r: (
                    float(r.get("texture_total_mb") or 0.0),
                    int(r.get("texture_count") or 0),
                    float(r.get("stable_sort_score") or 0.0),
                ),
            },
        ]

    @staticmethod
    def _one_line_summary(finding: Mapping[str, Any]) -> str:
        evidence = finding.get("evidence") or {}
        rule_id = finding.get("rule_id", "")
        if rule_id == "R001_overdraw_heavy":
            return (
                f"EID {evidence.get('worst_eid', '?')} 像素调用 "
                f"{int(evidence.get('worst_ps_invocations') or 0):,}，"
                f"过绘制系数 ~{evidence.get('worst_fill_to_coverage_ratio', 0)}"
            )
        if rule_id == "R002_fullscreen_heavy_ps":
            return (
                f"EID {evidence.get('worst_eid', '?')} 全屏 "
                f"PS 指令 {int(evidence.get('worst_ps_instruction_count') or 0)}"
            )
        if rule_id == "R003_fullscreen_bandwidth":
            return (
                f"EID {evidence.get('worst_eid', '?')} 全屏绑定贴图 "
                f"{float(evidence.get('worst_texture_total_mb') or 0):.2f} MB / "
                f"{int(evidence.get('worst_texture_count') or 0)} 张"
            )
        if rule_id == "R004_translucency_overdraw":
            return (
                f"EID {evidence.get('worst_eid', '?')} 半透 PS 调用 "
                f"{int(evidence.get('worst_ps_invocations') or 0):,}"
            )
        if rule_id == "R005_shadow_pass_too_heavy":
            return f"Shadow pass 占帧 {float(evidence.get('pass_percent') or 0):.1f}%"
        if rule_id == "R006_post_processing_heavy":
            return f"后处理占帧 {float(evidence.get('pass_percent') or 0):.1f}%"
        if rule_id == "R007_shader_alu_outlier":
            return (
                f"EID {evidence.get('worst_eid', '?')} PS 指令 "
                f"{int(evidence.get('worst_ps_instruction_count') or 0)}, "
                f"调用 {int(evidence.get('worst_ps_invocations') or 0):,}"
            )
        if rule_id == "R008_huge_texture_low_use":
            return (
                f"EID {evidence.get('worst_eid', '?')} 贴图 "
                f"{float(evidence.get('worst_texture_total_mb') or 0):.2f} MB / 覆盖 "
                f"{float(evidence.get('worst_coverage_percent') or 0):.2f}%"
            )
        if rule_id == "R010_high_tri_low_pixel":
            return (
                f"EID {evidence.get('worst_eid', '?')} 三角面 "
                f"{int(evidence.get('worst_triangles') or 0):,} / 覆盖 "
                f"{float(evidence.get('worst_coverage_percent') or 0):.2f}%"
            )
        if rule_id == "R014_unique_texture_explosion":
            return (
                f"`{evidence.get('scene_pass', '')}` pass 唯一贴图 "
                f"{int(evidence.get('unique_texture_count') or 0)} 张"
            )
        return finding.get("title", rule_id)

    @staticmethod
    def _affected_link(
        affected: Iterable[Mapping[str, Any]],
        capture_name: str,
        *,
        max_n: int = 3,
    ) -> str:
        items = list(affected)[:max_n]
        parts = []
        for entry in items:
            eid = _stringify(entry.get("eid"))
            cap_name = _stringify(entry.get("capture_name")) or capture_name
            if eid:
                parts.append(f"[EID {eid} @ {cap_name}](#perf-row-{eid})")
        if not parts:
            return "_(未定位到 EID)_"
        suffix = ""
        if len(list(affected)) > max_n:
            suffix = f" 等 {len(list(affected))} 个"
        return "、".join(parts) + suffix

    # -----------------------------------------------------------------------
    # HTML
    # -----------------------------------------------------------------------

    def _render_html(
        self,
        markdown_text: str,
        *,
        capture_name: str,
        severity_counts: Dict[str, int],
        extra_body_html: str = "",
    ) -> str:
        body_html = _markdown_to_html(markdown_text)
        title_escaped = html_module.escape(capture_name)
        # Self-contained doc (we may serve as raw HTML).  Frontend currently
        # extracts the <body> innerHTML via fetch + DOMParser so the wrapper
        # styles only matter for direct-browser viewing.
        return (
            "<!DOCTYPE html>\n"
            "<html lang=\"zh-CN\">\n"
            "<head>\n"
            "<meta charset=\"UTF-8\">\n"
            f"<title>性能分析报告 - {title_escaped}</title>\n"
            "<style>\n"
            "body{font-family:'Segoe UI',Arial,sans-serif;background:#0f1115;color:#e6e6e6;"
            "padding:16px 24px;line-height:1.55;}\n"
            "h1,h2,h3,h4{color:#fff;margin-top:1.4em;}\n"
            "h1{border-bottom:1px solid #272b33;padding-bottom:8px;}\n"
            "blockquote{border-left:3px solid #2f81f7;background:#171a20;padding:8px 12px;"
            "margin:12px 0;color:#a7b0bf;}\n"
            "table{border-collapse:collapse;margin:12px 0;}\n"
            "th,td{border:1px solid #272b33;padding:6px 10px;text-align:left;font-size:13px;}\n"
            "th{background:#171a20;}\n"
            "code{background:#171a20;padding:1px 4px;border-radius:3px;color:#a7b0bf;font-size:12.5px;}\n"
            "a{color:#2f81f7;text-decoration:none;}\n"
            "a:hover{text-decoration:underline;}\n"
            "ul{padding-left:1.4em;}\n"
            "#perf-results-section{margin-top:32px;}\n"
            "#perf-results-table{width:100%;border-collapse:collapse;font-size:12px;}\n"
            "#perf-results-table th{background:#171a20;position:sticky;top:0;z-index:1;}\n"
            "#perf-results-table td{vertical-align:top;}\n"
            "#perf-results-table tr:target>td{background:rgba(255,215,0,0.18);"
            "outline:1px solid rgba(255,215,0,0.65);}\n"
            "#perf-results-table tr.zebra>td{background:#11141a;}\n"
            "#perf-results-wrap{max-height:70vh;overflow:auto;border:1px solid #272b33;"
            "border-radius:6px;}\n"
            "#perf-results-table td img{display:block;margin:0;}\n"
            "</style>\n"
            "</head>\n"
            "<body>\n"
            + body_html
            + (("\n" + extra_body_html) if extra_body_html else "")
            + "\n</body>\n</html>\n"
        )

    @staticmethod
    def _render_embedded_perf_table(rows: Sequence[Mapping[str, Any]]) -> str:
        """Render the full per-draw performance table as a self-contained
        HTML block.  Anchors emitted by the MD report (``#perf-row-{eid}``)
        land directly on these ``<tr>`` rows so the standalone report can be
        navigated without any JS.
        """
        if not rows:
            return ""
        # Default ordering mirrors the SPA's default sort by stable score so
        # the table is meaningful even before the user clicks an anchor.
        sorted_rows = sorted(
            rows,
            key=lambda r: float(r.get("stable_sort_score") or 0.0),
            reverse=True,
        )

        header_cells = (
            "EID", "线框预览", "渲染分类", "来源", "Pass marker", "稳定得分", "GPU ms",
            "三角面", "PS 调用", "PS 指令", "覆盖%", "贴图 MB", "贴图数",
        )
        head_html = "".join(f"<th>{html_module.escape(h)}</th>" for h in header_cells)

        source_label_map = {
            "marker": "marker",
            "marker_raw": "marker(raw)",
            "render_state": "状态推断",
            "fallback": "未识别",
        }

        body_lines: List[str] = []
        for idx, row in enumerate(sorted_rows):
            eid = _stringify(row.get("eid"))
            row_id = f"perf-row-{html_module.escape(eid)}" if eid else ""
            decided_by = _stringify(row.get("scene_pass_decided_by"))
            # Build the wireframe preview cell as raw HTML so it is exempt
            # from the escape loop below.  We prefer the dedicated overlay
            # URL, but fall back to the legacy ``draw_preview_url`` if it is
            # the only thing we have for this row.
            overlay_url = _stringify(row.get("draw_preview_overlay_url"))
            if not overlay_url and _stringify(row.get("draw_preview_kind")) in {
                "wireframe", "wireframe_overlay",
            }:
                overlay_url = _stringify(row.get("draw_preview_url"))
            if overlay_url:
                preview_html = (
                    f'<img src="{html_module.escape(overlay_url)}" '
                    f'alt="wireframe EID {html_module.escape(eid)}" '
                    f'loading="lazy" '
                    'style="max-width:160px;max-height:96px;background:#0a0c10;'
                    'border:1px solid #272b33;border-radius:3px;display:block;"/>'
                )
            else:
                preview_html = (
                    '<span style="color:#7d8696;font-size:11px;">未生成</span>'
                )
            text_cells = (
                eid or "-",
                _stringify(row.get("scene_pass")) or "-",
                source_label_map.get(decided_by, decided_by or "-"),
                _stringify(row.get("pass_name")) or "-",
                f"{float(row.get('stable_sort_score') or 0.0):.2f}",
                f"{float(row.get('gpu_duration_ms') or 0.0):.3f}",
                f"{int(row.get('triangles') or 0):,}",
                f"{int(row.get('ps_invocations') or 0):,}",
                f"{int(row.get('ps_instruction_count') or 0):,}",
                f"{float(row.get('screen_coverage_percent') or 0.0):.2f}%",
                f"{float(row.get('texture_total_mb') or 0.0):.3f}",
                f"{int(row.get('texture_count') or 0)}",
            )
            # First cell is the EID (escaped); second cell is the preview
            # (raw HTML, not escaped); the remainder are escaped text.
            eid_td = f"<td>{html_module.escape(text_cells[0])}</td>"
            preview_td = f"<td>{preview_html}</td>"
            rest_tds = "".join(f"<td>{html_module.escape(str(c))}</td>" for c in text_cells[1:])
            tds = eid_td + preview_td + rest_tds
            row_classes = "zebra" if idx % 2 == 1 else ""
            attrs = []
            if row_id:
                attrs.append(f'id="{row_id}"')
            if row_classes:
                attrs.append(f'class="{row_classes}"')
            attr_str = (" " + " ".join(attrs)) if attrs else ""
            body_lines.append(f"<tr{attr_str}>{tds}</tr>")

        body_html = "\n".join(body_lines)
        return (
            '<section id="perf-results-section">\n'
            "<h2>6. 完整性能结果</h2>\n"
            f"<p style=\"color:#a7b0bf;font-size:13px\">共 {len(sorted_rows)} 个 draw，"
            "按 <code>stable_sort_score</code> 降序排列。"
            "点击诊断小节中的 EID 链接可以直接跳到对应行。</p>\n"
            '<div id="perf-results-wrap">\n'
            '<table id="perf-results-table">\n'
            f"<thead><tr>{head_html}</tr></thead>\n"
            f"<tbody>\n{body_html}\n</tbody>\n"
            "</table>\n"
            "</div>\n"
            "</section>\n"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _markdown_to_html(text: str) -> str:
    """Render markdown -> HTML.  Falls back to a minimal escape if the
    ``markdown`` library is unavailable so the report never blocks the perf
    pipeline."""
    try:
        import markdown as _markdown  # noqa: WPS433 (local import is intentional)
    except Exception:
        return f"<pre>{html_module.escape(text)}</pre>"
    return _markdown.markdown(
        text,
        extensions=["tables", "fenced_code"],
        output_format="html5",
    )


def _count_by_severity(findings: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    counts = {"high": 0, "med": 0, "low": 0}
    for finding in findings:
        sev = str(finding.get("severity") or "").lower()
        if sev in counts:
            counts[sev] += 1
    return counts


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _format_value(value: Any) -> str:
    if isinstance(value, float):
        if abs(value) >= 1000:
            return f"{value:,.3f}"
        return f"{value:.4f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _md_inline(value: Any) -> str:
    """Escape characters that would break Markdown table cells."""
    if value is None:
        return "-"
    text = str(value)
    return (
        text.replace("|", "\\|").replace("\n", " ").strip()
        or "-"
    )
