from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.services.fragment_glsl_to_ue426_custom_hlsl import ShaderInputSpec
from app.services.ue_material_t3d_builder import T3DNodeRef, UeMaterialT3DBuilder, _T3DContext


@dataclass(frozen=True)
class ScalarExpr:
    value: float


@dataclass(frozen=True)
class VectorExpr:
    values: tuple[float, ...]


@dataclass(frozen=True)
class VarExpr:
    name: str


@dataclass(frozen=True)
class BinaryExpr:
    op: str
    left: Any
    right: Any


@dataclass(frozen=True)
class CallExpr:
    name: str
    args: tuple[Any, ...]


@dataclass(frozen=True)
class SwizzleExpr:
    base: Any
    mask: str


@dataclass(frozen=True)
class ConstructorExpr:
    type_name: str
    args: tuple[Any, ...]


@dataclass(frozen=True)
class GraphConnection:
    node_ref: T3DNodeRef
    source_width: int
    channels: tuple[int, ...]
    output_index: int = 0

    @property
    def width(self) -> int:
        return len(self.channels)


@dataclass
class StageParseResult:
    stage: str
    body: str
    inputs: list[ShaderInputSpec]
    outputs: list[str]
    locals: dict[str, Any]
    warnings: list[str]
    unsupported: list[str]
    notes: list[str]


class UePureGraphT3DBuilder(UeMaterialT3DBuilder):
    SUPPORTED_FUNCTIONS = {
        "texture",
        "texturelod",
        "texturegrad",
        "mix",
        "clamp",
        "min",
        "max",
        "saturate",
    }
    CONSTRUCTOR_WIDTHS = {
        "vec2": 2,
        "vec3": 3,
        "vec4": 4,
        "float2": 2,
        "float3": 3,
        "float4": 4,
    }
    TOKEN_RE = re.compile(
        r"""
        \s*
        (
            [A-Za-z_]\w* |
            \d+\.\d*|\d*\.\d+|\d+ |
            \.\w+ |
            [()+\-*/=,]
        )
        """,
        re.X,
    )

    def build(
        self,
        *,
        fragment_source: str,
        vertex_source: str = "",
        shader_params_json: str = "",
    ) -> dict[str, Any]:
        notes: list[str] = []
        warnings: list[str] = []
        unsupported: list[str] = []

        vertex_stage = self._parse_stage(
            stage="vertex",
            source=vertex_source,
            shader_params_json=shader_params_json,
            fallback_empty=True,
        )
        fragment_stage = self._parse_stage(
            stage="fragment",
            source=fragment_source,
            shader_params_json=shader_params_json,
            vertex_stage=vertex_stage,
        )

        notes.extend(fragment_stage.notes)
        warnings.extend(fragment_stage.warnings)
        unsupported.extend(fragment_stage.unsupported)
        if vertex_stage is not None:
            notes.extend(f"[vertex] {item}" for item in vertex_stage.notes)
            warnings.extend(f"[vertex] {item}" for item in vertex_stage.warnings)
            unsupported.extend(f"[vertex] {item}" for item in vertex_stage.unsupported)
        else:
            notes.append("未提供 Vertex Shader，纯图模式已跳过 vertex 根语义分析。")

        graph = self._build_pure_graph(fragment_stage=fragment_stage, vertex_stage=vertex_stage)
        notes.extend(graph["notes"])
        unsupported.extend(graph["unsupported"])

        summary_parts = [
            f"纯图节点 {graph['node_count']} 个",
            f"fragment 输出 {graph['fragment_output_status']}",
            f"vertex 根语义 {graph['vertex_root_status']}",
            f"不支持 {len(graph['unsupported']) + len(fragment_stage.unsupported) + (len(vertex_stage.unsupported) if vertex_stage else 0)} 项",
        ]
        return {
            "pure_graph_t3d_text": graph["text"],
            "pure_graph_summary": "；".join(summary_parts),
            "pure_graph_notes": notes,
            "pure_graph_warnings": warnings,
            "pure_graph_unsupported": unsupported,
            "pure_graph_copy_package": self._build_pure_graph_copy_package(
                summary="；".join(summary_parts),
                t3d_text=graph["text"],
                notes=notes,
                warnings=warnings,
                unsupported=unsupported,
            ),
        }

    def _parse_stage(
        self,
        *,
        stage: str,
        source: str,
        shader_params_json: str,
        vertex_stage: StageParseResult | None = None,
        fallback_empty: bool = False,
    ) -> StageParseResult | None:
        text = self._normalize_source(source)
        if not text.strip():
            return None if fallback_empty else StageParseResult(stage, "", [], [], {}, [], [], [])

        warnings: list[str] = []
        unsupported: list[str] = []
        notes: list[str] = []
        cleaned = self._strip_renderdoc_header(text)
        main_range = self._find_main_block(cleaned)
        if main_range is None:
            unsupported.append(f"{stage} shader 未找到 void main()，纯图模式无法解析。")
            return StageParseResult(stage, "", [], [], {}, warnings, unsupported, notes)

        prefix = cleaned[: main_range["start"]].strip()
        prefix, _ = self._extract_known_helper_blocks(prefix)
        body = cleaned[main_range["body_start"] : main_range["body_end"]].strip()
        declarations = self._parse_global_declarations(prefix, warnings, unsupported)
        params_lookup = self._build_params_lookup_for_stage(shader_params_json, stage, warnings)
        vertex_hints = self._build_vertex_hints(text) if stage == "fragment" else {}

        inputs: list[ShaderInputSpec] = []
        for category in ("uniform", "in"):
            for item in declarations[category]:
                if not self._is_identifier_used(body, item["name"]):
                    continue
                spec = self._make_input_spec(
                    item=item,
                    category=category,
                    params_lookup=params_lookup,
                    vertex_hints=vertex_hints,
                    unsupported=unsupported,
                )
                inputs.append(spec)

        statements = self._split_stage_statements(body)
        env: dict[str, Any] = {}
        outputs: list[str] = []
        for statement in statements:
            parsed = self._parse_stage_statement(statement)
            if parsed is None:
                continue
            target, expr = parsed
            if expr is None:
                unsupported.append(f"{stage} 语句暂不支持纯节点化: {statement.strip()}")
                continue
            update_error = self._apply_assignment(env, target, expr)
            if update_error:
                unsupported.append(f"{stage} 赋值暂不支持纯节点化: {statement.strip()} ({update_error})")
                continue
            if target.split(".", 1)[0] == "gl_Position":
                outputs.append("gl_Position")
            else:
                base_name = target.split(".", 1)[0]
                declared_output_names = {item["name"] for item in declarations["out"]}
                if base_name in declared_output_names and base_name not in outputs:
                    outputs.append(base_name)

        if stage == "fragment" and vertex_stage is not None:
            vertex_outputs = {name: vertex_stage.locals.get(name) for name in vertex_stage.outputs}
            for item in inputs:
                if item.category == "in" and item.name in vertex_outputs and vertex_outputs[item.name] is not None:
                    env[item.name] = vertex_outputs[item.name]
                    notes.append(f"fragment 输入 {item.name} 已直接内联 vertex 输出表达式。")

        for item in declarations["const"]:
            statement = str(item.get("statement") or "").replace("const ", "", 1).strip()
            parsed = self._parse_stage_statement(statement)
            if parsed is None:
                continue
            target, expr = parsed
            if expr is not None:
                self._apply_assignment(env, target, expr)

        return StageParseResult(
            stage=stage,
            body=body,
            inputs=inputs,
            outputs=outputs,
            locals=env,
            warnings=warnings,
            unsupported=unsupported,
            notes=notes,
        )

    def _build_pure_graph(
        self,
        *,
        fragment_stage: StageParseResult,
        vertex_stage: StageParseResult | None,
    ) -> dict[str, Any]:
        context = _T3DContext()
        layout = _PureGraphLayout()
        shared_inputs: dict[str, GraphConnection] = {}
        expr_cache: dict[Any, GraphConnection] = {}
        blocks: list[str] = []
        notes: list[str] = []
        unsupported: list[str] = []

        fragment_output_name = self._resolve_fragment_output_name(fragment_stage)
        fragment_expr = fragment_stage.locals.get(fragment_output_name) if fragment_output_name else None
        fragment_status = "未生成"
        if fragment_expr is not None:
            try:
                conn, new_blocks = self._materialize_expr(
                    expr=fragment_expr,
                    stage=fragment_stage,
                    context=context,
                    layout=layout,
                    shared_inputs=shared_inputs,
                    expr_cache=expr_cache,
                )
                blocks.extend(new_blocks)
                fragment_status = f"{fragment_output_name} 已生成"
                notes.append(f"fragment 输出 {fragment_output_name} 已纯节点化。")
                blocks.extend(self._build_terminal_reroute(context, layout, conn, "Pure Fragment Output"))
            except ValueError as exc:
                unsupported.append(f"fragment 输出 {fragment_output_name} 无法纯节点化: {exc}")
                fragment_status = f"{fragment_output_name} 未生成"
        else:
            unsupported.append("fragment 输出表达式未解析成功，纯图模式无法生成结果。")

        vertex_root_status = "未提供 Vertex Shader"
        if vertex_stage is not None:
            vertex_root_status, root_blocks, root_notes, root_unsupported = self._build_vertex_root_candidates(
                stage=vertex_stage,
                context=context,
                layout=layout,
                shared_inputs=shared_inputs,
                expr_cache=expr_cache,
            )
            blocks.extend(root_blocks)
            notes.extend(root_notes)
            unsupported.extend(root_unsupported)

        return {
            "text": "\n".join(blocks).strip(),
            "notes": notes,
            "unsupported": unsupported,
            "node_count": len(re.findall(r'^Begin Object Class=/Script/UnrealEd.MaterialGraphNode', "\n".join(blocks), re.M)),
            "fragment_output_status": fragment_status,
            "vertex_root_status": vertex_root_status,
        }

    def _build_vertex_root_candidates(
        self,
        *,
        stage: StageParseResult,
        context: "_T3DContext",
        layout: "_PureGraphLayout",
        shared_inputs: dict[str, GraphConnection],
        expr_cache: dict[Any, GraphConnection],
    ) -> tuple[str, list[str], list[str], list[str]]:
        blocks: list[str] = []
        notes: list[str] = []
        unsupported: list[str] = []
        generated: list[str] = []

        uv_candidate = stage.locals.get("v_texture0")
        if uv_candidate is not None:
            try:
                uv_expr = self._normalize_customized_uv_expr(uv_candidate)
                if uv_expr is not None:
                    conn, new_blocks = self._materialize_expr(
                        expr=uv_expr,
                        stage=stage,
                        context=context,
                        layout=layout,
                        shared_inputs=shared_inputs,
                        expr_cache=expr_cache,
                    )
                    blocks.extend(new_blocks)
                    blocks.extend(self._build_terminal_reroute(context, layout, conn, "CustomizedUV0 Candidate"))
                    generated.append("CustomizedUV0")
                    notes.append("已将 v_texture0 识别为 CustomizedUV0 候选纯图链路。")
            except ValueError as exc:
                unsupported.append(f"vertex CustomizedUV0 候选无法纯节点化: {exc}")

        gl_position_expr = stage.locals.get("gl_Position")
        if gl_position_expr is not None:
            wpo_expr = self._extract_world_position_offset(gl_position_expr)
            if wpo_expr is not None:
                try:
                    conn, new_blocks = self._materialize_expr(
                        expr=wpo_expr,
                        stage=stage,
                        context=context,
                        layout=layout,
                        shared_inputs=shared_inputs,
                        expr_cache=expr_cache,
                    )
                    blocks.extend(new_blocks)
                    blocks.extend(self._build_terminal_reroute(context, layout, conn, "WorldPositionOffset Candidate"))
                    generated.append("WorldPositionOffset")
                    notes.append("已从 gl_Position 路径提取出简单的 WorldPositionOffset 候选。")
                except ValueError as exc:
                    unsupported.append(f"vertex WorldPositionOffset 候选无法纯节点化: {exc}")
            else:
                unsupported.append("gl_Position 路径未命中纯图可支持的顶点根语义规则。")

        status = "、".join(generated) if generated else "未生成"
        return status, blocks, notes, unsupported

    def _resolve_fragment_output_name(self, stage: StageParseResult) -> str:
        for name in stage.outputs:
            if name != "gl_Position":
                return name
        return ""

    def _materialize_expr(
        self,
        *,
        expr: Any,
        stage: StageParseResult,
        context: "_T3DContext",
        layout: "_PureGraphLayout",
        shared_inputs: dict[str, GraphConnection],
        expr_cache: dict[Any, GraphConnection],
    ) -> tuple[GraphConnection, list[str]]:
        cached = expr_cache.get(expr)
        if cached is not None:
            return cached, []

        blocks: list[str] = []
        if isinstance(expr, ScalarExpr):
            block, conn = self._build_scalar_constant_node(context, layout, expr.value)
            blocks.append(block)
        elif isinstance(expr, VectorExpr):
            block, conn = self._build_vector_constant_node(context, layout, expr.values)
            blocks.append(block)
        elif isinstance(expr, VarExpr):
            if expr.name in stage.locals:
                conn, new_blocks = self._materialize_expr(
                    expr=stage.locals[expr.name],
                    stage=stage,
                    context=context,
                    layout=layout,
                    shared_inputs=shared_inputs,
                    expr_cache=expr_cache,
                )
                blocks.extend(new_blocks)
                expr_cache[expr] = conn
                return conn, blocks
            conn, new_blocks = self._build_stage_input_connection(
                stage=stage,
                context=context,
                layout=layout,
                shared_inputs=shared_inputs,
                name=expr.name,
            )
            blocks.extend(new_blocks)
        elif isinstance(expr, SwizzleExpr):
            base_conn, new_blocks = self._materialize_expr(
                expr=expr.base,
                stage=stage,
                context=context,
                layout=layout,
                shared_inputs=shared_inputs,
                expr_cache=expr_cache,
            )
            blocks.extend(new_blocks)
            conn, extra_blocks = self._apply_swizzle_connection(
                stage=stage,
                context=context,
                layout=layout,
                shared_inputs=shared_inputs,
                expr_cache=expr_cache,
                base_conn=base_conn,
                expr=expr,
            )
            blocks.extend(extra_blocks)
        elif isinstance(expr, BinaryExpr):
            left_conn, left_blocks = self._materialize_expr(
                expr=expr.left,
                stage=stage,
                context=context,
                layout=layout,
                shared_inputs=shared_inputs,
                expr_cache=expr_cache,
            )
            right_conn, right_blocks = self._materialize_expr(
                expr=expr.right,
                stage=stage,
                context=context,
                layout=layout,
                shared_inputs=shared_inputs,
                expr_cache=expr_cache,
            )
            blocks.extend(left_blocks)
            blocks.extend(right_blocks)
            block, conn = self._build_binary_math_node(context, layout, expr.op, left_conn, right_conn)
            blocks.append(block)
        elif isinstance(expr, CallExpr):
            conn, new_blocks = self._materialize_call_expr(
                expr=expr,
                stage=stage,
                context=context,
                layout=layout,
                shared_inputs=shared_inputs,
                expr_cache=expr_cache,
            )
            blocks.extend(new_blocks)
        elif isinstance(expr, ConstructorExpr):
            conn, new_blocks = self._materialize_constructor_expr(
                expr=expr,
                stage=stage,
                context=context,
                layout=layout,
                shared_inputs=shared_inputs,
                expr_cache=expr_cache,
            )
            blocks.extend(new_blocks)
        else:
            raise ValueError(f"未知表达式类型 {type(expr).__name__}")

        expr_cache[expr] = conn
        return conn, blocks

    def _materialize_call_expr(
        self,
        *,
        expr: CallExpr,
        stage: StageParseResult,
        context: "_T3DContext",
        layout: "_PureGraphLayout",
        shared_inputs: dict[str, GraphConnection],
        expr_cache: dict[Any, GraphConnection],
    ) -> tuple[GraphConnection, list[str]]:
        name = expr.name.lower()
        blocks: list[str] = []
        if name not in self.SUPPORTED_FUNCTIONS:
            raise ValueError(f"函数 {expr.name} 不在纯图支持子集内")

        args: list[GraphConnection] = []
        for item in expr.args:
            conn, new_blocks = self._materialize_expr(
                expr=item,
                stage=stage,
                context=context,
                layout=layout,
                shared_inputs=shared_inputs,
                expr_cache=expr_cache,
            )
            args.append(conn)
            blocks.extend(new_blocks)

        if name == "texture":
            block, conn = self._build_texture_sample_node(context, layout, args[0], args[1], "TMVM_None")
            blocks.append(block)
            return conn, blocks
        if name == "texturelod":
            block, conn = self._build_texture_sample_node(context, layout, args[0], args[1], "TMVM_MipLevel")
            blocks.append(block)
            return conn, blocks
        if name == "texturegrad":
            block, conn = self._build_texture_sample_grad_node(context, layout, args[0], args[1], args[2], args[3])
            blocks.append(block)
            return conn, blocks
        if name == "mix":
            block, conn = self._build_lerp_node(context, layout, args[0], args[1], args[2])
            blocks.append(block)
            return conn, blocks
        if name == "clamp":
            block, conn = self._build_clamp_node(context, layout, args[0], args[1], args[2])
            blocks.append(block)
            return conn, blocks
        if name == "saturate":
            block, conn = self._build_unary_math_node(context, layout, "MaterialExpressionSaturate", args[0])
            blocks.append(block)
            return conn, blocks
        if name == "min":
            block, conn = self._build_binary_math_node(context, layout, "min", args[0], args[1])
            blocks.append(block)
            return conn, blocks
        block, conn = self._build_binary_math_node(context, layout, "max", args[0], args[1])
        blocks.append(block)
        return conn, blocks

    def _materialize_constructor_expr(
        self,
        *,
        expr: ConstructorExpr,
        stage: StageParseResult,
        context: "_T3DContext",
        layout: "_PureGraphLayout",
        shared_inputs: dict[str, GraphConnection],
        expr_cache: dict[Any, GraphConnection],
    ) -> tuple[GraphConnection, list[str]]:
        width = self.CONSTRUCTOR_WIDTHS.get(expr.type_name.lower())
        if width is None:
            raise ValueError(f"构造器 {expr.type_name} 不在纯图支持子集内")

        scalar_components: list[GraphConnection] = []
        blocks: list[str] = []
        for item in expr.args:
            conn, new_blocks = self._materialize_expr(
                expr=item,
                stage=stage,
                context=context,
                layout=layout,
                shared_inputs=shared_inputs,
                expr_cache=expr_cache,
            )
            blocks.extend(new_blocks)
            scalar_components.extend(self._explode_to_scalar_connections(conn))

        if len(expr.args) == 1 and scalar_components and len(scalar_components) == 1:
            scalar_components = scalar_components * width
        if len(scalar_components) != width:
            raise ValueError(f"{expr.type_name} 构造器参数宽度不匹配")
        conn, append_blocks = self._assemble_vector_from_components(context, layout, scalar_components)
        blocks.extend(append_blocks)
        return conn, blocks

    def _build_stage_input_connection(
        self,
        *,
        stage: StageParseResult,
        context: "_T3DContext",
        layout: "_PureGraphLayout",
        shared_inputs: dict[str, GraphConnection],
        name: str,
    ) -> tuple[GraphConnection, list[str]]:
        existing = shared_inputs.get(name)
        if existing is not None:
            return existing, []

        spec = next((item for item in stage.inputs if item.name == name), None)
        if spec is None:
            pure_spec = self._build_fallback_input_spec(name)
            if pure_spec is None:
                raise ValueError(f"变量 {name} 既不是局部表达式，也不是可识别输入")
            spec = pure_spec

        if not spec.source_hint and re.fullmatch(r"texcoord(\d+)", spec.name.lower()):
            match = re.fullmatch(r"texcoord(\d+)", spec.name.lower())
            spec = ShaderInputSpec(
                name=spec.name,
                glsl_type=spec.glsl_type,
                ue_input_type=spec.ue_input_type,
                category=spec.category,
                default_value=spec.default_value,
                source_hint=f"builtin:texture_coordinate:{match.group(1) if match else '0'}",
            )
        if str(spec.source_hint).startswith("builtin:transform_matrix:"):
            raise ValueError(
                f"变量 {name} 属于 UE 内建变换矩阵语义，纯图模式不会将其退化成普通参数节点；"
                "只有在被规约为正确的根语义或原生节点时才会生成。"
            )

        node_x, node_y = layout.allocate("Inputs")
        block, node_ref = self._build_input_node(
            context=context,
            input_spec=spec,
            node_x=node_x,
            node_y=node_y,
        )
        source_width = self._guess_input_width(spec, node_ref)
        conn = GraphConnection(node_ref=node_ref, source_width=source_width, channels=tuple(range(source_width)))
        shared_inputs[name] = conn
        return conn, [block]

    def _build_fallback_input_spec(self, name: str) -> ShaderInputSpec | None:
        lower_name = name.lower()
        if lower_name.startswith("texcoord"):
            return ShaderInputSpec(
                name=name,
                glsl_type="vec2",
                ue_input_type="Float2",
                category="in",
                source_hint=f"builtin:texture_coordinate:{lower_name.replace('texcoord', '') or '0'}",
            )
        if lower_name in {"vertexcolor", "diffuse"}:
            return ShaderInputSpec(
                name=name,
                glsl_type="vec4",
                ue_input_type="Float4",
                category="in",
                source_hint="builtin:vertex_color",
            )
        matrix_hint = self._infer_builtin_source_hint(name, "mat4", "uniform")
        if str(matrix_hint).startswith("builtin:transform_matrix:"):
            return ShaderInputSpec(
                name=name,
                glsl_type="mat4",
                ue_input_type="InternalMatrix",
                category="uniform",
                source_hint=matrix_hint,
            )
        return None

    def _guess_input_width(self, spec: ShaderInputSpec, node_ref: T3DNodeRef) -> int:
        if spec.source_hint.startswith("builtin:texture_coordinate:"):
            return 2
        return self._glsl_type_width(spec.glsl_type)

    def _apply_swizzle_connection(
        self,
        *,
        stage: StageParseResult,
        context: "_T3DContext",
        layout: "_PureGraphLayout",
        shared_inputs: dict[str, GraphConnection],
        expr_cache: dict[Any, GraphConnection],
        base_conn: GraphConnection,
        expr: SwizzleExpr,
    ) -> tuple[GraphConnection, list[str]]:
        indices = tuple(self._channel_index(ch) for ch in expr.mask)
        if len(indices) == 1:
            return GraphConnection(
                node_ref=base_conn.node_ref,
                source_width=base_conn.source_width,
                channels=(base_conn.channels[indices[0]],),
                output_index=base_conn.output_index,
            ), []
        if len(set(indices)) == len(indices) and list(indices) == sorted(indices):
            return GraphConnection(
                node_ref=base_conn.node_ref,
                source_width=base_conn.source_width,
                channels=tuple(base_conn.channels[index] for index in indices),
                output_index=base_conn.output_index,
            ), []

        scalar_connections = [
            GraphConnection(
                node_ref=base_conn.node_ref,
                source_width=base_conn.source_width,
                channels=(base_conn.channels[index],),
                output_index=base_conn.output_index,
            )
            for index in indices
        ]
        return self._assemble_vector_from_components(context, layout, scalar_connections)

    def _assemble_vector_from_components(
        self,
        context: "_T3DContext",
        layout: "_PureGraphLayout",
        components: list[GraphConnection],
    ) -> tuple[GraphConnection, list[str]]:
        if len(components) == 1:
            return components[0], []
        blocks: list[str] = []
        current = components[0]
        for next_item in components[1:]:
            block, current = self._build_append_node(context, layout, current, next_item)
            blocks.append(block)
        return current, blocks

    def _explode_to_scalar_connections(self, conn: GraphConnection) -> list[GraphConnection]:
        if conn.width == 1:
            return [conn]
        return [
            GraphConnection(
                node_ref=conn.node_ref,
                source_width=conn.source_width,
                channels=(channel,),
                output_index=conn.output_index,
            )
            for channel in conn.channels
        ]

    def _build_scalar_constant_node(
        self,
        context: "_T3DContext",
        layout: "_PureGraphLayout",
        value: float,
    ) -> tuple[str, GraphConnection]:
        expr_name = context.next_expression_name("MaterialExpressionConstant")
        graph_name = context.next_graph_name()
        pin_id = context.new_guid()
        node_x, node_y = layout.allocate("Inputs")
        lines = [
            f'Begin Object Class=/Script/UnrealEd.MaterialGraphNode Name="{graph_name}"',
            f'   Begin Object Class=/Script/Engine.MaterialExpressionConstant Name="{expr_name}"',
            "   End Object",
            f'   Begin Object Name="{expr_name}"',
            f"      R={value:.6f}",
            f"      MaterialExpressionEditorX={node_x}",
            f"      MaterialExpressionEditorY={node_y}",
            f"      MaterialExpressionGuid={context.new_guid()}",
            '      Material=PreviewMaterial\'"/Engine/Transient.NewMaterial"\'',
            "   End Object",
            f'   MaterialExpression=MaterialExpressionConstant\'"{expr_name}"\'',
            f"   NodePosX={node_x}",
            f"   NodePosY={node_y}",
            f"   NodeGuid={context.new_guid()}",
            self._format_parameter_output_pin(pin_id),
            "End Object",
        ]
        conn = GraphConnection(
            node_ref=T3DNodeRef(graph_name=graph_name, expr_class="MaterialExpressionConstant", expr_name=expr_name, default_pin_id=pin_id),
            source_width=1,
            channels=(0,),
        )
        return "\n".join(lines), conn

    def _build_vector_constant_node(
        self,
        context: "_T3DContext",
        layout: "_PureGraphLayout",
        values: tuple[float, ...],
    ) -> tuple[str, GraphConnection]:
        width = len(values)
        node_x, node_y = layout.allocate("Inputs")
        pin_id = context.new_guid()
        if width == 2:
            expr_class = "MaterialExpressionConstant2Vector"
            expr_name = context.next_expression_name(expr_class)
            graph_name = context.next_graph_name()
            props = [f"      R={values[0]:.6f}", f"      G={values[1]:.6f}"]
        elif width == 3:
            expr_class = "MaterialExpressionConstant3Vector"
            expr_name = context.next_expression_name(expr_class)
            graph_name = context.next_graph_name()
            props = [f"      Constant=(R={values[0]:.6f},G={values[1]:.6f},B={values[2]:.6f},A=1.000000)"]
        else:
            expr_class = "MaterialExpressionConstant4Vector"
            expr_name = context.next_expression_name(expr_class)
            graph_name = context.next_graph_name()
            props = [f"      Constant=(R={values[0]:.6f},G={values[1]:.6f},B={values[2]:.6f},A={values[3]:.6f})"]
        lines = [
            f'Begin Object Class=/Script/UnrealEd.MaterialGraphNode Name="{graph_name}"',
            f'   Begin Object Class=/Script/Engine.{expr_class} Name="{expr_name}"',
            "   End Object",
            f'   Begin Object Name="{expr_name}"',
            *props,
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
        conn = GraphConnection(
            node_ref=T3DNodeRef(graph_name=graph_name, expr_class=expr_class, expr_name=expr_name, default_pin_id=pin_id),
            source_width=width,
            channels=tuple(range(width)),
        )
        return "\n".join(lines), conn

    def _build_binary_math_node(
        self,
        context: "_T3DContext",
        layout: "_PureGraphLayout",
        op: str,
        left: GraphConnection,
        right: GraphConnection,
    ) -> tuple[str, GraphConnection]:
        mapping = {
            "+": "MaterialExpressionAdd",
            "-": "MaterialExpressionSubtract",
            "*": "MaterialExpressionMultiply",
            "/": "MaterialExpressionDivide",
            "min": "MaterialExpressionMin",
            "max": "MaterialExpressionMax",
        }
        expr_class = mapping.get(op)
        if expr_class is None:
            raise ValueError(f"二元运算 {op} 不在纯图支持子集内")
        expr_name = context.next_expression_name(expr_class)
        graph_name = context.next_graph_name()
        input_a_pin_id = context.new_guid()
        input_b_pin_id = context.new_guid()
        output_pin_id = context.new_guid()
        node_x, node_y = layout.allocate("ColorMath")
        lines = [
            f'Begin Object Class=/Script/UnrealEd.MaterialGraphNode Name="{graph_name}"',
            f'   Begin Object Class=/Script/Engine.{expr_class} Name="{expr_name}"',
            "   End Object",
            f'   Begin Object Name="{expr_name}"',
            f"      A={self._format_graph_input_value(left)}",
            f"      B={self._format_graph_input_value(right)}",
            f"      MaterialExpressionEditorX={node_x}",
            f"      MaterialExpressionEditorY={node_y}",
            f"      MaterialExpressionGuid={context.new_guid()}",
            '      Material=PreviewMaterial\'"/Engine/Transient.NewMaterial"\'',
            "   End Object",
            f'   MaterialExpression={expr_class}\'"{expr_name}"\'',
            f"   NodePosX={node_x}",
            f"   NodePosY={node_y}",
            f"   NodeGuid={context.new_guid()}",
            self._format_required_input_pin("A", input_a_pin_id, left.node_ref.graph_name, left.node_ref.default_pin_id),
            self._format_required_input_pin("B", input_b_pin_id, right.node_ref.graph_name, right.node_ref.default_pin_id),
            self._format_parameter_output_pin(output_pin_id),
            "End Object",
        ]
        conn = GraphConnection(
            node_ref=T3DNodeRef(graph_name=graph_name, expr_class=expr_class, expr_name=expr_name, default_pin_id=output_pin_id),
            source_width=max(left.width, right.width),
            channels=tuple(range(max(left.width, right.width))),
        )
        return "\n".join(lines), conn

    def _build_unary_math_node(
        self,
        context: "_T3DContext",
        layout: "_PureGraphLayout",
        expr_class: str,
        value: GraphConnection,
    ) -> tuple[str, GraphConnection]:
        expr_name = context.next_expression_name(expr_class)
        graph_name = context.next_graph_name()
        input_pin_id = context.new_guid()
        output_pin_id = context.new_guid()
        node_x, node_y = layout.allocate("ColorMath")
        lines = [
            f'Begin Object Class=/Script/UnrealEd.MaterialGraphNode Name="{graph_name}"',
            f'   Begin Object Class=/Script/Engine.{expr_class} Name="{expr_name}"',
            "   End Object",
            f'   Begin Object Name="{expr_name}"',
            f"      Input={self._format_graph_input_value(value)}",
            f"      MaterialExpressionEditorX={node_x}",
            f"      MaterialExpressionEditorY={node_y}",
            f"      MaterialExpressionGuid={context.new_guid()}",
            '      Material=PreviewMaterial\'"/Engine/Transient.NewMaterial"\'',
            "   End Object",
            f'   MaterialExpression={expr_class}\'"{expr_name}"\'',
            f"   NodePosX={node_x}",
            f"   NodePosY={node_y}",
            f"   NodeGuid={context.new_guid()}",
            self._format_required_input_pin("Input", input_pin_id, value.node_ref.graph_name, value.node_ref.default_pin_id),
            self._format_parameter_output_pin(output_pin_id),
            "End Object",
        ]
        conn = GraphConnection(
            node_ref=T3DNodeRef(graph_name=graph_name, expr_class=expr_class, expr_name=expr_name, default_pin_id=output_pin_id),
            source_width=value.width,
            channels=tuple(range(value.width)),
        )
        return "\n".join(lines), conn

    def _build_lerp_node(
        self,
        context: "_T3DContext",
        layout: "_PureGraphLayout",
        a: GraphConnection,
        b: GraphConnection,
        alpha: GraphConnection,
    ) -> tuple[str, GraphConnection]:
        expr_class = "MaterialExpressionLinearInterpolate"
        expr_name = context.next_expression_name(expr_class)
        graph_name = context.next_graph_name()
        pin_a = context.new_guid()
        pin_b = context.new_guid()
        pin_alpha = context.new_guid()
        pin_out = context.new_guid()
        node_x, node_y = layout.allocate("ColorMath")
        lines = [
            f'Begin Object Class=/Script/UnrealEd.MaterialGraphNode Name="{graph_name}"',
            f'   Begin Object Class=/Script/Engine.{expr_class} Name="{expr_name}"',
            "   End Object",
            f'   Begin Object Name="{expr_name}"',
            f"      A={self._format_graph_input_value(a)}",
            f"      B={self._format_graph_input_value(b)}",
            f"      Alpha={self._format_graph_input_value(alpha)}",
            f"      MaterialExpressionEditorX={node_x}",
            f"      MaterialExpressionEditorY={node_y}",
            f"      MaterialExpressionGuid={context.new_guid()}",
            '      Material=PreviewMaterial\'"/Engine/Transient.NewMaterial"\'',
            "   End Object",
            f'   MaterialExpression={expr_class}\'"{expr_name}"\'',
            f"   NodePosX={node_x}",
            f"   NodePosY={node_y}",
            f"   NodeGuid={context.new_guid()}",
            self._format_required_input_pin("A", pin_a, a.node_ref.graph_name, a.node_ref.default_pin_id),
            self._format_required_input_pin("B", pin_b, b.node_ref.graph_name, b.node_ref.default_pin_id),
            self._format_required_input_pin("Alpha", pin_alpha, alpha.node_ref.graph_name, alpha.node_ref.default_pin_id),
            self._format_parameter_output_pin(pin_out),
            "End Object",
        ]
        conn = GraphConnection(
            node_ref=T3DNodeRef(graph_name=graph_name, expr_class=expr_class, expr_name=expr_name, default_pin_id=pin_out),
            source_width=max(a.width, b.width),
            channels=tuple(range(max(a.width, b.width))),
        )
        return "\n".join(lines), conn

    def _build_clamp_node(
        self,
        context: "_T3DContext",
        layout: "_PureGraphLayout",
        value: GraphConnection,
        min_value: GraphConnection,
        max_value: GraphConnection,
    ) -> tuple[str, GraphConnection]:
        expr_class = "MaterialExpressionClamp"
        expr_name = context.next_expression_name(expr_class)
        graph_name = context.next_graph_name()
        pin_input = context.new_guid()
        pin_min = context.new_guid()
        pin_max = context.new_guid()
        pin_out = context.new_guid()
        node_x, node_y = layout.allocate("ColorMath")
        lines = [
            f'Begin Object Class=/Script/UnrealEd.MaterialGraphNode Name="{graph_name}"',
            f'   Begin Object Class=/Script/Engine.{expr_class} Name="{expr_name}"',
            "   End Object",
            f'   Begin Object Name="{expr_name}"',
            f"      Input={self._format_graph_input_value(value)}",
            f"      Min={self._format_graph_input_value(min_value)}",
            f"      Max={self._format_graph_input_value(max_value)}",
            "      ClampMode=CMODE_Clamp",
            "      MinDefault=0.000000",
            "      MaxDefault=1.000000",
            f"      MaterialExpressionEditorX={node_x}",
            f"      MaterialExpressionEditorY={node_y}",
            f"      MaterialExpressionGuid={context.new_guid()}",
            '      Material=PreviewMaterial\'"/Engine/Transient.NewMaterial"\'',
            "   End Object",
            f'   MaterialExpression={expr_class}\'"{expr_name}"\'',
            f"   NodePosX={node_x}",
            f"   NodePosY={node_y}",
            f"   NodeGuid={context.new_guid()}",
            self._format_required_input_pin("Input", pin_input, value.node_ref.graph_name, value.node_ref.default_pin_id),
            self._format_required_input_pin("Min", pin_min, min_value.node_ref.graph_name, min_value.node_ref.default_pin_id),
            self._format_required_input_pin("Max", pin_max, max_value.node_ref.graph_name, max_value.node_ref.default_pin_id),
            self._format_parameter_output_pin(pin_out),
            "End Object",
        ]
        conn = GraphConnection(
            node_ref=T3DNodeRef(graph_name=graph_name, expr_class=expr_class, expr_name=expr_name, default_pin_id=pin_out),
            source_width=value.width,
            channels=tuple(range(value.width)),
        )
        return "\n".join(lines), conn

    def _build_append_node(
        self,
        context: "_T3DContext",
        layout: "_PureGraphLayout",
        a: GraphConnection,
        b: GraphConnection,
    ) -> tuple[str, GraphConnection]:
        expr_class = "MaterialExpressionAppendVector"
        expr_name = context.next_expression_name(expr_class)
        graph_name = context.next_graph_name()
        pin_a = context.new_guid()
        pin_b = context.new_guid()
        pin_out = context.new_guid()
        node_x, node_y = layout.allocate("ColorMath")
        lines = [
            f'Begin Object Class=/Script/UnrealEd.MaterialGraphNode Name="{graph_name}"',
            f'   Begin Object Class=/Script/Engine.{expr_class} Name="{expr_name}"',
            "   End Object",
            f'   Begin Object Name="{expr_name}"',
            f"      A={self._format_graph_input_value(a)}",
            f"      B={self._format_graph_input_value(b)}",
            f"      MaterialExpressionEditorX={node_x}",
            f"      MaterialExpressionEditorY={node_y}",
            f"      MaterialExpressionGuid={context.new_guid()}",
            '      Material=PreviewMaterial\'"/Engine/Transient.NewMaterial"\'',
            "   End Object",
            f'   MaterialExpression={expr_class}\'"{expr_name}"\'',
            f"   NodePosX={node_x}",
            f"   NodePosY={node_y}",
            f"   NodeGuid={context.new_guid()}",
            self._format_required_input_pin("A", pin_a, a.node_ref.graph_name, a.node_ref.default_pin_id),
            self._format_required_input_pin("B", pin_b, b.node_ref.graph_name, b.node_ref.default_pin_id),
            self._format_parameter_output_pin(pin_out),
            "End Object",
        ]
        total_width = a.width + b.width
        conn = GraphConnection(
            node_ref=T3DNodeRef(graph_name=graph_name, expr_class=expr_class, expr_name=expr_name, default_pin_id=pin_out),
            source_width=total_width,
            channels=tuple(range(total_width)),
        )
        return "\n".join(lines), conn

    def _build_texture_sample_node(
        self,
        context: "_T3DContext",
        layout: "_PureGraphLayout",
        texture: GraphConnection,
        coords: GraphConnection,
        mip_value_mode: str,
    ) -> tuple[str, GraphConnection]:
        expr_class = "MaterialExpressionTextureSample"
        expr_name = context.next_expression_name(expr_class)
        graph_name = context.next_graph_name()
        pin_tex = context.new_guid()
        pin_uv = context.new_guid()
        pin_out = context.new_guid()
        node_x, node_y = layout.allocate("SamplingAndUV")
        lines = [
            f'Begin Object Class=/Script/UnrealEd.MaterialGraphNode Name="{graph_name}"',
            f'   Begin Object Class=/Script/Engine.{expr_class} Name="{expr_name}"',
            "   End Object",
            f'   Begin Object Name="{expr_name}"',
            f"      Coordinates={self._format_graph_input_value(coords)}",
            f"      TextureObject={self._format_graph_input_value(texture)}",
            f"      MipValueMode={mip_value_mode}",
            "      SamplerSource=SSM_FromTextureAsset",
            "      ConstCoordinate=0",
            f"      MaterialExpressionEditorX={node_x}",
            f"      MaterialExpressionEditorY={node_y}",
            f"      MaterialExpressionGuid={context.new_guid()}",
            '      Material=PreviewMaterial\'"/Engine/Transient.NewMaterial"\'',
            "   End Object",
            f'   MaterialExpression={expr_class}\'"{expr_name}"\'',
            f"   NodePosX={node_x}",
            f"   NodePosY={node_y}",
            f"   NodeGuid={context.new_guid()}",
            self._format_required_input_pin("Coordinates", pin_uv, coords.node_ref.graph_name, coords.node_ref.default_pin_id),
            self._format_required_input_pin("TextureObject", pin_tex, texture.node_ref.graph_name, texture.node_ref.default_pin_id),
            self._format_parameter_output_pin(pin_out),
            "End Object",
        ]
        conn = GraphConnection(
            node_ref=T3DNodeRef(graph_name=graph_name, expr_class=expr_class, expr_name=expr_name, default_pin_id=pin_out),
            source_width=4,
            channels=(0, 1, 2, 3),
        )
        return "\n".join(lines), conn

    def _build_texture_sample_grad_node(
        self,
        context: "_T3DContext",
        layout: "_PureGraphLayout",
        texture: GraphConnection,
        coords: GraphConnection,
        ddx_value: GraphConnection,
        ddy_value: GraphConnection,
    ) -> tuple[str, GraphConnection]:
        expr_class = "MaterialExpressionTextureSample"
        expr_name = context.next_expression_name(expr_class)
        graph_name = context.next_graph_name()
        pin_tex = context.new_guid()
        pin_uv = context.new_guid()
        pin_dx = context.new_guid()
        pin_dy = context.new_guid()
        pin_out = context.new_guid()
        node_x, node_y = layout.allocate("SamplingAndUV")
        lines = [
            f'Begin Object Class=/Script/UnrealEd.MaterialGraphNode Name="{graph_name}"',
            f'   Begin Object Class=/Script/Engine.{expr_class} Name="{expr_name}"',
            "   End Object",
            f'   Begin Object Name="{expr_name}"',
            f"      Coordinates={self._format_graph_input_value(coords)}",
            f"      TextureObject={self._format_graph_input_value(texture)}",
            f"      CoordinatesDX={self._format_graph_input_value(ddx_value)}",
            f"      CoordinatesDY={self._format_graph_input_value(ddy_value)}",
            "      MipValueMode=TMVM_Derivative",
            "      SamplerSource=SSM_FromTextureAsset",
            "      ConstCoordinate=0",
            f"      MaterialExpressionEditorX={node_x}",
            f"      MaterialExpressionEditorY={node_y}",
            f"      MaterialExpressionGuid={context.new_guid()}",
            '      Material=PreviewMaterial\'"/Engine/Transient.NewMaterial"\'',
            "   End Object",
            f'   MaterialExpression={expr_class}\'"{expr_name}"\'',
            f"   NodePosX={node_x}",
            f"   NodePosY={node_y}",
            f"   NodeGuid={context.new_guid()}",
            self._format_required_input_pin("Coordinates", pin_uv, coords.node_ref.graph_name, coords.node_ref.default_pin_id),
            self._format_required_input_pin("TextureObject", pin_tex, texture.node_ref.graph_name, texture.node_ref.default_pin_id),
            self._format_required_input_pin("CoordinatesDX", pin_dx, ddx_value.node_ref.graph_name, ddx_value.node_ref.default_pin_id),
            self._format_required_input_pin("CoordinatesDY", pin_dy, ddy_value.node_ref.graph_name, ddy_value.node_ref.default_pin_id),
            self._format_parameter_output_pin(pin_out),
            "End Object",
        ]
        conn = GraphConnection(
            node_ref=T3DNodeRef(graph_name=graph_name, expr_class=expr_class, expr_name=expr_name, default_pin_id=pin_out),
            source_width=4,
            channels=(0, 1, 2, 3),
        )
        return "\n".join(lines), conn

    def _build_terminal_reroute(
        self,
        context: "_T3DContext",
        layout: "_PureGraphLayout",
        value: GraphConnection,
        title: str,
    ) -> list[str]:
        expr_class = "MaterialExpressionReroute"
        expr_name = context.next_expression_name(expr_class)
        graph_name = context.next_graph_name()
        input_pin_id = context.new_guid()
        output_pin_id = context.new_guid()
        node_x, node_y = layout.allocate("MaterialOutputs")
        lines = [
            f'Begin Object Class=/Script/UnrealEd.MaterialGraphNode Name="{graph_name}"',
            f'   Begin Object Class=/Script/Engine.{expr_class} Name="{expr_name}"',
            "   End Object",
            f'   Begin Object Name="{expr_name}"',
            f"      Input={self._format_graph_input_value(value)}",
            f"      MaterialExpressionEditorX={node_x}",
            f"      MaterialExpressionEditorY={node_y}",
            f"      MaterialExpressionGuid={context.new_guid()}",
            '      Material=PreviewMaterial\'"/Engine/Transient.NewMaterial"\'',
            "   End Object",
            f'   MaterialExpression={expr_class}\'"{expr_name}"\'',
            f"   NodePosX={node_x}",
            f"   NodePosY={node_y}",
            "   bCanRenameNode=True",
            f"   NodeComment=\"{self._escape_string(title)}\"",
            f"   NodeGuid={context.new_guid()}",
            self._format_required_input_pin("Input", input_pin_id, value.node_ref.graph_name, value.node_ref.default_pin_id),
            self._format_parameter_output_pin(output_pin_id),
            "End Object",
        ]
        return ["\n".join(lines)]

    def _format_graph_input_value(self, value: GraphConnection) -> str:
        expr_ref = f'{value.node_ref.expr_class}\'"{value.node_ref.graph_name}.{value.node_ref.expr_name}"\''
        parts = [f"Expression={expr_ref}"]
        if value.output_index:
            parts.append(f"OutputIndex={value.output_index}")
        if not (value.source_width == value.width and value.channels == tuple(range(value.width))):
            parts.append("Mask=1")
            if 0 in value.channels:
                parts.append("MaskR=1")
            if 1 in value.channels:
                parts.append("MaskG=1")
            if 2 in value.channels:
                parts.append("MaskB=1")
            if 3 in value.channels:
                parts.append("MaskA=1")
        return f"({','.join(parts)})"

    def _normalize_customized_uv_expr(self, expr: Any) -> Any | None:
        if isinstance(expr, SwizzleExpr) and expr.mask.startswith("xy"):
            return SwizzleExpr(expr.base, expr.mask[:2])
        if isinstance(expr, ConstructorExpr) and expr.type_name.lower() in {"vec4", "float4"} and expr.args:
            first = expr.args[0]
            if isinstance(first, SwizzleExpr) and first.mask == "xy":
                return first
        return None

    def _extract_world_position_offset(self, expr: Any) -> Any | None:
        if not isinstance(expr, BinaryExpr) or expr.op != "*":
            return None
        if not isinstance(expr.left, VarExpr) or expr.left.name not in {"WorldViewProjection", "ViewProjection"}:
            return None
        if not isinstance(expr.right, ConstructorExpr) or expr.right.type_name.lower() not in {"vec4", "float4"}:
            return None
        if len(expr.right.args) != 2:
            return None
        xyz_expr, w_expr = expr.right.args
        if not isinstance(w_expr, ScalarExpr) or abs(w_expr.value - 1.0) > 1e-6:
            return None
        if not isinstance(xyz_expr, BinaryExpr) or xyz_expr.op != "+":
            return None
        left = xyz_expr.left
        right = xyz_expr.right
        if isinstance(left, SwizzleExpr) and isinstance(left.base, VarExpr) and left.base.name == "position" and left.mask == "xyz":
            return right
        return None

    def _split_stage_statements(self, body: str) -> list[str]:
        text = re.sub(r"//.*", "", body or "")
        statements: list[str] = []
        current: list[str] = []
        depth = 0
        for char in text:
            if char == ";" and depth == 0:
                statement = "".join(current).strip()
                if statement:
                    statements.append(statement)
                current = []
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                depth = max(depth - 1, 0)
            current.append(char)
        tail = "".join(current).strip()
        if tail:
            statements.append(tail)
        return statements

    def _parse_stage_statement(self, statement: str) -> tuple[str, Any | None] | None:
        text = statement.strip()
        if not text or text == "void main()":
            return None
        eq_index = self._find_top_level_equals(text)
        if eq_index < 0:
            return None
        left = text[:eq_index].strip()
        right = text[eq_index + 1 :].strip()
        left = re.sub(r"^(const\s+)?(?:[A-Za-z_]\w*)\s+", "", left)
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[xyzwrgba]{1,4})?", left):
            return left, None
        try:
            return left, _ExprParser.parse(right)
        except ValueError:
            return left, None

    def _apply_assignment(self, env: dict[str, Any], target: str, expr: Any) -> str | None:
        if "." not in target:
            env[target] = expr
            return None
        base_name, mask = target.split(".", 1)
        base_expr = env.get(base_name)
        if base_expr is None:
            return "目标变量尚未初始化"
        expr = self._substitute_var(expr, base_name, base_expr)
        base_width = self._infer_expr_width(base_expr, env)
        target_indices = [self._channel_index(item) for item in mask]
        if len(set(target_indices)) != len(target_indices):
            return "重复 swizzle 写入暂不支持"
        source_width = self._infer_expr_width(expr, env)
        if source_width != len(target_indices):
            return "swizzle 写入宽度不匹配"
        base_components = self._explode_expr_to_components(base_expr, env)
        source_components = self._explode_expr_to_components(expr, env)
        if len(base_components) != base_width or len(source_components) != source_width:
            return "swizzle 写入分量展开失败"
        for index, component in zip(target_indices, source_components):
            base_components[index] = component
        env[base_name] = self._components_to_constructor(base_components)
        if base_name == "gl_Position":
            env["gl_Position"] = env[base_name]
        return None

    def _substitute_var(self, expr: Any, target_name: str, replacement: Any) -> Any:
        if isinstance(expr, VarExpr):
            return replacement if expr.name == target_name else expr
        if isinstance(expr, SwizzleExpr):
            return SwizzleExpr(self._substitute_var(expr.base, target_name, replacement), expr.mask)
        if isinstance(expr, BinaryExpr):
            return BinaryExpr(
                expr.op,
                self._substitute_var(expr.left, target_name, replacement),
                self._substitute_var(expr.right, target_name, replacement),
            )
        if isinstance(expr, CallExpr):
            return CallExpr(
                expr.name,
                tuple(self._substitute_var(item, target_name, replacement) for item in expr.args),
            )
        if isinstance(expr, ConstructorExpr):
            return ConstructorExpr(
                expr.type_name,
                tuple(self._substitute_var(item, target_name, replacement) for item in expr.args),
            )
        return expr

    def _find_top_level_equals(self, text: str) -> int:
        depth = 0
        for index, char in enumerate(text):
            if char == "(":
                depth += 1
            elif char == ")":
                depth = max(depth - 1, 0)
            elif char == "=" and depth == 0:
                return index
        return -1

    def _infer_expr_width(self, expr: Any, env: dict[str, Any]) -> int:
        if isinstance(expr, ScalarExpr):
            return 1
        if isinstance(expr, VectorExpr):
            return len(expr.values)
        if isinstance(expr, VarExpr):
            if expr.name in env:
                return self._infer_expr_width(env[expr.name], env)
            fallback = self._build_fallback_input_spec(expr.name)
            if fallback is not None:
                return self._glsl_type_width(fallback.glsl_type)
            return 1
        if isinstance(expr, SwizzleExpr):
            return len(expr.mask)
        if isinstance(expr, BinaryExpr):
            return max(self._infer_expr_width(expr.left, env), self._infer_expr_width(expr.right, env))
        if isinstance(expr, ConstructorExpr):
            return self.CONSTRUCTOR_WIDTHS.get(expr.type_name.lower(), 1)
        if isinstance(expr, CallExpr):
            name = expr.name.lower()
            if name.startswith("texture"):
                return 4
            if expr.args:
                return self._infer_expr_width(expr.args[0], env)
        return 1

    def _explode_expr_to_components(self, expr: Any, env: dict[str, Any]) -> list[Any]:
        if isinstance(expr, ScalarExpr):
            return [expr]
        if isinstance(expr, VectorExpr):
            return [ScalarExpr(item) for item in expr.values]
        if isinstance(expr, VarExpr):
            if expr.name in env:
                return self._explode_expr_to_components(env[expr.name], env)
            fallback = self._build_fallback_input_spec(expr.name)
            if fallback is not None:
                width = self._glsl_type_width(fallback.glsl_type)
                return [SwizzleExpr(expr, "xyzw"[index]) for index in range(width)]
            return [expr]
        if isinstance(expr, SwizzleExpr):
            return [SwizzleExpr(expr.base, channel) for channel in expr.mask]
        if isinstance(expr, ConstructorExpr):
            components: list[Any] = []
            for item in expr.args:
                components.extend(self._explode_expr_to_components(item, env))
            if len(expr.args) == 1 and len(components) == 1:
                return components * self._infer_expr_width(expr, env)
            return components
        if isinstance(expr, BinaryExpr) or isinstance(expr, CallExpr):
            width = self._infer_expr_width(expr, env)
            if width == 1:
                return [expr]
            return [SwizzleExpr(expr, "xyzw"[index]) for index in range(width)]
        return [expr]

    def _components_to_constructor(self, components: list[Any]) -> Any:
        if len(components) == 1:
            return components[0]
        return ConstructorExpr(f"vec{len(components)}", tuple(components))

    def _channel_index(self, channel: str) -> int:
        mapping = {"x": 0, "r": 0, "y": 1, "g": 1, "z": 2, "b": 2, "w": 3, "a": 3}
        return mapping[channel]

    def _glsl_type_width(self, glsl_type: str) -> int:
        return {
            "float": 1,
            "int": 1,
            "uint": 1,
            "bool": 1,
            "vec2": 2,
            "ivec2": 2,
            "uvec2": 2,
            "vec3": 3,
            "ivec3": 3,
            "uvec3": 3,
            "vec4": 4,
            "ivec4": 4,
            "uvec4": 4,
            "sampler2D": 1,
        }.get(glsl_type, 1)

    def _build_pure_graph_copy_package(
        self,
        *,
        summary: str,
        t3d_text: str,
        notes: list[str],
        warnings: list[str],
        unsupported: list[str],
    ) -> str:
        sections = [
            "[PureGraphSummary]",
            summary,
            "",
            "[PureGraphT3D]",
            t3d_text or "未生成",
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


class _PureGraphLayout:
    def __init__(self) -> None:
        self.x_map = {
            "Inputs": -980,
            "SamplingAndUV": -620,
            "ColorMath": -220,
            "VertexFlow": 160,
            "MaterialOutputs": 540,
        }
        self.y_map = {
            "Inputs": -420,
            "SamplingAndUV": -120,
            "ColorMath": 180,
            "VertexFlow": 520,
            "MaterialOutputs": -40,
        }

    def allocate(self, group: str) -> tuple[int, int]:
        x = self.x_map[group]
        y = self.y_map[group]
        self.y_map[group] += 170
        return x, y


class _ExprParser:
    def __init__(self, text: str) -> None:
        self.tokens = self._tokenize(text)
        self.index = 0

    @classmethod
    def parse(cls, text: str) -> Any:
        parser = cls(text)
        expr = parser.parse_expression()
        if parser.index != len(parser.tokens):
            raise ValueError("unexpected trailing tokens")
        return expr

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        tokens = [match.group(1) for match in UePureGraphT3DBuilder.TOKEN_RE.finditer(text)]
        compact = "".join(tokens).replace(" ", "")
        original = re.sub(r"\s+", "", text)
        if compact != original:
            raise ValueError("unsupported token")
        return tokens

    def parse_expression(self) -> Any:
        return self.parse_additive()

    def parse_additive(self) -> Any:
        expr = self.parse_multiplicative()
        while self.peek() in {"+", "-"}:
            op = self.consume()
            right = self.parse_multiplicative()
            expr = BinaryExpr(op, expr, right)
        return expr

    def parse_multiplicative(self) -> Any:
        expr = self.parse_unary()
        while self.peek() in {"*", "/"}:
            op = self.consume()
            right = self.parse_unary()
            expr = BinaryExpr(op, expr, right)
        return expr

    def parse_unary(self) -> Any:
        token = self.peek()
        if token == "+":
            self.consume()
            return self.parse_unary()
        if token == "-":
            self.consume()
            return BinaryExpr("*", ScalarExpr(-1.0), self.parse_unary())
        return self.parse_postfix()

    def parse_postfix(self) -> Any:
        expr = self.parse_primary()
        while self.peek() and self.peek().startswith("."):
            expr = SwizzleExpr(expr, self.consume()[1:])
        return expr

    def parse_primary(self) -> Any:
        token = self.peek()
        if token is None:
            raise ValueError("unexpected end")
        if token == "(":
            self.consume()
            expr = self.parse_expression()
            self.expect(")")
            return expr
        if re.fullmatch(r"\d+\.\d*|\d*\.\d+|\d+", token):
            self.consume()
            return ScalarExpr(float(token))
        if re.fullmatch(r"[A-Za-z_]\w*", token):
            name = self.consume()
            if self.peek() == "(":
                self.consume()
                args: list[Any] = []
                if self.peek() != ")":
                    while True:
                        args.append(self.parse_expression())
                        if self.peek() != ",":
                            break
                        self.consume()
                self.expect(")")
                if name.lower() in UePureGraphT3DBuilder.CONSTRUCTOR_WIDTHS:
                    return ConstructorExpr(name, tuple(args))
                return CallExpr(name, tuple(args))
            return VarExpr(name)
        raise ValueError("unsupported primary")

    def peek(self) -> str | None:
        if self.index >= len(self.tokens):
            return None
        return self.tokens[self.index]

    def consume(self) -> str:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def expect(self, value: str) -> None:
        token = self.consume()
        if token != value:
            raise ValueError(f"expected {value}, got {token}")
