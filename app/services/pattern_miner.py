"""Pattern mining for GLSL→HLSL conversion.

Scans previous verify-session results and the existing converter code to
extract recurring transformation patterns, failure modes, and successful
strategies.  The output is a structured report that feeds into the
deterministic rule engine (Phase 4.2).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class PatternEntry:
    """One discovered conversion pattern."""
    category: str          # e.g. "type_map", "function_rename", "texture_call", "builtin_var"
    glsl_pattern: str      # regex or literal GLSL token
    hlsl_replacement: str  # corresponding HLSL replacement
    confidence: float = 1.0
    source: str = ""       # where the pattern was discovered
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "glsl_pattern": self.glsl_pattern,
            "hlsl_replacement": self.hlsl_replacement,
            "confidence": self.confidence,
            "source": self.source,
            "notes": self.notes,
        }


@dataclass
class MiningReport:
    patterns: List[PatternEntry] = field(default_factory=list)
    failure_signatures: List[Dict[str, Any]] = field(default_factory=list)
    session_count: int = 0
    success_count: int = 0
    failure_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_count": self.session_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "pattern_count": len(self.patterns),
            "patterns": [p.to_dict() for p in self.patterns],
            "failure_signatures": self.failure_signatures[:50],
        }


# ------------------------------------------------------------------ #
# Static patterns extracted from FragmentGlslToUe426CustomHlslService
# ------------------------------------------------------------------ #

_STATIC_TYPE_MAPS: List[Tuple[str, str]] = [
    ("float", "float"),
    ("int", "int"),
    ("uint", "uint"),
    ("bool", "bool"),
    ("vec2", "float2"),
    ("vec3", "float3"),
    ("vec4", "float4"),
    ("uvec2", "uint2"),
    ("uvec3", "uint3"),
    ("uvec4", "uint4"),
    ("ivec2", "int2"),
    ("ivec3", "int3"),
    ("ivec4", "int4"),
    ("bvec2", "bool2"),
    ("bvec3", "bool3"),
    ("bvec4", "bool4"),
    ("mat2", "float2x2"),
    ("mat3", "float3x3"),
    ("mat4", "float4x4"),
    ("sampler2D", "Texture2D"),
]

_STATIC_FUNCTION_RENAMES: List[Tuple[str, str]] = [
    ("dFdx", "ddx"),
    ("dFdy", "ddy"),
    ("fract", "frac"),
    ("mod", "fmod"),
    ("mix", "lerp"),
    ("floatBitsToUint", "asuint"),
    ("uintBitsToFloat", "asfloat"),
]

_STATIC_TEXTURE_PATTERNS: List[Tuple[str, str, str]] = [
    ("texture(S, UV)", r"texture\(\s*(\w+)\s*,\s*(.+?)\s*\)", r"\1.Sample(\1Sampler, \2)"),
    ("textureLod(S, UV, L)", r"textureLod\(\s*(\w+)\s*,\s*(.+?)\s*,\s*(.+?)\s*\)", r"\1.SampleLevel(\1Sampler, \2, \3)"),
    ("textureGrad(S, UV, DDX, DDY)", r"textureGrad\(\s*(\w+)\s*,\s*(.+?)\s*,\s*(.+?)\s*,\s*(.+?)\s*\)", r"\1.SampleGrad(\1Sampler, \2, \3, \4)"),
]

_STATIC_CONSTRUCTOR_SPLATS: List[Tuple[str, int]] = [
    ("float2", 2),
    ("float3", 3),
    ("float4", 4),
    ("int2", 2),
    ("int3", 3),
    ("int4", 4),
]

_STATIC_BUILTIN_VARS: List[Tuple[str, str, str]] = [
    ("gl_FragCoord", "SvPosition", "float4"),
    ("gl_FrontFacing", "SV_IsFrontFace", "bool"),
]

_STATIC_PREAMBLE_STRIPS: List[str] = [
    r"#version\s+\d+.*",
    r"precision\s+\w+\s+\w+\s*;",
    r"#extension\s+\w+.*",
]


class PatternMiner:
    """Mine GLSL→HLSL patterns from static knowledge and session logs."""

    def mine_static_patterns(self) -> List[PatternEntry]:
        """Extract patterns from the known converter logic."""
        patterns: List[PatternEntry] = []

        for glsl, hlsl in _STATIC_TYPE_MAPS:
            patterns.append(PatternEntry(
                category="type_map",
                glsl_pattern=rf"\b{re.escape(glsl)}\b",
                hlsl_replacement=hlsl,
                source="static:type_map",
            ))

        for glsl_fn, hlsl_fn in _STATIC_FUNCTION_RENAMES:
            patterns.append(PatternEntry(
                category="function_rename",
                glsl_pattern=rf"\b{re.escape(glsl_fn)}\b",
                hlsl_replacement=hlsl_fn,
                source="static:function_rename",
            ))

        for desc, pat, repl in _STATIC_TEXTURE_PATTERNS:
            patterns.append(PatternEntry(
                category="texture_call",
                glsl_pattern=pat,
                hlsl_replacement=repl,
                source="static:texture_call",
                notes=desc,
            ))

        for ctor, width in _STATIC_CONSTRUCTOR_SPLATS:
            patterns.append(PatternEntry(
                category="constructor_splat",
                glsl_pattern=rf"\b{ctor}\(\s*([^,)]+)\s*\)",
                hlsl_replacement=f"{ctor}(" + ", ".join([r"\1"] * width) + ")",
                source="static:constructor_splat",
                notes=f"{ctor}(x) → {ctor}({', '.join(['x'] * width)})",
            ))

        for glsl_var, hlsl_semantic, var_type in _STATIC_BUILTIN_VARS:
            patterns.append(PatternEntry(
                category="builtin_var",
                glsl_pattern=rf"\b{re.escape(glsl_var)}\b",
                hlsl_replacement=hlsl_semantic,
                source="static:builtin_var",
                notes=f"{glsl_var} ({var_type}) → {hlsl_semantic}",
            ))

        for strip in _STATIC_PREAMBLE_STRIPS:
            patterns.append(PatternEntry(
                category="preamble_strip",
                glsl_pattern=strip,
                hlsl_replacement="",
                source="static:preamble_strip",
            ))

        patterns.append(PatternEntry(
            category="qualifier_strip",
            glsl_pattern=r"\b(uniform|varying|in|out)\s+",
            hlsl_replacement="",
            source="static:qualifier_strip",
            notes="Remove GLSL qualifiers not used in HLSL function body",
        ))

        patterns.append(PatternEntry(
            category="layout_strip",
            glsl_pattern=r"layout\s*\([^)]*\)\s*",
            hlsl_replacement="",
            source="static:layout_strip",
        ))

        return patterns

    def mine_session_logs(self, session_root: Path) -> Tuple[List[PatternEntry], List[Dict[str, Any]]]:
        """Scan verify-session directories for conversion results."""
        extra_patterns: List[PatternEntry] = []
        failures: List[Dict[str, Any]] = []

        if not session_root.exists():
            return extra_patterns, failures

        for result_file in sorted(session_root.rglob("hlsl_verify_result.json")):
            try:
                data = json.loads(result_file.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue

            if data.get("success"):
                method = data.get("method_used", "")
                if method:
                    extra_patterns.append(PatternEntry(
                        category="session_method",
                        glsl_pattern="*",
                        hlsl_replacement=method,
                        source=f"session:{result_file.parent.name}",
                        notes=f"Method '{method}' succeeded",
                    ))
            else:
                for ilog in data.get("iterations", []):
                    if ilog.get("compile_errors"):
                        failures.append({
                            "session": result_file.parent.name,
                            "method": ilog.get("method"),
                            "errors": ilog["compile_errors"][:500],
                        })

        return extra_patterns, failures

    def run(self, session_root: Optional[Path] = None) -> MiningReport:
        """Run full pattern mining and produce a report."""
        static = self.mine_static_patterns()

        extra: List[PatternEntry] = []
        failures: List[Dict[str, Any]] = []
        sessions = 0
        successes = 0
        fail_count = 0

        if session_root and session_root.exists():
            for result_file in sorted(session_root.rglob("hlsl_verify_result.json")):
                sessions += 1
                try:
                    data = json.loads(result_file.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    continue
                if data.get("success"):
                    successes += 1
                else:
                    fail_count += 1

            extra, failures = self.mine_session_logs(session_root)

        all_patterns = static + extra
        return MiningReport(
            patterns=all_patterns,
            failure_signatures=failures,
            session_count=sessions,
            success_count=successes,
            failure_count=fail_count,
        )
