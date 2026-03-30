from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple


def _normalize_json_text(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


class EidDeepDiveService:
    @staticmethod
    def _build_ue_checklist(top_hypothesis: Dict[str, Any], summary: Dict[str, Any]) -> List[Dict[str, Any]]:
        hypothesis_id = top_hypothesis.get("id", "")
        checklist: List[Dict[str, Any]] = []

        if hypothesis_id == "shader-permutation-switch":
            checklist.extend(
                [
                    {
                        "title": "核对材质实例是否切换了静态开关",
                        "why": "Shader permutation 变化通常意味着 Static Switch、Quality Level、Feature Level 或材质编译 key 发生了变化。",
                        "actions": [
                            "检查面部 Mesh 在异常帧和正常帧使用的 Material Instance 是否是同一个对象。",
                            "检查是否有蓝图、Sequencer、动画通知或挂载流程在运行时重新 Set Material。",
                            "对比手动改参数和 Sequencer 改参数时，是否触发了不同的材质重建路径。",
                        ],
                    },
                    {
                        "title": "检查 MID / MPC 初始化顺序",
                        "why": "若挂载完成后重新创建了 MID 或重置了 MPC，会导致面部进入不同 shader 路径。",
                        "actions": [
                            "确认面部挂载后是否重新 Create Dynamic Material Instance。",
                            "确认 BeginPlay、Attach 完成、AnimBP 初始化、Sequencer 生效这几个时机谁先谁后。",
                            "若手动改任意参数就恢复正常，重点检查是否缺少一次显式的 MID 参数回写。",
                        ],
                    },
                    {
                        "title": "对比异常帧与正常帧的材质 key",
                        "why": "RenderDoc 已显示 shader id 不同，UE 侧需要反查到底是谁让材质 key 变了。",
                        "actions": [
                            "在面部组件上打印当前材质名称、材质实例名称、父材质名称。",
                            "若有条件，在运行时记录面部材质静态参数集或关键开关值。",
                        ],
                    },
                ]
            )

        if hypothesis_id == "resource-chain-shift":
            checklist.extend(
                [
                    {
                        "title": "逐项核对面部贴图资源链",
                        "why": "新增或缺失的资源槽通常意味着某张面部专用贴图、mask、LUT、法线/AO 走了不同绑定路径。",
                        "actions": [
                            "检查面部材质实例中的 Texture Parameter 是否在异常帧被替换。",
                            "重点核对法线、AO、Subsurface、Mask、LUT、脸部特效贴图。",
                            "确认挂载后是否从角色主体复制参数时漏掉了某个 Texture Parameter。",
                        ],
                    },
                    {
                        "title": "检查绑定槽来源是否受挂载流程影响",
                        "why": "外接骨骼挂载的面部模型可能在附着后走另一套组件初始化逻辑。",
                        "actions": [
                            "检查面部组件是否在 Attach 后重新初始化材质或覆盖材质槽。",
                            "检查是否存在专门给脸部挂件用的 Post Process / Overlay / Decal 材质注入。",
                        ],
                    },
                ]
            )

        if hypothesis_id == "uniform-layout-shift":
            checklist.extend(
                [
                    {
                        "title": "核对动态参数写入路径",
                        "why": "CBuffer 数量和签名变化，通常意味着参数集结构或启用的功能块变了。",
                        "actions": [
                            "核对 Sequencer、蓝图、动画蓝图、角色状态机是否都在写同一组面部参数。",
                            "检查是否存在参数只在手动修改时触发 PostEdit 或刷新逻辑，但 Sequencer 改值不会触发。",
                            "对比异常前后 Scalar / Vector 参数默认值和运行时值。",
                        ],
                    },
                    {
                        "title": "验证是否需要一次软重建",
                        "why": "你之前已经观察到手动改任意参数就恢复正常，这非常像参数回写触发了渲染状态刷新。",
                        "actions": [
                            "在挂载完成后一帧，对面部 MID 做一次参数微抖动并还原。",
                            "若这样能稳定修复，就说明问题更偏向初始化/刷新时机，而不是参数值本身。",
                        ],
                    },
                ]
            )

        if hypothesis_id == "upstream-input-drift":
            checklist.extend(
                [
                    {
                        "title": "检查挂载骨骼后的姿态同步",
                        "why": "若静态材质路径一致但画面不同，更像骨骼姿态、法线、切线或输入插值问题。",
                        "actions": [
                            "检查面部组件使用的是 Leader Pose、Copy Pose 还是其他同步方式。",
                            "确认 Attach 完成后是否立即刷新了姿态，是否存在首帧或切换帧拿旧骨骼数据。",
                            "检查面部是否有独立动画蓝图或后处理动画节点覆盖了姿态。",
                        ],
                    },
                    {
                        "title": "检查 Skin Cache / 法线切线刷新时机",
                        "why": "面部是独立挂载网格时，最容易出问题的是蒙皮和切线空间刷新链。",
                        "actions": [
                            "确认异常发生时面部是否切了 LOD。",
                            "确认法线贴图与切线空间是否在挂载后保持一致。",
                        ],
                    },
                ]
            )

        if not checklist:
            checklist.append(
                {
                    "title": "补充更多 UE 侧上下文",
                    "why": "当前 RenderDoc 证据还不足以唯一定位到 UE 逻辑。",
                    "actions": [
                        "补充异常帧和正常帧的组件初始化顺序、材质设置流程、Sequencer 控制项。",
                        "继续提供像素历史、截图、以及异常前后蓝图事件链。",
                    ],
                }
            )

        checklist.append(
            {
                "title": "最小复现验证",
                "why": "无论哪种根因，最有效的方式都是验证“手动改参数恢复正常”到底触发了哪条路径。",
                "actions": [
                    "在面部挂载完成后一帧，尝试 Visibility 切换或 MID 参数微抖动，观察是否稳定修复。",
                    "若 Sequencer 无法复现，但手动编辑能复现，优先排查 Editor 手改与运行时改值路径差异。",
                ],
            }
        )

        return checklist

    def _run(self, args: List[str]) -> Tuple[int, str]:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        return proc.returncode, output.strip()

    def _capture_artifacts(self, capture_path: Path, eid: str) -> Dict[str, Any]:
        eid = str(eid).strip()
        open_rc, open_out = self._run(["rdc", "open", str(capture_path)])
        if open_rc != 0:
            raise RuntimeError(f"无法打开 capture: {capture_path}\n{open_out}")

        commands = {
            "draw": ["rdc", "draw", eid, "--json"],
            "draws": ["rdc", "draws", "--json"],
            "pipeline": ["rdc", "pipeline", eid, "--json"],
            "pipeline_ps": ["rdc", "pipeline", eid, "ps", "--json"],
            "bindings": ["rdc", "bindings", eid, "--json"],
            "shader_ps": ["rdc", "shader", eid, "ps", "--reflect", "--json"],
            "events": ["rdc", "events", "--range", f"{eid}:{eid}", "--json"],
        }

        result: Dict[str, Any] = {
            "eid": eid,
            "open": open_out,
            "commands": {},
        }

        for key, cmd in commands.items():
            rc, out = self._run(cmd)
            result["commands"][key] = {
                "rc": rc,
                "output": _normalize_json_text(out),
                "raw": out,
            }

        self._run(["rdc", "close"])
        return result

    @staticmethod
    def _extract_exact_draw(payload: Dict[str, Any]) -> Dict[str, Any]:
        direct_draw = payload.get("commands", {}).get("draw", {}).get("output") or {}
        if isinstance(direct_draw, dict) and "error" not in direct_draw and direct_draw:
            return direct_draw

        draws = payload.get("commands", {}).get("draws", {}).get("output") or []
        eid = str(payload.get("eid", "")).strip()
        if isinstance(draws, list):
            for item in draws:
                if not isinstance(item, dict):
                    continue
                item_eid = item.get("eid") or item.get("Event")
                if str(item_eid) == eid:
                    return item
        return {}

    @staticmethod
    def _extract_exact_event(payload: Dict[str, Any]) -> Dict[str, Any]:
        events = payload.get("commands", {}).get("events", {}).get("output") or []
        eid = str(payload.get("eid", "")).strip()
        if isinstance(events, list):
            for item in events:
                if not isinstance(item, dict):
                    continue
                item_eid = item.get("eid") or item.get("Event")
                if str(item_eid) == eid:
                    return item
        return {}

    @staticmethod
    def _extract_draw_marker(payload: Dict[str, Any]) -> str:
        draw = EidDeepDiveService._extract_exact_draw(payload)
        if isinstance(draw, dict):
            return str(draw.get("Marker") or draw.get("marker") or "")
        return ""

    @staticmethod
    def _extract_shader_id(payload: Dict[str, Any]) -> str:
        shader = payload.get("commands", {}).get("shader_ps", {}).get("output") or {}
        if isinstance(shader, dict):
            return str(shader.get("shader") or "")
        return ""

    @staticmethod
    def _extract_binding_names(payload: Dict[str, Any]) -> List[str]:
        bindings = payload.get("commands", {}).get("bindings", {}).get("output") or []
        names: List[str] = []
        if isinstance(bindings, list):
            for item in bindings:
                if isinstance(item, dict):
                    name = item.get("name")
                    if name:
                        names.append(str(name))
        return names

    @staticmethod
    def _extract_pipeline_ps_detail(payload: Dict[str, Any]) -> Dict[str, Any]:
        pipeline_ps = payload.get("commands", {}).get("pipeline_ps", {}).get("output") or {}
        if isinstance(pipeline_ps, dict):
            return pipeline_ps.get("section_detail") or {}
        return {}

    @staticmethod
    def _extract_shader_reflection(payload: Dict[str, Any]) -> Dict[str, Any]:
        shader = payload.get("commands", {}).get("shader_ps", {}).get("output") or {}
        if isinstance(shader, dict):
            return shader.get("reflection") or {}
        return {}

    @staticmethod
    def _extract_cbuffer_signature(payload: Dict[str, Any]) -> List[str]:
        reflection = EidDeepDiveService._extract_shader_reflection(payload)
        cbuffers = reflection.get("cbuffers") or []
        result: List[str] = []
        if isinstance(cbuffers, list):
            for item in cbuffers:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                slot = str(item.get("slot") if item.get("slot") is not None else "")
                vars_count = str(item.get("vars") if item.get("vars") is not None else "")
                result.append(f"{name}@slot{slot}:vars{vars_count}")
        return result

    @staticmethod
    def _extract_input_count(payload: Dict[str, Any]) -> int:
        reflection = EidDeepDiveService._extract_shader_reflection(payload)
        inputs = reflection.get("inputs") or []
        return len(inputs) if isinstance(inputs, list) else 0

    @staticmethod
    def _extract_output_count(payload: Dict[str, Any]) -> int:
        reflection = EidDeepDiveService._extract_shader_reflection(payload)
        outputs = reflection.get("outputs") or []
        return len(outputs) if isinstance(outputs, list) else 0

    @staticmethod
    def _build_hypotheses(
        *,
        before_shader: str,
        after_shader: str,
        before_bindings: List[str],
        after_bindings: List[str],
        before_ro: Any,
        after_ro: Any,
        before_cbuffer_count: Any,
        after_cbuffer_count: Any,
        before_cbuffers: List[str],
        after_cbuffers: List[str],
        before_input_count: int,
        after_input_count: int,
        before_marker: str,
        after_marker: str,
    ) -> List[Dict[str, Any]]:
        hypotheses: List[Dict[str, Any]] = []

        added_bindings = [name for name in after_bindings if name not in before_bindings]
        removed_bindings = [name for name in before_bindings if name not in after_bindings]
        before_only_cb = [item for item in before_cbuffers if item not in after_cbuffers]
        after_only_cb = [item for item in after_cbuffers if item not in before_cbuffers]

        if before_shader and after_shader and before_shader != after_shader:
            hypotheses.append(
                {
                    "id": "shader-permutation-switch",
                    "title": "材质变体或 Shader permutation 切换",
                    "score": 92,
                    "confidence": "high",
                    "because": [
                        f"PS Shader ID 发生变化：before=`{before_shader}`，after=`{after_shader}`。",
                        f"Shader 输入数量变化：before=`{before_input_count}`，after=`{after_input_count}`。",
                    ],
                    "suggestions": [
                        "核对面部材质实例在异常帧和正常帧是否切到了不同静态开关或 quality/permutation。",
                        "检查挂载完成后是否有代码或蓝图重新设置材质、MPC、MID，导致 shader key 改变。",
                    ],
                }
            )

        if added_bindings or removed_bindings:
            hypotheses.append(
                {
                    "id": "resource-chain-shift",
                    "title": "资源绑定链变化导致面部走入不同输入路径",
                    "score": 88,
                    "confidence": "high",
                    "because": [
                        f"绑定槽差异：新增=`{', '.join(added_bindings) if added_bindings else '无'}`，缺失=`{', '.join(removed_bindings) if removed_bindings else '无'}`。",
                        f"PS 资源访问数量变化：before=`{before_ro}`，after=`{after_ro}`。",
                    ],
                    "suggestions": [
                        "重点核对新增资源槽对应的面部专用贴图、mask、LUT、AO、法线等输入。",
                        "检查面部挂载后是否重新创建了材质实例，导致纹理绑定数量或顺序发生变化。",
                    ],
                }
            )

        if before_cbuffer_count != after_cbuffer_count or before_only_cb or after_only_cb:
            hypotheses.append(
                {
                    "id": "uniform-layout-shift",
                    "title": "材质实例参数或常量缓冲布局变化",
                    "score": 84,
                    "confidence": "high",
                    "because": [
                        f"CBuffer 数量变化：before=`{before_cbuffer_count}`，after=`{after_cbuffer_count}`。",
                        f"CBuffer 签名差异：before_only=`{'; '.join(before_only_cb[:3]) if before_only_cb else '无'}`，after_only=`{'; '.join(after_only_cb[:3]) if after_only_cb else '无'}`。",
                    ],
                    "suggestions": [
                        "核对异常前后是否有 MID/MPC 参数初始化顺序差异，尤其是挂载后动态设置参数的逻辑。",
                        "检查 Sequencer 与手动改参数是否触发了不同的材质更新路径。",
                    ],
                }
            )

        if before_marker and after_marker and before_marker == after_marker and before_shader == after_shader:
            hypotheses.append(
                {
                    "id": "upstream-input-drift",
                    "title": "上游输入内容或姿态插值差异",
                    "score": 68,
                    "confidence": "medium",
                    "because": [
                        "Marker 与 Shader 一致，静态材质路径未明显切换。",
                        "这种情况下更像是上游贴图内容、骨骼姿态、法线/切线或输入插值本身发生变化。",
                    ],
                    "suggestions": [
                        "对比同一帧面部相关贴图内容和姿态矩阵，确认是否只有输入内容变了。",
                        "检查外接骨骼挂载时机，确认面部是否在某些帧拿到了旧姿态或未刷新数据。",
                    ],
                }
            )

        if not hypotheses:
            hypotheses.append(
                {
                    "id": "insufficient-evidence",
                    "title": "当前证据不足，需补充像素级或更细 EID 信息",
                    "score": 40,
                    "confidence": "low",
                    "because": [
                        "静态状态差异不足以直接判断根因。",
                    ],
                    "suggestions": [
                        "补充该 EID 对应像素的 Pixel History 和 debug pixel。",
                        "若问题集中在面部，建议同时抓取异常帧与正常帧的面部区域截图作为辅助输入。",
                    ],
                }
            )

        hypotheses.sort(key=lambda item: item["score"], reverse=True)
        return hypotheses

    def _build_summary(self, before_payload: Dict[str, Any], after_payload: Dict[str, Any]) -> Dict[str, Any]:
        before_marker = self._extract_draw_marker(before_payload)
        after_marker = self._extract_draw_marker(after_payload)
        before_draw = self._extract_exact_draw(before_payload)
        after_draw = self._extract_exact_draw(after_payload)
        before_event = self._extract_exact_event(before_payload)
        after_event = self._extract_exact_event(after_payload)
        before_shader = self._extract_shader_id(before_payload)
        after_shader = self._extract_shader_id(after_payload)
        before_bindings = self._extract_binding_names(before_payload)
        after_bindings = self._extract_binding_names(after_payload)
        before_ps = self._extract_pipeline_ps_detail(before_payload)
        after_ps = self._extract_pipeline_ps_detail(after_payload)
        before_cbuffers = self._extract_cbuffer_signature(before_payload)
        after_cbuffers = self._extract_cbuffer_signature(after_payload)
        before_input_count = self._extract_input_count(before_payload)
        after_input_count = self._extract_input_count(after_payload)
        before_output_count = self._extract_output_count(before_payload)
        after_output_count = self._extract_output_count(after_payload)

        findings: List[str] = []
        confidence = "medium"

        if before_marker and after_marker and before_marker == after_marker:
            findings.append(f"before/after EID 指向相同 marker：`{before_marker}`。")
        else:
            findings.append(
                f"before/after EID 的 marker 不一致：before=`{before_marker or '未知'}`，after=`{after_marker or '未知'}`。"
            )
            confidence = "low"

        if before_shader and after_shader and before_shader == after_shader:
            findings.append(f"PS Shader ID 一致：`{before_shader}`，说明大概率不是像素着色器二进制本体变化。")
        else:
            findings.append(
                f"PS Shader ID 不一致：before=`{before_shader or '未知'}`，after=`{after_shader or '未知'}`。"
            )
            confidence = "medium" if before_shader and after_shader else "low"

        added = [name for name in after_bindings if name not in before_bindings]
        removed = [name for name in before_bindings if name not in after_bindings]

        if before_bindings == after_bindings and before_bindings:
            findings.append("PS 绑定槽位名一致，静态资源槽布局未出现明显漂移。")
        elif before_bindings or after_bindings:
            findings.append(
                "PS 绑定槽位存在差异："
                f"新增=`{', '.join(added) if added else '无'}`，"
                f"缺失=`{', '.join(removed) if removed else '无'}`。"
            )

        before_ro = before_ps.get("ro")
        after_ro = after_ps.get("ro")
        before_rw = before_ps.get("rw")
        after_rw = after_ps.get("rw")
        before_cbuffer_count = before_ps.get("cbuffers")
        after_cbuffer_count = after_ps.get("cbuffers")
        if before_ro != after_ro or before_rw != after_rw:
            findings.append(
                f"PS 资源访问数量变化：before ro/rw=`{before_ro}/{before_rw}`，after ro/rw=`{after_ro}/{after_rw}`。"
            )
        if before_cbuffer_count != after_cbuffer_count:
            findings.append(
                f"PS 常量缓冲数量变化：before=`{before_cbuffer_count}`，after=`{after_cbuffer_count}`。"
            )

        if before_input_count != after_input_count or before_output_count != after_output_count:
            findings.append(
                f"Shader 反射输入/输出数量变化：before=`{before_input_count}/{before_output_count}`，"
                f"after=`{after_input_count}/{after_output_count}`。"
            )

        before_only = [item for item in before_cbuffers if item not in after_cbuffers]
        after_only = [item for item in after_cbuffers if item not in before_cbuffers]
        if before_cbuffers != after_cbuffers:
            findings.append(
                "反射出的 cbuffer 签名存在差异："
                f"before_only=`{'; '.join(before_only[:4]) if before_only else '无'}`，"
                f"after_only=`{'; '.join(after_only[:4]) if after_only else '无'}`。"
            )

        before_triangles = before_draw.get("Triangles") or before_draw.get("triangles")
        after_triangles = after_draw.get("Triangles") or after_draw.get("triangles")
        if before_triangles and after_triangles and str(before_triangles) == str(after_triangles):
            findings.append(f"两侧 draw 的三角形数一致：`{before_triangles}`。")
        elif before_triangles or after_triangles:
            findings.append(f"两侧 draw 的三角形数不一致：before=`{before_triangles}`，after=`{after_triangles}`。")

        before_event_type = before_event.get("type") or before_event.get("Type")
        after_event_type = after_event.get("type") or after_event.get("Type")
        if before_event_type or after_event_type:
            findings.append(f"EID 事件类型：before=`{before_event_type or '未知'}`，after=`{after_event_type or '未知'}`。")

        hypotheses = self._build_hypotheses(
            before_shader=before_shader,
            after_shader=after_shader,
            before_bindings=before_bindings,
            after_bindings=after_bindings,
            before_ro=before_ro,
            after_ro=after_ro,
            before_cbuffer_count=before_cbuffer_count,
            after_cbuffer_count=after_cbuffer_count,
            before_cbuffers=before_cbuffers,
            after_cbuffers=after_cbuffers,
            before_input_count=before_input_count,
            after_input_count=after_input_count,
            before_marker=before_marker,
            after_marker=after_marker,
        )
        top = hypotheses[0]
        ue_checklist = self._build_ue_checklist(top, {})

        if before_shader and after_shader and before_shader != after_shader:
            conclusion = (
                "EID 深挖显示 before/after 的像素着色器与资源访问结构都发生了变化，"
                "当前优先怀疑材质变体切换、资源绑定链变化、或者面部独立挂载后走入了不同材质路径。"
            )
        elif before_bindings != after_bindings or before_cbuffers != after_cbuffers:
            conclusion = (
                "EID 深挖显示 shader 本体未必变化明显，但资源槽位或常量缓冲签名已经不同，"
                "应优先排查面部材质实例、MPC/MID 初始化顺序、以及挂载后输入资源链。"
            )
        else:
            conclusion = (
                "EID 深挖显示主要静态状态接近，若问题仍存在，更像是输入插值、上游贴图内容、"
                "或者面部独立挂载带来的姿态/骨骼同步问题。"
            )

        return {
            "confidence": max(confidence, top.get("confidence", "low"), key=lambda x: {"low": 1, "medium": 2, "high": 3}.get(x, 0)),
            "findings": findings,
            "conclusion": conclusion,
            "top_hypothesis": top,
            "hypotheses": hypotheses,
            "ue_checklist": ue_checklist,
            "before_marker": before_marker,
            "after_marker": after_marker,
            "before_shader": before_shader,
            "after_shader": after_shader,
            "before_bindings": before_bindings,
            "after_bindings": after_bindings,
            "before_ro": before_ro,
            "after_ro": after_ro,
            "before_rw": before_rw,
            "after_rw": after_rw,
            "before_cbuffer_count": before_cbuffer_count,
            "after_cbuffer_count": after_cbuffer_count,
            "before_cbuffer_signature": before_cbuffers,
            "after_cbuffer_signature": after_cbuffers,
            "before_input_count": before_input_count,
            "after_input_count": after_input_count,
            "before_output_count": before_output_count,
            "after_output_count": after_output_count,
        }

    def run(
        self,
        before_capture: Path,
        after_capture: Path,
        eid_before: str,
        eid_after: str,
        out_dir: Path,
    ) -> Dict[str, Any]:
        out_dir.mkdir(parents=True, exist_ok=True)
        before_payload = self._capture_artifacts(before_capture, eid_before)
        after_payload = self._capture_artifacts(after_capture, eid_after)
        summary = self._build_summary(before_payload, after_payload)

        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "before_capture": str(before_capture),
            "after_capture": str(after_capture),
            "eid_before": str(eid_before),
            "eid_after": str(eid_after),
            "summary": summary,
            "before": before_payload,
            "after": after_payload,
        }

        json_path = out_dir / "eid_deep_dive.json"
        md_path = out_dir / "eid_deep_dive.md"
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path.write_text(self._to_markdown(report), encoding="utf-8")
        return {
            "json_path": str(json_path),
            "md_path": str(md_path),
            "report": report,
        }

    @staticmethod
    def _to_markdown(report: Dict[str, Any]) -> str:
        summary = report["summary"]
        lines = [
            "# EID 深挖报告",
            "",
            f"- 生成时间: {report['generated_at']}",
            f"- Before EID: `{report['eid_before']}`",
            f"- After EID: `{report['eid_after']}`",
            f"- 置信度: `{summary['confidence']}`",
            "",
            "## 关键发现",
        ]
        for item in summary["findings"]:
            lines.append(f"- {item}")
        lines.extend(
            [
                "",
                "## Top 根因候选",
            ]
        )
        for idx, item in enumerate(summary.get("hypotheses", [])[:3], start=1):
            lines.append(f"### {idx}. {item['title']}  (score={item['score']}, {item['confidence']})")
            for because in item.get("because", [])[:3]:
                lines.append(f"- 依据: {because}")
            for suggestion in item.get("suggestions", [])[:2]:
                lines.append(f"- 建议验证: {suggestion}")
            lines.append("")

        lines.extend(
            [
                "## 结论建议",
                f"- {summary['conclusion']}",
                "",
                "## 结构化对比",
                f"- PS Shader: before=`{summary.get('before_shader', '')}` / after=`{summary.get('after_shader', '')}`",
                f"- PS RO/RW: before=`{summary.get('before_ro', '')}/{summary.get('before_rw', '')}` / after=`{summary.get('after_ro', '')}/{summary.get('after_rw', '')}`",
                f"- PS CBuffer 数量: before=`{summary.get('before_cbuffer_count', '')}` / after=`{summary.get('after_cbuffer_count', '')}`",
                f"- Shader IO 数量: before=`{summary.get('before_input_count', '')}/{summary.get('before_output_count', '')}` / after=`{summary.get('after_input_count', '')}/{summary.get('after_output_count', '')}`",
                f"- 绑定槽数量: before=`{len(summary.get('before_bindings', []))}` / after=`{len(summary.get('after_bindings', []))}`",
                "",
                "## UE 排查建议清单",
            ]
        )
        for idx, item in enumerate(summary.get("ue_checklist", []), start=1):
            lines.append(f"### {idx}. {item['title']}")
            lines.append(f"- 原因: {item['why']}")
            for action in item.get("actions", [])[:4]:
                lines.append(f"- 操作: {action}")
            lines.append("")

        lines.extend(
            [
                "## 候选方向",
                "- 若 shader 或 cbuffer 数量变化，优先排查材质变体、材质实例参数、MPC/MID 初始化顺序。",
                "- 若绑定槽数量变化，优先核对面部专用贴图、mask、LUT、法线/AO 输入是否走了不同资源链。",
                "- 若后续还要深挖，建议补充该 EID 对应像素的 Pixel History 与 debug pixel。",
            ]
        )
        return "\n".join(lines) + "\n"
