"""LLM-assisted GLSL shader simplification.

Uses the configured OpenAI-compatible LLM to analyze shader code and propose
structural simplifications that rule-based analysis might miss, such as:
- Identifying semantically dead code paths
- Recognizing equivalent shorter expressions
- Detecting unused variable chains across complex control flow
- Suggesting branch elimination based on shader context understanding

Each LLM suggestion is converted into a RemovalCandidate that the visual
probe pipeline can verify via rendering comparison.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

import app.config as app_config
from app.services.glsl_code_analyzer import RemovalCandidate


@dataclass
class LlmSimplifyResult:
    candidates: List[RemovalCandidate] = field(default_factory=list)
    raw_response: str = ""
    model_used: str = ""
    elapsed_ms: int = 0
    error: str = ""
    token_usage: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_count": len(self.candidates),
            "model_used": self.model_used,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "token_usage": self.token_usage,
        }


_SYSTEM_PROMPT = """你是一个 GLSL shader 简化专家。你的任务是分析 fragment shader 源码，找出可以安全删除或简化的代码段。

规则：
1. 你必须返回 JSON 数组，每个元素是一个简化建议
2. 每个建议必须包含: "action" (remove_lines / replace_lines / remove_branch), "start_line", "end_line", "reason", "replacement" (仅 replace_lines 需要)
3. 不要修改写入 gl_FragColor / gl_FragData 等输出变量的最终赋值语句
4. 不要删除包含 discard 的语句
5. 优先建议：
   - 删除对最终输出无影响的中间计算
   - 用常量替换可被简化的表达式
   - 删除永远不会执行的代码分支
   - 删除无用的预处理指令和宏定义
   - 将复杂的 if/else 简化为单一分支（如果另一分支不影响输出）
6. 每个建议要独立可验证，不要假设其他建议已被采纳
7. 只返回 JSON，不要包含其他文本

响应格式：
```json
[
  {
    "action": "remove_lines",
    "start_line": 10,
    "end_line": 15,
    "reason": "这些行计算的值从未被输出变量使用"
  },
  {
    "action": "replace_lines",
    "start_line": 20,
    "end_line": 22,
    "reason": "此表达式可以被简化",
    "replacement": "  vec3 color = baseColor;"
  },
  {
    "action": "remove_branch",
    "start_line": 30,
    "end_line": 45,
    "keep": "if",
    "reason": "else 分支在此上下文中不可达"
  }
]
```"""


class LlmShaderSimplifier:
    """Use LLM to generate shader simplification candidates."""

    def __init__(self, timeout: float | None = None):
        self.timeout = timeout or app_config.OPENAI_TIMEOUT_SECONDS

    def is_available(self) -> bool:
        return bool(
            app_config.OPENAI_BASE_URL
            and app_config.OPENAI_API_KEY
            and app_config.OPENAI_MODEL
        )

    def generate_candidates(
        self,
        source: str,
        *,
        max_suggestions: int = 20,
    ) -> LlmSimplifyResult:
        if not self.is_available():
            return LlmSimplifyResult(error="LLM 未配置 (需要 openai_base_url, openai_api_key, openai_model)")

        t0 = time.time()
        lines = source.split("\n")
        numbered = "\n".join(f"{i+1:4d}| {line}" for i, line in enumerate(lines))
        context_limit = app_config.LLM_MAX_CONTEXT_CHARS
        if len(numbered) > context_limit:
            numbered = numbered[:context_limit - 100] + "\n...[truncated]"

        user_prompt = (
            f"请分析以下 GLSL fragment shader 并提出最多 {max_suggestions} 个简化建议。\n"
            f"shader 共 {len(lines)} 行：\n\n"
            f"```glsl\n{numbered}\n```"
        )

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        try:
            raw, token_usage = self._call_llm(messages)
        except Exception as exc:
            return LlmSimplifyResult(
                error=f"LLM 调用失败: {exc}",
                elapsed_ms=int((time.time() - t0) * 1000),
            )

        candidates = self._parse_response(raw, source, lines)

        return LlmSimplifyResult(
            candidates=candidates,
            raw_response=raw,
            model_used=app_config.OPENAI_MODEL,
            elapsed_ms=int((time.time() - t0) * 1000),
            token_usage=token_usage,
        )

    def _call_llm(self, messages: List[Dict]) -> Tuple[str, Dict[str, int]]:
        headers = {
            "Authorization": f"Bearer {app_config.OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": app_config.OPENAI_MODEL,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 4096,
        }
        url = f"{app_config.OPENAI_BASE_URL.rstrip('/')}/chat/completions"

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("LLM 返回空结果")

        content = choices[0].get("message", {}).get("content", "").strip()
        usage = data.get("usage") or {}
        token_usage = {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
        return content, token_usage

    def _parse_response(
        self, raw: str, source: str, lines: List[str],
    ) -> List[RemovalCandidate]:
        json_match = re.search(r"\[[\s\S]*\]", raw)
        if not json_match:
            return []

        try:
            suggestions = json.loads(json_match.group(0))
        except json.JSONDecodeError:
            return []

        if not isinstance(suggestions, list):
            return []

        candidates: List[RemovalCandidate] = []
        for idx, s in enumerate(suggestions):
            if not isinstance(s, dict):
                continue

            action = s.get("action", "")
            start = s.get("start_line", 0)
            end = s.get("end_line", start)
            reason = s.get("reason", "")

            if start < 1 or end < start or end > len(lines):
                continue

            try:
                candidate = self._build_candidate(
                    action, start, end, reason, s, source, lines, idx,
                )
                if candidate is not None:
                    candidates.append(candidate)
            except Exception:
                continue

        return candidates

    def _build_candidate(
        self,
        action: str,
        start: int,
        end: int,
        reason: str,
        suggestion: Dict,
        source: str,
        lines: List[str],
        idx: int,
    ) -> Optional[RemovalCandidate]:
        original_snippet = "\n".join(lines[start - 1:end])

        if action == "remove_lines":
            modified_lines = lines[:start - 1] + lines[end:]
            return RemovalCandidate(
                kind="llm_remove",
                label=f"llm-remove L{start}-{end}",
                description=f"[LLM] {reason[:100]}",
                line_range=(start, end),
                modified_source="\n".join(modified_lines),
                original_snippet=original_snippet[:200],
            )

        if action == "replace_lines":
            replacement = suggestion.get("replacement", "")
            if not replacement:
                return None
            replacement_lines = replacement.split("\n")
            modified_lines = lines[:start - 1] + replacement_lines + lines[end:]
            return RemovalCandidate(
                kind="llm_replace",
                label=f"llm-replace L{start}-{end}",
                description=f"[LLM] {reason[:100]}",
                line_range=(start, end),
                modified_source="\n".join(modified_lines),
                original_snippet=original_snippet[:200],
            )

        if action == "remove_branch":
            modified_lines = lines[:start - 1] + lines[end:]
            return RemovalCandidate(
                kind="llm_branch",
                label=f"llm-branch L{start}-{end}",
                description=f"[LLM 分支简化] {reason[:100]}",
                line_range=(start, end),
                modified_source="\n".join(modified_lines),
                original_snippet=original_snippet[:200],
            )

        return None
