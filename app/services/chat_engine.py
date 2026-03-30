from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

import httpx

import app.config as app_config


def _truncate_text(text: str, limit: int | None = None) -> str:
    limit = limit or app_config.LLM_MAX_CONTEXT_CHARS
    if len(text) <= limit:
        return text
    return text[: limit - 32] + "\n...[truncated by context limiter]"


class PromptBuilder:
    def build_messages(self, question: str, session_detail: Dict[str, Any]) -> List[Dict[str, str]]:
        metadata = session_detail["metadata"]
        analysis_json = session_detail.get("analysis_json") or {}
        analysis_markdown = session_detail.get("analysis_markdown") or ""
        deep_dive_json = session_detail.get("eid_deep_dive_json") or {}
        deep_dive_markdown = session_detail.get("eid_deep_dive_markdown") or ""
        ue_scan_json = session_detail.get("ue_scan_json") or {}
        ue_scan_markdown = session_detail.get("ue_scan_markdown") or ""
        chat_history = session_detail.get("chat_history") or []

        inputs = metadata.get("inputs", {})
        summary = metadata.get("summary", {})

        compact_context = {
            "session_id": metadata.get("session_id"),
            "status": metadata.get("status"),
            "inputs": inputs,
            "summary": summary,
            "analysis_top_causes": (analysis_json.get("ranked_causes") or [])[:3],
            "eid_deep_dive_summary": (deep_dive_json.get("summary") or {}),
            "ue_scan_summary": (ue_scan_json.get("summary") or {}),
        }

        system_prompt = (
            "你是一个 GPU/UE 联合诊断助手。"
            "你的任务是基于 RenderDoc 分析、EID 深挖结果、UE 源码扫描结果，回答用户的后续追问。"
            "回答必须使用中文简体，优先给出结论、依据和可执行建议。"
            "如果证据不足，要明确说明不确定点，但仍给出最合理的下一步。"
            "不要编造不存在的文件、变量、插件或引擎机制。"
            "若 UE 源码扫描结果存在，应优先结合源码证据回答。"
        )

        user_context = (
            "以下是当前 session 的上下文，请严格基于这些信息作答。\n\n"
            f"[结构化上下文]\n{json.dumps(compact_context, ensure_ascii=False, indent=2)}\n\n"
            f"[首轮分析 Markdown]\n{_truncate_text(analysis_markdown, 6000)}\n\n"
            f"[EID 深挖 Markdown]\n{_truncate_text(deep_dive_markdown, 8000)}\n\n"
            f"[UE 源码扫描 Markdown]\n{_truncate_text(ue_scan_markdown, 8000)}\n\n"
            f"[最近聊天记录]\n{_truncate_text(json.dumps(chat_history[-8:], ensure_ascii=False, indent=2), 4000)}\n\n"
            f"[用户问题]\n{question}"
        )

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_context},
        ]


class OpenAICompatibleProvider:
    def __init__(self) -> None:
        self.prompt_builder = PromptBuilder()

    def is_configured(self) -> bool:
        return bool(app_config.OPENAI_BASE_URL and app_config.OPENAI_API_KEY and app_config.OPENAI_MODEL)

    def answer(self, question: str, session_detail: Dict[str, Any]) -> Tuple[str, List[str]]:
        if not self.is_configured():
            raise RuntimeError("LLM Provider 未配置完整的 base_url/api_key/model")

        messages = self.prompt_builder.build_messages(question, session_detail)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": app_config.OPENAI_MODEL,
            "messages": messages,
            "temperature": 0.2,
        }
        url = f"{app_config.OPENAI_BASE_URL.rstrip('/')}/chat/completions"
        with httpx.Client(timeout=app_config.OPENAI_TIMEOUT_SECONDS) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("LLM Provider 返回为空")

        content = (
            choices[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        if not content:
            raise RuntimeError("LLM Provider 未返回文本内容")

        return content, [
            "analysis/analysis.json",
            "analysis/eid_deep_dive.json",
            "analysis/ue_scan.json",
            "llm:openai-compatible",
        ]


class LocalFallbackProvider:
    def answer(self, question: str, session_detail: Dict[str, Any]) -> Tuple[str, List[str]]:
        metadata = session_detail["metadata"]
        analysis_json = session_detail.get("analysis_json") or {}
        analysis_markdown = session_detail.get("analysis_markdown") or ""
        deep_dive_json = session_detail.get("eid_deep_dive_json") or {}
        deep_dive_markdown = session_detail.get("eid_deep_dive_markdown") or ""
        ue_scan_json = session_detail.get("ue_scan_json") or {}
        ue_scan_markdown = session_detail.get("ue_scan_markdown") or ""

        inputs = metadata.get("inputs", {})
        ranked = analysis_json.get("ranked_causes", [])
        top = ranked[0] if ranked else None

        q = question.strip()
        q_lower = q.lower()
        sources = ["analysis/analysis.json", "analysis/analysis.md"]
        deep_sources = ["analysis/eid_deep_dive.json", "analysis/eid_deep_dive.md"]
        ue_sources = ["analysis/ue_scan.json", "analysis/ue_scan.md"]

        if ue_scan_json and any(token in q for token in ("源码", "代码", "文件", "插件", "模块", "项目")):
            summary = ue_scan_json.get("summary") or {}
            top_matches = ue_scan_json.get("top_matches") or []
            first = top_matches[0] if top_matches else {}
            answer = (
                f"当前 UE 源码扫描建议优先关注：{', '.join(summary.get('suggested_focus', [])[:4]) or 'Source/Plugins'}。"
                f" 最可疑文件之一是：`{first.get('path', '暂无')}`。"
                f" 下一步建议：{summary.get('next_action', '优先阅读高分文件并对照当前 RenderDoc 结论。')}"
            )
            return answer, ue_sources

        if deep_dive_json and any(token in q for token in ("eid", "事件", "深挖", "draw", "shader", "bindings", "pipeline")):
            summary = deep_dive_json.get("summary") or {}
            top_hypothesis = summary.get("top_hypothesis") or {}
            answer = (
                f"当前 EID 深挖结论置信度为 `{summary.get('confidence', 'medium')}`。"
                f" Top 根因候选是：`{top_hypothesis.get('title', '未知')}`。"
                f" 关键依据包括：{'；'.join((top_hypothesis.get('because') or summary.get('findings') or [])[:3]) if (top_hypothesis.get('because') or summary.get('findings')) else '暂无关键发现'}"
            )
            return answer, deep_sources

        if deep_dive_json and any(token in q for token in ("ue", "蓝图", "sequencer", "材质实例", "mid", "mpc", "排查清单", "引擎")):
            summary = deep_dive_json.get("summary") or {}
            top_hypothesis = summary.get("top_hypothesis") or {}
            ue_checklist = summary.get("ue_checklist") or []
            first = ue_checklist[0] if ue_checklist else {}
            answer = (
                f"结合当前 RenderDoc 结论，UE 侧最优先排查方向是：`{top_hypothesis.get('title', '未知原因')}`。"
                f" 第一条建议是：{first.get('title', '先核对材质实例与挂载时机。')}"
                f" 原因：{first.get('why', '当前差异显示材质或输入链结构发生了变化。')}"
            )
            return answer, deep_sources

        if "eid" in q_lower:
            answer = (
                f"当前 session 记录的补充 EID 为：before=`{inputs.get('eid_before') or '未提供'}`，"
                f"after=`{inputs.get('eid_after') or '未提供'}`。"
                " 如果你要继续做事件级深挖，可以围绕这两个 EID 的 pipeline、bindings、shader 和 pixel history 扩展。"
            )
            return answer, sources

        if any(token in q for token in ("建议", "怎么验证", "下一步", "排查")):
            if ue_scan_json:
                summary = ue_scan_json.get("summary") or {}
                top_matches = ue_scan_json.get("top_matches") or []
                top_paths = [item.get("path", "") for item in top_matches[:3] if item.get("path")]
                answer = (
                    f"基于 UE 源码扫描，建议优先阅读这些文件：{'；'.join(top_paths) if top_paths else '暂无高优先级文件'}。"
                    f" 同时按目录优先级关注：{', '.join(summary.get('suggested_focus', [])[:4]) or 'Source/Plugins'}。"
                )
                return answer, ue_sources
            if deep_dive_json:
                summary = deep_dive_json.get("summary") or {}
                top_hypothesis = summary.get("top_hypothesis") or {}
                suggestions = top_hypothesis.get("suggestions") or []
                ue_checklist = summary.get("ue_checklist") or []
                ue_actions = []
                if ue_checklist:
                    ue_actions = ue_checklist[0].get("actions") or []
                answer = (
                    f"基于 EID 深挖，当前最优先的方向是：`{top_hypothesis.get('title', '未知原因')}`。"
                    f" 建议先做：{'；'.join((ue_actions or suggestions)[:3]) if (ue_actions or suggestions) else summary.get('conclusion', '优先排查 EID 对应的资源输入与上游 pass。')}"
                )
                return answer, deep_sources
            if top:
                validations = top.get("validation") or []
                advice = "；".join(validations[:3]) if validations else "优先对比目标 pass 的资源绑定、shader 与 pipeline。"
                answer = (
                    f"当前最优先的排查方向是：`{top.get('title', '未知原因')}`。"
                    f" 建议先做这几步：{advice}"
                )
            else:
                answer = "当前没有足够的结构化结论，建议先补充 EID 或像素坐标，再做事件级分析。"
            return answer, sources

        if any(token in q for token in ("为什么", "原因", "结论", "判断依据")):
            if ue_scan_json:
                summary = ue_scan_json.get("summary") or {}
                top_matches = ue_scan_json.get("top_matches") or []
                first = top_matches[0] if top_matches else {}
                answer = (
                    f"之所以优先怀疑 UE 侧这些路径，是因为源码扫描在 `{first.get('path', '暂无')}` 中命中了与当前 RenderDoc 假设高度相关的关键词："
                    f"{', '.join((first.get('matched_keywords') or [])[:6]) if first else '暂无'}。"
                )
                return answer, ue_sources
            if deep_dive_json:
                summary = deep_dive_json.get("summary") or {}
                top_hypothesis = summary.get("top_hypothesis") or {}
                answer = (
                    f"EID 深挖后的主结论是：{summary.get('conclusion', '暂无。')}"
                    f" 当前最强候选是：`{top_hypothesis.get('title', '未知原因')}`。"
                    f" 关键依据：{'；'.join((top_hypothesis.get('because') or summary.get('findings') or [])[:3])}"
                )
                return answer, deep_sources
            if top:
                evidence = top.get("evidence") or []
                evidence_text = "；".join(evidence[:3]) if evidence else "目前证据主要来自首轮 diff 与规则匹配。"
                answer = (
                    f"当前结论偏向：`{top.get('title', '未知原因')}`，"
                    f"置信度 `{top.get('confidence', 'low')}`。"
                    f" 主要依据是：{evidence_text}"
                )
            else:
                answer = "当前首轮分析没有形成高置信根因，更多像是需要补充 EID、像素或截图后继续定位。"
            return answer, sources

        if any(token in q for token in ("面部", "脸", "挂载", "骨骼")):
            if deep_dive_json:
                summary = deep_dive_json.get("summary") or {}
                top_hypothesis = summary.get("top_hypothesis") or {}
                ue_checklist = summary.get("ue_checklist") or []
                ue_hint = ue_checklist[0]["title"] if ue_checklist else "检查面部材质实例和挂载时机"
                answer = (
                    "结合当前 session 的 EID 深挖，面部问题更像是输入链路异常而不是 shader 本体变化。"
                    f" 当前最强候选是：`{top_hypothesis.get('title', '未知原因')}`。"
                    f" 结论：{summary.get('conclusion', '')}"
                    f" UE 侧建议先从：{ue_hint} 开始。"
                )
                return answer, deep_sources
            answer = (
                "结合当前 session，若问题集中在面部且 pass 已锁定，优先怀疑独立挂载面部网格的输入链路异常，"
                "包括骨骼姿态同步、独立材质实例参数、以及面部专用纹理输入。"
            )
            return answer, sources

        if top:
            answer = (
                f"当前 session 的首要结论是：`{top.get('title', '未知原因')}`。"
                f" 你当前关注的 pass 是 `{inputs.get('pass_name', '')}`，"
                f"问题描述是“{inputs.get('issue', '')}”。"
                " 如果你愿意，我建议下一轮提问聚焦“为什么是这个原因”或“如何验证这个原因”。"
            )
            return answer, sources

        summary = analysis_markdown.strip().splitlines()[:10]
        answer = "当前已保存该 session 的首轮结果，但结构化根因较弱。你可以继续补充 EID 或提出更具体的问题。"
        if deep_dive_markdown:
            answer += " 当前 session 已包含 EID 深挖结果，可直接继续问事件级问题。"
        if ue_scan_markdown:
            answer += " 当前 session 已包含 UE 源码扫描结果，可直接继续问源码和插件层问题。"
        if summary:
            answer += " 已有摘要：" + " ".join(summary[:3])
        return answer, sources


class ChatEngine:
    def __init__(self) -> None:
        self.local_provider = LocalFallbackProvider()
        self.remote_provider = OpenAICompatibleProvider()

    def answer(self, question: str, session_detail: Dict[str, Any]) -> Dict[str, Any]:
        provider_used = "local-fallback"
        if app_config.LLM_PROVIDER in {"openai", "openai_compatible", "remote"} and self.remote_provider.is_configured():
            try:
                answer, sources = self.remote_provider.answer(question, session_detail)
                provider_used = "openai-compatible"
            except Exception as exc:
                answer, sources = self.local_provider.answer(question, session_detail)
                answer += f"\n\n[提示] 远程 LLM 调用失败，已回退到本地规则引擎：{exc}"
        else:
            answer, sources = self.local_provider.answer(question, session_detail)
        return {
            "answer": answer,
            "sources": sources,
            "provider": provider_used,
        }
