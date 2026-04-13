# UE Custom/T3D Notes

## Key UE Types

- `UMaterialExpressionCustom`
  - `Code`
  - `OutputType`
  - `Inputs`
  - `AdditionalOutputs`
- `UMaterialExpressionParameter`
  - `ParameterName`
  - `ExpressionGUID`
- common helper nodes:
  - `ScalarParameter`
  - `VectorParameter`
  - `TextureObjectParameter`
  - `VertexColor`

## T3D Minimum Structure

Typical pasted material graph text contains:

1. `Begin Object Class=/Script/UnrealEd.MaterialGraphNode`
2. nested `Begin Object Class=/Script/Engine.MaterialExpression...`
3. expression properties such as `Code=`, `OutputType=`, `Inputs(...)`
4. wrapper properties such as `MaterialExpression=`, `NodePosX/Y`, `NodeGuid`
5. `CustomProperties Pin (...)` entries for visual graph linkage

## Custom Node Output Behavior

- single-output custom node:
  - main output pin is effectively the single return pin
- multi-output custom node:
  - first output is the `return`
  - additional outputs come from `AdditionalOutputs`

## VS/FS Varying Interface Rule

- if `vs.glsl` declares `out <type> name;` and `fs.glsl` declares `in <type> name;`, treat this pair as the same cross-stage interface
- in generated UE T3D, the fragment `Custom` input for that interface should be connected to the vertex `Custom` output directly
- do not also emit a separate parameter/source node for the same fragment input
- primary vertex custom return maps to output index `0`
- vertex `AdditionalOutputs` map to subsequent output indices and should be selected with `OutputIndex`
- the rule is generic and name-based; sample names like `vertexColor` or `v_texture0` are examples only

## Conservative Mapping Guidance

- if an input is not confidently mappable to a built-in node, emit a parameter node
- keep optional file handling soft-fail
- when in doubt, preserve data in notes/warnings rather than silently dropping it

## Built-in Semantic Hints

- safe direct mappings now include:
  - `CameraPosition` -> `CameraPositionWS`
  - `v2f_position_world` / `position_world` -> `WorldPosition`
  - `v2f_normal` -> `VertexNormalWS`
  - `v2f_tangent` -> `VertexTangentWS`
  - `texcoordN` (`vec2`) -> `TextureCoordinate(N)`
  - `vertexColor` / `diffuse` (`vec4`) -> `VertexColor` or `VertexColor + AppendVector`
- standard transform matrices should be tagged as matrix semantics, not described as ordinary parameters:
  - `WorldViewProjection`
  - `WorldView`
  - `Projection`
  - `ViewProjection`
  - `World`
  - `InverseWorld`
  - `ViewMatrix`
- these transform matrices should be lowered to UE internal expressions, not emitted as ordinary external parameter nodes
- `TexTransform0` should be reviewed separately as UV transform data
- `LightViewProj` / `InvLightViewProj` should be reviewed separately as light-space/shadow-space data

## Default Output Width Summary

- `TextureCoordinate`
  - default output: `float2`
- `Constant3Vector`
  - output: `float3`
- `Constant4Vector`
  - output: `float4`
- `VectorParameter`
  - default main output is RGB-style
  - alpha is a separate output pin
  - if GLSL expects `vec4`, prefer `VectorParameter + AppendVector`
- `VertexColor`
  - default main output is RGB-style
  - alpha is a separate output pin
  - if GLSL expects `vec4`, prefer `VertexColor + AppendVector`

In the current project generator, `vec4` inputs should not assume a built-in node's first pin is already `float4`.

## gl_Position Summary

- GLSL `gl_Position` is a built-in vertex-stage final output
- it is not a normal intermediate graph node value
- Unreal material graph does not expose a direct 1:1 `gl_Position` node equivalent
- the closest semantic destination is material root vertex-position-related outputs, not a standalone expression
- generator logic should avoid exporting `gl_Position` as a regular `Custom` output pin

## Packed Helper Expansion

- common RenderDoc helper preambles include `unpackHalf2x16_emu`, `f16tof32`, `floatBitsToUint`, `uintBitsToFloat`
- if generated HLSL still references these but the helper body is missing, the final UE `Custom` code is not self-contained
- preferred handling:
  - strip the original GLSL helper block from declaration analysis
  - rewrite bit-cast helpers to HLSL-safe wrappers
  - inject a self-contained HLSL unpack helper block when packed-half decode is still used in the translated body

## Strict Pure Graph Notes

- pure graph mode is intentionally a subset mode, not a full GLSL compiler
- valid pure graph output must not contain `MaterialExpressionCustom`
- currently good first-stage pure graph targets include:
  - `TextureCoordinate`
  - `TextureObjectParameter` + `TextureSample`
  - `VertexColor`
  - `ScalarParameter` / `VectorParameter`
  - math nodes such as `Add`, `Subtract`, `Multiply`, `Divide`, `LinearInterpolate`, `Clamp`, `AppendVector`
- vertex handling should prioritize:
  - collapsing clear UV passthrough varyings into `CustomizedUV` candidates
  - treating unclear `gl_Position` logic as unsupported instead of inventing fake intermediate nodes

## Typical RenderDoc File Set

- `*_fs.glsl`
- `*_vs.glsl`
- `*_shader_params.json`

The fragment file drives the main output. Vertex data is useful for:

- naming varyings
- recovering hints for fragment `in`
- building a reference vertex custom node in T3D
- reconstructing direct vertex-to-fragment interface wiring when names/types match
