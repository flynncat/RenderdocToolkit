"""Export a perf job's analysis JSON into flat CSV/TSV tables suitable for
TA / engine / art / QA downstream analysis.

The exporter is intentionally pure-Python (csv module only) so it is cheap to
run inside the perf service after every analysis.  It reads the in-memory
``analysis`` dict (same shape as ``perf_sessions/{job_id}/artifacts/perf_analysis.json``)
plus an optional ``findings`` list (from ``PerfRuleEngine``), and writes five
CSV files plus one TSV file under ``{out_dir}/``.

Design notes:
- All numeric columns keep raw precision (no rounding). Front-end formatting is
  a presentation concern and must not bleed into export.
- Lists / dicts inside row schema are flattened: ``breadcrumbs`` becomes a
  slash-joined string; nested ``texture_summary_items`` are exploded into a
  separate long-form table.
- Long tables use composite keys (``capture_id, eid[, slot]``) so that data
  from multiple captures can be concatenated for cross-capture trend analysis
  in pandas/Excel without further processing.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional


# Column orderings are explicit so analyst-side scripts can rely on a stable
# schema across versions.  When adding columns, append to the end of the list.
OVERVIEW_COLUMNS = [
    "capture_id",
    "capture_name",
    "capture_path",
    "driver_name",
    "analysis_mode",
    "draw_count",
    "total_gpu_duration_ms",
    "total_triangles",
    "total_vertices_read",
    "total_instruction_count",
    "total_stable_sort_score",
    "total_texture_mb",
    "finding_count_total",
    "finding_count_high",
    "finding_count_med",
    "finding_count_low",
    "report_md_relpath",
    "report_html_relpath",
]


DRAW_COLUMNS = [
    "capture_id",
    "capture_name",
    "eid",
    "scene_pass",
    "pass_name",
    "breadcrumbs_path",
    "draw_type",
    "instances",
    "triangles",
    "vertices_read",
    "input_primitives",
    "gpu_duration_ms",
    "vs_invocations",
    "ps_invocations",
    "samples_passed",
    "vs_instruction_count",
    "ps_instruction_count",
    "instruction_total",
    "target_width",
    "target_height",
    "target_samples",
    "screen_coverage_percent",
    "coverage_pixels_estimate",
    "instruction_coverage_score",
    "stable_sort_score",
    "stable_sort_basis",
    "texture_count",
    "texture_total_bytes",
    "texture_total_mb",
    "texture_bandwidth_risk",
    "texture_summary_text",
    "shader_id_vs",
    "shader_id_ps",
    "draw_preview_kind",
    "draw_preview_url",
]


PASS_COLUMNS = [
    "capture_id",
    "capture_name",
    "scene_pass",
    "draw_count",
    "gpu_duration_ms",
    "triangles",
    "ps_invocations",
    "vs_invocations",
    "instruction_total",
    "texture_total_mb",
    "percent_of_frame",
]


TEXTURE_COLUMNS = [
    "capture_id",
    "capture_name",
    "eid",
    "scene_pass",
    "pass_name",
    "slot",
    "resource_id",
    "width",
    "height",
    "format",
    "format_full",
    "byte_size",
    "byte_size_mb",
    "data_buffer_id",
    "data_width",
    "data_height",
]


SHADER_COLUMNS = [
    "capture_id",
    "capture_name",
    "shader_id",
    "stage",
    "instruction_count",
    "used_by_eid_count",
    "total_ps_invocations",
    "total_vs_invocations",
    "total_alu_pressure",
    "used_by_eids",
]


FINDING_COLUMNS = [
    "capture_id",
    "capture_name",
    "rule_id",
    "category",
    "severity",
    "scope",
    "affected_eids",
    "affected_count",
    "title",
    "expected_gain_text",
    "expected_gain_ms",
    "report_anchor",
]


class PerfExporter:
    """Write perf job analysis to CSV/TSV files."""

    def write_all(
        self,
        analysis: Mapping[str, Any],
        out_dir: Path,
        *,
        findings: Optional[List[Mapping[str, Any]]] = None,
        capture_id: str = "",
        report_md_relpath: str = "",
        report_html_relpath: str = "",
    ) -> Dict[str, Path]:
        """Write all CSV/TSV tables to ``out_dir`` and return their paths.

        ``capture_id`` defaults to the capture filename stem so multi-capture
        joins work without extra plumbing.
        """
        out_dir.mkdir(parents=True, exist_ok=True)
        capture_name = str(analysis.get("capture_name") or "").strip()
        capture_path = str(analysis.get("capture_path") or "").strip()
        if not capture_id:
            capture_id = Path(capture_name).stem or capture_name or "unknown_capture"

        rows = list(analysis.get("rows") or [])
        pass_chart = list(analysis.get("pass_chart") or [])
        overview = dict(analysis.get("overview") or {})
        capture_info = dict(analysis.get("capture_info") or {})
        findings = list(findings or [])

        paths: Dict[str, Path] = {}
        paths["overview"] = self._write_overview(
            out_dir / "overview.csv",
            analysis=analysis,
            overview=overview,
            capture_info=capture_info,
            capture_id=capture_id,
            capture_name=capture_name,
            capture_path=capture_path,
            findings=findings,
            report_md_relpath=report_md_relpath,
            report_html_relpath=report_html_relpath,
        )
        paths["draws"] = self._write_draws(
            out_dir / "draws.csv", rows, capture_id=capture_id, capture_name=capture_name,
            delimiter=",",
        )
        paths["draws_tsv"] = self._write_draws(
            out_dir / "draws.tsv", rows, capture_id=capture_id, capture_name=capture_name,
            delimiter="\t",
        )
        paths["passes"] = self._write_passes(
            out_dir / "passes.csv", rows, pass_chart, capture_id=capture_id, capture_name=capture_name,
        )
        paths["textures"] = self._write_textures(
            out_dir / "textures.csv", rows, capture_id=capture_id, capture_name=capture_name,
        )
        paths["shaders"] = self._write_shaders(
            out_dir / "shaders.csv", rows, capture_id=capture_id, capture_name=capture_name,
        )
        paths["findings"] = self._write_findings(
            out_dir / "findings.csv", findings, capture_id=capture_id, capture_name=capture_name,
        )
        return paths

    def _write_overview(
        self,
        path: Path,
        *,
        analysis: Mapping[str, Any],
        overview: Mapping[str, Any],
        capture_info: Mapping[str, Any],
        capture_id: str,
        capture_name: str,
        capture_path: str,
        findings: List[Mapping[str, Any]],
        report_md_relpath: str,
        report_html_relpath: str,
    ) -> Path:
        severity_counts = _count_by_severity(findings)
        analysis_mode = ""
        features = analysis.get("analysis_features") or {}
        if isinstance(features, Mapping):
            analysis_mode = str(features.get("analysis_mode") or "")
        if not analysis_mode:
            analysis_mode = "xml_fallback" if features else "direct_replay"
        row = {
            "capture_id": capture_id,
            "capture_name": capture_name,
            "capture_path": capture_path,
            "driver_name": str(capture_info.get("driver_name") or ""),
            "analysis_mode": analysis_mode,
            "draw_count": int(overview.get("draw_count") or 0),
            "total_gpu_duration_ms": float(overview.get("total_gpu_duration_ms") or 0.0),
            "total_triangles": int(overview.get("total_triangles") or 0),
            "total_vertices_read": int(overview.get("total_vertices_read") or 0),
            "total_instruction_count": int(overview.get("total_instruction_count") or 0),
            "total_stable_sort_score": float(overview.get("total_stable_sort_score") or 0.0),
            "total_texture_mb": float(overview.get("total_texture_mb") or 0.0),
            "finding_count_total": len(findings),
            "finding_count_high": severity_counts["high"],
            "finding_count_med": severity_counts["med"],
            "finding_count_low": severity_counts["low"],
            "report_md_relpath": report_md_relpath,
            "report_html_relpath": report_html_relpath,
        }
        _write_csv(path, OVERVIEW_COLUMNS, [row], delimiter=",")
        return path

    def _write_draws(
        self,
        path: Path,
        rows: Iterable[Mapping[str, Any]],
        *,
        capture_id: str,
        capture_name: str,
        delimiter: str = ",",
    ) -> Path:
        flattened: List[Dict[str, Any]] = []
        for row in rows:
            shader_ids = row.get("shader_ids") or {}
            if not isinstance(shader_ids, Mapping):
                shader_ids = {}
            breadcrumbs = row.get("breadcrumbs") or []
            if isinstance(breadcrumbs, list):
                breadcrumbs_path = "/".join(str(item) for item in breadcrumbs if item)
            else:
                breadcrumbs_path = str(breadcrumbs)
            flattened.append({
                "capture_id": capture_id,
                "capture_name": capture_name,
                "eid": _stringify(row.get("eid")),
                "scene_pass": _stringify(row.get("scene_pass")),
                "pass_name": _stringify(row.get("pass_name")),
                "breadcrumbs_path": breadcrumbs_path,
                "draw_type": _stringify(row.get("draw_type")),
                "instances": int(row.get("instances") or 0),
                "triangles": int(row.get("triangles") or 0),
                "vertices_read": int(row.get("vertices_read") or 0),
                "input_primitives": int(row.get("input_primitives") or 0),
                "gpu_duration_ms": float(row.get("gpu_duration_ms") or 0.0),
                "vs_invocations": int(row.get("vs_invocations") or 0),
                "ps_invocations": int(row.get("ps_invocations") or 0),
                "samples_passed": int(row.get("samples_passed") or 0),
                "vs_instruction_count": int(row.get("vs_instruction_count") or 0),
                "ps_instruction_count": int(row.get("ps_instruction_count") or 0),
                "instruction_total": int(row.get("instruction_total") or 0),
                "target_width": int(row.get("target_width") or 0),
                "target_height": int(row.get("target_height") or 0),
                "target_samples": int(row.get("target_samples") or 1),
                "screen_coverage_percent": float(row.get("screen_coverage_percent") or 0.0),
                "coverage_pixels_estimate": int(row.get("coverage_pixels_estimate") or 0),
                "instruction_coverage_score": float(row.get("instruction_coverage_score") or 0.0),
                "stable_sort_score": float(row.get("stable_sort_score") or 0.0),
                "stable_sort_basis": _stringify(row.get("stable_sort_basis")),
                "texture_count": int(row.get("texture_count") or 0),
                "texture_total_bytes": int(row.get("texture_total_bytes") or 0),
                "texture_total_mb": float(row.get("texture_total_mb") or 0.0),
                "texture_bandwidth_risk": float(row.get("texture_bandwidth_risk") or 0.0),
                "texture_summary_text": _stringify(row.get("texture_summary_text")),
                "shader_id_vs": _stringify(shader_ids.get("vs") or shader_ids.get("program")),
                "shader_id_ps": _stringify(shader_ids.get("ps")),
                "draw_preview_kind": _stringify(row.get("draw_preview_kind")),
                "draw_preview_url": _stringify(row.get("draw_preview_url")),
            })
        _write_csv(path, DRAW_COLUMNS, flattened, delimiter=delimiter)
        return path

    def _write_passes(
        self,
        path: Path,
        rows: Iterable[Mapping[str, Any]],
        pass_chart: Iterable[Mapping[str, Any]],
        *,
        capture_id: str,
        capture_name: str,
    ) -> Path:
        chart_lookup = {
            _stringify(item.get("name")): item for item in pass_chart if isinstance(item, Mapping)
        }
        agg: Dict[str, Dict[str, float]] = defaultdict(lambda: {
            "draw_count": 0.0,
            "gpu_duration_ms": 0.0,
            "triangles": 0.0,
            "ps_invocations": 0.0,
            "vs_invocations": 0.0,
            "instruction_total": 0.0,
            "texture_total_mb": 0.0,
        })
        for row in rows:
            name = _stringify(row.get("scene_pass")) or "Other"
            bucket = agg[name]
            bucket["draw_count"] += 1
            bucket["gpu_duration_ms"] += float(row.get("gpu_duration_ms") or 0.0)
            bucket["triangles"] += int(row.get("triangles") or 0)
            bucket["ps_invocations"] += int(row.get("ps_invocations") or 0)
            bucket["vs_invocations"] += int(row.get("vs_invocations") or 0)
            bucket["instruction_total"] += int(row.get("instruction_total") or 0)
            bucket["texture_total_mb"] += float(row.get("texture_total_mb") or 0.0)

        out_rows: List[Dict[str, Any]] = []
        for name, bucket in agg.items():
            chart_item = chart_lookup.get(name) or {}
            percent = float(chart_item.get("percent") or 0.0)
            out_rows.append({
                "capture_id": capture_id,
                "capture_name": capture_name,
                "scene_pass": name,
                "draw_count": int(bucket["draw_count"]),
                "gpu_duration_ms": bucket["gpu_duration_ms"],
                "triangles": int(bucket["triangles"]),
                "ps_invocations": int(bucket["ps_invocations"]),
                "vs_invocations": int(bucket["vs_invocations"]),
                "instruction_total": int(bucket["instruction_total"]),
                "texture_total_mb": bucket["texture_total_mb"],
                "percent_of_frame": percent,
            })
        out_rows.sort(key=lambda item: item["gpu_duration_ms"], reverse=True)
        _write_csv(path, PASS_COLUMNS, out_rows, delimiter=",")
        return path

    def _write_textures(
        self,
        path: Path,
        rows: Iterable[Mapping[str, Any]],
        *,
        capture_id: str,
        capture_name: str,
    ) -> Path:
        out_rows: List[Dict[str, Any]] = []
        for row in rows:
            eid = _stringify(row.get("eid"))
            scene_pass = _stringify(row.get("scene_pass"))
            pass_name = _stringify(row.get("pass_name"))
            items = row.get("texture_summary_items") or []
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                byte_size = int(item.get("byte_size") or item.get("estimated_bytes") or 0)
                out_rows.append({
                    "capture_id": capture_id,
                    "capture_name": capture_name,
                    "eid": eid,
                    "scene_pass": scene_pass,
                    "pass_name": pass_name,
                    "slot": int(item.get("slot") or 0),
                    "resource_id": _stringify(item.get("resource_id") or item.get("res_id")),
                    "width": int(item.get("width") or 0),
                    "height": int(item.get("height") or 0),
                    "format": _stringify(item.get("format")),
                    "format_full": _stringify(item.get("format_full") or item.get("format")),
                    "byte_size": byte_size,
                    "byte_size_mb": float(item.get("byte_size_mb") or byte_size / (1024.0 * 1024.0)),
                    "data_buffer_id": _stringify(item.get("data_buffer_id")),
                    "data_width": int(item.get("data_width") or 0),
                    "data_height": int(item.get("data_height") or 0),
                })
        _write_csv(path, TEXTURE_COLUMNS, out_rows, delimiter=",")
        return path

    def _write_shaders(
        self,
        path: Path,
        rows: Iterable[Mapping[str, Any]],
        *,
        capture_id: str,
        capture_name: str,
    ) -> Path:
        agg: Dict[tuple, Dict[str, Any]] = {}
        for row in rows:
            shader_ids = row.get("shader_ids") or {}
            if not isinstance(shader_ids, Mapping):
                continue
            eid = _stringify(row.get("eid"))
            for stage_key in ("vs", "ps", "program"):
                shader_id = _stringify(shader_ids.get(stage_key))
                if not shader_id or shader_id.endswith("::0"):
                    continue
                stage = "vs" if stage_key == "vs" else ("ps" if stage_key == "ps" else "program")
                key = (shader_id, stage)
                if key not in agg:
                    agg[key] = {
                        "shader_id": shader_id,
                        "stage": stage,
                        "instruction_count": 0,
                        "used_by_eid_count": 0,
                        "total_ps_invocations": 0,
                        "total_vs_invocations": 0,
                        "total_alu_pressure": 0,
                        "used_by_eids": set(),
                    }
                entry = agg[key]
                if stage == "ps":
                    entry["instruction_count"] = max(
                        entry["instruction_count"], int(row.get("ps_instruction_count") or 0),
                    )
                    ps_inv = int(row.get("ps_invocations") or 0)
                    entry["total_ps_invocations"] += ps_inv
                    entry["total_alu_pressure"] += ps_inv * int(row.get("ps_instruction_count") or 0)
                elif stage == "vs":
                    entry["instruction_count"] = max(
                        entry["instruction_count"], int(row.get("vs_instruction_count") or 0),
                    )
                    entry["total_vs_invocations"] += int(row.get("vs_invocations") or 0)
                if eid:
                    entry["used_by_eids"].add(eid)

        out_rows: List[Dict[str, Any]] = []
        for entry in agg.values():
            used_eids = sorted(entry["used_by_eids"], key=_eid_sort_key)
            out_rows.append({
                "capture_id": capture_id,
                "capture_name": capture_name,
                "shader_id": entry["shader_id"],
                "stage": entry["stage"],
                "instruction_count": entry["instruction_count"],
                "used_by_eid_count": len(used_eids),
                "total_ps_invocations": entry["total_ps_invocations"],
                "total_vs_invocations": entry["total_vs_invocations"],
                "total_alu_pressure": entry["total_alu_pressure"],
                "used_by_eids": ";".join(used_eids[:50]),
            })
        out_rows.sort(key=lambda item: item["total_alu_pressure"], reverse=True)
        _write_csv(path, SHADER_COLUMNS, out_rows, delimiter=",")
        return path

    def _write_findings(
        self,
        path: Path,
        findings: Iterable[Mapping[str, Any]],
        *,
        capture_id: str,
        capture_name: str,
    ) -> Path:
        out_rows: List[Dict[str, Any]] = []
        for finding in findings:
            if not isinstance(finding, Mapping):
                continue
            affected = finding.get("affected") or []
            eids = []
            if isinstance(affected, list):
                for item in affected:
                    if isinstance(item, Mapping):
                        eid = _stringify(item.get("eid"))
                        if eid:
                            eids.append(eid)
            expected_gain_ms = finding.get("expected_gain_ms")
            out_rows.append({
                "capture_id": capture_id,
                "capture_name": capture_name,
                "rule_id": _stringify(finding.get("rule_id")),
                "category": _stringify(finding.get("category")),
                "severity": _stringify(finding.get("severity")),
                "scope": _stringify(finding.get("scope")),
                "affected_eids": ";".join(eids[:50]),
                "affected_count": len(eids),
                "title": _stringify(finding.get("title")),
                "expected_gain_text": _stringify(finding.get("expected_gain_text")),
                "expected_gain_ms": "" if expected_gain_ms is None else float(expected_gain_ms),
                "report_anchor": _stringify(finding.get("report_anchor")),
            })
        _write_csv(path, FINDING_COLUMNS, out_rows, delimiter=",")
        return path


def _count_by_severity(findings: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    counts = {"high": 0, "med": 0, "low": 0}
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        sev = _stringify(finding.get("severity")).lower()
        if sev in counts:
            counts[sev] += 1
    return counts


def _write_csv(
    path: Path,
    columns: List[str],
    rows: List[Mapping[str, Any]],
    *,
    delimiter: str = ",",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # ``utf-8-sig`` so Excel double-click opens with proper Chinese encoding;
    # this is harmless to pandas/awk consumers because the BOM is treated as
    # whitespace by csv readers that auto-detect encoding.
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=columns, delimiter=delimiter)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: _csv_value(row.get(col, "")) for col in columns})


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _eid_sort_key(eid: str) -> tuple:
    try:
        return (0, int(eid))
    except (TypeError, ValueError):
        return (1, eid)
