"""Self-test for GlslSimplifier — no RenderDoc required."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.glsl_simplifier import GlslSimplifier

SAMPLE_SHADER = """
// Stage: fragment
// Shader ResourceId: 42
// Entry Point: main
// RenderDoc Target: GLSL
// Export Mode: disassembly

#version 310 es
precision highp float;

uint _rdt_f32_to_u32(float value)
{
    return floatBitsToUint(value);
}

float _rdt_f16_to_f32(uint val)
{
    uint sign = (val & 0x8000u) << 16;
    int exponent = int((val & 0x7C00u) >> 10);
    return 0.0;
}

float2 unpackHalf2x16_emu(uint u)
{
    uint y = (u >> 16);
    uint x = u & 0xFFFFu;
    return float2(_rdt_f16_to_f32(x), _rdt_f16_to_f32(y));
}

uniform float brightness;
uniform vec3 tintColor;
uniform float unusedParam;
uniform mat4 unusedMatrix;

in vec2 v_texcoord0;
in vec3 v_normal;
in float v_unused_varying;

out vec4 fragColor;

void main()
{
    vec3 color = tintColor * brightness;
    vec3 normal = normalize(v_normal);
    float nDotL = max(dot(normal, vec3(0.0, 1.0, 0.0)), 0.0);
    color = color * nDotL;
    color = mix(color, color, 0.0);
    float one = brightness * 1.0;
    float zero = brightness + 0.0;
    if (false) {
        color = vec3(1.0, 0.0, 0.0);
    }
    if (true) {
        color = color * 0.5;
    }
    fragColor = vec4(color, 1.0);
}
"""

SAMPLE_PARAMS = json.dumps({
    "stages": {
        "fragment": {
            "constant_blocks": [
                {
                    "variables": [
                        {"name": "brightness", "value": 1.5},
                        {"name": "tintColor", "value": [0.8, 0.9, 1.0]},
                        {"name": "unusedParam", "value": 0.0},
                        {"name": "unusedMatrix", "value": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]},
                    ]
                }
            ]
        }
    }
})


def main():
    simp = GlslSimplifier()
    passed = 0
    total = 0

    # Test L0: strip preamble + helpers
    total += 1
    result = simp.simplify(SAMPLE_SHADER, levels="L0")
    if "#version" not in result.simplified_source and "_rdt_" not in result.simplified_source:
        print(f"[PASS] L0: Removed header + helpers (lines {result.original_line_count}→{result.simplified_line_count})")
        passed += 1
    else:
        print(f"[FAIL] L0: Still contains header/helpers")

    # Test L1: dead code elimination
    total += 1
    result = simp.simplify(SAMPLE_SHADER, levels="L0,L1")
    if "unusedParam" not in result.simplified_source and "v_unused_varying" not in result.simplified_source:
        print(f"[PASS] L1: Removed unused declarations (lines {result.original_line_count}→{result.simplified_line_count})")
        passed += 1
    else:
        print(f"[FAIL] L1: Unused declarations still present")

    # Test L1: keep used declarations
    total += 1
    if "brightness" in result.simplified_source and "tintColor" in result.simplified_source:
        print(f"[PASS] L1: Kept used declarations (brightness, tintColor)")
        passed += 1
    else:
        print(f"[FAIL] L1: Lost used declarations")

    # Test L2: constant folding
    total += 1
    result = simp.simplify(SAMPLE_SHADER, shader_params_json=SAMPLE_PARAMS, levels="L0,L1,L2")
    if "const float brightness = 1.5" in result.simplified_source:
        print(f"[PASS] L2: Constant folding applied (brightness=1.5)")
        passed += 1
    else:
        print(f"[FAIL] L2: Constant folding not applied")

    # Test L3: branch elimination
    total += 1
    result = simp.simplify(SAMPLE_SHADER, levels="L0,L3")
    if "if (false)" not in result.simplified_source and "vec3(1.0, 0.0, 0.0)" not in result.simplified_source:
        print(f"[PASS] L3: Dead branch eliminated")
        passed += 1
    else:
        print(f"[FAIL] L3: Dead branch not eliminated")

    # Test L3: true branch unwrapped
    total += 1
    result_text = result.simplified_source
    if "color = color * 0.5" in result_text and "if (true)" not in result_text:
        print(f"[PASS] L3: True branch unwrapped")
        passed += 1
    else:
        print(f"[FAIL] L3: True branch not unwrapped")

    # Test L4: algebraic simplification
    total += 1
    result = simp.simplify(SAMPLE_SHADER, levels="L0,L4")
    if "mix(color, color, 0.0)" not in result.simplified_source:
        print(f"[PASS] L4: mix(a,b,0) simplified")
        passed += 1
    else:
        print(f"[FAIL] L4: mix(a,b,0) not simplified")

    # Test full pipeline
    total += 1
    result = simp.simplify(SAMPLE_SHADER, shader_params_json=SAMPLE_PARAMS, levels="L0,L1,L2,L3,L4")
    pct = round((1 - result.simplified_line_count / max(result.original_line_count, 1)) * 100, 1)
    if result.simplified_line_count < result.original_line_count:
        print(f"[PASS] Full: {result.original_line_count}→{result.simplified_line_count} lines ({pct}% reduction)")
        passed += 1
    else:
        print(f"[FAIL] Full: No reduction")

    print(f"\n{passed}/{total} tests passed")
    if passed < total:
        print("\nSimplified output:\n" + result.simplified_source[:500])
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
