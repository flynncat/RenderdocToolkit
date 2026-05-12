"""GLSL code analyzer: parse shader into removable units and build dependency graph.

Produces a list of RemovalCandidate objects, each representing a code segment
that *might* be removable without affecting the final rendering.  The actual
verification is done by the VisualProbeSimplifier which hot-swaps each
candidate in RenderDoc and compares screenshots.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


@dataclass
class Statement:
    line_start: int
    line_end: int
    text: str
    assigns_to: List[str] = field(default_factory=list)
    reads_from: List[str] = field(default_factory=list)
    is_output_write: bool = False
    is_discard: bool = False
    kind: str = "assign"


@dataclass
class RemovalCandidate:
    kind: str
    label: str
    description: str
    line_range: Tuple[int, int]
    modified_source: str
    original_snippet: str = ""
    default_assignments: Dict[str, str] = field(default_factory=dict)


_IDENT_RE = re.compile(r"\b([A-Za-z_]\w*)\b")
_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_]\w*(?:\.[xyzwrgba]+)?)\s*(?:[+\-*/&|^]?=)\s*(.+)")
_OUTPUT_VARS = {"gl_FragColor", "gl_FragData", "gl_FragDepth"}
_TYPE_KEYWORDS = {
    "void", "float", "int", "uint", "bool", "half",
    "vec2", "vec3", "vec4", "ivec2", "ivec3", "ivec4",
    "uvec2", "uvec3", "uvec4", "bvec2", "bvec3", "bvec4",
    "mat2", "mat3", "mat4", "sampler2D", "sampler3D", "samplerCube",
    "sampler2DShadow", "samplerBuffer",
}
_FLOW_KEYWORDS = {"if", "else", "for", "while", "do", "switch", "case", "default", "return", "break", "continue", "discard"}
_BUILTIN_FUNCS = {
    "texture", "texture2D", "textureLod", "textureProj", "textureProjLod",
    "texelFetch", "texelFetchOffset", "textureGrad", "textureSize",
    "normalize", "length", "distance", "dot", "cross", "reflect", "refract",
    "min", "max", "clamp", "mix", "step", "smoothstep", "abs", "sign",
    "floor", "ceil", "fract", "mod", "pow", "exp", "exp2", "log", "log2",
    "sqrt", "inversesqrt", "sin", "cos", "tan", "asin", "acos", "atan",
    "dFdx", "dFdy", "fwidth", "transpose", "inverse", "determinant",
}
_GLSL_TYPE_DEFAULT = {
    "float": "0.0", "int": "0", "uint": "0u", "bool": "false",
    "vec2": "vec2(0.0)", "vec3": "vec3(0.0)", "vec4": "vec4(0.0)",
    "ivec2": "ivec2(0)", "ivec3": "ivec3(0)", "ivec4": "ivec4(0)",
    "uvec2": "uvec2(0u)", "uvec3": "uvec3(0u)", "uvec4": "uvec4(0u)",
    "bvec2": "bvec2(false)", "bvec3": "bvec3(false)", "bvec4": "bvec4(false)",
    "mat2": "mat2(1.0)", "mat3": "mat3(1.0)", "mat4": "mat4(1.0)",
}
_SAMPLER_TYPES = {
    "sampler2D", "sampler3D", "samplerCube", "sampler2DShadow",
    "samplerBuffer", "sampler2DArray", "samplerCubeShadow",
    "sampler2DMS", "isampler2D", "usampler2D",
}


class GlslCodeAnalyzer:
    """Parse a GLSL fragment shader into structured removable units."""

    def analyze(
        self,
        source: str,
        *,
        output_vars: Optional[Set[str]] = None,
    ) -> List[RemovalCandidate]:
        source = source.replace("\r\n", "\n").replace("\r", "\n")
        main_block = self._find_main_block(source)
        if main_block is None:
            return []

        preamble = source[:main_block["start"]]
        body = source[main_block["body_start"]:main_block["body_end"]]
        tail = source[main_block["end"]:]

        out_vars = set(output_vars or set())
        out_vars.update(self._find_output_vars(preamble, body))

        candidates: List[RemovalCandidate] = []

        candidates.extend(self._probe_preprocessor(source))
        candidates.extend(self._probe_uniforms(source, preamble, main_block, body, tail, out_vars))
        candidates.extend(self._probe_if_blocks(source, main_block, body, out_vars))
        candidates.extend(self._probe_statements(source, main_block, body, out_vars))

        return candidates

    def _find_output_vars(self, preamble: str, body: str) -> Set[str]:
        out_vars: Set[str] = set()
        for v in _OUTPUT_VARS:
            if v in body:
                out_vars.add(v)
        out_re = re.compile(r"^\s*(?:layout\s*\([^)]*\)\s*)?out\s+\w+\s+(\w+)\s*;", re.MULTILINE)
        for m in out_re.finditer(preamble):
            out_vars.add(m.group(1))
        return out_vars

    def _probe_preprocessor(self, source: str) -> List[RemovalCandidate]:
        """Generate candidates for preprocessor directive simplification.

        Handles: #define (unused), #extension (try remove), #if/#ifdef/#ifndef
        conditional blocks (try each branch, try remove entire block).
        """
        candidates: List[RemovalCandidate] = []
        lines = source.split("\n")

        candidates.extend(self._probe_unused_defines(source, lines))
        candidates.extend(self._probe_extensions(source, lines))
        candidates.extend(self._probe_preprocessor_conditionals(source, lines))

        return candidates

    def _probe_unused_defines(self, source: str, lines: List[str]) -> List[RemovalCandidate]:
        candidates: List[RemovalCandidate] = []
        define_re = re.compile(r"^\s*#\s*define\s+(\w+)")

        for i, line in enumerate(lines):
            m = define_re.match(line)
            if not m:
                continue
            macro_name = m.group(1)
            rest_before = "\n".join(lines[:i])
            rest_after = "\n".join(lines[i + 1:])
            rest = rest_before + "\n" + rest_after

            usage_re = re.compile(rf"\b{re.escape(macro_name)}\b")
            uses_in_rest = usage_re.findall(rest)
            in_other_defines = any(
                define_re.match(lines[j]) and macro_name in lines[j]
                for j in range(len(lines)) if j != i
            )

            if not uses_in_rest and not in_other_defines:
                modified = "\n".join(lines[:i] + lines[i + 1:])
                candidates.append(RemovalCandidate(
                    kind="preprocessor_define",
                    label=f"#define {macro_name}",
                    description=f"移除未使用的宏定义 #define {macro_name}",
                    line_range=(i + 1, i + 1),
                    modified_source=modified,
                    original_snippet=line.strip()[:200],
                ))
            else:
                value_match = re.match(r"^\s*#\s*define\s+\w+\s*(?:\([^)]*\))?\s*(.*)", line)
                if value_match and value_match.group(1).strip():
                    macro_value = value_match.group(1).strip()
                    expanded = usage_re.sub(macro_value, rest_after)
                    modified = rest_before + "\n" + expanded
                    candidates.append(RemovalCandidate(
                        kind="preprocessor_define_inline",
                        label=f"inline #define {macro_name}",
                        description=f"内联展开宏 {macro_name} = {macro_value[:50]}",
                        line_range=(i + 1, i + 1),
                        modified_source=modified,
                        original_snippet=line.strip()[:200],
                    ))

        return candidates

    def _probe_extensions(self, source: str, lines: List[str]) -> List[RemovalCandidate]:
        candidates: List[RemovalCandidate] = []
        ext_re = re.compile(r"^\s*#\s*extension\s+(\w+)\s*:\s*(\w+)")

        for i, line in enumerate(lines):
            m = ext_re.match(line)
            if not m:
                continue
            ext_name = m.group(1)
            modified = "\n".join(lines[:i] + lines[i + 1:])
            candidates.append(RemovalCandidate(
                kind="preprocessor_extension",
                label=f"#extension {ext_name}",
                description=f"移除扩展声明 #extension {ext_name}",
                line_range=(i + 1, i + 1),
                modified_source=modified,
                original_snippet=line.strip()[:200],
            ))

        return candidates

    def _probe_preprocessor_conditionals(self, source: str, lines: List[str]) -> List[RemovalCandidate]:
        """Probe #if/#ifdef/#ifndef/#elif/#else/#endif conditional blocks.

        For each top-level conditional group, generate candidates to:
        - Keep only the #if branch (flatten)
        - Keep only the #else branch (flatten)
        - Remove entire conditional block
        """
        candidates: List[RemovalCandidate] = []
        cond_re = re.compile(r"^\s*#\s*(if|ifdef|ifndef)\b")

        i = 0
        while i < len(lines):
            m = cond_re.match(lines[i])
            if not m:
                i += 1
                continue

            block = self._parse_pp_conditional_block(lines, i)
            if block is None:
                i += 1
                continue

            cond_start = block["start"]
            cond_end = block["end"]
            branches = block["branches"]
            directive_text = lines[cond_start].strip()

            line_start = cond_start + 1
            line_end = cond_end + 1

            if len(branches) >= 1:
                if_branch_lines = branches[0]["body"]
                modified_lines = lines[:cond_start] + if_branch_lines + lines[cond_end + 1:]
                modified = "\n".join(modified_lines)
                branch_desc = directive_text[:60]
                candidates.append(RemovalCandidate(
                    kind="preprocessor_keep_if",
                    label=f"pp-keep-if L{line_start}-{line_end}",
                    description=f"保留 {branch_desc} 分支，移除其他分支和预处理指令",
                    line_range=(line_start, line_end),
                    modified_source=modified,
                    original_snippet="\n".join(lines[cond_start:cond_end + 1])[:300],
                ))

            if len(branches) >= 2:
                else_branch = branches[-1]
                else_branch_lines = else_branch["body"]
                modified_lines = lines[:cond_start] + else_branch_lines + lines[cond_end + 1:]
                modified = "\n".join(modified_lines)
                candidates.append(RemovalCandidate(
                    kind="preprocessor_keep_else",
                    label=f"pp-keep-else L{line_start}-{line_end}",
                    description=f"保留 #else 分支，移除 {directive_text[:40]} 分支和预处理指令",
                    line_range=(line_start, line_end),
                    modified_source=modified,
                    original_snippet="\n".join(lines[cond_start:cond_end + 1])[:300],
                ))

            all_body_lines: List[str] = []
            for b in branches:
                all_body_lines.extend(b["body"])
            content_has_substance = any(
                ln.strip() and not ln.strip().startswith("//")
                for ln in all_body_lines
            )

            removed_lines = lines[:cond_start] + lines[cond_end + 1:]
            removed_modified = "\n".join(removed_lines)
            candidates.append(RemovalCandidate(
                kind="preprocessor_remove_block",
                label=f"pp-remove L{line_start}-{line_end}",
                description=f"移除整个预处理条件块 {directive_text[:60]} ({line_end - line_start + 1} 行)",
                line_range=(line_start, line_end),
                modified_source=removed_modified,
                original_snippet="\n".join(lines[cond_start:cond_end + 1])[:300],
            ))

            i = cond_end + 1

        return candidates

    @staticmethod
    def _parse_pp_conditional_block(lines: List[str], start: int) -> Optional[Dict]:
        """Parse a preprocessor conditional block starting at *start*.

        Returns dict with 'start', 'end', 'branches' where each branch is
        {'directive': str, 'body': list[str]}.
        """
        depth = 0
        branches: List[Dict] = []
        current_body: List[str] = []
        current_directive = lines[start].strip()
        endif_line = -1

        for i in range(start, len(lines)):
            stripped = lines[i].strip()

            if i == start:
                depth = 1
                current_directive = stripped
                current_body = []
                continue

            if re.match(r"^\s*#\s*(if|ifdef|ifndef)\b", stripped):
                depth += 1
                current_body.append(lines[i])
                continue

            if re.match(r"^\s*#\s*endif\b", stripped):
                depth -= 1
                if depth == 0:
                    branches.append({"directive": current_directive, "body": current_body})
                    endif_line = i
                    break
                current_body.append(lines[i])
                continue

            if depth == 1 and re.match(r"^\s*#\s*(else|elif)\b", stripped):
                branches.append({"directive": current_directive, "body": current_body})
                current_directive = stripped
                current_body = []
                continue

            current_body.append(lines[i])

        if endif_line < 0:
            return None

        return {"start": start, "end": endif_line, "branches": branches}

    def _probe_uniforms(
        self, source: str, preamble: str, main_block: dict, body: str, tail: str, out_vars: Set[str],
    ) -> List[RemovalCandidate]:
        candidates = []
        uniform_re = re.compile(r"^\s*uniform\s+(\w+)\s+(\w+)(\s*\[.*?\])?\s*;", re.MULTILINE)

        for m in uniform_re.finditer(preamble):
            utype, uname = m.group(1), m.group(2)
            is_array = bool(m.group(3))

            if uname not in body:
                continue

            if utype in _SAMPLER_TYPES:
                candidates.extend(
                    self._probe_sampler_usage(source, preamble, main_block, body, utype, uname)
                )
                continue

            default_val = _GLSL_TYPE_DEFAULT.get(utype)
            if default_val is None or is_array:
                continue

            modified_body = re.sub(
                rf"\b{re.escape(uname)}\b",
                default_val,
                body,
            )
            modified_source = (
                preamble
                + source[main_block["start"]:main_block["body_start"]]
                + modified_body
                + source[main_block["body_end"]:]
            )

            line_start = preamble[:m.start()].count("\n") + 1
            candidates.append(RemovalCandidate(
                kind="uniform",
                label=f"uniform {utype} {uname}",
                description=f"将 uniform {uname} 替换为默认值 {default_val}",
                line_range=(line_start, line_start),
                modified_source=modified_source,
                original_snippet=m.group(0).strip(),
                default_assignments={uname: default_val},
            ))

        return candidates

    def _probe_sampler_usage(
        self, source: str, preamble: str, main_block: dict, body: str,
        sampler_type: str, sampler_name: str,
    ) -> List[RemovalCandidate]:
        """For sampler uniforms, generate candidates that replace texture fetch results with defaults."""
        candidates = []
        body_offset = main_block["body_start"]
        tex_call_re = re.compile(
            rf"(\w+)\s*=\s*(?:texture|texture2D|textureLod|texelFetch|textureProj)\s*\(\s*{re.escape(sampler_name)}\b[^;]*;",
        )

        for m in tex_call_re.finditer(body):
            var_name = m.group(1).strip()
            declared_type = self._infer_variable_type(source, var_name)
            default_val = _GLSL_TYPE_DEFAULT.get(declared_type, "vec4(0.0)")

            replacement = f"{var_name} = {default_val};"
            modified_body = body[:m.start()] + replacement + body[m.end():]
            modified_source = (
                source[:body_offset]
                + modified_body
                + source[main_block["body_end"]:]
            )

            abs_start = body_offset + m.start()
            line_start = source[:abs_start].count("\n") + 1

            candidates.append(RemovalCandidate(
                kind="sampler_fetch",
                label=f"texture({sampler_name}) → {var_name}",
                description=f"将 texture({sampler_name}) 的结果 {var_name} 替换为 {default_val}",
                line_range=(line_start, line_start),
                modified_source=modified_source,
                original_snippet=m.group(0).strip()[:200],
                default_assignments={var_name: default_val},
            ))

        return candidates

    def _probe_if_blocks(
        self, source: str, main_block: dict, body: str, out_vars: Set[str],
    ) -> List[RemovalCandidate]:
        candidates = []
        body_offset = main_block["body_start"]
        if_re = re.compile(r"\bif\s*\(")

        for m in if_re.finditer(body):
            brace_pos = body.find("{", m.end())
            if brace_pos < 0:
                continue
            if_end = self._find_matching_brace(body, brace_pos)
            if if_end is None:
                continue

            if_body_text = body[brace_pos + 1:if_end - 1]
            condition_text = body[m.end():brace_pos].strip().rstrip("{").strip()
            if condition_text.startswith("(") and condition_text.endswith(")"):
                condition_text = condition_text[1:-1].strip()

            has_else = False
            else_body_text = ""
            full_end = if_end
            rest = body[if_end:].lstrip()
            if rest.startswith("else"):
                has_else = True
                else_brace = body.find("{", if_end)
                if else_brace >= 0:
                    else_end = self._find_matching_brace(body, else_brace)
                    if else_end is not None:
                        else_body_text = body[else_brace + 1:else_end - 1]
                        full_end = else_end
                    else:
                        has_else = False
                else:
                    has_else = False

            block_text = body[m.start():full_end]
            has_output = any(v in block_text for v in out_vars)
            has_discard = "discard" in block_text

            abs_start = body_offset + m.start()
            line_start = source[:abs_start].count("\n") + 1
            line_end = source[:body_offset + full_end].count("\n") + 1

            if not has_output and not has_discard:
                modified_body = body[:m.start()] + body[full_end:]
                modified_source = (
                    source[:body_offset]
                    + modified_body
                    + source[main_block["body_end"]:]
                )
                candidates.append(RemovalCandidate(
                    kind="if_block",
                    label=f"if block L{line_start}-{line_end}",
                    description=f"移除 if/else 代码块 ({line_end - line_start + 1} 行)",
                    line_range=(line_start, line_end),
                    modified_source=modified_source,
                    original_snippet=block_text[:200],
                ))

            if has_else:
                keep_if_body = body[:m.start()] + if_body_text + "\n" + body[full_end:]
                keep_if_source = (
                    source[:body_offset]
                    + keep_if_body
                    + source[main_block["body_end"]:]
                )
                candidates.append(RemovalCandidate(
                    kind="if_branch_keep_if",
                    label=f"if-only L{line_start}-{line_end}",
                    description=f"保留 if 分支，移除 else 分支 (条件: {condition_text[:60]})",
                    line_range=(line_start, line_end),
                    modified_source=keep_if_source,
                    original_snippet=block_text[:200],
                ))

                keep_else_body = body[:m.start()] + else_body_text + "\n" + body[full_end:]
                keep_else_source = (
                    source[:body_offset]
                    + keep_else_body
                    + source[main_block["body_end"]:]
                )
                candidates.append(RemovalCandidate(
                    kind="if_branch_keep_else",
                    label=f"else-only L{line_start}-{line_end}",
                    description=f"保留 else 分支，移除 if 分支 (条件: {condition_text[:60]})",
                    line_range=(line_start, line_end),
                    modified_source=keep_else_source,
                    original_snippet=block_text[:200],
                ))

        return candidates

    def _probe_statements(
        self, source: str, main_block: dict, body: str, out_vars: Set[str],
    ) -> List[RemovalCandidate]:
        """Generate safe statement-level probe candidates.

        Strategy: never delete a statement entirely. Instead, replace the RHS
        with a type-appropriate default value. This preserves variable
        declarations and prevents 'undefined variable' errors.
        """
        candidates = []
        body_offset = main_block["body_start"]

        statements = self._parse_statements(body)

        for stmt in statements:
            if stmt.is_output_write or stmt.is_discard:
                continue
            if stmt.kind in ("return", "break", "continue"):
                continue
            if not stmt.assigns_to:
                continue

            stmt_abs_start = body_offset + stmt.line_start
            stmt_abs_end = body_offset + stmt.line_end
            stripped = stmt.text.strip()

            replacement = self._build_default_replacement(source, stripped, stmt.assigns_to)
            if replacement is None:
                continue

            modified_source = (
                source[:stmt_abs_start]
                + "  " + replacement + "\n"
                + source[stmt_abs_end:]
            )

            line_start = source[:stmt_abs_start].count("\n") + 1

            candidates.append(RemovalCandidate(
                kind="statement_default",
                label=f"stmt→default L{line_start}",
                description=f"将语句替换为默认值: {stripped[:80]}",
                line_range=(line_start, line_start),
                modified_source=modified_source,
                original_snippet=stripped[:200],
                default_assignments={v: "default" for v in stmt.assigns_to},
            ))

        return candidates

    def _build_default_replacement(self, source: str, stmt_text: str, assigns_to: List[str]) -> Optional[str]:
        """Build a safe replacement statement preserving variable declarations."""
        decl_re = re.compile(r"^\s*(\w+)\s+(\w+)\s*=")
        dm = decl_re.match(stmt_text)
        if dm and dm.group(1) in _TYPE_KEYWORDS:
            var_type = dm.group(1)
            var_name = dm.group(2)
            default_val = _GLSL_TYPE_DEFAULT.get(var_type, "0.0")
            return f"{var_type} {var_name} = {default_val};"

        if assigns_to:
            var = assigns_to[0]
            base_var = var.split(".")[0]
            declared_type = self._infer_variable_type(source, base_var)
            default_val = _GLSL_TYPE_DEFAULT.get(declared_type, "0.0")
            if "." in var:
                return f"{var} = {default_val};"
            else:
                return f"{var} = {default_val};"

        return None

    def _parse_statements(self, body: str) -> List[Statement]:
        statements: List[Statement] = []
        depth = 0
        current_start = 0
        i = 0

        while i < len(body):
            ch = body[i]

            if ch == "{":
                depth += 1
                i += 1
                continue
            if ch == "}":
                depth -= 1
                i += 1
                continue

            if depth == 0 and ch == ";":
                stmt_text = body[current_start:i + 1]
                stmt = self._classify_statement(stmt_text, current_start, i + 1)
                if stmt is not None:
                    statements.append(stmt)
                current_start = i + 1
                i += 1
                continue

            i += 1

        return statements

    def _classify_statement(self, text: str, char_start: int, char_end: int) -> Optional[Statement]:
        stripped = text.strip()
        if not stripped or stripped == ";":
            return None

        stmt = Statement(
            line_start=char_start,
            line_end=char_end,
            text=text,
        )

        if stripped.startswith("discard"):
            stmt.is_discard = True
            stmt.kind = "discard"
            return stmt

        for outvar in _OUTPUT_VARS:
            if re.search(rf"\b{re.escape(outvar)}\b\s*(?:\.\w+)?\s*(?:[+\-*/&|^]?=)", stripped):
                stmt.is_output_write = True
                stmt.kind = "output"
                break

        out_re = re.compile(r"^\s*(?:layout\s*\([^)]*\)\s*)?out\s+\w+\s+(\w+)\s*;")
        if out_re.match(stripped):
            return None

        m = _ASSIGN_RE.match(stripped)
        if m:
            lhs = m.group(1)
            rhs = m.group(2)
            stmt.assigns_to = [lhs]
            idents = _IDENT_RE.findall(rhs)
            stmt.reads_from = [x for x in idents if x not in _TYPE_KEYWORDS and x not in _FLOW_KEYWORDS and x not in _BUILTIN_FUNCS]

        decl_re = re.compile(r"^\s*(\w+)\s+(\w+)\s*=\s*(.+);")
        dm = decl_re.match(stripped)
        if dm and dm.group(1) in _TYPE_KEYWORDS:
            stmt.assigns_to = [dm.group(2)]
            idents = _IDENT_RE.findall(dm.group(3))
            stmt.reads_from = [x for x in idents if x not in _TYPE_KEYWORDS and x not in _FLOW_KEYWORDS and x not in _BUILTIN_FUNCS]

        return stmt

    def _build_dependency_graph(self, statements: List[Statement], output_vars: Set[str]) -> Set[str]:
        """BFS from output vars to find all transitively needed variables."""
        write_map: Dict[str, List[Statement]] = {}
        for stmt in statements:
            for v in stmt.assigns_to:
                base = v.split(".")[0]
                write_map.setdefault(base, []).append(stmt)

        needed: Set[str] = set()
        frontier = list(output_vars)
        for stmt in statements:
            if stmt.is_output_write:
                frontier.extend(stmt.reads_from)
            if stmt.is_discard:
                frontier.extend(stmt.reads_from)

        visited: Set[str] = set()
        while frontier:
            var = frontier.pop()
            base = var.split(".")[0]
            if base in visited:
                continue
            visited.add(base)
            needed.add(base)
            for stmt in write_map.get(base, []):
                for dep in stmt.reads_from:
                    dep_base = dep.split(".")[0]
                    if dep_base not in visited:
                        frontier.append(dep_base)

        return needed

    def _infer_variable_type(self, source: str, var_name: str) -> str:
        decl_re = re.compile(rf"\b(\w+)\s+{re.escape(var_name)}\s*[=;,\[]")
        for m in decl_re.finditer(source):
            t = m.group(1)
            if t in _TYPE_KEYWORDS:
                return t
        return "float"

    @staticmethod
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

    @staticmethod
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
