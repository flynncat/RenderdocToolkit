"""Business-semantic drawcall classifier for the enhanced perf report.

The existing perf pipeline already classifies draws into *engine* passes
(``MobileBasePass`` / ``Translucency`` / ... via
``RenderdocPerfService._SCENE_PASS_KEYWORDS``) and a render-state heuristic.
That answers "which rendering stage" but not "which game content".

This classifier adds the *business* taxonomy the reference report uses
(报告第二节: 农作物 / 宠物 / 场景装饰物 / 其他, and a finer ``class_id`` such
as ``crop_special`` / ``env_terrain`` / ``vfx_particle``).  It matches keyword
rules against a draw's ``pass_name``, ``breadcrumbs`` and shader names.

Rules are data-driven (see ``classifier_rules.example.json``) so each project
can ship its own naming conventions without code changes.

SKELETON STATUS
---------------
Matching is implemented; the default rule set is the bundled example.  The
quality of classification depends entirely on the project's debug-marker /
mesh / material naming, so projects are expected to supply their own
``classifier_rules.json``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from app.services.perf_report.models import BusinessCategory, DrawClassification


_DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "classifier_rules.example.json"


class DrawcallClassifier:
    """Classify draw rows into a two-level business taxonomy."""

    def __init__(
        self,
        rules: Optional[List[BusinessCategory]] = None,
        *,
        default_level1: str = "其他",
        default_class_id: str = "unknown",
    ) -> None:
        self._rules: List[BusinessCategory] = rules if rules is not None else []
        self._default = BusinessCategory(level1=default_level1, class_id=default_class_id)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, config_path: Optional[Path] = None) -> "DrawcallClassifier":
        """Load rules from a JSON config (falls back to the bundled example)."""
        path = config_path or _DEFAULT_RULES_PATH
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return cls(rules=[])

        rules: List[BusinessCategory] = []
        for entry in raw.get("rules", []) or []:
            keywords = [str(k) for k in (entry.get("keywords") or []) if str(k).strip()]
            rules.append(
                BusinessCategory(
                    level1=str(entry.get("level1") or "其他"),
                    class_id=str(entry.get("class_id") or "unknown"),
                    keywords=keywords,
                    note=str(entry.get("note") or ""),
                )
            )
        default = raw.get("default") or {}
        return cls(
            rules=rules,
            default_level1=str(default.get("level1") or "其他"),
            default_class_id=str(default.get("class_id") or "unknown"),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def classify(self, row: Mapping[str, Any]) -> DrawClassification:
        """Return the business classification for one draw row."""
        eid = str(row.get("eid") or "")
        haystacks = self._build_haystacks(row)

        for rule in self._rules:
            for keyword in rule.keywords:
                kw_low = keyword.lower()
                for source_label, text in haystacks:
                    if kw_low in text:
                        return DrawClassification(
                            eid=eid,
                            level1=rule.level1,
                            class_id=rule.class_id,
                            matched_keyword=keyword,
                            matched_source=source_label,
                        )
        return DrawClassification(
            eid=eid,
            level1=self._default.level1,
            class_id=self._default.class_id,
        )

    def classify_rows(self, rows: List[Mapping[str, Any]]) -> List[DrawClassification]:
        return [self.classify(row) for row in rows]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _build_haystacks(row: Mapping[str, Any]) -> List[tuple]:
        """Build ``(source_label, lowercased_text)`` pairs to match against.

        Order matters: ``pass_name`` first (most specific marker), then
        breadcrumbs, then shader ids as a last resort.
        """
        pairs: List[tuple] = []
        pass_name = str(row.get("pass_name") or "")
        if pass_name:
            pairs.append(("pass_name", pass_name.lower()))
        for crumb in row.get("breadcrumbs") or []:
            crumb_text = str(crumb or "")
            if crumb_text:
                pairs.append(("breadcrumb", crumb_text.lower()))
        shader_ids = row.get("shader_ids") or {}
        if isinstance(shader_ids, dict):
            for value in shader_ids.values():
                value_text = str(value or "")
                if value_text:
                    pairs.append(("shader", value_text.lower()))
        return pairs
