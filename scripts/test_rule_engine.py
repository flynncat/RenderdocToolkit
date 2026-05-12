"""Self-test for DeterministicRuleEngine."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.deterministic_rule_engine import DeterministicRuleEngine

engine = DeterministicRuleEngine()

SAMPLE_GLSL = """
// RenderDoc header comment
// Another header line
#version 310 es
precision highp float;

layout(location = 0) in vec2 texCoord0;
uniform sampler2D sam_diffuse;
uniform vec4 tintColor;
layout(location = 0) out vec4 fragColor;

void main()
{
    vec4 base = texture(sam_diffuse, texCoord0);
    vec4 mixed = mix(base, tintColor, 0.5);
    float edge = fract(texCoord0.x);
    fragColor = mixed + vec4(edge);
}
""".strip()

result = engine.convert(SAMPLE_GLSL)
assert result.success, f"Conversion failed: {result.error}"

hlsl = result.standalone_hlsl
print("=== Standalone HLSL ===")
print(hlsl)
print()

checks = [
    ("float2" in hlsl, "vec2 → float2"),
    ("float4" in hlsl, "vec4 → float4"),
    ("Texture2D" in hlsl, "sampler2D → Texture2D"),
    ("lerp(" in hlsl, "mix → lerp"),
    ("frac(" in hlsl, "fract → frac"),
    (".Sample(" in hlsl, "texture() → .Sample()"),
    ("main_standalone" in hlsl, "main renamed to main_standalone"),
    ("#version" not in hlsl, "#version stripped"),
    ("precision" not in hlsl, "precision stripped"),
    ("layout(" not in hlsl, "layout stripped"),
]

ok = True
for check, desc in checks:
    status = "PASS" if check else "FAIL"
    print(f"  [{status}] {desc}")
    if not check:
        ok = False

assert result.ue_custom_hlsl, "UE custom HLSL should be generated"
print(f"\n  Rules applied: {len(result.rules_applied)}")
for r in result.rules_applied:
    print(f"    - {r.rule_name}: {r.lines_before} → {r.lines_after} lines")

if ok:
    print("\n[PASS] DeterministicRuleEngine self-test OK")
else:
    print("\n[FAIL] Some checks failed")
    sys.exit(1)
