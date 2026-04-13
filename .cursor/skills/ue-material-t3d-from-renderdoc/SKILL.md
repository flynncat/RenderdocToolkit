---
name: ue-material-t3d-from-renderdoc
description: Converts RenderDoc-exported GLSL shaders into Unreal Engine 4.26 material Custom node HLSL and copy-pasteable T3D node text. Use when the user mentions RenderDoc GLSL, UE4.26 materials, Custom nodes, material graph clipboard text, T3D, or wants shader nodes that can be pasted directly into Unreal.
---

# UE Material T3D From RenderDoc

## When To Use

Use this skill when the task is to turn RenderDoc shader exports into Unreal material graph content, especially:

- `*_fs.glsl`, `*_vs.glsl`, `*_shader_params.json`
- UE4.26 `Custom` node HLSL generation
- material graph clipboard/T3D generation
- direct paste into Unreal material editor

## Workflow

1. Read the three exported files when available:
   - fragment shader
   - vertex shader
   - shader params JSON
2. Generate a fragment-stage UE `Custom` node payload:
   - infer inputs from `uniform` / `in`
   - convert GLSL syntax to UE-friendly HLSL
   - determine `OutputType`
3. Generate a vertex-stage payload if a vertex shader exists:
   - preserve relevant `out` variables
   - treat non-built-in `out` variables as cross-stage interfaces first
   - do not export `gl_Position` as a normal reusable output pin
   - expose multiple outputs via `AdditionalOutputs` when needed
4. Build UE4.26 material graph `T3D` text:
   - create `MaterialExpressionCustom` nodes for fragment and vertex
   - create parameter/source nodes only for real external inputs
   - if `vs.out` and `fs.in` share the same name and compatible type, connect vertex `Custom` outputs directly into fragment `Custom` inputs
   - serialize `Inputs(...)`, `AdditionalOutputs(...)`, `MaterialGraphNode`, and `CustomProperties Pin`
5. Return:
   - fragment HLSL
   - vertex HLSL when a vertex shader exists
   - fragment inputs/output summary
   - vertex summary
   - T3D text
   - pure UE Shader Graph T3D when the expression subset is supported
   - unsupported items and warnings

## Current Conventions

- `fragment shader` path is required
- `vertex shader` and `shader params` are optional
- missing optional files should not hard-fail the workflow; report that they were ignored
- first-stage T3D output should generate node sets only
- do not auto-connect to `Material Output` unless the user explicitly asks for it

## Node Mapping Defaults

- `sampler2D` -> `TextureObjectParameter`
- scalar uniforms -> `ScalarParameter`
- vector uniforms / unresolved stage inputs -> `VectorParameter`
- exact `vertexColor` input -> `VertexColor`
- exact `CameraPosition` (`vec3`) -> `CameraPositionWS`
- exact `v2f_position_world` / `position_world` (`vec3`) -> `WorldPosition`
- exact `v2f_normal` (`vec3`) -> `VertexNormalWS`
- exact `v2f_tangent` (`vec3`) -> `VertexTangentWS`
- exact `texcoordN` (`vec2`) -> `TextureCoordinate` with matching `CoordinateIndex`
- keep mapping conservative; prefer valid pasteable T3D over aggressive semantic guesses

## Matrix Semantics

- standard matrices such as `WorldViewProjection`, `WorldView`, `Projection`, `ViewProjection`, `World`, `InverseWorld`, `ViewMatrix`, `TexTransform0`, `LightViewProj`, `InvLightViewProj` should be classified as built-in transform semantics first
- do not describe these as ordinary business parameters in review output
- current generator should:
  - rewrite `WorldViewProjection`, `WorldView`, `Projection`, `ViewProjection`, `World`, `InverseWorld`, `ViewMatrix` to UE internal expressions such as `ResolvedView.*` or `GetPrimitiveData(Parameters.PrimitiveId).*`
  - do not emit those transform matrices as ordinary external parameter nodes
  - prefer converting downstream varyings and built-in vectors to native UE nodes where safe
- not every matrix-looking uniform is a UE built-in transform:
  - `TexTransform0` is usually a UV transform payload, not the same category as `WorldViewProjection`
  - `LightViewProj` / `InvLightViewProj` are shadow/light-space data and should not be blindly treated as ordinary camera transform nodes
- when exact lowering is unclear, keep the result conservative and explicit

## VS/FS Interface Matching

- always analyze `vertex shader out` and `fragment shader in` together when both stages are available
- matching rule for first-stage generator:
  - same interface name
  - compatible GLSL type
  - fragment side is a stage input (`in`), not a uniform
- when a match exists, do not emit a new UE parameter/source node for that fragment input
- instead:
  - keep the fragment `Custom` input entry
  - wire it to the vertex `Custom` output pin or `AdditionalOutput`
- this rule must be name-driven and generic; do not hardcode `vertexColor`, `v_texture0`, or any other sample-specific identifier
- if no matching vertex output exists, fall back to conservative parameter/source node generation

## UE Pin Width Notes

- `TextureCoordinate` default output is `float2`
- `Constant3Vector` is `float3`
- `Constant4Vector` is `float4`
- `VectorParameter` default main pin is RGB-style output, not a safe direct `float4`
- `VertexColor` default main pin is RGB-style output, not a safe direct `float4`
- when GLSL expects `vec4` but the mapped UE node's main output is not a safe `float4`, insert `AppendVector`
- current safe default:
  - `vec4` parameter-like input -> `VectorParameter` + `AppendVector(RGB, A)`
  - `vertexColor` as `vec4` -> `VertexColor` + `AppendVector(RGB, A)`

## gl_Position Rule

- `gl_Position` is a GLSL built-in final vertex position output, not a normal reusable node value
- do not serialize `gl_Position` as a normal UE `Custom` output pin by default
- in UE material graphs, its closest semantic home is material root vertex-position-related outputs, not an intermediate expression node
- if the shader keeps clip-space `x/y/w` and mainly rewrites depth, prefer treating it as a `PixelDepthOffset` candidate
- if the shader derives new vertex/world position before projection, prefer treating it as a `WorldPositionOffset` candidate
- first-stage generator should treat `gl_Position` as:
  - analysis/reference information, or
  - a clearly named reference value such as `VertexClipPositionRef` only when no other vertex outputs exist
- never present `gl_Position` itself as if it were a standard UE material expression node

## Important Limits

- UE material graphs are not a 1:1 vertex/pixel pipeline mirror
- direct cross-stage reconstruction is lossy
- `discard`, `gl_FragCoord`, `gl_FragDepth`, and complex helper-function-heavy GLSL may still require manual handling
- if T3D is generated for multi-output vertex logic, treat it as a helper/reference node set first

## Strict Pure Graph Mode

- strict pure graph mode must not emit `MaterialExpressionCustom`
- only the supported subset should be lowered:
  - parameters / built-in sources
  - `texture`, `textureLod`, `textureGrad`
  - `mix`, `clamp`, `min`, `max`
  - simple arithmetic `+ - * /`
  - basic constructors and swizzles
- when a sub-expression is not in the supported subset:
  - do not silently keep it in pure graph output
  - list it under pure graph `unsupported`
- current pure graph path should still keep the legacy Custom result side by side for comparison
- vertex pure graph handling should be conservative:
  - prefer `CustomizedUV` candidates when a varying clearly resolves to UV passthrough
  - only emit `WorldPositionOffset` when the `gl_Position` pattern can be reduced safely

## Packed Helper Expansion

- RenderDoc-exported GLSL may prepend helper blocks such as `unpackHalf2x16_emu`, `f16tof32`, `floatBitsToUint`, `uintBitsToFloat`
- do not rely on those original GLSL helper blocks surviving intact in the final UE `Custom` code
- current generator should normalize these cases by:
  - removing the original GLSL helper preamble from declaration parsing
  - rewriting `floatBitsToUint` / `uintBitsToFloat` to HLSL-safe wrappers
  - prepending a self-contained HLSL unpack helper block when `unpackHalf2x16_emu`-style packed-half decoding is used
- goal: generated HLSL should be directly copyable and not depend on missing external helper context

## Files To Check

- `app/services/fragment_glsl_to_ue426_custom_hlsl.py`
- `app/services/ue_material_t3d_builder.py`
- `app/main.py`
- `app/templates/index.html`
- `app/static/app.js`

## Additional Reference

- For UE Custom node and T3D notes, see [reference.md](reference.md)
