"""Phase 4 gate: 10-shader one-click conversion test + success rate report.

Each test case is a synthetic but realistic RenderDoc GLSL fragment shader.
We feed it through the DeterministicRuleEngine and verify the output meets
basic quality bars:
  - Conversion succeeds
  - Output contains no GLSL-only tokens (vec2/vec3/vec4, fract, mix, etc.)
  - Output contains expected HLSL tokens
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.deterministic_rule_engine import DeterministicRuleEngine
from app.services.glsl_simplifier import GlslSimplifier

engine = DeterministicRuleEngine()
simplifier = GlslSimplifier()

SHADERS = [
    {
        "name": "01_basic_textured",
        "glsl": """
#version 310 es
precision highp float;
layout(location = 0) in vec2 texCoord0;
uniform sampler2D sam_diffuse;
layout(location = 0) out vec4 fragColor;
void main() {
    fragColor = texture(sam_diffuse, texCoord0);
}
""",
        "must_contain": [".Sample(", "Texture2D"],
        "must_not_contain": ["sampler2D", "texture("],
    },
    {
        "name": "02_mix_lerp",
        "glsl": """
#version 310 es
precision highp float;
uniform vec4 colorA;
uniform vec4 colorB;
uniform float blendFactor;
layout(location = 0) out vec4 fragColor;
void main() {
    fragColor = mix(colorA, colorB, blendFactor);
}
""",
        "must_contain": ["lerp("],
        "must_not_contain": ["mix("],
    },
    {
        "name": "03_fract_derivatives",
        "glsl": """
#version 310 es
precision highp float;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 fragColor;
void main() {
    float f = fract(uv.x * 10.0);
    float dx = dFdx(f);
    float dy = dFdy(f);
    fragColor = vec4(f, dx, dy, 1.0);
}
""",
        "must_contain": ["frac(", "ddx(", "ddy("],
        "must_not_contain": ["fract(", "dFdx(", "dFdy("],
    },
    {
        "name": "04_texture_lod",
        "glsl": """
#version 310 es
precision highp float;
uniform sampler2D sam_env;
layout(location = 0) in vec2 texCoord0;
layout(location = 0) out vec4 fragColor;
void main() {
    fragColor = textureLod(sam_env, texCoord0, 2.0);
}
""",
        "must_contain": [".SampleLevel("],
        "must_not_contain": ["textureLod("],
    },
    {
        "name": "05_texture_grad",
        "glsl": """
#version 310 es
precision highp float;
uniform sampler2D sam_detail;
layout(location = 0) in vec2 texCoord0;
layout(location = 0) out vec4 fragColor;
void main() {
    vec2 dx = dFdx(texCoord0);
    vec2 dy = dFdy(texCoord0);
    fragColor = textureGrad(sam_detail, texCoord0, dx, dy);
}
""",
        "must_contain": [".SampleGrad("],
        "must_not_contain": ["textureGrad("],
    },
    {
        "name": "06_type_conversions",
        "glsl": """
#version 310 es
precision highp float;
uniform mat4 worldViewProjection;
uniform vec3 lightDir;
layout(location = 0) in vec3 normal;
layout(location = 0) in vec4 position;
layout(location = 0) out vec4 fragColor;
void main() {
    vec4 projected = worldViewProjection * position;
    float ndotl = max(dot(normal, lightDir), 0.0);
    fragColor = vec4(vec3(ndotl), 1.0) + projected * 0.001;
}
""",
        "must_contain": ["float4x4", "float3", "float4"],
        "must_not_contain": ["mat4", " vec3", " vec4"],
    },
    {
        "name": "07_mod_to_fmod",
        "glsl": """
#version 310 es
precision highp float;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 fragColor;
void main() {
    float x = mod(uv.x, 0.5);
    fragColor = vec4(x, x, x, 1.0);
}
""",
        "must_contain": ["fmod("],
        "must_not_contain": [" mod("],
    },
    {
        "name": "08_multi_texture",
        "glsl": """
#version 310 es
precision highp float;
uniform sampler2D sam_albedo;
uniform sampler2D sam_normal;
layout(location = 0) in vec2 texCoord0;
layout(location = 0) out vec4 fragColor;
void main() {
    vec4 albedo = texture(sam_albedo, texCoord0);
    vec4 normal = texture(sam_normal, texCoord0);
    fragColor = albedo * normal.r;
}
""",
        "must_contain": ["sam_albedo.Sample(", "sam_normal.Sample("],
        "must_not_contain": ["texture(sam_albedo", "texture(sam_normal"],
    },
    {
        "name": "09_scalar_splat",
        "glsl": """
#version 310 es
precision highp float;
uniform float brightness;
layout(location = 0) out vec4 fragColor;
void main() {
    fragColor = vec4(brightness) * vec4(1.0, 0.5, 0.25, 1.0);
}
""",
        "must_contain": ["float4(brightness, brightness, brightness, brightness)"],
        "must_not_contain": [],
    },
    {
        "name": "10_complex_mixed",
        "glsl": """
#version 310 es
precision highp float;
layout(location = 0) in vec2 texCoord0;
layout(location = 0) in vec3 normal;
uniform sampler2D sam_diffuse;
uniform sampler2D sam_spec;
uniform vec3 lightDir;
uniform vec4 ambientColor;
uniform float specPower;
layout(location = 0) out vec4 fragColor;
void main() {
    vec4 diff = texture(sam_diffuse, texCoord0);
    vec4 spec = textureLod(sam_spec, texCoord0, 0.0);
    float ndotl = max(dot(normalize(normal), lightDir), 0.0);
    vec3 lit = diff.rgb * ndotl + spec.rgb * pow(ndotl, specPower);
    fragColor = vec4(mix(ambientColor.rgb, lit, ndotl), diff.a);
}
""",
        "must_contain": [".Sample(", ".SampleLevel(", "lerp(", "float3", "float4"],
        "must_not_contain": ["texture(", "textureLod(", "mix(", " vec3", " vec4"],
    },
]

passed = 0
failed = 0
results = []

for idx, shader in enumerate(SHADERS):
    name = shader["name"]
    glsl = shader["glsl"].strip()

    simplified = simplifier.simplify(glsl, levels="L0,L1,L2,L3,L4")
    result = engine.convert(simplified.simplified_source, mode="both")

    errors = []
    if not result.success:
        errors.append(f"conversion failed: {result.error}")

    hlsl = result.standalone_hlsl
    for token in shader.get("must_contain", []):
        if token not in hlsl:
            errors.append(f"missing expected token: {token!r}")

    for token in shader.get("must_not_contain", []):
        if token in hlsl:
            errors.append(f"unexpected GLSL token remains: {token!r}")

    if not result.ue_custom_hlsl.strip():
        errors.append("UE Custom HLSL is empty")

    if errors:
        failed += 1
        status = "FAIL"
    else:
        passed += 1
        status = "PASS"

    results.append((name, status, errors))
    print(f"  [{status}] {name}" + (f"  -- {'; '.join(errors)}" if errors else ""))

print(f"\n{'='*60}")
print(f"  Total: {len(SHADERS)}  |  Passed: {passed}  |  Failed: {failed}  |  Rate: {passed}/{len(SHADERS)} ({passed*100//len(SHADERS)}%)")
print(f"{'='*60}")

if failed > 0:
    sys.exit(1)
else:
    print("\n[PASS] All 10 shaders converted successfully!")
