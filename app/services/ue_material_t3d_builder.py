from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from app.services.fragment_glsl_to_ue426_custom_hlsl import (
    FragmentGlslToUe426CustomHlslService,
    ShaderInputSpec,
)


@dataclass
class ShaderOutputSpec:
    name: str
    glsl_type: str
    ue_output_type: str
    is_primary: bool = False


@dataclass
class ShaderStageSpec:
    stage: str
    inputs: list[ShaderInputSpec]
    outputs: list[ShaderOutputSpec]
    hlsl_code: str
    root_mapping: dict[str, Any] | None
    notes: list[str]
    warnings: list[str]
    unsupported: list[str]


@dataclass
class T3DNodeRef:
    graph_name: str
    expr_class: str
    expr_name: str
    default_pin_id: str
    pin_ids: dict[str, str] = field(default_factory=dict)
    output_indices: dict[str, int] = field(default_factory=dict)


@dataclass
class T3DInputConnection:
    node_ref: T3DNodeRef
    output_name: str | None = None


class UeMaterialT3DBuilder(FragmentGlslToUe426CustomHlslService):
    INTERNAL_MATRIX_EXPRESSIONS = {
        "builtin:transform_matrix:world_view_projection": "ResolvedView.TranslatedWorldToClip",
        "builtin:transform_matrix:world_view": "ResolvedView.TranslatedWorldToView",
        "builtin:transform_matrix:projection": "ResolvedView.ViewToClip",
        "builtin:transform_matrix:view_projection": "ResolvedView.TranslatedWorldToClip",
        "builtin:transform_matrix:world": "GetPrimitiveData(Parameters.PrimitiveId).LocalToWorld",
        "builtin:transform_matrix:inverse_world": "GetPrimitiveData(Parameters.PrimitiveId).WorldToLocal",
        "builtin:transform_matrix:view_matrix": "ResolvedView.TranslatedWorldToView",
    }

    def build(
        self,
        *,
        fragment_source: str,
        vertex_source: str = "",
        shader_params_json: str = "",
    ) -> dict[str, Any]:
        fragment_spec = self._build_stage_spec(
            stage="fragment",
            source=fragment_source,
            shader_params_json=shader_params_json,
            vertex_source=vertex_source,
        )
        vertex_spec = (
            self._build_stage_spec(
                stage="vertex",
                source=vertex_source,
                shader_params_json=shader_params_json,
                vertex_source="",
            )
            if str(vertex_source or "").strip()
            else None
        )
        t3d = self._build_t3d_graph(fragment_spec=fragment_spec, vertex_spec=vertex_spec)

        warnings = list(fragment_spec.warnings)
        notes = list(fragment_spec.notes)
        unsupported = list(fragment_spec.unsupported)
        if vertex_spec is None:
            notes.append("未提供 Vertex Shader，已跳过 vertex custom node 生成。")
        else:
            warnings.extend(f"[vertex] {item}" for item in vertex_spec.warnings)
            notes.extend(f"[vertex] {item}" for item in vertex_spec.notes)
            unsupported.extend(f"[vertex] {item}" for item in vertex_spec.unsupported)

        return {
            "fragment_stage": self._stage_to_payload(fragment_spec),
            "vertex_stage": self._stage_to_payload(vertex_spec) if vertex_spec else None,
            "vertex_hlsl_code": vertex_spec.hlsl_code if vertex_spec else "",
            "t3d_text": t3d["text"],
            "t3d_summary": t3d["summary"],
            "t3d_copy_package": self._build_t3d_copy_package(
                t3d_text=t3d["text"],
                fragment_spec=fragment_spec,
                vertex_spec=vertex_spec,
                notes=notes,
                warnings=warnings,
                unsupported=unsupported,
            ),
            "notes": notes,
            "warnings": warnings,
            "unsupported": unsupported,
        }

    def _stage_to_payload(self, stage: ShaderStageSpec | None) -> dict[str, Any] | None:
        if stage is None:
            return None
        return {
            "stage": stage.stage,
            "inputs": [asdict(item) for item in stage.inputs],
            "outputs": [asdict(item) for item in stage.outputs],
            "hlsl_code": stage.hlsl_code,
            "root_mapping": stage.root_mapping,
            "notes": stage.notes,
            "warnings": stage.warnings,
            "unsupported": stage.unsupported,
        }

    def _build_stage_spec(
        self,
        *,
        stage: str,
        source: str,
        shader_params_json: str,
        vertex_source: str,
    ) -> ShaderStageSpec:
        warnings: list[str] = []
        unsupported: list[str] = []
        notes: list[str] = []
        normalized = self._normalize_source(source)
        if not normalized.strip():
            raise ValueError(f"未提供 {stage} shader 源码。")

        cleaned = self._strip_renderdoc_header(normalized)
        main_range = self._find_main_block(cleaned)
        if main_range is None:
            raise ValueError(f"未在 {stage} shader 中找到 void main()。")

        prefix = cleaned[: main_range["start"]].strip()
        prefix, helper_markers = self._extract_known_helper_blocks(prefix)
        body = cleaned[main_range["body_start"] : main_range["body_end"]].strip()
        suffix = cleaned[main_range["end"] :].strip()
        if suffix:
            unsupported.append(f"{stage} main() 之后仍有额外 GLSL 代码；当前版本不会自动合并。")
        if "{" in prefix or "}" in prefix:
            unsupported.append(f"{stage} main() 之前存在函数或复杂代码块；当前版本只稳定支持声明式全局区。")

        declarations = self._parse_global_declarations(prefix, warnings, unsupported)
        params_lookup = self._build_params_lookup_for_stage(shader_params_json, stage, warnings)
        vertex_hints = self._build_vertex_hints(vertex_source) if stage == "fragment" else {}

        inputs: list[ShaderInputSpec] = []
        for category in ("uniform", "in"):
            for item in declarations[category]:
                if not self._is_identifier_used(body, item["name"]):
                    notes.append(f"已省略未在 {stage} main() 中使用的输入: {item['name']}")
                    continue
                inputs.append(
                    self._make_input_spec(
                        item=item,
                        category=category,
                        params_lookup=params_lookup,
                        vertex_hints=vertex_hints,
                        unsupported=unsupported,
                    )
                )

        internal_matrix_inputs = [
            item.name for item in inputs if str(item.source_hint).startswith("builtin:transform_matrix:")
        ]
        if internal_matrix_inputs:
            notes.append(
                f"已识别 UE 内建变换矩阵语义: {', '.join(internal_matrix_inputs)}；它们将直接改写为 UE 内部表达，不再生成外部输入节点。"
            )
        inline_matrix_inputs = [
            item.name
            for item in inputs
            if item.glsl_type in {"mat2", "mat3", "mat4"} and item.default_value is not None
        ]
        if inline_matrix_inputs:
            notes.append(
                f"已将抓帧参数中的矩阵常量直接内联到 HLSL: {', '.join(inline_matrix_inputs)}；这些矩阵不会作为 Custom 输入节点暴露。"
            )
        inputs = [
            item
            for item in inputs
            if not str(item.source_hint).startswith("builtin:transform_matrix:")
            and not (item.glsl_type in {"mat2", "mat3", "mat4"} and item.default_value is not None)
        ]

        builtin_source_inputs = [
            item.name for item in inputs
            if str(item.source_hint).startswith("builtin:")
            and not str(item.source_hint).startswith("builtin:transform_matrix:")
        ]
        if builtin_source_inputs:
            notes.append(f"已识别可映射为 UE 内置节点的输入: {', '.join(builtin_source_inputs)}。")

        gl_position_used = stage == "vertex" and self._is_identifier_used(body, "gl_Position")
        outputs = (
            self._pick_fragment_outputs(declarations["out"], body, warnings, unsupported)
            if stage == "fragment"
            else self._pick_vertex_outputs(declarations["out"], body, warnings, unsupported)
        )
        synthetic_gl_position_output = False
        if stage == "vertex" and gl_position_used and not outputs:
            outputs = [
                ShaderOutputSpec(
                    name="VertexClipPositionRef",
                    glsl_type="vec4",
                    ue_output_type="CMOT Float4",
                    is_primary=True,
                )
            ]
            synthetic_gl_position_output = True
            notes.append("gl_Position 是顶点阶段内建最终位置；当前未映射为 UE 原生节点出口，已保留为 VertexClipPositionRef 参考输出。")
        if not outputs:
            raise ValueError(f"未在 {stage} shader 中找到可用输出。")
        primary = outputs[0]

        translated_consts = [self._translate_statement(item["statement"], unsupported) for item in declarations["const"]]
        translated_body = self._translate_body(body, unsupported)
        internal_matrix_decls = self._build_internal_matrix_decls(declarations["uniform"], params_lookup, body)
        extra_local_decls: list[str] = []
        if stage == "vertex" and gl_position_used:
            translated_body = re.sub(r"\bgl_Position\b", "VertexClipPositionRef", translated_body)
            if not synthetic_gl_position_output:
                extra_local_decls.append("float4 VertexClipPositionRef;")
                notes.append("gl_Position 属于顶点阶段内建输出，不会作为普通 UE 节点输出；当前仅保留其计算过程供参考。")
        root_mapping = self._analyze_vertex_root_mapping(
            body=body,
            inputs=inputs,
            outputs=outputs,
            gl_position_used=gl_position_used,
        ) if stage == "vertex" else None
        if root_mapping and root_mapping.get("summary"):
            notes.append(str(root_mapping["summary"]))

        additional_output_names = {item.name for item in outputs if not item.is_primary}
        declared_outputs = []
        for output in outputs:
            if output.name in additional_output_names:
                continue
            hlsl_type = self.GLSL_TYPE_TO_HLSL.get(output.glsl_type, output.glsl_type)
            declared_outputs.append(f"{hlsl_type} {output.name};")
        hlsl_code = "\n".join(
            line
            for line in [
                *self._build_support_helpers_hlsl(helper_markers, translated_body),
                *translated_consts,
                *internal_matrix_decls,
                *declared_outputs,
                *extra_local_decls,
                translated_body,
                f"return {primary.name};",
            ]
            if str(line).strip()
        ).strip()

        return ShaderStageSpec(
            stage=stage,
            inputs=inputs,
            outputs=outputs,
            hlsl_code=hlsl_code,
            root_mapping=root_mapping,
            notes=notes,
            warnings=warnings,
            unsupported=unsupported,
        )

    def _build_internal_matrix_decls(
        self,
        uniform_declarations: list[dict[str, str]],
        params_lookup: dict[str, Any],
        body: str,
    ) -> list[str]:
        declarations: list[str] = []
        for item in uniform_declarations:
            name = str(item.get("name") or "").strip()
            glsl_type = str(item.get("type") or "").strip()
            if not name or not self._is_identifier_used(body, name):
                continue
            hint = self._infer_builtin_source_hint(item.get("name", ""), item.get("type", ""), "uniform")
            expression = self.INTERNAL_MATRIX_EXPRESSIONS.get(hint)
            if expression:
                declarations.append(f"float4x4 {name} = {expression};")
                continue
            if glsl_type in {"mat2", "mat3", "mat4"} and name in params_lookup:
                declarations.append(f"{self.GLSL_TYPE_TO_HLSL.get(glsl_type, glsl_type)} {name} = {self._format_matrix_default(params_lookup[name], glsl_type)};")
        return declarations

    def _analyze_vertex_root_mapping(
        self,
        *,
        body: str,
        inputs: list[ShaderInputSpec],
        outputs: list[ShaderOutputSpec],
        gl_position_used: bool,
    ) -> dict[str, Any] | None:
        if not gl_position_used:
            return None
        compact = " ".join(str(body or "").split())
        recommendations: list[str] = []
        rationale: list[str] = []
        confidence = "low"
        summary = "检测到 gl_Position；它属于顶点阶段内建最终位置，当前不会作为普通 UE 节点输出。"

        output_names = {item.name for item in outputs}
        if "v_texture0" in output_names:
            recommendations.append("CustomizedUV0")
            rationale.append("检测到 v_texture0 顶点输出；若 fragment 主要使用其 xy，可优先尝试映射到 CustomizedUV0。")

        matrix_names = {
            item.name for item in inputs
            if item.glsl_type == "mat4" or str(item.source_hint).startswith("builtin:transform_matrix:")
        }
        if {"WorldViewProjection", "WorldView", "Projection"} & matrix_names:
            rationale.append("顶点逻辑依赖标准视图/投影矩阵，这类不应继续作为普通 Custom 输入，而应改写为 UE 内建 Parameters/ResolvedView 访问。")

        if all(token in compact for token in ("WorldViewProjection", "WorldView", "Projection", "depth_bias")):
            recommendations.insert(0, "PixelDepthOffset")
            confidence = "medium"
            summary = "当前 gl_Position 路径更接近“保持屏幕 x/y 不变、只改裁剪深度”的写法，优先视作 PixelDepthOffset 候选；不是普通中间节点。"
            rationale.append("检测到 WorldView/Projection/depth_bias 组合，说明当前路径更像深度偏移而不是完整顶点位移。")
        elif "WorldViewProjection" in compact:
            recommendations.insert(0, "WorldPositionOffset")
            confidence = "low"
            summary = "当前 gl_Position 依赖完整投影矩阵计算，无法直接等价映射到普通 UE 节点；若继续落地，应优先尝试拆成 WorldPositionOffset。"
            rationale.append("检测到顶点最终位置参与完整投影计算，说明需要拆回 UE 可表达的根语义，而不是保留 clip-space 输出。")
        else:
            recommendations.append("ManualReview")
            rationale.append("当前 gl_Position 计算模式未命中已知规则，需要人工判断是 WorldPositionOffset 还是其他根语义。")

        return {
            "builtin_role": "final_vertex_clip_position",
            "confidence": confidence,
            "recommended_root_slots": recommendations,
            "rationale": rationale,
            "summary": summary,
        }

    def _build_params_lookup_for_stage(
        self,
        shader_params_json: str,
        stage_key: str,
        warnings: list[str],
    ) -> dict[str, Any]:
        text = str(shader_params_json or "").strip()
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            warnings.append(f"shader params 不是合法 JSON，已忽略默认值信息: {exc}")
            return {}
        result: dict[str, Any] = {}
        stage_payload = (payload.get("stages") or {}).get(stage_key) or {}
        for block in stage_payload.get("constant_blocks") or []:
            for variable in block.get("variables") or []:
                name = str(variable.get("name") or "").strip()
                if name:
                    result[name] = variable.get("value")
        return result

    def _pick_fragment_outputs(
        self,
        out_declarations: list[dict[str, str]],
        body: str,
        warnings: list[str],
        unsupported: list[str],
    ) -> list[ShaderOutputSpec]:
        output_decl = self._pick_output_declaration(out_declarations, body, warnings, unsupported)
        if output_decl is None:
            return []
        return [
            ShaderOutputSpec(
                name=output_decl["name"],
                glsl_type=output_decl["type"],
                ue_output_type=self.OUTPUT_TYPE_TO_UE.get(output_decl["type"], "Unsupported"),
                is_primary=True,
            )
        ]

    def _pick_vertex_outputs(
        self,
        out_declarations: list[dict[str, str]],
        body: str,
        warnings: list[str],
        unsupported: list[str],
    ) -> list[ShaderOutputSpec]:
        outputs: list[ShaderOutputSpec] = []
        used_outs = [item for item in out_declarations if self._is_identifier_used(body, item["name"])]
        for index, item in enumerate(used_outs):
            if item["type"] not in self.OUTPUT_TYPE_TO_UE:
                unsupported.append(f"vertex 输出 {item['name']} 的类型 {item['type']} 暂不支持。")
                continue
            outputs.append(
                ShaderOutputSpec(
                    name=item["name"],
                    glsl_type=item["type"],
                    ue_output_type=self.OUTPUT_TYPE_TO_UE.get(item["type"], "Unsupported"),
                    is_primary=index == 0,
                )
            )
        if self._is_identifier_used(body, "gl_Position"):
            warnings.append("检测到 gl_Position；它是顶点阶段内建最终位置，不会被当作普通 Custom 输出节点导出。")
        if len(outputs) > 1:
            warnings.append("vertex 阶段存在多个输出；T3D 将使用 Custom 节点 AdditionalOutputs 表达。")
        return outputs

    def _build_t3d_graph(
        self,
        *,
        fragment_spec: ShaderStageSpec,
        vertex_spec: ShaderStageSpec | None,
    ) -> dict[str, Any]:
        context = _T3DContext()
        shared_nodes: dict[tuple[str, str], T3DNodeRef] = {}
        blocks: list[str] = []
        vertex_ref: T3DNodeRef | None = None
        if vertex_spec is not None:
            vertex_blocks, vertex_ref = self._build_custom_node_block(
                context=context,
                stage_spec=vertex_spec,
                origin_x=-320,
                origin_y=460,
                shared_nodes=shared_nodes,
                title="Vertex Custom",
            )
            blocks.extend(vertex_blocks)

        fragment_connections = self._build_fragment_input_connections(fragment_spec, vertex_spec, vertex_ref)
        fragment_blocks, _ = self._build_custom_node_block(
            context=context,
            stage_spec=fragment_spec,
            origin_x=120,
            origin_y=0,
            shared_nodes=shared_nodes,
            title="Fragment Custom",
            input_connections=fragment_connections,
        )
        blocks.extend(fragment_blocks)

        summary = (
            f"已生成 UE4.26 材质图 T3D 文本，"
            f"fragment 节点 1 个，vertex 节点 {1 if vertex_spec else 0} 个，"
            f"输入源节点 {len(shared_nodes)} 个，"
            f"vs/fs 接口直连 {len(fragment_connections)} 处。"
        )
        return {"text": "\n".join(blocks).strip(), "summary": summary}

    def _build_custom_node_block(
        self,
        *,
        context: "_T3DContext",
        stage_spec: ShaderStageSpec,
        origin_x: int,
        origin_y: int,
        shared_nodes: dict[tuple[str, str], T3DNodeRef],
        title: str,
        input_connections: dict[str, T3DInputConnection] | None = None,
    ) -> tuple[list[str], T3DNodeRef]:
        expr_name = context.next_expression_name("MaterialExpressionCustom")
        graph_name = context.next_graph_name()
        expression_guid = context.new_guid()
        node_guid = context.new_guid()

        input_lines: list[str] = []
        input_pin_lines: list[str] = []
        node_blocks: list[str] = []
        input_y = origin_y - 180
        for index, input_spec in enumerate(stage_spec.inputs):
            connection = (input_connections or {}).get(input_spec.name)
            if connection is None:
                key = (input_spec.name, input_spec.glsl_type)
                existing = shared_nodes.get(key)
                if existing is None:
                    input_node_block, existing = self._build_input_node(
                        context=context,
                        input_spec=input_spec,
                        node_x=origin_x - 480,
                        node_y=input_y,
                    )
                    shared_nodes[key] = existing
                    node_blocks.append(input_node_block)
                    input_y += 170
                connection = T3DInputConnection(node_ref=existing)
            input_lines.append(self._format_custom_input_property(index, input_spec, connection))
            input_pin_lines.append(
                self._format_custom_input_pin(
                    input_spec.name,
                    connection.node_ref.graph_name,
                    self._resolve_connection_pin_id(connection),
                )
            )

        additional_outputs = [item for item in stage_spec.outputs if not item.is_primary]
        output_pin_lines = []
        custom_ref = T3DNodeRef(
            graph_name=graph_name,
            expr_class="MaterialExpressionCustom",
            expr_name=expr_name,
            default_pin_id="",
        )
        for item in stage_spec.outputs:
            pin_name = item.name if bool(additional_outputs) else "Output"
            pin_id = context.new_guid()
            if item.is_primary:
                custom_ref.default_pin_id = pin_id
            custom_ref.pin_ids[item.name] = pin_id
            custom_ref.output_indices[item.name] = 0 if item.is_primary else len(custom_ref.output_indices)
            output_pin_lines.append(self._format_custom_output_pin(item, bool(additional_outputs), pin_id))

        custom_props = [
            self._format_hlsl_property("Code", stage_spec.hlsl_code),
            f'      OutputType={self._to_enum_name(stage_spec.outputs[0].ue_output_type)}',
            f'      Description="{self._escape_string(title)}"',
        ]
        custom_props.extend(input_lines)
        for index, output_spec in enumerate(additional_outputs):
            custom_props.append(
                f'      AdditionalOutputs({index})=(OutputName="{self._escape_string(output_spec.name)}",'
                f"OutputType={self._to_enum_name(output_spec.ue_output_type)})"
            )
        custom_props.extend(
            [
                f"      MaterialExpressionEditorX={origin_x}",
                f"      MaterialExpressionEditorY={origin_y}",
                f"      MaterialExpressionGuid={expression_guid}",
                '      Material=PreviewMaterial\'"/Engine/Transient.NewMaterial"\'',
            ]
        )

        graph_lines = [
            f'Begin Object Class=/Script/UnrealEd.MaterialGraphNode Name="{graph_name}"',
            f'   Begin Object Class=/Script/Engine.MaterialExpressionCustom Name="{expr_name}"',
            "   End Object",
            f'   Begin Object Name="{expr_name}"',
            *custom_props,
            "   End Object",
            f'   MaterialExpression=MaterialExpressionCustom\'"{expr_name}"\'',
            f"   NodePosX={origin_x}",
            f"   NodePosY={origin_y}",
            f"   NodeGuid={node_guid}",
            *input_pin_lines,
            *output_pin_lines,
            "End Object",
        ]
        return [*node_blocks, "\n".join(graph_lines)], custom_ref

    def _build_input_node(
        self,
        *,
        context: "_T3DContext",
        input_spec: ShaderInputSpec,
        node_x: int,
        node_y: int,
    ) -> tuple[str, T3DNodeRef]:
        builtin_hint = str(input_spec.source_hint or "")
        if builtin_hint == "builtin:camera_position_ws" and input_spec.glsl_type == "vec3":
            return self._build_camera_position_ws_node(context, node_x, node_y)
        if builtin_hint == "builtin:world_position" and input_spec.glsl_type == "vec3":
            return self._build_world_position_node(context, node_x, node_y)
        if builtin_hint == "builtin:vertex_normal_ws" and input_spec.glsl_type == "vec3":
            return self._build_vertex_normal_ws_node(context, node_x, node_y)
        if builtin_hint == "builtin:vertex_tangent_ws" and input_spec.glsl_type == "vec3":
            return self._build_vertex_tangent_ws_node(context, node_x, node_y)
        if builtin_hint == "builtin:vertex_color":
            if input_spec.glsl_type == "vec4":
                return self._build_vec4_from_vertex_color(context, input_spec, node_x, node_y)
            return self._build_vertex_color_node(context, input_spec, node_x, node_y)
        if builtin_hint.startswith("builtin:texture_coordinate:") and input_spec.glsl_type == "vec2":
            coordinate_index = self._parse_texture_coordinate_index(builtin_hint)
            return self._build_texture_coordinate_node(context, node_x, node_y, coordinate_index)
        if input_spec.glsl_type == "sampler2D":
            return self._build_texture_object_parameter_node(context, input_spec, node_x, node_y)
        if input_spec.name.lower() == "vertexcolor" and input_spec.glsl_type == "vec4":
            return self._build_vec4_from_vertex_color(context, input_spec, node_x, node_y)
        if input_spec.glsl_type == "vec4":
            return self._build_vec4_from_vector_parameter(context, input_spec, node_x, node_y)
        if input_spec.ue_input_type == "Scalar":
            return self._build_scalar_parameter_node(context, input_spec, node_x, node_y)
        if input_spec.name.lower() == "vertexcolor":
            return self._build_vertex_color_node(context, input_spec, node_x, node_y)
        return self._build_vector_parameter_node(context, input_spec, node_x, node_y)

    @staticmethod
    def _parse_texture_coordinate_index(source_hint: str) -> int:
        match = re.search(r"builtin:texture_coordinate:(\d+)", str(source_hint or ""))
        if not match:
            return 0
        return int(match.group(1))

    def _build_texture_coordinate_node(
        self,
        context: "_T3DContext",
        node_x: int,
        node_y: int,
        coordinate_index: int,
    ) -> tuple[str, T3DNodeRef]:
        expr_name = context.next_expression_name("MaterialExpressionTextureCoordinate")
        graph_name = context.next_graph_name()
        pin_id = context.new_guid()
        lines = [
            f'Begin Object Class=/Script/UnrealEd.MaterialGraphNode Name="{graph_name}"',
            f'   Begin Object Class=/Script/Engine.MaterialExpressionTextureCoordinate Name="{expr_name}"',
            "   End Object",
            f'   Begin Object Name="{expr_name}"',
            f"      CoordinateIndex={coordinate_index}",
            "      UTiling=1.000000",
            "      VTiling=1.000000",
            f"      MaterialExpressionEditorX={node_x}",
            f"      MaterialExpressionEditorY={node_y}",
            f"      MaterialExpressionGuid={context.new_guid()}",
            '      Material=PreviewMaterial\'"/Engine/Transient.NewMaterial"\'',
            "   End Object",
            f'   MaterialExpression=MaterialExpressionTextureCoordinate\'"{expr_name}"\'',
            f"   NodePosX={node_x}",
            f"   NodePosY={node_y}",
            f"   NodeGuid={context.new_guid()}",
            self._format_parameter_output_pin(pin_id),
            "End Object",
        ]
        return "\n".join(lines), T3DNodeRef(
            graph_name=graph_name,
            expr_class="MaterialExpressionTextureCoordinate",
            expr_name=expr_name,
            default_pin_id=pin_id,
            output_indices={"Output": 0},
        )

    def _build_camera_position_ws_node(
        self,
        context: "_T3DContext",
        node_x: int,
        node_y: int,
    ) -> tuple[str, T3DNodeRef]:
        return self._build_builtin_vector_source_node(
            context=context,
            expr_class="MaterialExpressionCameraPositionWS",
            node_x=node_x,
            node_y=node_y,
        )

    def _build_world_position_node(
        self,
        context: "_T3DContext",
        node_x: int,
        node_y: int,
    ) -> tuple[str, T3DNodeRef]:
        expr_name = context.next_expression_name("MaterialExpressionWorldPosition")
        graph_name = context.next_graph_name()
        pin_id = context.new_guid()
        lines = [
            f'Begin Object Class=/Script/UnrealEd.MaterialGraphNode Name="{graph_name}"',
            f'   Begin Object Class=/Script/Engine.MaterialExpressionWorldPosition Name="{expr_name}"',
            "   End Object",
            f'   Begin Object Name="{expr_name}"',
            "      WorldPositionShaderOffset=WPT_Default",
            f"      MaterialExpressionEditorX={node_x}",
            f"      MaterialExpressionEditorY={node_y}",
            f"      MaterialExpressionGuid={context.new_guid()}",
            '      Material=PreviewMaterial\'"/Engine/Transient.NewMaterial"\'',
            "   End Object",
            f'   MaterialExpression=MaterialExpressionWorldPosition\'"{expr_name}"\'',
            f"   NodePosX={node_x}",
            f"   NodePosY={node_y}",
            f"   NodeGuid={context.new_guid()}",
            self._format_parameter_output_pin(pin_id),
            "End Object",
        ]
        return "\n".join(lines), T3DNodeRef(
            graph_name=graph_name,
            expr_class="MaterialExpressionWorldPosition",
            expr_name=expr_name,
            default_pin_id=pin_id,
            output_indices={"Output": 0},
        )

    def _build_vertex_normal_ws_node(
        self,
        context: "_T3DContext",
        node_x: int,
        node_y: int,
    ) -> tuple[str, T3DNodeRef]:
        return self._build_builtin_vector_source_node(
            context=context,
            expr_class="MaterialExpressionVertexNormalWS",
            node_x=node_x,
            node_y=node_y,
        )

    def _build_vertex_tangent_ws_node(
        self,
        context: "_T3DContext",
        node_x: int,
        node_y: int,
    ) -> tuple[str, T3DNodeRef]:
        return self._build_builtin_vector_source_node(
            context=context,
            expr_class="MaterialExpressionVertexTangentWS",
            node_x=node_x,
            node_y=node_y,
        )

    def _build_builtin_vector_source_node(
        self,
        *,
        context: "_T3DContext",
        expr_class: str,
        node_x: int,
        node_y: int,
    ) -> tuple[str, T3DNodeRef]:
        expr_name = context.next_expression_name(expr_class)
        graph_name = context.next_graph_name()
        pin_id = context.new_guid()
        lines = [
            f'Begin Object Class=/Script/UnrealEd.MaterialGraphNode Name="{graph_name}"',
            f'   Begin Object Class=/Script/Engine.{expr_class} Name="{expr_name}"',
            "   End Object",
            f'   Begin Object Name="{expr_name}"',
            f"      MaterialExpressionEditorX={node_x}",
            f"      MaterialExpressionEditorY={node_y}",
            f"      MaterialExpressionGuid={context.new_guid()}",
            '      Material=PreviewMaterial\'"/Engine/Transient.NewMaterial"\'',
            "   End Object",
            f'   MaterialExpression={expr_class}\'"{expr_name}"\'',
            f"   NodePosX={node_x}",
            f"   NodePosY={node_y}",
            f"   NodeGuid={context.new_guid()}",
            self._format_parameter_output_pin(pin_id),
            "End Object",
        ]
        return "\n".join(lines), T3DNodeRef(
            graph_name=graph_name,
            expr_class=expr_class,
            expr_name=expr_name,
            default_pin_id=pin_id,
            output_indices={"Output": 0},
        )

    def _build_scalar_parameter_node(
        self,
        context: "_T3DContext",
        input_spec: ShaderInputSpec,
        node_x: int,
        node_y: int,
    ) -> tuple[str, T3DNodeRef]:
        expr_name = context.next_expression_name("MaterialExpressionScalarParameter")
        graph_name = context.next_graph_name()
        pin_id = context.new_guid()
        lines = [
            f'Begin Object Class=/Script/UnrealEd.MaterialGraphNode Name="{graph_name}"',
            f'   Begin Object Class=/Script/Engine.MaterialExpressionScalarParameter Name="{expr_name}"',
            "   End Object",
            f'   Begin Object Name="{expr_name}"',
            f"      DefaultValue={self._format_scalar_default(input_spec.default_value)}",
            f'      ParameterName="{self._escape_string(input_spec.name)}"',
            f"      ExpressionGUID={context.new_guid()}",
            f"      MaterialExpressionEditorX={node_x}",
            f"      MaterialExpressionEditorY={node_y}",
            f"      MaterialExpressionGuid={context.new_guid()}",
            '      Material=PreviewMaterial\'"/Engine/Transient.NewMaterial"\'',
            "   End Object",
            f'   MaterialExpression=MaterialExpressionScalarParameter\'"{expr_name}"\'',
            f"   NodePosX={node_x}",
            f"   NodePosY={node_y}",
            "   bCanRenameNode=True",
            f"   NodeGuid={context.new_guid()}",
            self._format_parameter_output_pin(pin_id),
            "End Object",
        ]
        return "\n".join(lines), T3DNodeRef(
            graph_name=graph_name,
            expr_class="MaterialExpressionScalarParameter",
            expr_name=expr_name,
            default_pin_id=pin_id,
        )

    def _build_vector_parameter_node(
        self,
        context: "_T3DContext",
        input_spec: ShaderInputSpec,
        node_x: int,
        node_y: int,
    ) -> tuple[str, T3DNodeRef]:
        expr_name = context.next_expression_name("MaterialExpressionVectorParameter")
        graph_name = context.next_graph_name()
        pin_id = context.new_guid()
        red_pin_id = context.new_guid()
        green_pin_id = context.new_guid()
        blue_pin_id = context.new_guid()
        alpha_pin_id = context.new_guid()
        lines = [
            f'Begin Object Class=/Script/UnrealEd.MaterialGraphNode Name="{graph_name}"',
            f'   Begin Object Class=/Script/Engine.MaterialExpressionVectorParameter Name="{expr_name}"',
            "   End Object",
            f'   Begin Object Name="{expr_name}"',
            f"      DefaultValue={self._format_vector_default(input_spec.default_value)}",
            f'      ParameterName="{self._escape_string(input_spec.name)}"',
            f"      ExpressionGUID={context.new_guid()}",
            f"      MaterialExpressionEditorX={node_x}",
            f"      MaterialExpressionEditorY={node_y}",
            f"      MaterialExpressionGuid={context.new_guid()}",
            '      Material=PreviewMaterial\'"/Engine/Transient.NewMaterial"\'',
            "   End Object",
            f'   MaterialExpression=MaterialExpressionVectorParameter\'"{expr_name}"\'',
            f"   NodePosX={node_x}",
            f"   NodePosY={node_y}",
            "   bCanRenameNode=True",
            f"   NodeGuid={context.new_guid()}",
            self._format_parameter_output_pin(pin_id),
            self._format_mask_channel_pin(red_pin_id, "Output2", "red"),
            self._format_mask_channel_pin(green_pin_id, "Output3", "green"),
            self._format_mask_channel_pin(blue_pin_id, "Output4", "blue"),
            self._format_mask_channel_pin(alpha_pin_id, "Output5", "alpha"),
            "End Object",
        ]
        return "\n".join(lines), T3DNodeRef(
            graph_name=graph_name,
            expr_class="MaterialExpressionVectorParameter",
            expr_name=expr_name,
            default_pin_id=pin_id,
            pin_ids={"rgb": pin_id, "r": red_pin_id, "g": green_pin_id, "b": blue_pin_id, "a": alpha_pin_id},
        )

    def _build_texture_object_parameter_node(
        self,
        context: "_T3DContext",
        input_spec: ShaderInputSpec,
        node_x: int,
        node_y: int,
    ) -> tuple[str, T3DNodeRef]:
        expr_name = context.next_expression_name("MaterialExpressionTextureObjectParameter")
        graph_name = context.next_graph_name()
        pin_id = context.new_guid()
        lines = [
            f'Begin Object Class=/Script/UnrealEd.MaterialGraphNode Name="{graph_name}"',
            f'   Begin Object Class=/Script/Engine.MaterialExpressionTextureObjectParameter Name="{expr_name}"',
            "   End Object",
            f'   Begin Object Name="{expr_name}"',
            f'      ParameterName="{self._escape_string(input_spec.name)}"',
            f"      ExpressionGUID={context.new_guid()}",
            f"      MaterialExpressionEditorX={node_x}",
            f"      MaterialExpressionEditorY={node_y}",
            f"      MaterialExpressionGuid={context.new_guid()}",
            '      Material=PreviewMaterial\'"/Engine/Transient.NewMaterial"\'',
            "   End Object",
            f'   MaterialExpression=MaterialExpressionTextureObjectParameter\'"{expr_name}"\'',
            f"   NodePosX={node_x}",
            f"   NodePosY={node_y}",
            "   bCanRenameNode=True",
            f"   NodeGuid={context.new_guid()}",
            self._format_parameter_output_pin(pin_id),
            "End Object",
        ]
        return "\n".join(lines), T3DNodeRef(
            graph_name=graph_name,
            expr_class="MaterialExpressionTextureObjectParameter",
            expr_name=expr_name,
            default_pin_id=pin_id,
        )

    def _build_vertex_color_node(
        self,
        context: "_T3DContext",
        input_spec: ShaderInputSpec,
        node_x: int,
        node_y: int,
    ) -> tuple[str, T3DNodeRef]:
        expr_name = context.next_expression_name("MaterialExpressionVertexColor")
        graph_name = context.next_graph_name()
        pin_id = context.new_guid()
        red_pin_id = context.new_guid()
        green_pin_id = context.new_guid()
        blue_pin_id = context.new_guid()
        alpha_pin_id = context.new_guid()
        lines = [
            f'Begin Object Class=/Script/UnrealEd.MaterialGraphNode Name="{graph_name}"',
            f'   Begin Object Class=/Script/Engine.MaterialExpressionVertexColor Name="{expr_name}"',
            "   End Object",
            f'   Begin Object Name="{expr_name}"',
            f"      MaterialExpressionEditorX={node_x}",
            f"      MaterialExpressionEditorY={node_y}",
            f"      MaterialExpressionGuid={context.new_guid()}",
            '      Material=PreviewMaterial\'"/Engine/Transient.NewMaterial"\'',
            "   End Object",
            f'   MaterialExpression=MaterialExpressionVertexColor\'"{expr_name}"\'',
            f"   NodePosX={node_x}",
            f"   NodePosY={node_y}",
            f"   NodeGuid={context.new_guid()}",
            self._format_parameter_output_pin(pin_id),
            self._format_mask_channel_pin(red_pin_id, "Output2", "red"),
            self._format_mask_channel_pin(green_pin_id, "Output3", "green"),
            self._format_mask_channel_pin(blue_pin_id, "Output4", "blue"),
            self._format_mask_channel_pin(alpha_pin_id, "Output5", "alpha"),
            "End Object",
        ]
        return "\n".join(lines), T3DNodeRef(
            graph_name=graph_name,
            expr_class="MaterialExpressionVertexColor",
            expr_name=expr_name,
            default_pin_id=pin_id,
            pin_ids={"rgb": pin_id, "r": red_pin_id, "g": green_pin_id, "b": blue_pin_id, "a": alpha_pin_id},
        )

    def _build_vec4_from_vector_parameter(
        self,
        context: "_T3DContext",
        input_spec: ShaderInputSpec,
        node_x: int,
        node_y: int,
    ) -> tuple[str, T3DNodeRef]:
        source_block, source_ref = self._build_vector_parameter_node(context, input_spec, node_x - 220, node_y - 20)
        append_block, append_ref = self._build_append_vec4_node(
            context=context,
            source_ref=source_ref,
            node_x=node_x,
            node_y=node_y,
        )
        return "\n".join([source_block, append_block]), append_ref

    def _build_vec4_from_vertex_color(
        self,
        context: "_T3DContext",
        input_spec: ShaderInputSpec,
        node_x: int,
        node_y: int,
    ) -> tuple[str, T3DNodeRef]:
        source_block, source_ref = self._build_vertex_color_node(context, input_spec, node_x - 220, node_y - 20)
        append_block, append_ref = self._build_append_vec4_node(
            context=context,
            source_ref=source_ref,
            node_x=node_x,
            node_y=node_y,
        )
        return "\n".join([source_block, append_block]), append_ref

    def _build_append_vec4_node(
        self,
        *,
        context: "_T3DContext",
        source_ref: T3DNodeRef,
        node_x: int,
        node_y: int,
    ) -> tuple[str, T3DNodeRef]:
        expr_name = context.next_expression_name("MaterialExpressionAppendVector")
        graph_name = context.next_graph_name()
        input_a_pin_id = context.new_guid()
        input_b_pin_id = context.new_guid()
        output_pin_id = context.new_guid()
        lines = [
            f'Begin Object Class=/Script/UnrealEd.MaterialGraphNode Name="{graph_name}"',
            f'   Begin Object Class=/Script/Engine.MaterialExpressionAppendVector Name="{expr_name}"',
            "   End Object",
            f'   Begin Object Name="{expr_name}"',
            f"      A={self._format_expression_input_value(source_ref, output_index=0, mask=(1, 1, 1, 1, 0))}",
            f"      B={self._format_expression_input_value(source_ref, output_index=4, mask=(1, 0, 0, 0, 1))}",
            f"      MaterialExpressionEditorX={node_x}",
            f"      MaterialExpressionEditorY={node_y}",
            f"      MaterialExpressionGuid={context.new_guid()}",
            '      Material=PreviewMaterial\'"/Engine/Transient.NewMaterial"\'',
            "   End Object",
            f'   MaterialExpression=MaterialExpressionAppendVector\'"{expr_name}"\'',
            f"   NodePosX={node_x}",
            f"   NodePosY={node_y}",
            f"   NodeGuid={context.new_guid()}",
            self._format_required_input_pin("A", input_a_pin_id, source_ref.graph_name, source_ref.pin_ids.get("rgb", source_ref.default_pin_id)),
            self._format_required_input_pin("B", input_b_pin_id, source_ref.graph_name, source_ref.pin_ids.get("a", source_ref.default_pin_id)),
            self._format_parameter_output_pin(output_pin_id),
            "End Object",
        ]
        return "\n".join(lines), T3DNodeRef(
            graph_name=graph_name,
            expr_class="MaterialExpressionAppendVector",
            expr_name=expr_name,
            default_pin_id=output_pin_id,
            output_indices={"Output": 0},
        )

    def _build_fragment_input_connections(
        self,
        fragment_spec: ShaderStageSpec,
        vertex_spec: ShaderStageSpec | None,
        vertex_ref: T3DNodeRef | None,
    ) -> dict[str, T3DInputConnection]:
        connections: dict[str, T3DInputConnection] = {}
        if vertex_spec is None or vertex_ref is None:
            return connections
        vertex_outputs = {item.name: item for item in vertex_spec.outputs}
        for item in fragment_spec.inputs:
            if item.category != "in":
                continue
            vertex_output = vertex_outputs.get(item.name)
            if vertex_output is None or vertex_output.glsl_type != item.glsl_type:
                continue
            connections[item.name] = T3DInputConnection(node_ref=vertex_ref, output_name=item.name)
        return connections

    def _format_custom_input_property(self, index: int, input_spec: ShaderInputSpec, connection: T3DInputConnection) -> str:
        input_prefix = f'      Inputs({index})=(InputName="{self._escape_string(input_spec.name)}",Input='
        node_ref = connection.node_ref
        expr_ref = f'{node_ref.expr_class}\'"{node_ref.graph_name}.{node_ref.expr_name}"\''
        if input_spec.glsl_type == "sampler2D":
            return input_prefix + f"(Expression={expr_ref}))"
        output_index = self._resolve_connection_output_index(connection)
        output_index_text = f",OutputIndex={output_index}" if output_index else ""
        mask_suffix = self._mask_suffix_for_input(input_spec, connection)
        return input_prefix + f"(Expression={expr_ref}{output_index_text}{mask_suffix}))"

    def _format_expression_input_value(
        self,
        node_ref: T3DNodeRef,
        *,
        output_index: int,
        mask: tuple[int, int, int, int, int],
    ) -> str:
        expr_ref = f'{node_ref.expr_class}\'"{node_ref.graph_name}.{node_ref.expr_name}"\''
        mask_value, mask_r, mask_g, mask_b, mask_a = mask
        parts = [f"Expression={expr_ref}"]
        if output_index:
            parts.append(f"OutputIndex={output_index}")
        if mask_value:
            parts.append("Mask=1")
        if mask_r:
            parts.append("MaskR=1")
        if mask_g:
            parts.append("MaskG=1")
        if mask_b:
            parts.append("MaskB=1")
        if mask_a:
            parts.append("MaskA=1")
        return f"({','.join(parts)})"

    def _resolve_connection_output_index(self, connection: T3DInputConnection) -> int:
        if not connection.output_name:
            return 0
        return connection.node_ref.output_indices.get(connection.output_name, 0)

    def _resolve_connection_pin_id(self, connection: T3DInputConnection) -> str:
        if not connection.output_name:
            return connection.node_ref.default_pin_id
        return connection.node_ref.pin_ids.get(connection.output_name, connection.node_ref.default_pin_id)

    def _mask_suffix_for_input(self, input_spec: ShaderInputSpec, connection: T3DInputConnection) -> str:
        if connection.output_name:
            return ""
        if input_spec.glsl_type in {"vec3", "ivec3"}:
            return ",Mask=1,MaskR=1,MaskG=1,MaskB=1"
        if input_spec.glsl_type in {"vec2", "ivec2"}:
            return ",Mask=1,MaskR=1,MaskG=1"
        if input_spec.glsl_type in {"float", "int", "uint", "bool"}:
            return ",Mask=1,MaskR=1"
        return ""

    def _format_custom_input_pin(self, input_name: str, linked_graph_name: str, linked_pin_id: str) -> str:
        return (
            f'   CustomProperties Pin (PinId={self._new_guid()},PinName="{self._escape_string(input_name)}",'
            'PinType.PinCategory="required",PinType.PinSubCategory="",PinType.PinSubCategoryObject=None,'
            "PinType.PinSubCategoryMemberReference=(),PinType.PinValueType=(),PinType.ContainerType=None,"
            'PinType.bIsReference=False,PinType.bIsConst=False,PinType.bIsWeakPointer=False,PinType.bIsUObjectWrapper=False,'
            f"LinkedTo=({linked_graph_name} {linked_pin_id},),PersistentGuid=00000000000000000000000000000000,"
            "bHidden=False,bNotConnectable=False,bDefaultValueIsReadOnly=False,bDefaultValueIsIgnored=False,"
            "bAdvancedView=False,bOrphanedPin=False,)"
        )

    def _format_required_input_pin(self, pin_name: str, pin_id: str, linked_graph_name: str, linked_pin_id: str) -> str:
        return (
            f'   CustomProperties Pin (PinId={pin_id},PinName="{pin_name}",'
            'PinType.PinCategory="required",PinType.PinSubCategory="",PinType.PinSubCategoryObject=None,'
            "PinType.PinSubCategoryMemberReference=(),PinType.PinValueType=(),PinType.ContainerType=None,"
            'PinType.bIsReference=False,PinType.bIsConst=False,PinType.bIsWeakPointer=False,PinType.bIsUObjectWrapper=False,'
            f"LinkedTo=({linked_graph_name} {linked_pin_id},),PersistentGuid=00000000000000000000000000000000,"
            "bHidden=False,bNotConnectable=False,bDefaultValueIsReadOnly=False,bDefaultValueIsIgnored=False,"
            "bAdvancedView=False,bOrphanedPin=False,)"
        )

    def _format_custom_output_pin(self, output_spec: ShaderOutputSpec, has_additional_outputs: bool, pin_id: str) -> str:
        if has_additional_outputs:
            pin_name = "return" if output_spec.is_primary else output_spec.name
            friendly_name = pin_name
        else:
            pin_name = "Output"
            friendly_name = " "
        return (
            f'   CustomProperties Pin (PinId={pin_id},PinName="{self._escape_string(pin_name)}",'
            f'PinFriendlyName=NSLOCTEXT("MaterialGraphNode", "Space", "{self._escape_string(friendly_name)}"),'
            'Direction="EGPD_Output",PinType.PinCategory="",PinType.PinSubCategory="",PinType.PinSubCategoryObject=None,'
            "PinType.PinSubCategoryMemberReference=(),PinType.PinValueType=(),PinType.ContainerType=None,"
            "PinType.bIsReference=False,PinType.bIsConst=False,PinType.bIsWeakPointer=False,PinType.bIsUObjectWrapper=False,"
            "PersistentGuid=00000000000000000000000000000000,bHidden=False,bNotConnectable=False,"
            "bDefaultValueIsReadOnly=False,bDefaultValueIsIgnored=False,bAdvancedView=False,bOrphanedPin=False,)"
        )

    def _format_parameter_output_pin(self, pin_id: str) -> str:
        return (
            f'   CustomProperties Pin (PinId={pin_id},PinName="Output",'
            'PinFriendlyName=NSLOCTEXT("MaterialGraphNode", "Space", " "),Direction="EGPD_Output",'
            'PinType.PinCategory="mask",PinType.PinSubCategory="",PinType.PinSubCategoryObject=None,'
            "PinType.PinSubCategoryMemberReference=(),PinType.PinValueType=(),PinType.ContainerType=None,"
            "PinType.bIsReference=False,PinType.bIsConst=False,PinType.bIsWeakPointer=False,PinType.bIsUObjectWrapper=False,"
            "PersistentGuid=00000000000000000000000000000000,bHidden=False,bNotConnectable=False,"
            "bDefaultValueIsReadOnly=False,bDefaultValueIsIgnored=False,bAdvancedView=False,bOrphanedPin=False,)"
        )

    def _format_mask_channel_pin(self, pin_id: str, pin_name: str, sub_category: str) -> str:
        return (
            f'   CustomProperties Pin (PinId={pin_id},PinName="{pin_name}",'
            f'PinFriendlyName=NSLOCTEXT("MaterialGraphNode", "Space", " "),Direction="EGPD_Output",'
            f'PinType.PinCategory="mask",PinType.PinSubCategory="{sub_category}",PinType.PinSubCategoryObject=None,'
            "PinType.PinSubCategoryMemberReference=(),PinType.PinValueType=(),PinType.ContainerType=None,"
            "PinType.bIsReference=False,PinType.bIsConst=False,PinType.bIsWeakPointer=False,PinType.bIsUObjectWrapper=False,"
            "PersistentGuid=00000000000000000000000000000000,bHidden=False,bNotConnectable=False,"
            "bDefaultValueIsReadOnly=False,bDefaultValueIsIgnored=False,bAdvancedView=False,bOrphanedPin=False,)"
        )

    @staticmethod
    def _format_hlsl_property(key: str, value: str) -> str:
        escaped = UeMaterialT3DBuilder._escape_t3d_multiline_string(value or "")
        return f'      {key}="{escaped}"'

    @staticmethod
    def _escape_string(text: str) -> str:
        return str(text or "").replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _escape_t3d_multiline_string(text: str) -> str:
        return str(text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\r\\n").replace('"', '\\"')

    @staticmethod
    def _format_scalar_default(value: Any) -> str:
        try:
            return f"{float(value if value is not None else 0.0):.6f}"
        except (TypeError, ValueError):
            return "0.000000"

    @staticmethod
    def _format_vector_default(value: Any) -> str:
        components = [0.0, 0.0, 0.0, 1.0]
        if isinstance(value, list):
            flat = value
            if value and isinstance(value[0], list):
                flat = value[0]
            for index, item in enumerate(list(flat)[:4]):
                try:
                    components[index] = float(item)
                except (TypeError, ValueError):
                    continue
        return (
            f"(R={components[0]:.6f},G={components[1]:.6f},"
            f"B={components[2]:.6f},A={components[3]:.6f})"
        )

    @staticmethod
    def _format_matrix_default(value: Any, glsl_type: str) -> str:
        size = {"mat2": 2, "mat3": 3, "mat4": 4}.get(glsl_type, 4)
        rows: list[list[float]] = []
        if isinstance(value, list):
            if value and isinstance(value[0], list):
                for raw_row in value[:size]:
                    row = []
                    for item in list(raw_row)[:size]:
                        try:
                            row.append(float(item))
                        except (TypeError, ValueError):
                            row.append(0.0)
                    while len(row) < size:
                        row.append(0.0)
                    rows.append(row)
            else:
                flat: list[float] = []
                for item in value[: size * size]:
                    try:
                        flat.append(float(item))
                    except (TypeError, ValueError):
                        flat.append(0.0)
                while len(flat) < size * size:
                    flat.append(0.0)
                rows = [flat[index * size : (index + 1) * size] for index in range(size)]
        while len(rows) < size:
            rows.append([0.0] * size)
        row_chunks = [f"{'float' + str(size)}({', '.join(f'{component:.6f}' for component in row)})" for row in rows[:size]]
        return f"{'float' + str(size) + 'x' + str(size)}({', '.join(row_chunks)})"

    @staticmethod
    def _to_enum_name(name: str) -> str:
        return name.replace(" ", "_")

    @staticmethod
    def _new_guid() -> str:
        return uuid.uuid4().hex.upper()

    def _build_t3d_copy_package(
        self,
        *,
        t3d_text: str,
        fragment_spec: ShaderStageSpec,
        vertex_spec: ShaderStageSpec | None,
        notes: list[str],
        warnings: list[str],
        unsupported: list[str],
    ) -> str:
        sections = [
            "[FragmentNode]",
            f"- output: {fragment_spec.outputs[0].ue_output_type}",
            f"- inputs: {len(fragment_spec.inputs)}",
            "",
            "[FragmentHLSL]",
            fragment_spec.hlsl_code,
            "",
            "[VertexNode]",
            f"- enabled: {'yes' if vertex_spec else 'no'}",
            f"- inputs: {len(vertex_spec.inputs) if vertex_spec else 0}",
            f"- outputs: {len(vertex_spec.outputs) if vertex_spec else 0}",
            f"- root_mapping: {(vertex_spec.root_mapping or {}).get('summary') if vertex_spec and vertex_spec.root_mapping else '无'}",
            "",
            "[VertexHLSL]",
            vertex_spec.hlsl_code if vertex_spec else "未提供 Vertex Shader。",
            "",
            "[T3D]",
            t3d_text,
            "",
            "[Notes]",
            *([f"- {item}" for item in notes] or ["- 无"]),
            "",
            "[Warnings]",
            *([f"- {item}" for item in warnings] or ["- 无"]),
            "",
            "[Unsupported]",
            *([f"- {item}" for item in unsupported] or ["- 无"]),
        ]
        return "\n".join(sections).strip()


class _T3DContext:
    def __init__(self) -> None:
        self.graph_index = 0
        self.expr_indices: dict[str, int] = {}

    def next_graph_name(self) -> str:
        name = f"MaterialGraphNode_{self.graph_index}"
        self.graph_index += 1
        return name

    def next_expression_name(self, base: str) -> str:
        current = self.expr_indices.get(base, 0)
        self.expr_indices[base] = current + 1
        return f"{base}_{current}"

    @staticmethod
    def new_guid() -> str:
        return uuid.uuid4().hex.upper()
