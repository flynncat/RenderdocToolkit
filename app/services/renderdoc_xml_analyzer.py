"""Fallback analyzer that extracts performance data from a RenderDoc XML
capture dump when the standard Python replay API is unavailable (e.g.
custom/older renderdoc builds with incompatible file format).

The XML is produced by ``renderdoccmd convert -c xml`` and contains the full
structured data of the capture — API calls, resources, state changes, and
marker labels — but *not* GPU hardware counters.

This module performs a single streaming pass over the XML to:
- Build a texture catalog (dimensions, format, estimated size)
- Track GL state (bound textures per unit, active program, viewport)
- Extract draw calls and marker hierarchy
- Annotate each draw with its bound texture summary
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_capture_xml(xml_path: str | Path) -> Dict[str, Any]:
    """Parse the XML dump and return an analysis dict compatible with the
    standard ``RenderdocPerfService`` output format.
    """
    xml_path = Path(xml_path)
    header = _parse_header(xml_path)
    ctx = _parse_capture(xml_path)

    draw_rows = ctx.draw_rows
    draw_rows.sort(key=lambda r: r["triangles"], reverse=True)

    overview = _build_overview(draw_rows)
    pass_chart = _build_pass_chart(draw_rows)

    unavailable = []
    if not any(r["gpu_duration_ms"] for r in draw_rows):
        unavailable.append("GPU Duration")
    unavailable.extend(["Shader 指令数", "线框预览"])

    warnings = [
        "当前 capture 使用非标准 RenderDoc 格式，已通过 XML 回退分析。"
        f"{', '.join(unavailable)} 等依赖回放 API 的字段不可用，"
        "排序使用三角面数代替。",
    ]

    sort_fields = [
        {"id": "triangles", "label": "三角面数"},
        {"id": "instances", "label": "实例数"},
        {"id": "texture_total_mb", "label": "贴图总量 (MB)"},
        {"id": "texture_count", "label": "贴图数量"},
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
        "sort_fields": sort_fields,
        "rows": draw_rows,
        "pass_chart": pass_chart,
        "hotspot_hints": _build_hotspot_hints(pass_chart, draw_rows),
    }


# ---------------------------------------------------------------------------
# Texture format helpers
# ---------------------------------------------------------------------------

_GL_FORMAT_BPP: Dict[str, float] = {
    "GL_RGBA8": 4, "GL_RGBA": 4, "GL_RGBA16F": 8, "GL_RGBA32F": 16,
    "GL_RGB8": 3, "GL_RGB": 3, "GL_RGB16F": 6, "GL_RGB32F": 12,
    "GL_RG8": 2, "GL_RG16F": 4, "GL_RG32F": 8,
    "GL_R8": 1, "GL_R16F": 2, "GL_R32F": 4, "GL_R16": 2, "GL_R32I": 4,
    "GL_DEPTH_COMPONENT16": 2, "GL_DEPTH_COMPONENT24": 3,
    "GL_DEPTH_COMPONENT32F": 4, "GL_DEPTH24_STENCIL8": 4,
    "GL_DEPTH32F_STENCIL8": 5,
    "GL_SRGB8_ALPHA8": 4, "GL_SRGB8": 3,
    "GL_RGBA4": 2, "GL_RGB5_A1": 2, "GL_RGB565": 2, "GL_RGB10_A2": 4,
    "GL_R11F_G11F_B10F": 4,
    "GL_COMPRESSED_RGBA_ASTC_4x4": 1.0,
    "GL_COMPRESSED_RGBA_ASTC_6x6": 0.45,
    "GL_COMPRESSED_RGBA_ASTC_8x8": 0.25,
    "GL_COMPRESSED_SRGB8_ALPHA8_ASTC_4x4": 1.0,
    "GL_COMPRESSED_SRGB8_ALPHA8_ASTC_6x6": 0.45,
    "GL_COMPRESSED_SRGB8_ALPHA8_ASTC_8x8": 0.25,
    "GL_COMPRESSED_RGB_S3TC_DXT1_EXT": 0.5,
    "GL_COMPRESSED_RGBA_S3TC_DXT5_EXT": 1.0,
    "GL_COMPRESSED_RED_RGTC1": 0.5,
    "GL_COMPRESSED_RG_RGTC2": 1.0,
    "GL_ETC1_RGB8_OES": 0.5,
    "GL_COMPRESSED_RGB8_ETC2": 0.5,
    "GL_COMPRESSED_RGBA8_ETC2_EAC": 1.0,
}


def _estimate_texture_bytes(width: int, height: int, fmt: str, levels: int) -> int:
    bpp = _GL_FORMAT_BPP.get(fmt, 4.0)
    total = 0.0
    w, h = float(width), float(height)
    for _ in range(max(levels, 1)):
        total += w * h * bpp
        w = max(w / 2, 1)
        h = max(h / 2, 1)
    return int(total)


def _format_short(fmt: str) -> str:
    return fmt.replace("GL_", "").replace("COMPRESSED_", "C/")


# ---------------------------------------------------------------------------
# Texture info dataclass
# ---------------------------------------------------------------------------

@dataclass
class _TexInfo:
    res_id: str = ""
    label: str = ""
    width: int = 0
    height: int = 0
    levels: int = 1
    fmt: str = ""
    target: str = ""
    estimated_bytes: int = 0


# ---------------------------------------------------------------------------
# GL state tracker used during single-pass parsing
# ---------------------------------------------------------------------------

@dataclass
class _GLState:
    active_unit: int = 0
    bound_textures: Dict[int, str] = field(default_factory=dict)
    current_program: str = ""
    viewport_w: int = 0
    viewport_h: int = 0


# ---------------------------------------------------------------------------
# Parse context — collects everything in a single pass
# ---------------------------------------------------------------------------

@dataclass
class _ParseContext:
    texture_catalog: Dict[str, _TexInfo] = field(default_factory=dict)
    label_map: Dict[str, str] = field(default_factory=dict)
    draw_rows: List[Dict[str, Any]] = field(default_factory=list)
    state: _GLState = field(default_factory=_GLState)


# ---------------------------------------------------------------------------
# Chunk name sets
# ---------------------------------------------------------------------------

_DRAW_NAMES = {
    "glDrawElements", "glDrawElementsInstanced", "glDrawElementsBaseVertex",
    "glDrawElementsInstancedBaseVertex", "glDrawArrays", "glDrawArraysInstanced",
    "glDrawRangeElements", "glDrawRangeElementsBaseVertex",
    "vkCmdDraw", "vkCmdDrawIndexed", "vkCmdDrawIndirect",
    "vkCmdDrawIndexedIndirect", "vkCmdDrawIndirectCount",
    "DrawIndexed", "DrawInstanced", "DrawIndexedInstanced", "Draw",
}

_MARKER_PUSH = {"glPushGroupMarkerEXT", "glPushDebugGroup"}
_MARKER_POP = {"glPopGroupMarkerEXT", "glPopDebugGroup"}

_TEX_STORAGE = {
    "glTexStorage2D", "glTexStorage3D",
    "glTexStorage2DMultisample", "glTexStorage3DMultisample",
    "glTexImage2D", "glTexImage3D",
    "glCompressedTexImage2D", "glCompressedTexImage3D",
}

_STATE_CHUNKS = (
    _TEX_STORAGE
    | {"glBindTexture", "glActiveTexture", "glGenTextures",
       "glUseProgram", "glViewport", "glLabelObjectEXT"}
)

_ALL_INTERESTING = _DRAW_NAMES | _MARKER_PUSH | _MARKER_POP | _STATE_CHUNKS

_SCENE_PASS_MAP = {
    "shadowdepths": "ShadowDepths",
    "mobilerenderprepass": "MobileRenderPrePass",
    "mobilebasepass": "MobileBasePass",
    "translucency": "Translucency",
    "postprocessing": "PostProcessing",
    "basepass": "BasePass",
    "prepass": "PrePass",
}

_PARAM_TAGS = {"uint", "int", "enum", "string", "float", "bool", "ResourceId"}


# ---------------------------------------------------------------------------
# Single-pass XML parser
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


def _parse_capture(xml_path: Path) -> _ParseContext:
    """Single streaming pass over the XML.

    Builds the texture catalog, tracks GL state, and emits draw rows with
    texture annotations — all in one pass for performance on large files.
    """
    ctx = _ParseContext()
    marker_stack: List[str] = []

    in_target = False
    chunk_name = ""
    chunk_attrs: Dict[str, str] = {}
    params: Dict[str, str] = {}
    label = ""

    for ev_type, elem in ET.iterparse(str(xml_path), events=("start", "end")):
        if ev_type == "start" and elem.tag == "chunk":
            name = elem.get("name", "")
            if name in _ALL_INTERESTING:
                in_target = True
                chunk_name = name
                chunk_attrs = dict(elem.attrib)
                params = {}
                label = ""
            continue

        if ev_type == "end" and elem.tag == "chunk":
            if in_target:
                _process_chunk(ctx, chunk_name, chunk_attrs, params, label,
                               marker_stack)
                in_target = False
            elem.clear()
            continue

        if in_target and ev_type == "end" and elem.tag in _PARAM_TAGS:
            pname = elem.get("name", "")
            text = (elem.text or "").strip()
            string_val = elem.get("string", "")
            if pname:
                params[pname] = string_val or text
            if elem.tag == "string" and text:
                label = label or text
        elif ev_type == "end" and not in_target:
            elem.clear()

    return ctx


def _process_chunk(
    ctx: _ParseContext,
    name: str,
    attrs: Dict[str, str],
    params: Dict[str, str],
    label: str,
    marker_stack: List[str],
) -> None:
    """Dispatch a parsed chunk to the appropriate handler."""
    eid = int(attrs.get("chunkIndex", "0"))

    # --- Markers ---
    if name in _MARKER_PUSH:
        msg = label or params.get("message") or name
        marker_stack.append(msg)
        return
    if name in _MARKER_POP:
        if marker_stack:
            marker_stack.pop()
        return

    # --- Texture catalog ---
    if name in _TEX_STORAGE:
        _handle_tex_storage(ctx, params)
        return

    if name == "glLabelObjectEXT":
        res_id = params.get("Resource", "")
        lbl = params.get("Label", "") or label
        if res_id and lbl:
            ctx.label_map[res_id] = lbl
            if res_id in ctx.texture_catalog:
                ctx.texture_catalog[res_id].label = lbl
        return

    # --- State tracking ---
    if name == "glActiveTexture":
        unit_str = params.get("texture", "")
        if unit_str.startswith("GL_TEXTURE"):
            try:
                ctx.state.active_unit = int(unit_str.replace("GL_TEXTURE", ""))
            except ValueError:
                pass
        elif unit_str.isdigit():
            ctx.state.active_unit = max(int(unit_str) - 33984, 0)
        return

    if name == "glBindTexture":
        res_id = params.get("texture", "")
        if res_id:
            ctx.state.bound_textures[ctx.state.active_unit] = res_id
        return

    if name == "glUseProgram":
        ctx.state.current_program = params.get("program", "")
        return

    if name == "glViewport":
        try:
            ctx.state.viewport_w = int(params.get("width", "0"))
            ctx.state.viewport_h = int(params.get("height", "0"))
        except ValueError:
            pass
        return

    if name == "glGenTextures":
        return

    # --- Draw calls ---
    if name in _DRAW_NAMES:
        _handle_draw(ctx, name, eid, params, marker_stack)


def _handle_tex_storage(ctx: _ParseContext, params: Dict[str, str]) -> None:
    res_id = params.get("texture", "")
    if not res_id:
        return
    fmt = params.get("internalformat", "")
    try:
        w = int(params.get("width", "0"))
        h = int(params.get("height", "0"))
        levels = max(int(params.get("levels", "1")), 1)
    except ValueError:
        w, h, levels = 0, 0, 1

    estimated = _estimate_texture_bytes(w, h, fmt, levels) if w and h else 0
    info = ctx.texture_catalog.get(res_id)
    if info is None:
        info = _TexInfo(res_id=res_id)
        ctx.texture_catalog[res_id] = info
    if w > info.width or h > info.height:
        info.width = w
        info.height = h
        info.fmt = fmt
        info.levels = levels
        info.target = params.get("target", "")
        info.estimated_bytes = estimated
    if res_id in ctx.label_map:
        info.label = ctx.label_map[res_id]


def _handle_draw(
    ctx: _ParseContext,
    name: str,
    eid: int,
    params: Dict[str, str],
    marker_stack: List[str],
) -> None:
    breadcrumbs = list(marker_stack)
    pass_name = breadcrumbs[-1] if breadcrumbs else f"EID {eid}"
    scene_pass = _detect_scene_pass(breadcrumbs)
    triangles, instances = _extract_draw_counts(name, params)

    tex_items, tex_count, tex_bytes = _snapshot_textures(ctx)
    tex_mb = tex_bytes / (1024 * 1024)
    bw_risk = min(tex_mb / 8.0, 1.0)

    ctx.draw_rows.append({
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
        "target_width": ctx.state.viewport_w,
        "target_height": ctx.state.viewport_h,
        "target_samples": 1,
        "screen_coverage_percent": 0.0,
        "coverage_pixels_estimate": 0,
        "instruction_coverage_score": 0.0,
        "stable_sort_score": float(triangles),
        "stable_sort_basis": "triangles_fallback",
        "draw_preview_url": "",
        "draw_preview_kind": "unavailable",
        "texture_count": tex_count,
        "texture_total_bytes": tex_bytes,
        "texture_total_mb": round(tex_mb, 3),
        "texture_bandwidth_risk": round(bw_risk, 4),
        "texture_summary_items": tex_items,
        "texture_summary_text": "; ".join(
            f"{t['label'] or t['res_id']} {t['width']}x{t['height']} {t['format']}"
            for t in tex_items[:6]
        ),
        "shader_ids": {"program": ctx.state.current_program} if ctx.state.current_program else {},
    })


def _snapshot_textures(ctx: _ParseContext) -> Tuple[List[Dict[str, Any]], int, int]:
    """Return (summary_items, count, total_bytes) for currently bound textures."""
    seen: set[str] = set()
    items: List[Dict[str, Any]] = []
    total_bytes = 0
    for _unit, res_id in sorted(ctx.state.bound_textures.items()):
        if res_id in seen or res_id == "0":
            continue
        seen.add(res_id)
        info = ctx.texture_catalog.get(res_id)
        if info is None or (info.width == 0 and info.height == 0):
            continue
        items.append({
            "res_id": res_id,
            "label": info.label,
            "width": info.width,
            "height": info.height,
            "format": _format_short(info.fmt),
            "levels": info.levels,
            "estimated_bytes": info.estimated_bytes,
        })
        total_bytes += info.estimated_bytes
    return items, len(items), total_bytes


# ---------------------------------------------------------------------------
# Draw param helpers
# ---------------------------------------------------------------------------

def _extract_draw_counts(name: str, params: Dict[str, str]) -> Tuple[int, int]:
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
    total_tex_mb = 0.0
    seen_tex: set[str] = set()
    for row in rows:
        for item in row.get("texture_summary_items", []):
            rid = item.get("res_id", "")
            if rid not in seen_tex:
                seen_tex.add(rid)
                total_tex_mb += item.get("estimated_bytes", 0) / (1024 * 1024)

    return {
        "draw_count": len(rows),
        "total_gpu_duration_ms": 0.0,
        "total_triangles": sum(int(r.get("triangles", 0)) for r in rows),
        "total_vertices_read": 0,
        "total_instruction_count": 0,
        "total_stable_sort_score": sum(float(r.get("stable_sort_score", 0)) for r in rows),
        "total_instruction_coverage_score": 0.0,
        "total_texture_mb": round(total_tex_mb, 3),
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


def _build_hotspot_hints(
    pass_chart: List[Dict[str, Any]],
    rows: List[Dict[str, Any]],
) -> List[str]:
    hints: List[str] = [
        "⚠️ 当前为 XML 回退分析模式，GPU Duration 不可用，排序按三角面数。",
    ]
    if pass_chart:
        top = pass_chart[0]
        hints.append(
            f"三角面最多的 Pass 是 `{top['name']}`，占比 {top['percent']}%。"
        )
    if rows:
        top_draw = rows[0]
        hints.append(
            f"三角面最多的 Draw 是 `EID {top_draw['eid']} | "
            f"{top_draw['pass_name']}`，{top_draw['triangles']} 面。"
        )
    tex_heavy = [r for r in rows if r.get("texture_total_mb", 0) > 4.0]
    if tex_heavy:
        hints.append(
            f"有 {len(tex_heavy)} 个 Draw 单次采样贴图 > 4 MB，"
            f"可能存在纹理带宽瓶颈。"
        )
    return hints
