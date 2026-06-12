"""Optimization-priority planner for the enhanced perf report.

Turns the evidence-only ``findings`` (from ``app/services/perf_rule_engine.py``)
plus Mali shader metrics and the pass chart into a prioritised, actionable
optimisation list with rough expected-gain estimates - the data behind the
reference report's "八、优化优先级建议" table (P0 ... P3 + 预期收益).

Design notes
------------
- ``perf_rule_engine`` deliberately stays recommendation-free; this planner is
  where evidence becomes advice, so it can be tuned/extended independently.
- Expected-gain numbers are intentionally coarse heuristics expressed as a
  ``[low, high]`` ms range, never a false-precision single value.

SKELETON STATUS
---------------
A small starter mapping is wired (rule_id -> priority + gain heuristic) so the
report renders a meaningful table from real findings.  The heuristics and
priority assignment are first-pass and explicitly marked for tuning.

TODO(tuning): calibrate ``_GAIN_HEURISTICS`` against real before/after
captures, and incorporate ``shader_metrics`` register-spill severity into the
shader-related priority once Mali data is flowing (see ``mali_shader_analyzer``
integration TODO).
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from app.services.perf_report.models import OptimizationItem, ShaderMaliMetrics


# rule_id -> (priority, effort, gain_low_ms, gain_high_ms, title, rationale).
# First-pass heuristic table; tune against measured data.
_GAIN_HEURISTICS: Dict[str, Dict[str, Any]] = {
    "R002_fullscreen_heavy_ps": {
        "priority": "P0",
        "effort": "中",
        "gain": (3.0, 6.0),
        "title": "全屏后处理/Tonemap 降本（考虑关闭 Mobile HDR 或换轻量 LDR）",
        "rationale": "全屏 PS 覆盖整屏像素，移动端关闭 HDR 可省 Tonemap + SceneColor Copy + 深度 Fetch。",
    },
    "R003_fullscreen_bandwidth": {
        "priority": "P0",
        "effort": "中",
        "gain": (2.0, 5.0),
        "title": "全屏带宽优化（SceneColor Copy / 大纹理采样）",
        "rationale": "全屏 pass 反复读写大纹理吃满带宽。",
    },
    "R001_overdraw_heavy": {
        "priority": "P1",
        "effort": "中",
        "gain": (1.0, 3.0),
        "title": "降低过绘制（半透层数 / 提前深度剔除）",
        "rationale": "像素被重复着色多次，减少 overdraw 直接省 fragment 工作量。",
    },
    "R004_translucency_overdraw": {
        "priority": "P1",
        "effort": "中",
        "gain": (1.0, 2.0),
        "title": "半透明过绘制优化（粒子 LOD / 距离裁剪）",
        "rationale": "半透叠加层数高，粒子 LOD 能显著降低 PS 调用。",
    },
    "R005_shadow_pass_too_heavy": {
        "priority": "P1",
        "effort": "中",
        "gain": (1.0, 3.0),
        "title": "阴影 pass 降本（分辨率/级联/投影物体数）",
        "rationale": "Shadow pass 占比偏高。",
    },
    "R006_post_processing_heavy": {
        "priority": "P0",
        "effort": "中",
        "gain": (2.0, 5.0),
        "title": "后处理链路精简",
        "rationale": "后处理占帧比例高，逐项关闭/合并可观回收。",
    },
    "R007_shader_alu_outlier": {
        "priority": "P1",
        "effort": "高",
        "gain": (1.0, 2.0),
        "title": "重 shader 拆分变体 / 降 work_register（避免 register spill）",
        "rationale": "PS 指令数显著偏高，逼近寄存器上限会触发 spilling。",
    },
    "R008_huge_texture_low_use": {
        "priority": "P2",
        "effort": "低",
        "gain": (0.0, 0.5),
        "title": "大纹理低使用率：缩小分辨率 / 复用图集",
        "rationale": "大贴图覆盖率低，缩小可省显存与带宽。",
    },
    "R010_high_tri_low_pixel": {
        "priority": "P3",
        "effort": "中",
        "gain": (0.3, 1.0),
        "title": "高面低像素：强制 LOD / 合批",
        "rationale": "三角面多但屏幕覆盖小，几何过密。",
    },
    "R014_unique_texture_explosion": {
        "priority": "P2",
        "effort": "中",
        "gain": (0.0, 1.0),
        "title": "唯一纹理过多：图集化 / 合并",
        "rationale": "单 pass 唯一纹理数量大，绑定/采样切换开销高。",
    },
}

_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


class OptimizationPlanner:
    """Map findings (+ Mali metrics) to a prioritised optimisation list."""

    def plan(
        self,
        findings: List[Mapping[str, Any]],
        *,
        shader_metrics: Optional[List[ShaderMaliMetrics]] = None,
        pass_chart: Optional[List[Mapping[str, Any]]] = None,
    ) -> List[OptimizationItem]:
        items: List[OptimizationItem] = []

        for finding in findings or []:
            rule_id = str(finding.get("rule_id") or "")
            heuristic = _GAIN_HEURISTICS.get(rule_id)
            if heuristic is None:
                # Unknown rule: still surface it at low priority with no gain
                # estimate so nothing is silently dropped.
                items.append(
                    OptimizationItem(
                        priority="P3",
                        title=str(finding.get("title") or rule_id),
                        rationale="（未配置收益启发式，待补充）",
                        effort="中",
                        related_eids=self._collect_eids(finding),
                        related_rule_ids=[rule_id] if rule_id else [],
                    )
                )
                continue
            gain_low, gain_high = heuristic["gain"]
            items.append(
                OptimizationItem(
                    priority=str(heuristic["priority"]),
                    title=str(heuristic["title"]),
                    rationale=str(heuristic["rationale"]),
                    expected_gain_ms_low=float(gain_low),
                    expected_gain_ms_high=float(gain_high),
                    effort=str(heuristic["effort"]),
                    related_eids=self._collect_eids(finding),
                    related_rule_ids=[rule_id] if rule_id else [],
                )
            )

        # TODO(tuning): add register-spill driven items directly from
        # shader_metrics (independent of whether a rule fired) once Mali data
        # is available.
        _ = shader_metrics
        _ = pass_chart

        items.sort(key=lambda i: (_PRIORITY_ORDER.get(i.priority, 9), -i.expected_gain_ms_high))
        return items

    @staticmethod
    def total_expected_gain_ms(items: List[OptimizationItem]) -> tuple:
        low = sum(i.expected_gain_ms_low for i in items)
        high = sum(i.expected_gain_ms_high for i in items)
        return round(low, 2), round(high, 2)

    @staticmethod
    def _collect_eids(finding: Mapping[str, Any]) -> List[str]:
        eids: List[str] = []
        for entry in finding.get("affected") or []:
            eid = str(entry.get("eid") or "").strip()
            if eid:
                eids.append(eid)
        return eids[:8]
