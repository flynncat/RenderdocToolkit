"""GLSL simplifier with multiple transformation levels.

Levels:
  L0 — Strip RenderDoc header/preamble, helper blocks, #version/precision
  L1 — Dead code elimination (backward reachability from output variables)
  L2 — Constant folding (substitute uniform runtime values from shader_params)
  L3 — Branch elimination (remove if(0), if(1) after constant folding)
  L4 — Algebraic simplification (mix(a,b,0)→a, a*1→a, etc.)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class TransformRecord:
    level: str
    name: str
    description: str
    lines_before: int
    lines_after: int
    removed_items: List[str] = field(default_factory=list)


@dataclass
class SimplifyResult:
    original_source: str
    simplified_source: str
    original_line_count: int
    simplified_line_count: int
    transforms: List[TransformRecord] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "original_line_count": self.original_line_count,
            "simplified_line_count": self.simplified_line_count,
            "reduction_pct": round(
                (1 - self.simplified_line_count / max(self.original_line_count, 1)) * 100, 1
            ),
            "transforms": [
                {
                    "level": t.level,
                    "name": t.name,
                    "description": t.description,
                    "lines_before": t.lines_before,
                    "lines_after": t.lines_after,
                    "removed_items": t.removed_items,
                }
                for t in self.transforms
            ],
        }


_RENDERDOC_HELPER_PATTERNS = [
    re.compile(r"^\s*(uint\d?|float\d?)\s+_rdt_\w+\s*\(", re.MULTILINE),
    re.compile(r"^\s*float2?\s+unpackHalf2x16_emu\s*\(", re.MULTILINE),
    re.compile(r"^\s*uint\s+packHalf2x16_emu\s*\(", re.MULTILINE),
]

_GLSL_OUTPUTS = {
    "gl_FragColor", "gl_FragData", "gl_FragDepth",
}

_IDENTIFIER_RE = re.compile(r"\b([A-Za-z_]\w*)\b")

_ALGEBRAIC_RULES: List[Tuple[re.Pattern, str, str]] = [
    (re.compile(r"\bmix\s*\(\s*(\w[\w.]*)\s*,\s*\w[\w.]*\s*,\s*0\.0\s*\)"), r"\1", "mix(a,b,0.0)→a"),
    (re.compile(r"\bmix\s*\(\s*\w[\w.]*\s*,\s*(\w[\w.]*)\s*,\s*1\.0\s*\)"), r"\1", "mix(a,b,1.0)→b"),
    (re.compile(r"\b(\w[\w.]*)\s*\*\s*1\.0\b"), r"\1", "a*1.0→a"),
    (re.compile(r"\b1\.0\s*\*\s*(\w[\w.]*)\b"), r"\1", "1.0*a→a"),
    (re.compile(r"\b(\w[\w.]*)\s*\+\s*0\.0\b"), r"\1", "a+0.0→a"),
    (re.compile(r"\b0\.0\s*\+\s*(\w[\w.]*)\b"), r"\1", "0.0+a→a"),
    (re.compile(r"\b(\w[\w.]*)\s*-\s*0\.0\b"), r"\1", "a-0.0→a"),
    (re.compile(r"\b(\w[\w.]*)\s*/\s*1\.0\b"), r"\1", "a/1.0→a"),
    (re.compile(r"\b0\.0\s*\*\s*\w[\w.]*\b"), "0.0", "0.0*a→0.0"),
    (re.compile(r"\b\w[\w.]*\s*\*\s*0\.0\b"), "0.0", "a*0.0→0.0"),
]


class GlslSimplifier:
    """Multi-level GLSL simplifier."""

    def simplify(
        self,
        source: str,
        shader_params_json: str = "",
        levels: str = "L0,L1,L2,L3,L4",
    ) -> SimplifyResult:
        source = _normalize(source)
        original = source
        original_lines = _count_lines(source)
        transforms: List[TransformRecord] = []
        enabled = {s.strip().upper() for s in levels.split(",")}

        if "L0" in enabled:
            source, rec = self._apply_l0(source)
            if rec:
                transforms.append(rec)

        if "L1" in enabled:
            source, rec = self._apply_l1(source)
            if rec:
                transforms.append(rec)

        if "L2" in enabled:
            source, rec = self._apply_l2(source, shader_params_json)
            if rec:
                transforms.append(rec)

        if "L3" in enabled:
            source, rec = self._apply_l3(source)
            if rec:
                transforms.append(rec)

        if "L4" in enabled:
            source, rec = self._apply_l4(source)
            if rec:
                transforms.append(rec)

        return SimplifyResult(
            original_source=original,
            simplified_source=source,
            original_line_count=original_lines,
            simplified_line_count=_count_lines(source),
            transforms=transforms,
        )

    # ------------------------------------------------------------------
    # L0: Strip RenderDoc preamble and helper blocks
    # ------------------------------------------------------------------

    def _apply_l0(self, source: str) -> Tuple[str, Optional[TransformRecord]]:
        before = _count_lines(source)
        removed: List[str] = []

        lines = source.splitlines()
        clean: List[str] = []
        in_header = True
        for line in lines:
            stripped = line.strip()
            if in_header:
                if not stripped or stripped.startswith("//"):
                    removed.append(stripped)
                    continue
                in_header = False
            clean.append(line)
        source = "\n".join(clean)

        source = re.sub(r"^\s*#version[^\n]*\n?", "", source, flags=re.MULTILINE)
        source = re.sub(r"^\s*precision\s+\w+\s+\w+\s*;\s*\n?", "", source, flags=re.MULTILINE)

        source = self._remove_helper_functions(source, removed)
        source = source.strip()

        after = _count_lines(source)
        if after == before:
            return source, None
        return source, TransformRecord(
            level="L0", name="strip_preamble",
            description="Remove RenderDoc header, #version, precision, helper blocks",
            lines_before=before, lines_after=after, removed_items=removed[:20],
        )

    @staticmethod
    def _remove_helper_functions(source: str, removed: List[str]) -> str:
        """Remove known RenderDoc-injected helper functions."""
        changed = True
        while changed:
            changed = False
            for pat in _RENDERDOC_HELPER_PATTERNS:
                m = pat.search(source)
                if not m:
                    continue
                fn_start = m.start()
                brace = source.find("{", m.end())
                if brace < 0:
                    continue
                end = _find_matching_brace(source, brace)
                if end is None:
                    continue
                name_match = re.search(r"(\w+)\s*\(", source[m.start():m.end()])
                if name_match:
                    removed.append(f"helper: {name_match.group(1)}")
                source = source[:fn_start].rstrip("\n") + "\n" + source[end:].lstrip("\n")
                changed = True
                break
        return source

    # ------------------------------------------------------------------
    # L1: Dead code elimination
    # ------------------------------------------------------------------

    def _apply_l1(self, source: str) -> Tuple[str, Optional[TransformRecord]]:
        before = _count_lines(source)
        main_block = _find_main_block(source)
        if main_block is None:
            return source, None

        preamble = source[:main_block["start"]]
        body = source[main_block["body_start"]:main_block["body_end"]]
        tail = source[main_block["end"]:]

        out_vars = set()
        for v in _GLSL_OUTPUTS:
            if v in body:
                out_vars.add(v)
        out_re = re.compile(r"^\s*(?:layout\s*\([^)]*\)\s*)?out\s+\w+\s+(\w+)\s*;", re.MULTILINE)
        for m in out_re.finditer(preamble):
            out_vars.add(m.group(1))

        reachable = _find_reachable_vars(body, out_vars)

        preamble_lines = preamble.splitlines()
        clean_preamble: List[str] = []
        removed: List[str] = []
        uniform_re = re.compile(r"^\s*uniform\s+\w+\s+(\w+)\s*;")
        varying_re = re.compile(r"^\s*(?:in|varying)\s+\w+\s+(\w+)\s*;")
        for line in preamble_lines:
            m = uniform_re.match(line) or varying_re.match(line)
            if m:
                name = m.group(1)
                if name not in reachable:
                    removed.append(name)
                    continue
            clean_preamble.append(line)

        if not removed:
            return source, None

        new_preamble = "\n".join(clean_preamble)
        source = new_preamble + source[main_block["start"]:]

        after = _count_lines(source)
        return source, TransformRecord(
            level="L1", name="dead_code_elimination",
            description="Remove unreferenced uniform/varying declarations",
            lines_before=before, lines_after=after, removed_items=removed[:30],
        )

    # ------------------------------------------------------------------
    # L2: Constant folding (substitute uniform values)
    # ------------------------------------------------------------------

    def _apply_l2(self, source: str, params_json: str) -> Tuple[str, Optional[TransformRecord]]:
        lookup = _parse_params(params_json)
        if not lookup:
            return source, None

        before = _count_lines(source)
        substituted: List[str] = []

        uniform_re = re.compile(
            r"^\s*uniform\s+(\w+)\s+(\w+)\s*;",
            re.MULTILINE,
        )
        uniforms_found: List[Tuple[str, str, str]] = []
        for m in uniform_re.finditer(source):
            glsl_type, name = m.group(1), m.group(2)
            if name in lookup:
                uniforms_found.append((m.group(0), glsl_type, name))

        for decl_text, glsl_type, name in uniforms_found:
            value = lookup[name]
            literal = _value_to_glsl_literal(glsl_type, value)
            if literal is None:
                continue
            replacement = f"const {glsl_type} {name} = {literal};"
            source = source.replace(decl_text, replacement, 1)
            substituted.append(f"{name} = {literal}")

        if not substituted:
            return source, None
        after = _count_lines(source)
        return source, TransformRecord(
            level="L2", name="constant_folding",
            description="Substitute uniform values from shader_params.json",
            lines_before=before, lines_after=after, removed_items=substituted[:30],
        )

    # ------------------------------------------------------------------
    # L3: Branch elimination
    # ------------------------------------------------------------------

    def _apply_l3(self, source: str) -> Tuple[str, Optional[TransformRecord]]:
        before = _count_lines(source)
        removed: List[str] = []

        dead_true = re.compile(r"\bif\s*\(\s*(?:false|0(?:\.0)?)\s*\)\s*\{")
        dead_false = re.compile(r"\bif\s*\(\s*(?:true|1(?:\.0)?)\s*\)\s*\{")

        modified = True
        while modified:
            modified = False
            m = dead_true.search(source)
            if m:
                brace = m.end() - 1
                end = _find_matching_brace(source, brace)
                if end is not None:
                    else_m = re.match(r"\s*else\s*\{", source[end:])
                    if else_m:
                        else_brace = end + else_m.end() - 1
                        else_end = _find_matching_brace(source, else_brace)
                        if else_end is not None:
                            inner = source[else_brace + 1:else_end - 1].strip()
                            source = source[:m.start()] + inner + "\n" + source[else_end:]
                            removed.append("if(false){...}else{kept}")
                            modified = True
                            continue
                    source = source[:m.start()] + source[end:]
                    removed.append("if(false){...removed}")
                    modified = True
                    continue

            m = dead_false.search(source)
            if m:
                brace = m.end() - 1
                end = _find_matching_brace(source, brace)
                if end is not None:
                    inner = source[brace + 1:end - 1].strip()
                    else_m = re.match(r"\s*else\s*\{", source[end:])
                    if else_m:
                        else_brace = end + else_m.end() - 1
                        else_end = _find_matching_brace(source, else_brace)
                        if else_end is not None:
                            source = source[:m.start()] + inner + "\n" + source[else_end:]
                            removed.append("if(true){kept}else{...removed}")
                            modified = True
                            continue
                    source = source[:m.start()] + inner + "\n" + source[end:]
                    removed.append("if(true){kept}")
                    modified = True

        if not removed:
            return source, None
        after = _count_lines(source)
        return source, TransformRecord(
            level="L3", name="branch_elimination",
            description="Remove dead if(false)/if(0) and unwrap if(true)/if(1) branches",
            lines_before=before, lines_after=after, removed_items=removed[:20],
        )

    # ------------------------------------------------------------------
    # L4: Algebraic simplification
    # ------------------------------------------------------------------

    def _apply_l4(self, source: str) -> Tuple[str, Optional[TransformRecord]]:
        before = _count_lines(source)
        applied: List[str] = []

        for pat, repl, desc in _ALGEBRAIC_RULES:
            new = pat.sub(repl, source)
            if new != source:
                applied.append(desc)
                source = new

        if not applied:
            return source, None
        after = _count_lines(source)
        return source, TransformRecord(
            level="L4", name="algebraic_simplification",
            description="Apply algebraic identities",
            lines_before=before, lines_after=after, removed_items=applied,
        )


# ======================================================================
# Module-level helpers
# ======================================================================

def _normalize(text: str) -> str:
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n")


def _count_lines(text: str) -> int:
    return len(text.splitlines()) if text.strip() else 0


def _find_main_block(text: str) -> Optional[Dict[str, int]]:
    m = re.search(r"\bvoid\s+main\s*\(\s*\)\s*\{", text)
    if not m:
        return None
    brace = m.end() - 1
    depth = 0
    for i in range(brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return {
                    "start": m.start(),
                    "body_start": brace + 1,
                    "body_end": i,
                    "end": i + 1,
                }
    return None


def _find_matching_brace(text: str, start: int) -> Optional[int]:
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def _find_reachable_vars(body: str, seeds: set) -> set:
    """BFS over simple variable references in *body*."""
    assignments: Dict[str, str] = {}
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*(?:\.\w+)?\s*=\s*([^;]+);", body):
        assignments.setdefault(m.group(1), "")
        assignments[m.group(1)] += " " + m.group(2)

    for v in seeds.copy():
        for m in re.finditer(rf"\b{re.escape(v)}\b.*?;", body):
            for ident in _IDENTIFIER_RE.findall(m.group(0)):
                seeds.add(ident)

    visited = set()
    frontier = list(seeds)
    while frontier:
        var = frontier.pop()
        if var in visited:
            continue
        visited.add(var)
        rhs = assignments.get(var, "")
        for ident in _IDENTIFIER_RE.findall(rhs):
            if ident not in visited:
                frontier.append(ident)

    for line in body.splitlines():
        for ident in _IDENTIFIER_RE.findall(line):
            if ident in visited:
                for other in _IDENTIFIER_RE.findall(line):
                    if other not in visited:
                        visited.add(other)

    return visited


def _parse_params(params_json: str) -> Dict[str, Any]:
    text = (params_json or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    result: Dict[str, Any] = {}
    for stage_key in ("fragment", "vertex"):
        stage = (payload.get("stages") or {}).get(stage_key) or {}
        for block in stage.get("constant_blocks") or []:
            for var in block.get("variables") or []:
                name = str(var.get("name") or "").strip()
                if name:
                    result[name] = var.get("value")
    return result


def _value_to_glsl_literal(glsl_type: str, value: Any) -> Optional[str]:
    if value is None:
        return None
    if glsl_type == "float":
        return _format_float(value)
    if glsl_type == "int":
        return str(int(value))
    if glsl_type == "uint":
        return f"{int(value)}u"
    if glsl_type == "bool":
        return "true" if value else "false"
    if glsl_type in ("vec2", "vec3", "vec4"):
        if isinstance(value, (list, tuple)):
            parts = ", ".join(_format_float(v) for v in value)
            return f"{glsl_type}({parts})"
        return None
    if glsl_type in ("ivec2", "ivec3", "ivec4"):
        if isinstance(value, (list, tuple)):
            parts = ", ".join(str(int(v)) for v in value)
            return f"{glsl_type}({parts})"
        return None
    if glsl_type in ("uvec2", "uvec3", "uvec4"):
        if isinstance(value, (list, tuple)):
            parts = ", ".join(f"{int(v)}u" for v in value)
            return f"{glsl_type}({parts})"
        return None
    if glsl_type in ("mat2", "mat3", "mat4"):
        if isinstance(value, (list, tuple)) and value and isinstance(value[0], (list, tuple)):
            flat = [_format_float(c) for row in value for c in row]
            return f"{glsl_type}({', '.join(flat)})"
        return None
    return None


def _format_float(v: Any) -> str:
    f = float(v)
    if f == int(f) and abs(f) < 1e15:
        return f"{int(f)}.0"
    return f"{f:.9g}"
