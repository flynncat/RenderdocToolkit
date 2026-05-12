"""Quick self-test for PatternMiner."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pattern_miner import PatternMiner

miner = PatternMiner()
report = miner.run()

print(f"Static patterns mined: {len(report.patterns)}")
assert len(report.patterns) > 20, f"Expected >20 patterns, got {len(report.patterns)}"

categories = {p.category for p in report.patterns}
expected = {"type_map", "function_rename", "texture_call", "constructor_splat",
            "builtin_var", "preamble_strip", "qualifier_strip", "layout_strip"}
missing = expected - categories
assert not missing, f"Missing categories: {missing}"

import re
for p in report.patterns:
    if p.glsl_pattern != "*":
        try:
            re.compile(p.glsl_pattern)
        except re.error as exc:
            print(f"[FAIL] Bad regex in pattern {p.category}/{p.glsl_pattern}: {exc}")
            sys.exit(1)

d = report.to_dict()
assert "patterns" in d and "session_count" in d

print("[PASS] PatternMiner self-test OK")
