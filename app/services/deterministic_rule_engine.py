"""Deterministic GLSL→HLSL rule engine.

Applies an ordered pipeline of regex-based and structural rules to convert
RenderDoc fragment GLSL into compilable HLSL — both standalone (for DXC) and
UE4.26 Custom-node flavour.

The rule set is derived from the patterns mined in Phase 4.1 and hardened by
the verify loops in Phases 2–3.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


# ------------------------------------------------------------------ #
# Data structures
# ------------------------------------------------------------------ #

@dataclass
class RuleApplication:
    rule_name: str
    description: str
    lines_before: int
    lines_after: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_name": self.rule_name,
            "description": self.description,
            "lines_before": self.lines_before,
            "lines_after": self.lines_after,
        }


@dataclass
class ConversionResult:
    success: bool
    standalone_hlsl: str = ""
    ue_custom_hlsl: str = ""
    warnings: List[str] = field(default_factory=list)
    unsupported: List[str] = field(default_factory=list)
    rules_applied: List[RuleApplication] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "standalone_hlsl": self.standalone_hlsl,
            "ue_custom_hlsl": self.ue_custom_hlsl,
            "warnings": self.warnings,
            "unsupported": self.unsupported,
            "rules_applied": [r.to_dict() for r in self.rules_applied],
            "error": self.error,
        }


# ------------------------------------------------------------------ #
# Type maps
# ------------------------------------------------------------------ #

_TYPE_MAP = {
    "float": "float", "int": "int", "uint": "uint", "bool": "bool",
    "vec2": "float2", "vec3": "float3", "vec4": "float4",
    "uvec2": "uint2", "uvec3": "uint3", "uvec4": "uint4",
    "ivec2": "int2", "ivec3": "int3", "ivec4": "int4",
    "bvec2": "bool2", "bvec3": "bool3", "bvec4": "bool4",
    "mat2": "float2x2", "mat3": "float3x3", "mat4": "float4x4",
    "sampler2D": "Texture2D",
    "sampler2DShadow": "Texture2D",
    "samplerCube": "TextureCube",
    "sampler3D": "Texture3D",
}

_FUNC_MAP = {
    "dFdx": "ddx", "dFdy": "ddy",
    "fract": "frac", "mod": "fmod",
    "floatBitsToUint": "asuint", "uintBitsToFloat": "asfloat",
    "inversesqrt": "rsqrt",
    "atan": "atan2",
}

_VECTOR_CONSTRUCTORS = {
    "float2": 2, "float3": 3, "float4": 4,
    "int2": 2, "int3": 3, "int4": 4,
    "uint2": 2, "uint3": 3, "uint4": 4,
    "bool2": 2, "bool3": 3, "bool4": 4,
}

_UE_MATRIX_EXPRESSIONS = {
    "worldviewprojection": ("float4x4", "ResolvedView.TranslatedWorldToClip"),
    "worldview": ("float4x4", "ResolvedView.TranslatedWorldToView"),
    "projection": ("float4x4", "ResolvedView.ViewToClip"),
    "viewprojection": ("float4x4", "ResolvedView.TranslatedWorldToClip"),
    "world": ("float4x4", "GetPrimitiveData(Parameters.PrimitiveId).LocalToWorld"),
    "inverseworld": ("float4x4", "GetPrimitiveData(Parameters.PrimitiveId).WorldToLocal"),
    "viewmatrix": ("float4x4", "ResolvedView.TranslatedWorldToView"),
}


# ------------------------------------------------------------------ #
# Rule engine
# ------------------------------------------------------------------ #

class DeterministicRuleEngine:
    """Ordered pipeline of GLSL→HLSL transformation rules."""

    def convert(
        self,
        glsl_source: str,
        *,
        shader_params_json: str = "",
        mode: str = "both",
    ) -> ConversionResult:
        """Convert *glsl_source* to HLSL.

        *mode* can be ``"standalone"``, ``"ue_custom"``, or ``"both"``.
        """
        warnings: List[str] = []
        unsupported: List[str] = []
        rules_applied: List[RuleApplication] = []

        src = _normalize(glsl_source)
        if not src.strip():
            return ConversionResult(success=False, error="Empty GLSL source")

        pipeline: List[Tuple[str, str, Callable]] = [
            ("strip_preamble", "Remove #version, precision, header comments", self._rule_strip_preamble),
            ("strip_helper_blocks", "Remove RenderDoc-injected helper functions", self._rule_strip_helpers),
            ("strip_layout", "Remove layout(...) qualifiers", self._rule_strip_layout),
            ("rename_functions", "Rename GLSL functions to HLSL equivalents", self._rule_rename_functions),
            ("convert_texture_calls", "Convert texture/textureLod/textureGrad", self._rule_convert_textures),
            ("convert_mix_to_lerp", "Convert mix() to lerp()", self._rule_mix_to_lerp),
            ("convert_types", "Convert GLSL types to HLSL types", self._rule_convert_types),
            ("expand_scalar_splats", "Expand vecN(x) to vecN(x,x,...)", self._rule_expand_splats),
            ("convert_mat_multiply", "Convert mat*vec to mul(mat,vec)", self._rule_convert_mat_multiply),
            ("strip_qualifiers", "Remove uniform/in/out qualifiers from body", self._rule_strip_qualifiers),
            ("fix_assignment_parens", "Unwrap (x = y); to x = y;", self._rule_fix_assignment_parens),
            ("clean_whitespace", "Normalize blank lines", self._rule_clean_whitespace),
        ]

        for name, desc, fn in pipeline:
            before_lines = _count_lines(src)
            src, w, u = fn(src)
            after_lines = _count_lines(src)
            warnings.extend(w)
            unsupported.extend(u)
            if before_lines != after_lines or w or u:
                rules_applied.append(RuleApplication(
                    rule_name=name, description=desc,
                    lines_before=before_lines, lines_after=after_lines,
                ))

        standalone = self._wrap_standalone(src, warnings, unsupported)
        ue_custom = ""
        if mode in ("ue_custom", "both"):
            ue_custom = self._wrap_ue_custom(src, warnings, unsupported)

        return ConversionResult(
            success=True,
            standalone_hlsl=standalone,
            ue_custom_hlsl=ue_custom,
            warnings=warnings,
            unsupported=unsupported,
            rules_applied=rules_applied,
        )

    # ---- individual rules ---- #

    @staticmethod
    def _rule_strip_preamble(src: str) -> Tuple[str, List[str], List[str]]:
        lines = src.splitlines()
        clean: List[str] = []
        in_header = True
        for line in lines:
            s = line.strip()
            if in_header:
                if not s or s.startswith("//"):
                    continue
                in_header = False
            clean.append(line)
        src = "\n".join(clean)
        src = re.sub(r"^\s*#version[^\n]*\n?", "", src, flags=re.MULTILINE)
        src = re.sub(r"^\s*precision\s+\w+\s+\w+\s*;\s*\n?", "", src, flags=re.MULTILINE)
        src = re.sub(r"^\s*#extension[^\n]*\n?", "", src, flags=re.MULTILINE)
        src = re.sub(r"^\s*#define[^\n]*\n?", "", src, flags=re.MULTILINE)
        src = re.sub(r"^\s*#if[^\n]*\n?", "", src, flags=re.MULTILINE)
        src = re.sub(r"^\s*#else[^\n]*\n?", "", src, flags=re.MULTILINE)
        src = re.sub(r"^\s*#endif[^\n]*\n?", "", src, flags=re.MULTILINE)
        return src.strip(), [], []

    @staticmethod
    def _rule_strip_helpers(src: str) -> Tuple[str, List[str], List[str]]:
        src = re.sub(
            r"// BEGIN: Generated code for built-in function emulation.*?// END: Generated code for built-in function emulation",
            "", src, flags=re.S,
        )
        helper_pats = [
            re.compile(r"^\s*(uint\d?|float\d?)\s+_rdt_\w+\s*\(", re.MULTILINE),
            re.compile(r"^\s*float2?\s+unpackHalf2x16_emu\s*\(", re.MULTILINE),
            re.compile(r"^\s*uint\s+packHalf2x16_emu\s*\(", re.MULTILINE),
        ]
        changed = True
        while changed:
            changed = False
            for pat in helper_pats:
                m = pat.search(src)
                if not m:
                    continue
                brace = src.find("{", m.end())
                if brace < 0:
                    continue
                end = _find_matching_brace(src, brace)
                if end is None:
                    continue
                src = src[:m.start()].rstrip("\n") + "\n" + src[end:].lstrip("\n")
                changed = True
                break
        return src.strip(), [], []

    @staticmethod
    def _rule_strip_layout(src: str) -> Tuple[str, List[str], List[str]]:
        src = re.sub(r"layout\s*\([^)]*\)\s*", "", src)
        return src, [], []

    @staticmethod
    def _rule_rename_functions(src: str) -> Tuple[str, List[str], List[str]]:
        for glsl_fn, hlsl_fn in _FUNC_MAP.items():
            src = re.sub(rf"\b{re.escape(glsl_fn)}\b", hlsl_fn, src)
        return src, [], []

    @staticmethod
    def _rule_convert_textures(src: str) -> Tuple[str, List[str], List[str]]:
        def _replace_call(text, fn_name, replacer):
            needle = fn_name + "("
            idx = 0
            parts: List[str] = []
            while idx < len(text):
                pos = text.find(needle, idx)
                if pos < 0:
                    parts.append(text[idx:])
                    break
                prev = text[pos - 1] if pos > 0 else ""
                if prev and (prev.isalnum() or prev == "_"):
                    parts.append(text[idx:pos + len(fn_name)])
                    idx = pos + len(fn_name)
                    continue
                parts.append(text[idx:pos])
                open_p = pos + len(fn_name)
                close_p = _find_matching_paren(text, open_p)
                if close_p < 0:
                    parts.append(text[pos:])
                    break
                args = _split_args(text[open_p + 1:close_p])
                parts.append(replacer(args))
                idx = close_p + 1
            return "".join(parts)

        def _tex(args):
            if len(args) == 2:
                return f"{args[0]}.Sample({args[0]}Sampler, {args[1]})"
            if len(args) == 3:
                return f"{args[0]}.SampleBias({args[0]}Sampler, {args[1]}, {args[2]})"
            return f"texture({', '.join(args)})"

        def _texlod(args):
            if len(args) == 3:
                return f"{args[0]}.SampleLevel({args[0]}Sampler, {args[1]}, {args[2]})"
            return f"textureLod({', '.join(args)})"

        def _texgrad(args):
            if len(args) == 4:
                return f"{args[0]}.SampleGrad({args[0]}Sampler, {args[1]}, {args[2]}, {args[3]})"
            return f"textureGrad({', '.join(args)})"

        src = _replace_call(src, "textureGrad", _texgrad)
        src = _replace_call(src, "textureLod", _texlod)
        src = _replace_call(src, "texture", _tex)
        return src, [], []

    @staticmethod
    def _rule_mix_to_lerp(src: str) -> Tuple[str, List[str], List[str]]:
        def _replace_call(text, fn_name, replacer):
            needle = fn_name + "("
            idx = 0
            parts: List[str] = []
            while idx < len(text):
                pos = text.find(needle, idx)
                if pos < 0:
                    parts.append(text[idx:])
                    break
                prev = text[pos - 1] if pos > 0 else ""
                if prev and (prev.isalnum() or prev == "_"):
                    parts.append(text[idx:pos + len(fn_name)])
                    idx = pos + len(fn_name)
                    continue
                parts.append(text[idx:pos])
                open_p = pos + len(fn_name)
                close_p = _find_matching_paren(text, open_p)
                if close_p < 0:
                    parts.append(text[pos:])
                    break
                args = _split_args(text[open_p + 1:close_p])
                parts.append(replacer(args))
                idx = close_p + 1
            return "".join(parts)

        prev = None
        while prev != src:
            prev = src
            src = _replace_call(src, "mix", lambda a: f"lerp({', '.join(a)})")
        return src, [], []

    @staticmethod
    def _rule_convert_types(src: str) -> Tuple[str, List[str], List[str]]:
        for glsl_t, hlsl_t in _TYPE_MAP.items():
            src = re.sub(rf"\b{re.escape(glsl_t)}\b", hlsl_t, src)
        return src, [], []

    @staticmethod
    def _rule_expand_splats(src: str) -> Tuple[str, List[str], List[str]]:
        def _replace_call(text, fn_name, width):
            needle = fn_name + "("
            idx = 0
            parts: List[str] = []
            while idx < len(text):
                pos = text.find(needle, idx)
                if pos < 0:
                    parts.append(text[idx:])
                    break
                prev = text[pos - 1] if pos > 0 else ""
                if prev and (prev.isalnum() or prev == "_"):
                    parts.append(text[idx:pos + len(fn_name)])
                    idx = pos + len(fn_name)
                    continue
                parts.append(text[idx:pos])
                open_p = pos + len(fn_name)
                close_p = _find_matching_paren(text, open_p)
                if close_p < 0:
                    parts.append(text[pos:])
                    break
                args = _split_args(text[open_p + 1:close_p])
                if len(args) == 1:
                    val = args[0].strip()
                    if _is_scalar_expr(val):
                        parts.append(f"{fn_name}({', '.join([val] * width)})")
                    else:
                        parts.append(f"{fn_name}({val})")
                else:
                    parts.append(f"{fn_name}({', '.join(args)})")
                idx = close_p + 1
            return "".join(parts)

        for ctor, w in _VECTOR_CONSTRUCTORS.items():
            src = _replace_call(src, ctor, w)
        return src, [], []

    @staticmethod
    def _rule_strip_qualifiers(src: str) -> Tuple[str, List[str], List[str]]:
        src = re.sub(r"\buniform\s+", "", src)
        src = re.sub(r"(?<!\w)\bin\s+", "", src)
        src = re.sub(r"\bout\s+", "", src)
        src = re.sub(r"\bflat\s+", "", src)
        src = re.sub(r"\bnoperspective\s+", "", src)
        src = re.sub(r"\bsmooth\s+", "", src)
        src = re.sub(r"\bcentroid\s+", "", src)
        return src, [], []

    @staticmethod
    def _rule_convert_mat_multiply(src: str) -> Tuple[str, List[str], List[str]]:
        """Convert ``mat * vec`` and ``vec * mat`` to ``mul(a, b)`` for HLSL.

        Detects matrix types from variable declarations AND constructor calls.
        """
        _MAT_TYPE = r"(?:float|int|uint)[2-4]x[2-4]"

        _MAT_VAR_RE = re.compile(rf"\b({_MAT_TYPE})\s+([A-Za-z_]\w*)\s*[\[=;]")
        mat_vars: set = set()
        for m in _MAT_VAR_RE.finditer(src):
            mat_vars.add(m.group(2))

        def _replace_mat_mul(text: str) -> str:
            mat_ctor_mul_right = re.compile(rf"\)\s*\*\s*({_MAT_TYPE})\(")
            changed = True
            while changed:
                changed = False
                m = mat_ctor_mul_right.search(text)
                if m:
                    close_before = m.start()
                    mat_open = m.end() - 1
                    mat_close = _find_matching_paren(text, mat_open)
                    if mat_close > 0:
                        lhs_end = close_before + 1
                        depth = 0
                        lhs_start = close_before
                        for j in range(close_before, -1, -1):
                            if text[j] == ")":
                                depth += 1
                            elif text[j] == "(":
                                depth -= 1
                                if depth == 0:
                                    lhs_start = j
                                    break
                        lhs = text[lhs_start:lhs_end]
                        rhs = text[m.start() + 1:mat_close + 1].strip().lstrip("* ").strip()
                        text = text[:lhs_start] + f"mul({lhs}, {rhs})" + text[mat_close + 1:]
                        changed = True

            mat_ctor_mul_left = re.compile(rf"({_MAT_TYPE})\(")
            changed = True
            while changed:
                changed = False
                for m in mat_ctor_mul_left.finditer(text):
                    ctor_start = m.start()
                    open_p = m.end() - 1
                    close_p = _find_matching_paren(text, open_p)
                    if close_p < 0:
                        continue
                    after = text[close_p + 1:].lstrip()
                    if after.startswith("*"):
                        rest = after[1:].lstrip()
                        end_idx = close_p + 1 + (len(after) - len(after.lstrip())) + 1 + (len(after[1:]) - len(rest))
                        paren_depth = 0
                        rhs_end = end_idx
                        for k in range(end_idx, len(text)):
                            c = text[k]
                            if c in "([{":
                                paren_depth += 1
                            elif c in ")]}":
                                if paren_depth == 0:
                                    rhs_end = k
                                    break
                                paren_depth -= 1
                            elif c in ",;" and paren_depth == 0:
                                rhs_end = k
                                break
                        else:
                            rhs_end = len(text)
                        lhs = text[ctor_start:close_p + 1]
                        rhs = text[end_idx:rhs_end].strip()
                        if rhs:
                            text = text[:ctor_start] + f"mul({lhs}, {rhs})" + text[rhs_end:]
                            changed = True
                            break
            if mat_vars:
                mat_name_re = re.compile(
                    r"\((" + "|".join(re.escape(v) for v in sorted(mat_vars, key=len, reverse=True))
                    + r")(?:\[\d+\])?\s*\*\s*"
                )
                changed2 = True
                while changed2:
                    changed2 = False
                    m2 = mat_name_re.search(text)
                    if m2:
                        paren_open = m2.start()
                        paren_close = _find_matching_paren(text, paren_open)
                        if paren_close > 0:
                            inner = text[paren_open + 1:paren_close]
                            star_pos = inner.find("*", len(m2.group(1)))
                            if star_pos > 0:
                                lhs = inner[:star_pos].strip()
                                rhs = inner[star_pos + 1:].strip()
                                text = text[:paren_open] + f"mul({lhs}, {rhs})" + text[paren_close + 1:]
                                changed2 = True
            return text

        src = _replace_mat_mul(src)
        return src, [], []

    @staticmethod
    def _rule_fix_assignment_parens(src: str) -> Tuple[str, List[str], List[str]]:
        src = re.sub(
            r"\(\s*([A-Za-z_]\w*(?:\.[xyzwrgba]{1,4})?)\s*([+\-*/]?=)\s*(.+?)\s*\);",
            r"\1 \2 \3;",
            src,
        )
        return src, [], []

    @staticmethod
    def _rule_clean_whitespace(src: str) -> Tuple[str, List[str], List[str]]:
        src = re.sub(r"\n{3,}", "\n\n", src)
        return src.strip(), [], []

    # ---- wrappers ---- #

    def _wrap_standalone(self, converted_body: str, warnings: List[str], unsupported: List[str]) -> str:
        main_block = _find_main_block(converted_body)
        if main_block is None:
            warnings.append("No main() found — wrapping entire source as standalone")
            return f"void main_standalone()\n{{\n{_indent(converted_body)}\n}}"

        prefix = converted_body[:main_block["start"]].strip()
        body = converted_body[main_block["body_start"]:main_block["body_end"]].strip()
        tail = converted_body[main_block["end"]:].strip()

        sampler_decls = _generate_sampler_declarations(prefix + "\n" + body)
        helper_fns = _generate_hlsl_helpers(prefix + "\n" + body)

        full_code = prefix + "\n" + body
        if "gl_FragCoord" in full_code:
            prefix = "static float4 gl_FragCoord;\n" + prefix

        assigned_vars = set(re.findall(r"\b(\w+)\s*=\s*", body))
        output_decl_re = re.compile(r"^(\s*)(float\d?\s+)(\w+)\s*;", re.MULTILINE)
        moved_decls: List[str] = []
        cleaned_prefix_lines: List[str] = []
        for pline in prefix.splitlines():
            m = output_decl_re.match(pline)
            if m and m.group(3) in assigned_vars:
                moved_decls.append(f"    {m.group(2)}{m.group(3)};")
            else:
                cleaned_prefix_lines.append(pline)
        prefix = "\n".join(cleaned_prefix_lines).strip()

        lines: List[str] = []
        if helper_fns:
            lines.extend(helper_fns)
            lines.append("")
        if prefix:
            lines.append(prefix)
        if sampler_decls:
            lines.append("")
            lines.extend(sampler_decls)
        lines.append("")
        lines.append("void main_standalone()")
        lines.append("{")
        for decl in moved_decls:
            lines.append(decl)
        for line in body.splitlines():
            lines.append(f"    {line}" if line.strip() else "")
        lines.append("}")
        if tail:
            lines.append("")
            lines.append(tail)
        return "\n".join(lines).strip()

    def _wrap_ue_custom(self, converted_body: str, warnings: List[str], unsupported: List[str]) -> str:
        main_block = _find_main_block(converted_body)
        if main_block is None:
            return converted_body

        prefix = converted_body[:main_block["start"]].strip()
        body = converted_body[main_block["body_start"]:main_block["body_end"]].strip()

        out_re = re.compile(r"^\s*float4?\s+(\w+)\s*;", re.MULTILINE)
        output_name = ""
        output_type = "float4"
        for m in out_re.finditer(prefix):
            output_name = m.group(1)

        if not output_name:
            assign_re = re.compile(r"\b(\w+)\s*=\s*")
            candidates = assign_re.findall(body)
            if candidates:
                output_name = candidates[-1]

        if not output_name:
            output_name = "fragColor"
            output_type = "float4"

        lines: List[str] = []
        lines.append(f"float4 {output_name};")
        lines.append(body)
        lines.append(f"return {output_name};")
        return "\n".join(lines).strip()


# ------------------------------------------------------------------ #
# Module helpers
# ------------------------------------------------------------------ #

def _normalize(text: str) -> str:
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n")


def _count_lines(text: str) -> int:
    return len(text.splitlines()) if text.strip() else 0


def _indent(text: str, spaces: int = 4) -> str:
    prefix = " " * spaces
    return "\n".join(f"{prefix}{line}" if line.strip() else "" for line in text.splitlines())


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


_HLSL_HELPERS = {
    "unpackHalf2x16_emu": (
        "float2 unpackHalf2x16_emu(uint u)\n"
        "{\n"
        "    return float2(f16tof32(u & 0xFFFFu), f16tof32(u >> 16u));\n"
        "}"
    ),
    "packHalf2x16_emu": (
        "uint packHalf2x16_emu(float2 v)\n"
        "{\n"
        "    return f32tof16(v.x) | (f32tof16(v.y) << 16u);\n"
        "}"
    ),
}


def _generate_hlsl_helpers(code: str) -> List[str]:
    """Return HLSL helper function definitions needed by *code*."""
    lines: List[str] = []
    for fn_name, fn_def in _HLSL_HELPERS.items():
        if fn_name + "(" in code:
            lines.append(fn_def)
    return lines


_TEXTURE_TYPE_RE = re.compile(r"\b(Texture2D|TextureCube|Texture3D|Texture2DArray)\s+(\w+)\s*[;=]")
_SAMPLER_USAGE_RE = re.compile(r"(\w+)Sampler")


def _generate_sampler_declarations(code: str) -> List[str]:
    """Auto-declare ``SamplerState`` for every Texture object referenced via ``XSampler``."""
    declared_textures: set = set()
    for m in _TEXTURE_TYPE_RE.finditer(code):
        declared_textures.add(m.group(2))

    needed: set = set()
    for m in _SAMPLER_USAGE_RE.finditer(code):
        tex_name = m.group(1)
        if tex_name in declared_textures:
            needed.add(tex_name)

    if not needed:
        return []

    return [f"SamplerState {name}Sampler;" for name in sorted(needed)]


_SWIZZLE_RE = re.compile(r"\.[xyzwrgba]{2,4}$")
_SCALAR_LITERAL_RE = re.compile(
    r"^[+-]?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?[fFuU]?$"
)


def _is_scalar_expr(expr: str) -> bool:
    """Return True unless *expr* clearly ends with a multi-component swizzle.

    In HLSL ``float3(scalar)`` is invalid (needs 3 args), but ``float3(vec3)``
    is fine.  So we only skip expansion when the argument clearly has 2+
    components (a ``.xy``/``.xyz``/``.xyzw`` swizzle at the end).
    """
    e = expr.strip()
    if e.endswith(")"):
        inner_end = e[:-1].rstrip()
        if _SWIZZLE_RE.search(inner_end):
            return False
    if _SWIZZLE_RE.search(e):
        return False
    return True


def _find_matching_paren(text: str, start: int) -> int:
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split_args(text: str) -> List[str]:
    args: List[str] = []
    current: List[str] = []
    depth = 0
    for ch in text:
        if ch == "," and depth == 0:
            arg = "".join(current).strip()
            if arg:
                args.append(arg)
            current = []
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        current.append(ch)
    tail = "".join(current).strip()
    if tail:
        args.append(tail)
    return args
