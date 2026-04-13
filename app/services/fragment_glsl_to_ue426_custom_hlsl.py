from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class ShaderInputSpec:
    name: str
    glsl_type: str
    ue_input_type: str
    category: str
    default_value: Any = None
    source_hint: str = ""


class FragmentGlslToUe426CustomHlslService:
    INTERNAL_MATRIX_EXPRESSIONS = {
        "builtin:transform_matrix:world_view_projection": "ResolvedView.TranslatedWorldToClip",
        "builtin:transform_matrix:world_view": "ResolvedView.TranslatedWorldToView",
        "builtin:transform_matrix:projection": "ResolvedView.ViewToClip",
        "builtin:transform_matrix:view_projection": "ResolvedView.TranslatedWorldToClip",
        "builtin:transform_matrix:world": "GetPrimitiveData(Parameters.PrimitiveId).LocalToWorld",
        "builtin:transform_matrix:inverse_world": "GetPrimitiveData(Parameters.PrimitiveId).WorldToLocal",
        "builtin:transform_matrix:view_matrix": "ResolvedView.TranslatedWorldToView",
    }
    BUILTIN_INTERNAL_MATRIX_HINTS = {
        "worldviewprojection": "builtin:transform_matrix:world_view_projection",
        "worldview": "builtin:transform_matrix:world_view",
        "projection": "builtin:transform_matrix:projection",
        "viewprojection": "builtin:transform_matrix:view_projection",
        "world": "builtin:transform_matrix:world",
        "inverseworld": "builtin:transform_matrix:inverse_world",
        "viewmatrix": "builtin:transform_matrix:view_matrix",
    }
    BUILTIN_EXTERNAL_MATRIX_HINTS = {
        "textransform0": "builtin:external_matrix:tex_transform0",
        "lightviewproj": "builtin:external_matrix:light_view_proj",
        "invlightviewproj": "builtin:external_matrix:inv_light_view_proj",
    }
    PACKED_HALF_HELPERS_HLSL = """
uint _rdt_f32_to_u32(float value)
{
    return asuint(value);
}

uint2 _rdt_f32_to_u32(float2 value)
{
    return asuint(value);
}

uint3 _rdt_f32_to_u32(float3 value)
{
    return asuint(value);
}

uint4 _rdt_f32_to_u32(float4 value)
{
    return asuint(value);
}

float _rdt_u32_to_f32(uint value)
{
    return asfloat(value);
}

float2 _rdt_u32_to_f32(uint2 value)
{
    return asfloat(value);
}

float3 _rdt_u32_to_f32(uint3 value)
{
    return asfloat(value);
}

float4 _rdt_u32_to_f32(uint4 value)
{
    return asfloat(value);
}

float _rdt_f16_to_f32(uint val)
{
    uint sign = (val & 0x8000u) << 16;
    int exponent = int((val & 0x7C00u) >> 10);
    uint mantissa = val & 0x03FFu;
    float f32 = 0.0;
    if (exponent == 0)
    {
        if (mantissa != 0u)
        {
            const float scale = 1.0 / 16777216.0;
            f32 = scale * mantissa;
        }
    }
    else if (exponent == 31)
    {
        return asfloat(sign | 0x7F800000u | (mantissa << 13));
    }
    else
    {
        exponent -= 15;
        float scale = exponent < 0 ? (1.0 / float(1 << abs(exponent))) : float(1 << exponent);
        float decimal = 1.0 + float(mantissa) / 1024.0;
        f32 = scale * decimal;
    }
    if (sign != 0u)
    {
        f32 = -f32;
    }
    return f32;
}

float2 unpackHalf2x16_emu(uint u)
{
    uint y = (u >> 16);
    uint x = u & 0xFFFFu;
    return float2(_rdt_f16_to_f32(x), _rdt_f16_to_f32(y));
}
""".strip()

    GLSL_TYPE_TO_HLSL = {
        "float": "float",
        "int": "int",
        "uint": "uint",
        "bool": "bool",
        "vec2": "float2",
        "vec3": "float3",
        "vec4": "float4",
        "uvec2": "uint2",
        "uvec3": "uint3",
        "uvec4": "uint4",
        "ivec2": "int2",
        "ivec3": "int3",
        "ivec4": "int4",
        "bvec2": "bool2",
        "bvec3": "bool3",
        "bvec4": "bool4",
        "mat2": "float2x2",
        "mat3": "float3x3",
        "mat4": "float4x4",
        "sampler2D": "Texture2D",
    }

    GLSL_TYPE_TO_UE_INPUT = {
        "float": "Scalar",
        "int": "Scalar",
        "uint": "Scalar",
        "bool": "Scalar",
        "vec2": "Float2",
        "vec3": "Float3",
        "vec4": "Float4",
        "uvec2": "Float2",
        "uvec3": "Float3",
        "uvec4": "Float4",
        "ivec2": "Float2",
        "ivec3": "Float3",
        "ivec4": "Float4",
        "bvec2": "Float2",
        "bvec3": "Float3",
        "bvec4": "Float4",
        "mat2": "Unsupported(Matrix)",
        "mat3": "Unsupported(Matrix)",
        "mat4": "Unsupported(Matrix)",
        "sampler2D": "TextureObject",
    }

    OUTPUT_TYPE_TO_UE = {
        "float": "CMOT Float1",
        "vec2": "CMOT Float2",
        "vec3": "CMOT Float3",
        "vec4": "CMOT Float4",
    }

    def convert(
        self,
        *,
        fragment_source: str,
        shader_params_json: str = "",
        vertex_source: str = "",
    ) -> dict[str, Any]:
        warnings: list[str] = []
        unsupported: list[str] = []
        notes: list[str] = []

        fragment_source = self._normalize_source(fragment_source)
        vertex_source = self._normalize_source(vertex_source)
        params_lookup = self._build_params_lookup(shader_params_json, warnings)
        vertex_hints = self._build_vertex_hints(vertex_source)

        if re.search(r"\bdiscard\b", fragment_source):
            unsupported.append("检测到 discard；当前版本不会自动改写为 UE Custom 节点等价逻辑。")
        for builtin_name in ("gl_FragCoord", "gl_FragDepth", "gl_FrontFacing"):
            if re.search(rf"\b{builtin_name}\b", fragment_source):
                unsupported.append(f"检测到内建变量 {builtin_name}；当前版本未提供自动映射。")

        cleaned = self._strip_renderdoc_header(fragment_source)
        main_range = self._find_main_block(cleaned)
        if main_range is None:
            raise ValueError("未在 fragment shader 中找到 void main()。")

        prefix = cleaned[: main_range["start"]].strip()
        prefix, helper_markers = self._extract_known_helper_blocks(prefix)
        body = cleaned[main_range["body_start"] : main_range["body_end"]].strip()
        suffix = cleaned[main_range["end"] :].strip()

        if suffix:
            unsupported.append("main() 之后仍有额外 GLSL 代码；当前版本不会自动合并。")
        if "{" in prefix or "}" in prefix:
            unsupported.append("main() 之前存在函数或复杂代码块；当前版本只稳定支持声明式全局区。")

        declarations = self._parse_global_declarations(prefix, warnings, unsupported)
        output_decl = self._pick_output_declaration(declarations["out"], body, warnings, unsupported)
        if output_decl is None:
            raise ValueError("未找到可用的 fragment 输出，当前版本至少需要单个 vec4/vec3/vec2/float 输出。")

        inputs: list[ShaderInputSpec] = []
        omitted_inputs: list[str] = []
        used_input_names: list[str] = []
        for category in ("uniform", "in"):
            for item in declarations[category]:
                if not self._is_identifier_used(body, item["name"]):
                    omitted_inputs.append(item["name"])
                    continue
                input_spec = self._make_input_spec(
                    item=item,
                    category=category,
                    params_lookup=params_lookup,
                    vertex_hints=vertex_hints,
                    unsupported=unsupported,
                )
                inputs.append(input_spec)
                used_input_names.append(item["name"])

        for name in omitted_inputs:
            notes.append(f"已省略未在 fragment main() 中使用的输入: {name}")

        internal_matrix_inputs = [
            item.name for item in inputs if str(item.source_hint).startswith("builtin:transform_matrix:")
        ]
        if internal_matrix_inputs:
            notes.append(
                f"已识别 UE 内建变换矩阵语义: {', '.join(internal_matrix_inputs)}；它们会直接改写为 UE 内部表达，不再作为 Custom 输入。"
            )
        inputs = [
            item for item in inputs
            if not str(item.source_hint).startswith("builtin:transform_matrix:")
        ]

        const_locals = [
            self._translate_statement(item["statement"], unsupported)
            for item in declarations["const"]
        ]
        translated_body = self._translate_body(body, unsupported)
        internal_matrix_decls = self._build_internal_matrix_decls(declarations["uniform"], body)
        output_hlsl_type = self.GLSL_TYPE_TO_HLSL.get(output_decl["type"], output_decl["type"])

        if not re.search(rf"\b{re.escape(output_decl['name'])}\s*=", translated_body):
            warnings.append(f"未检测到对输出变量 {output_decl['name']} 的赋值，返回值可能为空。")

        code_lines: list[str] = []
        code_lines.extend(self._build_support_helpers_hlsl(helper_markers, translated_body))
        code_lines.extend(line for line in const_locals if line)
        code_lines.extend(internal_matrix_decls)
        code_lines.append(f"{output_hlsl_type} {output_decl['name']};")
        code_lines.append(translated_body)
        code_lines.append(f"return {output_decl['name']};")
        hlsl_code = "\n".join(line for line in code_lines if line.strip()).strip()

        used_input_names = [item.name for item in inputs]

        output_payload = {
            "name": output_decl["name"],
            "glsl_type": output_decl["type"],
            "ue_output_type": self.OUTPUT_TYPE_TO_UE.get(output_decl["type"], "Unsupported"),
        }
        input_payload = [asdict(item) for item in inputs]

        copy_package = self._build_copy_package(
            inputs=input_payload,
            output_payload=output_payload,
            hlsl_code=hlsl_code,
            notes=notes,
            warnings=warnings,
            unsupported=unsupported,
        )

        summary = (
            f"已生成 UE4.26 Custom 节点代码，输出类型 {output_payload['ue_output_type']}，"
            f"输入 {len(input_payload)} 个，不支持项 {len(unsupported)} 个。"
        )
        return {
            "summary": summary,
            "inputs": input_payload,
            "output": output_payload,
            "hlsl_code": hlsl_code,
            "copy_package": copy_package,
            "notes": notes,
            "warnings": warnings,
            "unsupported": unsupported,
            "used_input_names": used_input_names,
        }

    def _build_internal_matrix_decls(
        self,
        uniform_declarations: list[dict[str, str]],
        body: str,
    ) -> list[str]:
        declarations: list[str] = []
        for item in uniform_declarations:
            name = str(item.get("name") or "").strip()
            glsl_type = str(item.get("type") or "").strip()
            if not name or glsl_type not in {"mat2", "mat3", "mat4"}:
                continue
            if not self._is_identifier_used(body, name):
                continue
            hint = self._infer_builtin_source_hint(name, glsl_type, "uniform")
            expression = self.INTERNAL_MATRIX_EXPRESSIONS.get(hint)
            if not expression:
                continue
            hlsl_type = self.GLSL_TYPE_TO_HLSL.get(glsl_type, glsl_type)
            declarations.append(f"{hlsl_type} {name} = {expression};")
        return declarations

    @staticmethod
    def _normalize_source(text: str) -> str:
        return str(text or "").replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _strip_renderdoc_header(text: str) -> str:
        lines = text.splitlines()
        index = 0
        while index < len(lines):
            line = lines[index].strip()
            if not line:
                index += 1
                continue
            if line.startswith("//"):
                index += 1
                continue
            break
        trimmed = "\n".join(lines[index:])
        trimmed = re.sub(r"^\s*#version[^\n]*\n?", "", trimmed, flags=re.MULTILINE)
        trimmed = re.sub(r"^\s*precision\s+\w+\s+\w+\s*;\s*\n?", "", trimmed, flags=re.MULTILINE)
        return trimmed.strip()

    def _build_params_lookup(self, shader_params_json: str, warnings: list[str]) -> dict[str, Any]:
        text = str(shader_params_json or "").strip()
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            warnings.append(f"shader params 不是合法 JSON，已忽略默认值信息: {exc}")
            return {}

        result: dict[str, Any] = {}
        fragment_stage = (payload.get("stages") or {}).get("fragment") or {}
        for block in fragment_stage.get("constant_blocks") or []:
            for variable in block.get("variables") or []:
                name = str(variable.get("name") or "").strip()
                if name:
                    result[name] = variable.get("value")
        return result

    def _build_vertex_hints(self, vertex_source: str) -> dict[str, str]:
        if not vertex_source.strip():
            return {}
        stripped = self._strip_renderdoc_header(vertex_source)
        main_range = self._find_main_block(stripped)
        if main_range is None:
            return {}
        body = stripped[main_range["body_start"] : main_range["body_end"]]
        body = re.sub(
            r"\(\s*([A-Za-z_]\w*(?:\.[xyzwrgba]{1,4})?)\s*([+\-*/]?=)\s*(.+?)\s*\);",
            r"\1 \2 \3;",
            body,
        )
        hints: dict[str, str] = {}
        for match in re.finditer(r"\b([A-Za-z_]\w*)\s*=\s*(.+?);", body):
            name = match.group(1)
            expr = " ".join(match.group(2).split())
            hints[name] = expr[:160]
        return hints

    @staticmethod
    def _find_main_block(text: str) -> dict[str, int] | None:
        match = re.search(r"\bvoid\s+main\s*\(\s*\)\s*\{", text)
        if not match:
            return None
        brace_start = match.end() - 1
        depth = 0
        for index in range(brace_start, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return {
                        "start": match.start(),
                        "body_start": brace_start + 1,
                        "body_end": index,
                        "end": index + 1,
                    }
        return None

    def _parse_global_declarations(
        self,
        text: str,
        warnings: list[str],
        unsupported: list[str],
    ) -> dict[str, list[dict[str, str]]]:
        parsed: dict[str, list[dict[str, str]]] = {"uniform": [], "in": [], "out": [], "const": []}
        if not text:
            return parsed

        sanitized = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        statements = [item.strip() for item in sanitized.split(";") if item.strip()]
        for raw_statement in statements:
            statement = " ".join(raw_statement.split())
            statement = re.sub(r"layout\s*\([^)]*\)\s*", "", statement)
            if statement.startswith("uniform "):
                decl = self._parse_named_declaration(statement, "uniform", unsupported)
                if decl:
                    parsed["uniform"].append(decl)
                continue
            if statement.startswith("in "):
                decl = self._parse_named_declaration(statement, "in", unsupported)
                if decl:
                    parsed["in"].append(decl)
                continue
            if statement.startswith("out "):
                decl = self._parse_named_declaration(statement, "out", unsupported)
                if decl:
                    parsed["out"].append(decl)
                continue
            if statement.startswith("const "):
                parsed["const"].append({"statement": statement + ";"})
                continue
            warnings.append(f"未识别的全局声明已忽略: {statement}")
        return parsed

    def _parse_named_declaration(
        self,
        statement: str,
        category: str,
        unsupported: list[str],
    ) -> dict[str, str] | None:
        match = re.match(rf"{category}\s+([A-Za-z_]\w*)\s+(.+)$", statement)
        if not match:
            unsupported.append(f"无法解析的 {category} 声明: {statement}")
            return None
        glsl_type = match.group(1)
        names = [item.strip() for item in match.group(2).split(",") if item.strip()]
        if len(names) != 1:
            unsupported.append(f"{category} 暂不支持一行多个变量: {statement}")
            return None
        name = names[0]
        if "[" in name or "]" in name:
            unsupported.append(f"{category} 暂不支持数组输入: {statement}")
            return None
        return {"type": glsl_type, "name": name}

    def _pick_output_declaration(
        self,
        out_declarations: list[dict[str, str]],
        body: str,
        warnings: list[str],
        unsupported: list[str],
    ) -> dict[str, str] | None:
        if len(out_declarations) > 1:
            unsupported.append("检测到多个 fragment 输出；当前版本仅支持单输出 Custom 节点。")
            return None
        if out_declarations:
            decl = out_declarations[0]
            if decl["type"] not in self.OUTPUT_TYPE_TO_UE:
                unsupported.append(f"当前版本不支持输出类型 {decl['type']}。")
                return None
            return decl
        if "gl_FragColor" in body:
            warnings.append("未声明 out 变量，已按 gl_FragColor 推断输出。")
            return {"type": "vec4", "name": "gl_FragColor"}
        return None

    @staticmethod
    def _is_identifier_used(text: str, name: str) -> bool:
        return bool(re.search(rf"\b{re.escape(name)}\b", text))

    def _make_input_spec(
        self,
        *,
        item: dict[str, str],
        category: str,
        params_lookup: dict[str, Any],
        vertex_hints: dict[str, str],
        unsupported: list[str],
    ) -> ShaderInputSpec:
        glsl_type = item["type"]
        default_value = params_lookup.get(item["name"])
        source_hint = self._infer_builtin_source_hint(item["name"], glsl_type, category)
        if not source_hint and category == "in":
            source_hint = vertex_hints.get(item["name"], "")
        if source_hint.startswith("builtin:transform_matrix:"):
            ue_input_type = "InternalMatrix"
        else:
            ue_input_type = self.GLSL_TYPE_TO_UE_INPUT.get(glsl_type, "Unsupported")
            if ue_input_type.startswith("Unsupported"):
                unsupported.append(f"输入 {item['name']} 使用了暂不支持自动接线的类型 {glsl_type}。")
        return ShaderInputSpec(
            name=item["name"],
            glsl_type=glsl_type,
            ue_input_type=ue_input_type,
            category="texture" if glsl_type == "sampler2D" else category,
            default_value=default_value,
            source_hint=source_hint,
        )

    def _infer_builtin_source_hint(self, name: str, glsl_type: str, category: str) -> str:
        normalized = str(name or "").strip().lower()
        if not normalized:
            return ""
        matrix_hint = self.BUILTIN_INTERNAL_MATRIX_HINTS.get(normalized)
        if matrix_hint:
            return matrix_hint
        matrix_hint = self.BUILTIN_EXTERNAL_MATRIX_HINTS.get(normalized)
        if matrix_hint:
            return matrix_hint
        if normalized == "cameraposition" and glsl_type == "vec3":
            return "builtin:camera_position_ws"
        if normalized in {"vertexcolor", "diffuse"} and glsl_type == "vec4":
            return "builtin:vertex_color"
        texcoord_match = re.fullmatch(r"texcoord(\d+)", normalized)
        if texcoord_match and glsl_type == "vec2":
            return f"builtin:texture_coordinate:{texcoord_match.group(1)}"
        if normalized in {"v2f_position_world", "position_world"} and glsl_type == "vec3":
            return "builtin:world_position"
        if normalized in {"v2f_normal", "normal"} and glsl_type == "vec3":
            return "builtin:vertex_normal_ws"
        if normalized in {"v2f_tangent", "tangent"} and glsl_type == "vec3":
            return "builtin:vertex_tangent_ws"
        if normalized in {"v2f_binormal", "binormal"} and glsl_type == "vec3":
            return "builtin:vertex_binormal_ws"
        if category == "uniform" and normalized.startswith("sam_") and glsl_type == "sampler2d":
            return "builtin:texture_object"
        return ""

    def _translate_statement(self, statement: str, unsupported: list[str]) -> str:
        translated = self._translate_common(statement, unsupported)
        return translated

    def _translate_body(self, body: str, unsupported: list[str]) -> str:
        translated = self._translate_common(body, unsupported)
        translated = re.sub(
            r"\(\s*([A-Za-z_]\w*(?:\.[xyzwrgba]{1,4})?)\s*([+\-*/]?=)\s*(.+?)\s*\);",
            r"\1 \2 \3;",
            translated,
        )
        translated = re.sub(r"\n{3,}", "\n\n", translated)
        return translated.strip()

    def _translate_common(self, text: str, unsupported: list[str]) -> str:
        translated = str(text or "")
        translated = re.sub(r"layout\s*\([^)]*\)\s*", "", translated)
        translated = re.sub(r"\buniform\s+", "", translated)
        translated = re.sub(r"\bin\s+", "", translated)
        translated = re.sub(r"\bout\s+", "", translated)

        translated = self._replace_function_calls(translated, "textureGrad", self._replace_texture_grad)
        translated = self._replace_function_calls(translated, "textureLod", self._replace_texture_lod)
        translated = self._replace_function_calls(translated, "texture", self._replace_texture)
        translated = self._replace_function_calls(translated, "mix", self._replace_mix)

        translated = re.sub(r"\bdFdx\b", "ddx", translated)
        translated = re.sub(r"\bdFdy\b", "ddy", translated)
        translated = re.sub(r"\bfract\b", "frac", translated)
        translated = re.sub(r"\bmod\b", "fmod", translated)
        translated = re.sub(r"\bfloatBitsToUint\b", "_rdt_f32_to_u32", translated)
        translated = re.sub(r"\buintBitsToFloat\b", "_rdt_u32_to_f32", translated)

        for glsl_type, hlsl_type in self.GLSL_TYPE_TO_HLSL.items():
            translated = re.sub(rf"\b{glsl_type}\b", hlsl_type, translated)

        for ctor_name, width in (
            ("float2", 2),
            ("float3", 3),
            ("float4", 4),
            ("int2", 2),
            ("int3", 3),
            ("int4", 4),
            ("bool2", 2),
            ("bool3", 3),
            ("bool4", 4),
        ):
            translated = self._replace_function_calls(
                translated,
                ctor_name,
                lambda args, ctor_name=ctor_name, width=width: self._replace_scalar_splat_constructor(
                    ctor_name, width, args
                ),
            )

        for matrix_type in ("float2x2", "float3x3", "float4x4"):
            if re.search(rf"\b{matrix_type}\s*\(", translated):
                unsupported.append(f"检测到矩阵构造 {matrix_type}(...)；当前版本未做 UE Custom 节点专项适配。")
        return translated

    @staticmethod
    def _replace_mix(args: list[str]) -> str:
        if len(args) != 3:
            return f"lerp({', '.join(args)})"
        return f"lerp({args[0]}, {args[1]}, {args[2]})"

    @staticmethod
    def _replace_texture(args: list[str]) -> str:
        if len(args) == 2:
            sampler_name, uv = args
            return f"{sampler_name}.Sample({sampler_name}Sampler, {uv})"
        if len(args) == 3:
            sampler_name, uv, bias = args
            return f"{sampler_name}.SampleBias({sampler_name}Sampler, {uv}, {bias})"
        return f"texture({', '.join(args)})"

    @staticmethod
    def _replace_texture_grad(args: list[str]) -> str:
        if len(args) != 4:
            return f"textureGrad({', '.join(args)})"
        sampler_name, uv, ddx_value, ddy_value = args
        return f"{sampler_name}.SampleGrad({sampler_name}Sampler, {uv}, {ddx_value}, {ddy_value})"

    @staticmethod
    def _replace_texture_lod(args: list[str]) -> str:
        if len(args) != 3:
            return f"textureLod({', '.join(args)})"
        sampler_name, uv, lod = args
        return f"{sampler_name}.SampleLevel({sampler_name}Sampler, {uv}, {lod})"

    @staticmethod
    def _replace_scalar_splat_constructor(type_name: str, width: int, args: list[str]) -> str:
        if len(args) != 1:
            return f"{type_name}({', '.join(args)})"
        value = args[0]
        values = ", ".join(value for _ in range(width))
        return f"{type_name}({values})"

    def _replace_function_calls(
        self,
        text: str,
        function_name: str,
        replacer: Any,
    ) -> str:
        needle = function_name + "("
        index = 0
        result: list[str] = []
        while index < len(text):
            position = text.find(needle, index)
            if position < 0:
                result.append(text[index:])
                break
            prev_char = text[position - 1] if position > 0 else ""
            if prev_char and (prev_char.isalnum() or prev_char == "_"):
                result.append(text[index : position + len(function_name)])
                index = position + len(function_name)
                continue
            result.append(text[index:position])
            open_paren = position + len(function_name)
            close_paren = self._find_matching_paren(text, open_paren)
            if close_paren < 0:
                result.append(text[position:])
                break
            args_text = text[open_paren + 1 : close_paren]
            args = self._split_top_level_args(args_text)
            result.append(replacer(args))
            index = close_paren + 1
        return "".join(result)

    @staticmethod
    def _extract_known_helper_blocks(prefix: str) -> tuple[str, list[str]]:
        helper_markers: list[str] = []
        cleaned = str(prefix or "")
        if "unpackHalf2x16_emu" in cleaned:
            helper_markers.append("packed_half")
        cleaned = re.sub(
            r"// BEGIN: Generated code for built-in function emulation.*?// END: Generated code for built-in function emulation",
            "",
            cleaned,
            flags=re.S,
        )
        cleaned = re.sub(r"^\s*#extension[^\n]*\n?", "", cleaned, flags=re.MULTILINE)
        cleaned = re.sub(r"^\s*#define[^\n]*\n?", "", cleaned, flags=re.MULTILINE)
        cleaned = re.sub(r"^\s*#if[^\n]*\n?", "", cleaned, flags=re.MULTILINE)
        cleaned = re.sub(r"^\s*#else[^\n]*\n?", "", cleaned, flags=re.MULTILINE)
        cleaned = re.sub(r"^\s*#endif[^\n]*\n?", "", cleaned, flags=re.MULTILINE)
        return cleaned.strip(), helper_markers

    def _build_support_helpers_hlsl(self, helper_markers: list[str], translated_body: str) -> list[str]:
        helpers: list[str] = []
        body_text = str(translated_body or "")
        if (
            "packed_half" in helper_markers
            or "unpackHalf2x16_emu(" in body_text
            or "_rdt_f32_to_u32(" in body_text
            or "_rdt_u32_to_f32(" in body_text
        ):
            helpers.append(self.PACKED_HALF_HELPERS_HLSL)
        return helpers

    @staticmethod
    def _find_matching_paren(text: str, open_paren_index: int) -> int:
        depth = 0
        for index in range(open_paren_index, len(text)):
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return index
        return -1

    @staticmethod
    def _split_top_level_args(text: str) -> list[str]:
        args: list[str] = []
        current: list[str] = []
        depth = 0
        for char in text:
            if char == "," and depth == 0:
                arg = "".join(current).strip()
                if arg:
                    args.append(arg)
                current = []
                continue
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            current.append(char)
        tail = "".join(current).strip()
        if tail:
            args.append(tail)
        return args

    @staticmethod
    def _format_default_value(value: Any) -> str:
        if value is None:
            return "-"
        if isinstance(value, float):
            return f"{value:.6g}"
        if isinstance(value, (int, bool)):
            return str(value)
        if isinstance(value, list):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def _build_copy_package(
        self,
        *,
        inputs: list[dict[str, Any]],
        output_payload: dict[str, Any],
        hlsl_code: str,
        notes: list[str],
        warnings: list[str],
        unsupported: list[str],
    ) -> str:
        input_lines = []
        for item in inputs:
            detail = f"- {item['name']}: {item['ue_input_type']} (GLSL {item['glsl_type']})"
            if item.get("default_value") is not None:
                detail += f" default={self._format_default_value(item['default_value'])}"
            if item.get("source_hint"):
                detail += f" ; vertex hint={item['source_hint']}"
            input_lines.append(detail)
        if not input_lines:
            input_lines.append("- 无")

        note_lines = [f"- {item}" for item in notes] or ["- 无"]
        warning_lines = [f"- {item}" for item in warnings] or ["- 无"]
        unsupported_lines = [f"- {item}" for item in unsupported] or ["- 无"]

        sections = [
            "[Output]",
            f"- name: {output_payload['name']}",
            f"- type: {output_payload['ue_output_type']} (GLSL {output_payload['glsl_type']})",
            "",
            "[Inputs]",
            *input_lines,
            "",
            "[HLSL]",
            hlsl_code,
            "",
            "[Notes]",
            *note_lines,
            "",
            "[Warnings]",
            *warning_lines,
            "",
            "[Unsupported]",
            *unsupported_lines,
        ]
        return "\n".join(sections).strip()
