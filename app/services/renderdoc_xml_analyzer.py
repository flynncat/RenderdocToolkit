"""Fallback analyzer that extracts performance data from a RenderDoc XML
capture dump when the standard Python replay API is unavailable (e.g.
custom/older renderdoc builds with incompatible file format).

The XML is produced by ``renderdoccmd convert -c xml`` and contains the full
structured data of the capture — API calls, resources, state changes, and
marker labels — but *not* GPU hardware counters.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


def analyze_capture_xml(xml_path: str | Path) -> Dict[str, Any]:
    """Parse the XML dump and return an analysis dict compatible with the
    standard ``RenderdocPerfService`` output format.
    """
    xml_path = Path(xml_path)
    header = _parse_header(xml_path)
    events = _parse_events(xml_path)

    marker_stack: List[str] = []
    draw_rows: List[Dict[str, Any]] = []
    action_map: Dict[str, Dict[str, Any]] = {}

    for ev in events:
        eid = ev["eid"]
        name = ev["name"]

        if name in ("glPushGroupMarkerEXT", "glPushDebugGroup"):
            label = ev.get("label") or ev.get("params", {}).get("message") or name
            marker_stack.append(label)
            continue
        if name in ("glPopGroupMarkerEXT", "glPopDebugGroup"):
            if marker_stack:
                marker_stack.pop()
            continue

        if not _is_draw_call(name):
            continue

        breadcrumbs = list(marker_stack)
        pass_name = breadcrumbs[-1] if breadcrumbs else f"EID {eid}"
        scene_pass = _detect_scene_pass(breadcrumbs)
        triangles, instances = _extract_draw_counts(ev)

        draw_rows.append({
            "eid": str(eid),
            "pass_name": pass_name,
            "scene_pass": scene_pass or "Other",
            "selection_label": f"EID {eid} | {pass_name}",
            "breadcrumbs": breadcrumbs,
            "draw_type": _classify_draw(name),
            "instances": instances,
            "triangles": triangles,
            "vertices_read": 0,
            "input_primitives": 0,
            "gpu_duration_ms": 0.0,
            "vs_invocations": 0,
            "ps_invocations": 0,
            "samples_passed": 0,
            "vs_instruction_count": 0,
            "ps_instruction_count": 0,
            "instruction_total": 0,
            "target_width": 0,
            "target_height": 0,
            "target_samples": 1,
            "screen_coverage_percent": 0.0,
            "coverage_pixels_estimate": 0,
            "instruction_coverage_score": 0.0,
            "stable_sort_score": float(triangles),
            "stable_sort_basis": "triangles_fallback",
            "draw_preview_url": "",
            "draw_preview_kind": "unavailable",
            "texture_count": 0,
            "texture_total_bytes": 0,
            "texture_total_mb": 0.0,
            "texture_bandwidth_risk": 0.0,
            "texture_summary_items": [],
            "texture_summary_text": "",
            "shader_ids": {},
        })
        action_map[str(eid)] = {
            "pass_name": pass_name,
            "scene_pass": scene_pass,
            "breadcrumbs": breadcrumbs,
        }

    draw_rows.sort(key=lambda r: r["triangles"], reverse=True)

    overview = _build_overview(draw_rows)
    pass_chart = _build_pass_chart(draw_rows)
    warnings = [
        "当前 capture 使用非标准 RenderDoc 格式，已通过 XML 回退分析。"
        "GPU Duration、Shader 指令数、线框预览等依赖回放 API 的字段不可用，"
        "排序使用三角面数代替。",
    ]

    return {
        "capture_name": xml_path.stem,
        "capture_path": "",
        "capture_info": {
            "driver_name": header.get("driver", "Unknown"),
            "recorded_machine": header.get("machine_ident", ""),
            "timestamp_frequency": float(header.get("frequency", 0)),
            "timestamp_base": int(header.get("timebase", 0)),
        },
        "overview": overview,
        "warnings": warnings,
        "sort_fields": [
            {"id": "triangles", "label": "三角面数"},
            {"id": "instances", "label": "实例数"},
        ],
        "rows": draw_rows,
        "pass_chart": pass_chart,
        "hotspot_hints": _build_hotspot_hints(pass_chart, draw_rows),
    }


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------

def _parse_header(xml_path: Path) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for event, elem in ET.iterparse(str(xml_path), events=("end",)):
        tag = elem.tag
        if tag == "driver":
            result["driver"] = elem.get("id", "")
        elif tag == "machineIdent":
            result["machine_ident"] = (elem.text or "").strip()
        elif tag == "timebase":
            result["timebase"] = elem.get("base", "0")
            result["frequency"] = elem.get("frequency", "0")
        elif tag == "chunks":
            break
        elem.clear()
    return result


_DRAW_NAMES = {
    "glDrawElements", "glDrawElementsInstanced", "glDrawElementsBaseVertex",
    "glDrawElementsInstancedBaseVertex", "glDrawArrays", "glDrawArraysInstanced",
    "glDrawRangeElements", "glDrawRangeElementsBaseVertex",
    "vkCmdDraw", "vkCmdDrawIndexed", "vkCmdDrawIndirect",
    "vkCmdDrawIndexedIndirect", "vkCmdDrawIndirectCount",
    "DrawIndexed", "DrawInstanced", "DrawIndexedInstanced", "Draw",
}


def _is_draw_call(name: str) -> bool:
    return name in _DRAW_NAMES


def _parse_events(xml_path: Path) -> List[Dict[str, Any]]:
    """Parse XML chunks into a flat event list.

    Uses ``chunkIndex`` as the event ID (EID).  For marker and draw chunks
    we collect parameter values from child elements.
    """
    events: List[Dict[str, Any]] = []
    in_target = False
    chunk_attrs: Dict[str, str] = {}
    params: Dict[str, str] = {}
    label: str = ""

    _INTERESTING = _DRAW_NAMES | {
        "glPushGroupMarkerEXT", "glPopGroupMarkerEXT",
        "glPushDebugGroup", "glPopDebugGroup",
    }

    for ev_type, elem in ET.iterparse(str(xml_path), events=("start", "end")):
        if ev_type == "start" and elem.tag == "chunk":
            name = elem.get("name", "")
            if name in _INTERESTING:
                in_target = True
                chunk_attrs = dict(elem.attrib)
                params = {}
                label = ""
            continue

        if ev_type == "end" and elem.tag == "chunk":
            if in_target:
                eid = int(chunk_attrs.get("chunkIndex", "0"))
                name = chunk_attrs.get("name", "")
                events.append({
                    "eid": eid,
                    "name": name,
                    "params": params,
                    "label": label,
                })
                in_target = False
            elem.clear()
            continue

        if in_target and ev_type == "end" and elem.tag in (
            "uint", "int", "enum", "string", "float", "bool",
        ):
            pname = elem.get("name", "")
            text = (elem.text or "").strip()
            if pname:
                params[pname] = text
            if elem.tag == "string" and text:
                label = label or text
        elif ev_type == "end" and not in_target:
            elem.clear()

    return events


def _extract_draw_counts(ev: Dict[str, Any]) -> Tuple[int, int]:
    params = ev.get("params", {})
    count_val = 0
    for key in ("count", "indexCount", "vertexCount", "n"):
        val = params.get(key, "")
        if val:
            try:
                count_val = int(val)
            except ValueError:
                pass
            break
    instances = 1
    for key in ("instancecount", "instanceCount", "primcount"):
        val = params.get(key, "")
        if val:
            try:
                instances = max(int(val), 1)
            except ValueError:
                pass
            break
    triangles = max(count_val // 3, 0) * instances
    return triangles, instances


def _classify_draw(name: str) -> str:
    if "Instanced" in name:
        return "DrawInstanced"
    if "Indirect" in name:
        return "DrawIndirect"
    return "Draw"


_SCENE_PASS_MAP = {
    "shadowdepths": "ShadowDepths",
    "mobilerenderprepass": "MobileRenderPrePass",
    "mobilebasepass": "MobileBasePass",
    "translucency": "Translucency",
    "postprocessing": "PostProcessing",
}


def _detect_scene_pass(breadcrumbs: List[str]) -> str:
    for crumb in breadcrumbs:
        lower = crumb.lower()
        for key, value in _SCENE_PASS_MAP.items():
            if key in lower:
                return value
    return ""


# ---------------------------------------------------------------------------
# Aggregation (mirrors RenderdocPerfService helpers)
# ---------------------------------------------------------------------------

def _build_overview(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "draw_count": len(rows),
        "total_gpu_duration_ms": 0.0,
        "total_triangles": sum(int(r.get("triangles", 0)) for r in rows),
        "total_vertices_read": 0,
        "total_instruction_count": 0,
        "total_stable_sort_score": sum(float(r.get("stable_sort_score", 0)) for r in rows),
        "total_instruction_coverage_score": 0.0,
        "total_texture_mb": 0.0,
    }


def _build_pass_chart(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    bucket: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"name": "", "gpu_duration_ms": 0.0, "triangles": 0, "draw_count": 0}
    )
    for row in rows:
        name = row.get("scene_pass") or "Other"
        if name == "Other":
            continue
        item = bucket[name]
        item["name"] = name
        item["triangles"] += int(row.get("triangles", 0))
        item["draw_count"] += 1

    if not bucket:
        return []

    total_tris = max(sum(b["triangles"] for b in bucket.values()), 1)
    result = []
    for item in bucket.values():
        result.append({
            "name": item["name"],
            "gpu_duration_ms": 0.0,
            "triangles": item["triangles"],
            "draw_count": item["draw_count"],
            "percent": round(item["triangles"] / total_tris * 100, 2),
        })
    result.sort(key=lambda x: x["triangles"], reverse=True)
    return result


def _build_hotspot_hints(pass_chart: List[Dict[str, Any]], rows: List[Dict[str, Any]]) -> List[str]:
    hints: List[str] = [
        "⚠️ 当前为 XML 回退分析模式，GPU Duration 不可用，排序按三角面数。",
    ]
    if pass_chart:
        top = pass_chart[0]
        hints.append(f"三角面最多的 Pass 是 `{top['name']}`，占比 {top['percent']}%。")
    if rows:
        top_draw = rows[0]
        hints.append(f"三角面最多的 Draw 是 `EID {top_draw['eid']} | {top_draw['pass_name']}`，{top_draw['triangles']} 面。")
    return hints
