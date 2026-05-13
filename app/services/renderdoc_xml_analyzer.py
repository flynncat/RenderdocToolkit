"""Fallback analyzer that extracts performance data from a RenderDoc XML
capture dump when the standard Python replay API is unavailable (e.g.
custom/older renderdoc builds with incompatible file format).

The XML is produced by ``renderdoccmd convert -c xml`` and contains the full
structured data of the capture — API calls, resources, state changes, marker
labels, and shader source — but *not* GPU hardware counters.

Optionally a Chrome tracing JSON (from ``renderdoccmd convert -c chrome.json``)
provides CPU-side API call timestamps which approximate per-draw timing.

This module performs a single streaming pass over the XML to:
- Build a texture catalog (dimensions, format, estimated size)
- Build a shader catalog (GLSL source + estimated instruction count)
- Track GL state (bound textures, active program, viewport)
- Extract draw calls and marker hierarchy
- Annotate each draw with textures, shaders, viewport, coverage
- Merge Chrome JSON timestamps to populate API duration per draw
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_capture_xml(
    xml_path: str | Path,
    chrome_json_path: str | Path | None = None,
) -> Dict[str, Any]:
    """Parse the XML dump and return an analysis dict compatible with the
    standard ``RenderdocPerfService`` output format.

    If *chrome_json_path* is provided, per-draw API CPU durations are read
    from it and used to populate ``gpu_duration_ms`` (approximate).
    """
    xml_path = Path(xml_path)
    header = _parse_header(xml_path)
    ctx = _parse_capture(xml_path)

    # Apply Chrome JSON timing data if available
    chrome_used = False
    if chrome_json_path:
        try:
            durations = _parse_chrome_draw_durations(Path(chrome_json_path))
            chrome_used = _merge_chrome_durations(ctx.draw_rows, durations)
        except Exception as exc:
            log.warning("Failed to parse chrome.json: %s", exc)

    # Compute viewport-based coverage estimates
    _compute_coverage_estimates(ctx.draw_rows)

    draw_rows = ctx.draw_rows
    # Sort by API duration (if available) else by triangles
    if chrome_used:
        draw_rows.sort(key=lambda r: (r["gpu_duration_ms"], r["triangles"]), reverse=True)
    else:
        draw_rows.sort(key=lambda r: r["triangles"], reverse=True)

    overview = _build_overview(draw_rows)
    pass_chart = _build_pass_chart(draw_rows)

    unavailable = []
    if not chrome_used:
        unavailable.append("GPU/API Duration")
    unavailable.extend(["精确 GPU Duration", "线框预览（仅 capture 缩略图可用）"])

    warnings = []
    if chrome_used:
        warnings.append(
            "GPU 耗时使用 Chrome JSON 提供的 CPU 端 API 调用时长（近似值），"
            "可能与真实 GPU 工作时间存在偏差。"
        )
    warnings.append(
        "当前 capture 使用非标准 RenderDoc 格式，"
        f"{', '.join(unavailable)} 等依赖回放 API 的字段为近似或不可用。"
    )

    sort_fields = []
    if chrome_used:
        sort_fields.append({"id": "gpu_duration_ms", "label": "API 耗时 (ms)"})
    sort_fields.extend([
        {"id": "triangles", "label": "三角面数"},
        {"id": "instances", "label": "实例数"},
        {"id": "instruction_total", "label": "指令数 (估算)"},
        {"id": "texture_total_mb", "label": "贴图总量 (MB)"},
        {"id": "texture_count", "label": "贴图数量"},
        {"id": "screen_coverage_percent", "label": "屏幕覆盖率 (%)"},
    ])

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
        "hotspot_hints": _build_hotspot_hints(pass_chart, draw_rows, chrome_used),
        "analysis_features": {
            "api_duration_from_chrome_json": chrome_used,
            "instruction_count_estimated": True,
            "coverage_estimated_from_viewport": True,
            "wireframe_preview_supported": False,
            "thumbnail_preview_supported": True,
        },
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
# Resource info dataclasses
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


@dataclass
class _ShaderInfo:
    """A compiled GL shader stage (vertex/fragment/compute)."""
    res_id: str = ""
    stage: str = ""  # "vertex"|"fragment"|"compute"|"geometry"|"tess_control"|"tess_eval"
    source: str = ""
    instruction_count: int = 0


@dataclass
class _ProgramInfo:
    """A linked GL program — references multiple shader stages."""
    res_id: str = ""
    shader_ids: List[str] = field(default_factory=list)
    vs_instructions: int = 0
    ps_instructions: int = 0
    total_instructions: int = 0


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
    # Pending shader awaiting source attachment via glShaderSource
    pending_shader_id: str = ""
    pending_shader_type: str = ""


# ---------------------------------------------------------------------------
# Parse context — collects everything in a single pass
# ---------------------------------------------------------------------------

@dataclass
class _ParseContext:
    texture_catalog: Dict[str, _TexInfo] = field(default_factory=dict)
    label_map: Dict[str, str] = field(default_factory=dict)
    shader_catalog: Dict[str, _ShaderInfo] = field(default_factory=dict)
    program_catalog: Dict[str, _ProgramInfo] = field(default_factory=dict)
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

_SHADER_CHUNKS = {
    "glCreateShader", "glShaderSource", "glCompileShader",
    "glCreateProgram", "glAttachShader", "glLinkProgram",
}

_STATE_CHUNKS = (
    _TEX_STORAGE
    | _SHADER_CHUNKS
    | {"glBindTexture", "glActiveTexture", "glGenTextures",
       "glUseProgram", "glViewport", "glLabelObjectEXT"}
)

_ALL_INTERESTING = _DRAW_NAMES | _MARKER_PUSH | _MARKER_POP | _STATE_CHUNKS

# GL_VERTEX_SHADER=35633, GL_FRAGMENT_SHADER=35632, GL_COMPUTE_SHADER=37305,
# GL_GEOMETRY_SHADER=36313, GL_TESS_CONTROL_SHADER=36488, GL_TESS_EVAL_SHADER=36487
_SHADER_TYPE_MAP = {
    "35633": "vertex", "GL_VERTEX_SHADER": "vertex",
    "35632": "fragment", "GL_FRAGMENT_SHADER": "fragment",
    "37305": "compute", "GL_COMPUTE_SHADER": "compute",
    "36313": "geometry", "GL_GEOMETRY_SHADER": "geometry",
    "36488": "tess_control", "GL_TESS_CONTROL_SHADER": "tess_control",
    "36487": "tess_eval", "GL_TESS_EVALUATION_SHADER": "tess_eval",
}

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

    Builds catalogs (texture, shader, program), tracks GL state, and emits
    draw rows with annotations — all in one pass for performance.
    """
    ctx = _ParseContext()
    marker_stack: List[str] = []

    in_target = False
    chunk_name = ""
    chunk_attrs: Dict[str, str] = {}
    params: Dict[str, str] = {}
    label = ""
    # For glShaderSource we need to keep the FULL string value (no truncation)
    shader_source: List[str] = []

    for ev_type, elem in ET.iterparse(str(xml_path), events=("start", "end")):
        if ev_type == "start" and elem.tag == "chunk":
            name = elem.get("name", "")
            if name in _ALL_INTERESTING:
                in_target = True
                chunk_name = name
                chunk_attrs = dict(elem.attrib)
                params = {}
                label = ""
                shader_source = []
            continue

        if ev_type == "end" and elem.tag == "chunk":
            if in_target:
                if chunk_name == "glShaderSource" and shader_source:
                    params["__source__"] = "".join(shader_source)
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
            # Capture string values inside glShaderSource (unnamed strings in
            # an <array name="sources"> child)
            if chunk_name == "glShaderSource" and elem.tag == "string" and text:
                shader_source.append(text)
            elif elem.tag == "string" and text:
                label = label or text
        elif ev_type == "end" and not in_target:
            elem.clear()

    # Build program → instruction count summary after parsing finishes
    _finalize_programs(ctx)
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

    # --- Shader / program lifecycle ---
    if name == "glCreateShader":
        res_id = params.get("Shader", "") or params.get("shader", "")
        type_val = params.get("type", "") or params.get("shaderType", "")
        stage = _SHADER_TYPE_MAP.get(type_val, "")
        if res_id:
            ctx.shader_catalog[res_id] = _ShaderInfo(res_id=res_id, stage=stage)
        return

    if name == "glShaderSource":
        res_id = params.get("shader", "")
        source = params.get("__source__", "")
        if res_id and source:
            info = ctx.shader_catalog.setdefault(res_id, _ShaderInfo(res_id=res_id))
            info.source = source
            info.instruction_count = _estimate_glsl_instructions(source)
        return

    if name == "glCreateProgram":
        res_id = params.get("Program", "") or params.get("program", "")
        if res_id:
            ctx.program_catalog[res_id] = _ProgramInfo(res_id=res_id)
        return

    if name == "glAttachShader":
        prog_id = params.get("program", "")
        shader_id = params.get("shader", "")
        if prog_id and shader_id:
            prog = ctx.program_catalog.setdefault(prog_id, _ProgramInfo(res_id=prog_id))
            if shader_id not in prog.shader_ids:
                prog.shader_ids.append(shader_id)
        return

    if name == "glLinkProgram":
        return

    # --- Draw calls ---
    if name in _DRAW_NAMES:
        _handle_draw(ctx, name, eid, params, marker_stack)


def _finalize_programs(ctx: _ParseContext) -> None:
    """Compute per-stage instruction counts for each program after parsing."""
    for prog in ctx.program_catalog.values():
        vs_inst, ps_inst = _aggregate_program_instructions(ctx, prog.res_id)
        prog.vs_instructions = vs_inst
        prog.ps_instructions = ps_inst
        prog.total_instructions = vs_inst + ps_inst


def _aggregate_program_instructions(
    ctx: _ParseContext,
    program_id: str,
) -> Tuple[int, int]:
    """Aggregate vertex+fragment shader instruction counts for a program.
    Returns (vs_total, ps_total).
    """
    if not program_id:
        return 0, 0
    prog = ctx.program_catalog.get(program_id)
    if not prog:
        return 0, 0
    vs_total = 0
    ps_total = 0
    for sid in prog.shader_ids:
        shader = ctx.shader_catalog.get(sid)
        if not shader:
            continue
        if shader.stage == "vertex":
            vs_total += shader.instruction_count
        elif shader.stage == "fragment":
            ps_total += shader.instruction_count
    return vs_total, ps_total


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

    # Look up shader instruction counts via the currently bound program.
    # Compute on-demand from shader_catalog because _finalize_programs runs
    # AFTER parsing — shaders may not have been linked yet when this draw
    # appeared (though in practice all shaders for a program are linked
    # before glUseProgram is called).
    vs_inst, ps_inst = _aggregate_program_instructions(ctx, ctx.state.current_program)

    ctx.draw_rows.append({
        "eid": str(eid),
        "_chunk_index": eid,  # preserved for chrome.json matching by ordinal
        "pass_name": pass_name,
        "scene_pass": scene_pass or "Other",
        "selection_label": f"EID {eid} | {pass_name}",
        "breadcrumbs": breadcrumbs,
        "draw_type": _classify_draw(name),
        "draw_api_name": name,
        "instances": instances,
        "triangles": triangles,
        "vertices_read": 0,
        "input_primitives": 0,
        "gpu_duration_ms": 0.0,
        "vs_invocations": 0,
        "ps_invocations": 0,
        "samples_passed": 0,
        "vs_instruction_count": vs_inst,
        "ps_instruction_count": ps_inst,
        "instruction_total": vs_inst + ps_inst,
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
        "total_gpu_duration_ms": round(
            sum(float(r.get("gpu_duration_ms", 0)) for r in rows), 3
        ),
        "total_triangles": sum(int(r.get("triangles", 0)) for r in rows),
        "total_vertices_read": 0,
        "total_instruction_count": sum(int(r.get("instruction_total", 0)) for r in rows),
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
        item["gpu_duration_ms"] += float(row.get("gpu_duration_ms", 0))

    if not bucket:
        return []

    total_tris = max(sum(b["triangles"] for b in bucket.values()), 1)
    total_gpu = sum(b["gpu_duration_ms"] for b in bucket.values())
    result = []
    for item in bucket.values():
        result.append({
            "name": item["name"],
            "gpu_duration_ms": round(item["gpu_duration_ms"], 3),
            "triangles": item["triangles"],
            "draw_count": item["draw_count"],
            "percent": round(
                (item["gpu_duration_ms"] / total_gpu * 100)
                if total_gpu > 0
                else (item["triangles"] / total_tris * 100),
                2,
            ),
        })
    # Sort by GPU duration when available, else triangles
    if total_gpu > 0:
        result.sort(key=lambda x: x["gpu_duration_ms"], reverse=True)
    else:
        result.sort(key=lambda x: x["triangles"], reverse=True)
    return result


def _build_hotspot_hints(
    pass_chart: List[Dict[str, Any]],
    rows: List[Dict[str, Any]],
    chrome_used: bool,
) -> List[str]:
    hints: List[str] = []
    if chrome_used:
        hints.append(
            "ℹ️ 当前为 XML 回退分析，GPU 耗时使用 Chrome JSON 的 API CPU 时长（近似）。"
        )
    else:
        hints.append(
            "⚠️ 当前为 XML 回退分析模式，GPU Duration 不可用，排序按三角面数。"
        )
    if pass_chart:
        top = pass_chart[0]
        unit = "ms" if chrome_used else "面"
        val = top.get("gpu_duration_ms") if chrome_used else top.get("triangles")
        hints.append(
            f"耗时/数据量最大的 Pass 是 `{top['name']}`，"
            f"{val}{unit}，占比 {top['percent']}%。"
        )
    if rows:
        top_draw = rows[0]
        if chrome_used and top_draw.get("gpu_duration_ms", 0) > 0:
            hints.append(
                f"耗时最长的 Draw 是 `EID {top_draw['eid']} | "
                f"{top_draw['pass_name']}`，"
                f"{top_draw['gpu_duration_ms']:.3f} ms。"
            )
        else:
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
    heavy_inst = [r for r in rows if r.get("instruction_total", 0) > 500]
    if heavy_inst:
        hints.append(
            f"有 {len(heavy_inst)} 个 Draw 的着色器指令估算 > 500，"
            f"可能存在 ALU 瓶颈。"
        )
    return hints


# ---------------------------------------------------------------------------
# GLSL instruction count estimator
# ---------------------------------------------------------------------------

# Per-call cost weights for GLSL built-ins (approximate ALU/texture cycles
# based on typical mobile GPU pipelines).  Texture samples are 4-8x of ALU.
_GLSL_BUILTIN_COSTS = {
    # Texture sampling (expensive — bandwidth + filtering)
    "texture": 6, "texture2D": 6, "textureLod": 6, "textureGrad": 8,
    "textureProj": 7, "texelFetch": 4, "textureCube": 6, "textureCubeLod": 6,
    "textureGather": 6,
    # Transcendentals (multi-cycle)
    "sin": 4, "cos": 4, "tan": 6, "asin": 6, "acos": 6, "atan": 6,
    "pow": 4, "exp": 4, "log": 4, "exp2": 3, "log2": 3,
    "sqrt": 4, "inversesqrt": 3,
    # Vector / matrix
    "dot": 1, "cross": 3, "length": 4, "distance": 5, "normalize": 5,
    "reflect": 3, "refract": 6,
    # Conditionals / control flow
    "mix": 2, "clamp": 2, "smoothstep": 3, "step": 1,
    "min": 1, "max": 1, "abs": 1, "sign": 1,
    "floor": 1, "ceil": 1, "fract": 1, "mod": 2, "round": 1,
    # Geometric / matrix multiply per row
    "transpose": 4, "inverse": 16, "determinant": 8,
}

_COMMENT_RX = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_STRING_RX = re.compile(r'"[^"]*"')
_PREPROC_RX = re.compile(r"^\s*#[^\n]*", re.MULTILINE)


def _estimate_glsl_instructions(source: str) -> int:
    """Heuristic GLSL ALU/texture instruction estimate.

    This is NOT a hardware-accurate count — it's a rough comparative metric.
    Counts assignments, function calls (with built-in cost weights), and
    arithmetic operators while ignoring comments, preprocessor lines and
    strings.
    """
    if not source:
        return 0
    src = _COMMENT_RX.sub(" ", source)
    src = _STRING_RX.sub('""', src)
    src = _PREPROC_RX.sub("", src)

    # Count assignments (excluding ==, !=, <=, >=)
    assignments = len(re.findall(r"[^=!<>+\-*/%&|^]=(?!=)", src))
    # Compound assignments
    compound = len(re.findall(r"[+\-*/%&|^]=(?!=)", src))
    # Built-in / function calls
    builtin_cost = 0
    other_calls = 0
    for match in re.finditer(r"\b([A-Za-z_][A-Za-z_0-9]*)\s*\(", src):
        fn = match.group(1)
        if fn in _GLSL_BUILTIN_COSTS:
            builtin_cost += _GLSL_BUILTIN_COSTS[fn]
        elif fn not in {"if", "for", "while", "switch", "return", "main",
                        "void", "float", "int", "uint", "bool",
                        "vec2", "vec3", "vec4", "ivec2", "ivec3", "ivec4",
                        "uvec2", "uvec3", "uvec4", "mat2", "mat3", "mat4",
                        "struct"}:
            other_calls += 1
    # Plain arithmetic operators between identifiers / numbers (rough)
    arith = len(re.findall(r"[A-Za-z_0-9\)\]]\s*[+\-*/]\s*[A-Za-z_0-9\(\[]", src))
    # Branches add some overhead
    branches = len(re.findall(r"\b(if|else if|for|while)\b", src))
    # Discard / return statements
    flow = len(re.findall(r"\b(discard|return)\b", src))

    total = (
        assignments
        + compound * 2
        + builtin_cost
        + other_calls * 2
        + arith
        + branches * 2
        + flow
    )
    return total


# ---------------------------------------------------------------------------
# Chrome JSON timing helpers
# ---------------------------------------------------------------------------

def _parse_chrome_draw_durations(chrome_json_path: Path) -> List[Tuple[str, float]]:
    """Read a Chrome tracing JSON and return per-draw (name, duration_ms)
    in chronological order, matching draw call ordering in the capture.

    Chrome JSON uses microsecond timestamps in ``ts``.  Each API call emits a
    ``B`` (begin) and ``E`` (end) event with the same ``tid``.  We pair them
    via a tid-keyed stack to compute per-call durations.
    """
    text = chrome_json_path.read_text(encoding="utf-8")
    data = json.loads(text)
    events = data.get("traceEvents", [])

    draw_names = _DRAW_NAMES
    stacks: Dict[int, List[Tuple[str, float, bool]]] = defaultdict(list)
    results: List[Tuple[str, float]] = []

    for ev in events:
        ph = ev.get("ph", "")
        if ph not in ("B", "E"):
            continue
        tid = ev.get("tid", 0)
        ts = float(ev.get("ts", 0))
        if ph == "B":
            name = ev.get("name", "")
            stacks[tid].append((name, ts, name in draw_names))
        else:  # E
            if not stacks[tid]:
                continue
            begin_name, begin_ts, is_draw = stacks[tid].pop()
            if is_draw:
                # ts in microseconds, convert to ms
                results.append((begin_name, (ts - begin_ts) / 1000.0))

    return results


def _merge_chrome_durations(
    rows: List[Dict[str, Any]],
    durations: List[Tuple[str, float]],
) -> bool:
    """Apply Chrome JSON durations to draw rows in chunkIndex order.

    Returns *True* if matching succeeded (and at least one duration applied).
    """
    if not rows or not durations:
        return False
    # Rows are appended in chunkIndex order during XML parsing.  Chrome JSON
    # events are also in chronological order.  We zip them by ordinal.
    by_chunk_index = sorted(rows, key=lambda r: r["_chunk_index"])
    applied = 0
    for row, (chrome_name, dur_ms) in zip(by_chunk_index, durations):
        # Sanity check: API names should match (within draw family)
        if row.get("draw_api_name") and chrome_name and \
                row["draw_api_name"] != chrome_name:
            log.debug(
                "Chrome JSON draw mismatch: xml=%s chrome=%s",
                row["draw_api_name"], chrome_name,
            )
        row["gpu_duration_ms"] = round(dur_ms, 3)
        row["stable_sort_score"] = dur_ms
        row["stable_sort_basis"] = "api_duration"
        applied += 1
    log.info(
        "Merged Chrome JSON durations into %d/%d draw rows", applied, len(rows),
    )
    return applied > 0


# ---------------------------------------------------------------------------
# Coverage estimator
# ---------------------------------------------------------------------------

# Track the maximum viewport seen across all draws to use as a "screen size"
# baseline for relative coverage estimation.

def _compute_coverage_estimates(rows: List[Dict[str, Any]]) -> None:
    """Estimate viewport-based coverage metrics for each draw.

    ``coverage_pixels_estimate`` = viewport area (max possible pixels touched).
    ``screen_coverage_percent`` = viewport area as % of the largest viewport
    seen in this capture (used as a proxy for "screen size").
    ``instruction_coverage_score`` = instruction_total * sqrt(coverage_pixels)
    — a rough proxy for total shader workload.
    """
    if not rows:
        return
    max_vp_area = 0
    for row in rows:
        w = int(row.get("target_width", 0))
        h = int(row.get("target_height", 0))
        area = w * h
        if area > max_vp_area:
            max_vp_area = area

    for row in rows:
        w = int(row.get("target_width", 0))
        h = int(row.get("target_height", 0))
        area = w * h
        row["coverage_pixels_estimate"] = area
        if max_vp_area > 0:
            row["screen_coverage_percent"] = round(area / max_vp_area * 100, 2)
        inst = int(row.get("instruction_total", 0))
        if inst and area:
            row["instruction_coverage_score"] = round(inst * (area ** 0.5), 2)
