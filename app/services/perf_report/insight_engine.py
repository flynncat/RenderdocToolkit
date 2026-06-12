"""Data-driven performance insight engine for the enhanced report.

The counter-based ``perf_rule_engine`` is largely inert for desktop replays of
mobile GLES captures because ``PS Invocations`` / coverage / overdraw come back
as zero.  This engine instead derives actionable, prioritised optimisation
advice from the signals that ARE real in that situation:

  * per-draw ``gpu_duration_ms`` (the only working GPU counter)
  * GLSL-estimated instruction counts
  * Mali Offline Compiler metrics (work registers / ALU / LS / bound unit /
    register spill)
  * pass / marker names (full-screen post-process, UI/Slate, etc.)
  * draw repetition + instance counts (batching / instancing opportunities)
  * texture audit (large textures)
  * business classification (vfx / crop / pet / ...)

It produces the content behind report sections 三 (hotspot problems), 六
(scene content), 七 (other) and 八 (the reference report's prioritised
optimisation table), plus the TL;DR bottleneck sentence.

All "expected gain" numbers are coarse heuristics expressed as a ms range and
grounded in the *measured* gpu time of the involved draws - never invented
constants.  They are clearly labelled as estimates in the report.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from app.services.perf_report.models import OptimizationItem, ShaderMaliMetrics, TextureAuditEntry


# --- thresholds (tunable) --------------------------------------------------
FULLSCREEN_TRI_MAX = 8            # a full-screen pass draws ~2 triangles
FULLSCREEN_NAME_RX = re.compile(
    r"tonemap|tone[_ ]?map|post[_ ]?process|bloom|blur|\bdof\b|ssao|ssr|"
    r"composit|fxaa|\btaa\b|upscal|scene[_ ]?color|copy[_ ]?scene|resolve|"
    r"depth[_ ]?fetch|color[_ ]?fetch|gaussian|downsample|eyeadapt|exposure",
    re.I,
)
UI_NAME_RX = re.compile(r"slate|\bui\b|hud|widget|canvas|font|text", re.I)
VFX_PASS_NAMES = {"Particle", "Additive", "Translucent", "Translucency"}
MALI_LS_BOUND_CYCLES = 3.0
MALI_HIGH_ALU_CYCLES = 4.0
REPEAT_MIN_COUNT = 3
HIGH_TRIANGLE = 3000
MIN_ITEM_MS = 0.15               # ignore opportunities smaller than this


@dataclass
class InsightResult:
    optimization_items: List[OptimizationItem] = field(default_factory=list)
    scene_content_notes: List[str] = field(default_factory=list)
    other_notes: List[str] = field(default_factory=list)
    hotspot_problems: Dict[str, str] = field(default_factory=dict)
    bottleneck_summary: str = ""


_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


class InsightEngine:
    def analyze(
        self,
        rows: List[Mapping[str, Any]],
        *,
        shader_metrics: Optional[List[ShaderMaliMetrics]] = None,
        classifications: Optional[Mapping[str, Any]] = None,
        texture_audit: Optional[List[TextureAuditEntry]] = None,
        total_gpu_ms: float = 0.0,
    ) -> InsightResult:
        rows = list(rows or [])
        shader_metrics = list(shader_metrics or [])
        classifications = dict(classifications or {})
        texture_audit = list(texture_audit or [])
        total_gpu_ms = float(total_gpu_ms or sum(self._ms(r) for r in rows))

        mali_by_id = {m.shader_id: m for m in shader_metrics if m.shader_id}

        result = InsightResult()
        items: List[OptimizationItem] = []

        # Pre-bucket draws by signal.
        fullscreen = [r for r in rows if self._is_fullscreen_post(r)]
        ui_rows = [r for r in rows if self._is_ui(r)]
        vfx_rows = [r for r in rows if self._is_vfx(r, classifications)]

        # 1) Full-screen post-process (Mobile HDR / tonemap / copy / depth).
        fs_ms = sum(self._ms(r) for r in fullscreen)
        if fs_ms >= MIN_ITEM_MS:
            items.append(OptimizationItem(
                priority="P0",
                title="关闭 Mobile HDR / 精简全屏后处理（Tonemap·SceneColor Copy·深度 Fetch）",
                rationale=(
                    f"识别到 {len(fullscreen)} 个全屏 pass（低三角面、名称含 tonemap/copy/resolve 等），"
                    f"合计约 {fs_ms:.2f} ms。移动端这些全屏像素填充开销可通过关闭 Mobile HDR 或换轻量 LDR 显著回收。"
                ),
                effort="中",
                related_eids=self._eids(fullscreen),
                **self._gain(fs_ms, 0.4, 0.7),
            ))

        # 2) UI / Slate batching.
        ui_ms = sum(self._ms(r) for r in ui_rows)
        if len(ui_rows) >= 15 or ui_ms >= max(MIN_ITEM_MS, 0.1 * total_gpu_ms):
            items.append(OptimizationItem(
                priority="P0" if ui_ms >= 0.15 * total_gpu_ms else "P1",
                title="UI 合批：Slate.MergeBatch + Texture Atlas + Retainer Box 缓存静态 UI",
                rationale=(
                    f"UI/Slate 相关 drawcall {len(ui_rows)} 个，合计约 {ui_ms:.2f} ms。"
                    f"大量独立 Image/Text Widget 未合批是移动端常见热点，合批 + 图集 + RTT 缓存可观回收。"
                ),
                effort="中",
                related_eids=self._eids(ui_rows),
                **self._gain(ui_ms, 0.25, 0.5),
            ))

        # 3) Register-spill shaders (work_reg >= 64).
        items.extend(self._shader_items(rows, mali_by_id))

        # 4) Repeated / un-batched drawcalls (instancing opportunity).
        items.extend(self._repeat_items(rows))

        # 5) VFX / particle LOD.
        vfx_ms = sum(self._ms(r) for r in vfx_rows)
        if vfx_ms >= max(MIN_ITEM_MS, 0.08 * total_gpu_ms):
            items.append(OptimizationItem(
                priority="P2",
                title="特效/粒子 LOD 与距离裁剪（待机特效 / 传送门粒子）",
                rationale=(
                    f"特效/半透 pass 合计约 {vfx_ms:.2f} ms（{len(vfx_rows)} draw）。"
                    f"玩家未靠近时降低粒子数、启用粒子 LOD 可降低半透过绘制与 PS 调用。"
                ),
                effort="中",
                related_eids=self._eids(vfx_rows),
                **self._gain(vfx_ms, 0.15, 0.35),
            ))

        # 6) High-triangle geometry → LOD.
        items.extend(self._geometry_items(rows))

        # 7) Large textures → shrink (memory/bandwidth, no direct ms gain).
        items.extend(self._texture_items(texture_audit))

        # Sort + dedup-ish (keep all, ordered by priority then gain).
        items.sort(key=lambda i: (_PRIORITY_ORDER.get(i.priority, 9), -i.expected_gain_ms_high))
        result.optimization_items = items

        result.scene_content_notes = self._scene_notes(rows, classifications, mali_by_id)
        result.other_notes = self._other_notes(rows, ui_rows, fullscreen)
        result.hotspot_problems = self._hotspot_problems(rows, classifications, mali_by_id)
        result.bottleneck_summary = self._bottleneck(
            fs_ms, ui_ms, vfx_ms, mali_by_id, total_gpu_ms
        )
        return result

    # ------------------------------------------------------------------
    # Item builders
    # ------------------------------------------------------------------
    def _shader_items(self, rows, mali_by_id) -> List[OptimizationItem]:
        items: List[OptimizationItem] = []
        # Map ps shader id -> involved draws / time.
        ps_time: Dict[str, float] = defaultdict(float)
        ps_eids: Dict[str, List[str]] = defaultdict(list)
        for r in rows:
            sid = self._ps_shader(r)
            if sid:
                ps_time[sid] += self._ms(r)
                ps_eids[sid].append(str(r.get("eid") or ""))

        spill = [m for m in mali_by_id.values() if m.register_spill]
        spill.sort(key=lambda m: ps_time.get(m.shader_id, 0.0), reverse=True)
        for m in spill[:3]:
            t = ps_time.get(m.shader_id, 0.0)
            items.append(OptimizationItem(
                priority="P1",
                title=f"拆分重 shader 变体、降低 work_register（避免 register spill）— shader {m.shader_id}",
                rationale=(
                    f"Mali 静态分析: work_register={m.work_registers}（≥64 触发寄存器溢出，性能断崖下降）。"
                    f"该 shader 关联 draw 约 {t:.2f} ms。建议拆分材质变体 / 减少中间变量与高精度计算。"
                ),
                effort="高",
                related_eids=ps_eids.get(m.shader_id, [])[:8],
                **self._gain(t, 0.1, 0.25),
            ))

        ls_bound = [
            m for m in mali_by_id.values()
            if not m.register_spill and (m.bound_unit == "LS" or m.ls_cycles >= MALI_LS_BOUND_CYCLES)
        ]
        ls_bound.sort(key=lambda m: ps_time.get(m.shader_id, 0.0), reverse=True)
        for m in ls_bound[:2]:
            t = ps_time.get(m.shader_id, 0.0)
            items.append(OptimizationItem(
                priority="P1",
                title=f"减少 uniform / 纹理加载（LS-bound）— shader {m.shader_id}",
                rationale=(
                    f"Mali 静态分析: 瓶颈单元=Load/Store（LS={m.ls_cycles:.1f}, uniform_reg={m.uniform_registers}）。"
                    f"该 shader 关联 draw 约 {t:.2f} ms。建议精简 shader 输入：减少混合层数 / 合并采样 / channel-pack 纹理。"
                ),
                effort="中",
                related_eids=ps_eids.get(m.shader_id, [])[:8],
                **self._gain(t, 0.1, 0.2),
            ))
        return items

    def _repeat_items(self, rows) -> List[OptimizationItem]:
        groups: Dict[tuple, List[Mapping[str, Any]]] = defaultdict(list)
        for r in rows:
            base = self._marker_base(r)
            if not base:
                continue
            key = (re.sub(r"[_\s]\d+$", "", base), self._ps_shader(r))
            groups[key].append(r)

        items: List[OptimizationItem] = []
        candidates = []
        for (base, _sid), rs in groups.items():
            if len(rs) < REPEAT_MIN_COUNT:
                continue
            # Already instanced draws are fine; target the un-batched ones.
            avg_inst = sum(int(r.get("instances") or 1) for r in rs) / len(rs)
            if avg_inst > 1.5:
                continue
            t = sum(self._ms(r) for r in rs)
            if t < MIN_ITEM_MS:
                continue
            candidates.append((base, rs, t))
        candidates.sort(key=lambda c: c[2], reverse=True)
        for base, rs, t in candidates[:3]:
            # gain from collapsing N draws ~ saving the redundant ones.
            redundant_frac = max(0.0, (len(rs) - 1) / len(rs))
            items.append(OptimizationItem(
                priority="P2",
                title=f"合批 / GPU Instancing（HISM）— `{base}` 重复 {len(rs)} 次未合批",
                rationale=(
                    f"相同材质/网格 `{base}` 提交了 {len(rs)} 个独立 drawcall（合计约 {t:.2f} ms）且未做实例化。"
                    f"相同种类使用 Hierarchical Instanced Static Mesh 可显著减少 drawcall。"
                ),
                effort="中",
                related_eids=self._eids(rs),
                **self._gain(t * redundant_frac, 0.3, 0.6),
            ))
        return items

    def _geometry_items(self, rows) -> List[OptimizationItem]:
        heavy = sorted(
            [r for r in rows if int(r.get("triangles") or 0) >= HIGH_TRIANGLE],
            key=lambda r: int(r.get("triangles") or 0),
            reverse=True,
        )
        if not heavy:
            return []
        t = sum(self._ms(r) for r in heavy[:8])
        top = heavy[0]
        return [OptimizationItem(
            priority="P3",
            title=f"高三角面网格强制 LOD — `{self._marker_base(top)}` 等 {len(heavy)} 个 draw",
            rationale=(
                f"最重 {top.get('triangles'):,} 三角面（{self._ms(top):.2f} ms）。"
                f"默认相机距离若不必使用 LOD0，可强制 LOD1 降低顶点与像素工作量。"
            ),
            effort="中",
            related_eids=self._eids(heavy),
            **self._gain(t, 0.05, 0.15),
        )]

    def _texture_items(self, texture_audit) -> List[OptimizationItem]:
        large = [t for t in texture_audit if getattr(t, "is_large", False)]
        if not large:
            return []
        large.sort(key=lambda t: t.byte_size_mb, reverse=True)
        names = "、".join(
            f"{t.name}({t.width}x{t.height})" for t in large[:3]
        )
        total_mb = sum(t.byte_size_mb for t in large)
        return [OptimizationItem(
            priority="P2",
            title=f"缩小大尺寸纹理 / 图集化（{len(large)} 张 ≥2048）",
            rationale=(
                f"大纹理: {names} … 合计约 {total_mb:.1f} MB。"
                f"天空盒等可由 4K 降到 2K，公共小图图集化，可省显存与采样带宽（ms 收益间接）。"
            ),
            effort="低",
            related_eids=[],
            expected_gain_ms_low=0.0,
            expected_gain_ms_high=0.0,
        )]

    # ------------------------------------------------------------------
    # Section text
    # ------------------------------------------------------------------
    def _scene_notes(self, rows, classifications, mali_by_id) -> List[str]:
        notes: List[str] = []
        # Heaviest class.
        class_ms: Dict[str, float] = defaultdict(float)
        class_cnt: Dict[str, int] = defaultdict(int)
        for r in rows:
            cls = classifications.get(str(r.get("eid") or ""))
            cid = getattr(cls, "class_id", "unknown")
            class_ms[cid] += self._ms(r)
            class_cnt[cid] += 1
        for cid, ms in sorted(class_ms.items(), key=lambda kv: kv[1], reverse=True)[:3]:
            notes.append(f"业务 class `{cid}`: {class_cnt[cid]} draw / 约 {ms:.2f} ms。")
        # Spill / LS-bound shader callouts.
        spill = [m for m in mali_by_id.values() if m.register_spill]
        if spill:
            notes.append(
                f"发现 {len(spill)} 个 fragment shader work_register≥64（register spill 风险）: "
                + "、".join(m.shader_id for m in spill[:5]) + "。"
            )
        ls = [m for m in mali_by_id.values() if m.bound_unit == "LS" and not m.register_spill]
        if ls:
            notes.append(
                f"{len(ls)} 个 shader 为 LS-bound（uniform/纹理加载瓶颈），建议精简 shader 输入。"
            )
        return notes

    def _other_notes(self, rows, ui_rows, fullscreen) -> List[str]:
        notes: List[str] = []
        if ui_rows:
            notes.append(
                f"UI/Slate 共 {len(ui_rows)} 个 drawcall，约 {sum(self._ms(r) for r in ui_rows):.2f} ms："
                f"排查是否大量独立 Widget 未合批、是否使用 Retainer Box 缓存。"
            )
        if fullscreen:
            notes.append(
                f"全屏 pass {len(fullscreen)} 个（{sum(self._ms(r) for r in fullscreen):.2f} ms）多为后处理/Copy/深度 Fetch，"
                f"与 Mobile HDR 关联。"
            )
        orphans = [r for r in rows if not (r.get("breadcrumbs") or r.get("pass_name"))]
        if orphans:
            notes.append(f"{len(orphans)} 个无 marker 的孤儿 drawcall，多为引擎内部 pass，需结合 EID 定位。")
        if not notes:
            notes.append("未发现明显的合批/后处理/孤儿 drawcall 类问题。")
        return notes

    def _hotspot_problems(self, rows, classifications, mali_by_id) -> Dict[str, str]:
        problems: Dict[str, str] = {}
        for r in rows:
            eid = str(r.get("eid") or "")
            tags: List[str] = []
            if self._is_fullscreen_post(r):
                tags.append("全屏后处理(填充率)")
            sid = self._ps_shader(r)
            m = mali_by_id.get(sid)
            if m and m.register_spill:
                tags.append(f"shader 寄存器溢出(WR{m.work_registers})")
            elif m and m.bound_unit == "LS":
                tags.append("LS-bound(带宽/uniform)")
            if self._is_ui(r):
                tags.append("UI drawcall")
            if int(r.get("triangles") or 0) >= HIGH_TRIANGLE:
                tags.append(f"高三角面({int(r.get('triangles') or 0):,})")
            cls = classifications.get(eid)
            if getattr(cls, "class_id", "").startswith("vfx"):
                tags.append("特效/粒子")
            if not tags and int(r.get("ps_instruction_count") or 0) >= 200:
                tags.append(f"重 PS 指令({int(r.get('ps_instruction_count') or 0)})")
            problems[eid] = " · ".join(tags) if tags else "-"
        return problems

    def _bottleneck(self, fs_ms, ui_ms, vfx_ms, mali_by_id, total_ms) -> str:
        parts: List[str] = []
        if fs_ms >= 0.1 * max(total_ms, 1e-6):
            parts.append("全屏后处理/填充率")
        if ui_ms >= 0.1 * max(total_ms, 1e-6):
            parts.append("UI 合批")
        if any(m.register_spill for m in mali_by_id.values()):
            parts.append("Shader 寄存器溢出(register spill)")
        if any(m.bound_unit == "LS" for m in mali_by_id.values()):
            parts.append("Shader 带宽/uniform 加载(LS-bound)")
        if vfx_ms >= 0.1 * max(total_ms, 1e-6):
            parts.append("特效/粒子半透")
        if not parts:
            parts.append("Fragment Shader / 像素填充")
        return "、".join(parts) + "（非 Drawcall 数量或顶点瓶颈）"

    # ------------------------------------------------------------------
    # Predicates / helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _ms(row: Mapping[str, Any]) -> float:
        return float(row.get("gpu_duration_ms") or 0.0)

    @staticmethod
    def _ps_shader(row: Mapping[str, Any]) -> str:
        ids = row.get("shader_ids") or {}
        if isinstance(ids, Mapping):
            return str(ids.get("ps") or "").strip()
        return ""

    @staticmethod
    def _eids(rows: List[Mapping[str, Any]]) -> List[str]:
        ordered = sorted(rows, key=lambda r: float(r.get("gpu_duration_ms") or 0.0), reverse=True)
        return [str(r.get("eid") or "") for r in ordered[:8] if r.get("eid") is not None]

    def _is_fullscreen_post(self, row: Mapping[str, Any]) -> bool:
        if int(row.get("triangles") or 0) > FULLSCREEN_TRI_MAX:
            return False
        return bool(FULLSCREEN_NAME_RX.search(self._text(row)))

    def _is_ui(self, row: Mapping[str, Any]) -> bool:
        if str(row.get("scene_pass") or "") == "UI":
            return True
        return bool(UI_NAME_RX.search(self._text(row)))

    @staticmethod
    def _is_vfx(row: Mapping[str, Any], classifications: Mapping[str, Any]) -> bool:
        if str(row.get("scene_pass") or "") in VFX_PASS_NAMES:
            return True
        cls = classifications.get(str(row.get("eid") or ""))
        return getattr(cls, "class_id", "").startswith("vfx")

    @staticmethod
    def _text(row: Mapping[str, Any]) -> str:
        parts = [str(row.get("pass_name") or "")]
        bc = row.get("breadcrumbs")
        if isinstance(bc, list):
            parts.extend(str(x) for x in bc)
        return " ".join(parts)

    @staticmethod
    def _marker_base(row: Mapping[str, Any]) -> str:
        name = str(row.get("pass_name") or "")
        if not name:
            bc = row.get("breadcrumbs")
            if isinstance(bc, list) and bc:
                name = str(bc[-1])
        name = re.sub(r"\b\d+\s*instances?\b", "", name, flags=re.I)
        return name.strip()

    @staticmethod
    def _gain(ms: float, low_frac: float, high_frac: float) -> Dict[str, float]:
        return {
            "expected_gain_ms_low": round(max(0.0, ms * low_frac), 2),
            "expected_gain_ms_high": round(max(0.0, ms * high_frac), 2),
        }
