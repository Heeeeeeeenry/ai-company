# -*- coding: utf-8 -*-
"""Conversation Compressor — intelligent history compression for LLM context.

When conversation history grows too large, this module compresses it by:
- Keeping the most recent 3 turns in full detail
- Summarizing older turns into a compact paragraph
- Optionally using a lightweight LLM call for the summary
"""

from __future__ import annotations

import json
from typing import Optional

from src.utils.token_budget import _estimate_tokens


# ─── Summary Generation via LLM ───────────────

async def _llm_summarize(conversation_text: str) -> str:
    """Use a lightweight LLM call to summarize old conversation turns.

    Falls back to simple extraction on any failure.
    """
    try:
        from src.config import config
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = ChatOpenAI(
            model="deepseek-chat",
            api_key=config.deepseek_api_key,
            base_url="https://api.deepseek.com/v1",
            timeout=15,
            max_retries=0,
            max_tokens=300,
            temperature=0.3,
        )

        prompt = (
            "将以下对话历史压缩为一段简洁的中文摘要（不超过150字），"
            "保留关键事实、决策和结果：\n\n"
            f"{conversation_text[:3000]}"
        )

        response = await llm.ainvoke([
            SystemMessage(content="你是对话摘要助手，输出简洁的中文摘要。"),
            HumanMessage(content=prompt),
        ])

        summary = str(response.content).strip()
        if summary and len(summary) > 10:
            return f"[历史摘要] {summary}"
    except Exception:
        pass

    # Fallback: simple extraction
    return _simple_extract_summary(conversation_text)


def _simple_extract_summary(text: str) -> str:
    """Simple rule-based summary: extract key sentences."""
    lines = text.strip().split("\n")
    significant = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Keep lines that look like questions, answers, or decisions
        if any(kw in line for kw in ["Q:", "A:", "决定", "结果", "完成", "失败", "错误",
                                        "search", "fetch", "查", "搜", "总结"]):
            significant.append(line[:120])
        elif len(line) < 15:
            continue  # skip very short lines
        else:
            significant.append(line[:80])

    if significant:
        return "[历史概要] " + "；".join(significant[:8])
    return "[历史概要] 共 " + str(len(lines)) + " 行对话记录"


class Compressor:
    """Compress conversation history for LLM context windows.

    Strategy:
      - Split history into turns (Q/A pairs)
      - Keep the most recent 3 turns in full
      - Summarize older turns into a compact paragraph
      - Only triggers when history exceeds a char threshold
    """

    def __init__(self, keep_recent: int = 3, trigger_chars: int = 1000):
        """Initialize compressor.

        Args:
            keep_recent: Number of most recent turns to keep in full.
            trigger_chars: Only compress when total chars exceed this.
        """
        self.keep_recent = keep_recent
        self.trigger_chars = trigger_chars

    @staticmethod
    def _split_turns(history: list[dict]) -> list[dict]:
        """Ensure history items have 'user' and 'assistant' keys."""
        turns = []
        for item in history:
            if not isinstance(item, dict):
                continue
            q = item.get("user", item.get("question", item.get("q", "")))
            a = item.get("assistant", item.get("answer", item.get("a", "")))
            if q or a:
                turns.append({"user": str(q)[:500], "assistant": str(a)[:500]})
        return turns

    def compress_conversation(
        self,
        history: list[dict],
        max_tokens: Optional[int] = None,
    ) -> str:
        """Compress conversation history into a compact string.

        Args:
            history: List of turn dicts with 'user'/'assistant' keys.
            max_tokens: Optional token budget for the compressed output.
                       If set, the result is guaranteed to stay under this.

        Returns:
            Compact conversation string suitable for system context.
        """
        turns = self._split_turns(history)
        if not turns:
            return ""

        # Build full text to check if compression is needed
        full_text = "\n---\n".join(
            f"Q: {t['user']}\nA: {t['assistant']}" for t in turns
        )

        if len(full_text) <= self.trigger_chars and max_tokens is None:
            return full_text

        # Split: recent turns (keep full) vs old turns (summarize)
        if len(turns) <= self.keep_recent:
            # All turns are "recent" — no summarization needed
            # But may still need to trim for max_tokens
            if max_tokens and _estimate_tokens(full_text) > max_tokens:
                ratio = max_tokens / max(_estimate_tokens(full_text), 1)
                trim_len = int(len(full_text) * ratio * 0.9)
                return full_text[:trim_len] + "\n\n[conversation trimmed for token budget]"
            return full_text

        recent_turns = turns[-self.keep_recent:]
        old_turns = turns[:-self.keep_recent]

        # Build compact format for old turns
        old_text = "\n---\n".join(
            f"Q: {t['user']}\nA: {t['assistant']}" for t in old_turns
        )

        # Use simple extract (fast path — LLM summarization is async, call explicitly)
        summary = _simple_extract_summary(old_text)

        recent_text = "\n---\n".join(
            f"Q: {t['user']}\nA: {t['assistant']}" for t in recent_turns
        )

        result = f"{summary}\n\n## 最近对话 (Recent, {len(recent_turns)} turns)\n{recent_text}"

        # Enforce max_tokens if set
        if max_tokens:
            est = _estimate_tokens(result)
            if est > max_tokens:
                ratio = max_tokens / max(est, 1)
                trim_len = int(len(result) * ratio * 0.9)
                result = result[:trim_len] + "\n\n[result trimmed for token budget]"

        return result

    async def compress_conversation_async(
        self,
        history: list[dict],
        max_tokens: Optional[int] = None,
    ) -> str:
        """Async version that uses LLM for summary generation."""
        turns = self._split_turns(history)
        if not turns:
            return ""

        full_text = "\n---\n".join(
            f"Q: {t['user']}\nA: {t['assistant']}" for t in turns
        )

        if len(full_text) <= self.trigger_chars and max_tokens is None:
            return full_text

        if len(turns) <= self.keep_recent:
            if max_tokens and _estimate_tokens(full_text) > max_tokens:
                ratio = max_tokens / max(_estimate_tokens(full_text), 1)
                trim_len = int(len(full_text) * ratio * 0.9)
                return full_text[:trim_len] + "\n\n[conversation trimmed for token budget]"
            return full_text

        recent_turns = turns[-self.keep_recent:]
        old_turns = turns[:-self.keep_recent]

        old_text = "\n---\n".join(
            f"Q: {t['user']}\nA: {t['assistant']}" for t in old_turns
        )

        summary = await _llm_summarize(old_text)

        recent_text = "\n---\n".join(
            f"Q: {t['user']}\nA: {t['assistant']}" for t in recent_turns
        )

        result = f"{summary}\n\n## 最近对话 (Recent, {len(recent_turns)} turns)\n{recent_text}"

        if max_tokens:
            est = _estimate_tokens(result)
            if est > max_tokens:
                ratio = max_tokens / max(est, 1)
                trim_len = int(len(result) * ratio * 0.9)
                result = result[:trim_len] + "\n\n[result trimmed for token budget]"

        return result
