# -*- coding: utf-8 -*-
"""Token Budget Controller — tracks and enforces token limits per request.

DeepSeek-chat has a 16K context window. This module ensures we stay
within budget by estimating text sizes and trimming context when needed.
"""

from __future__ import annotations

import re
from typing import Optional


# ─── Token Estimation ──────────────────────────

def _estimate_tokens(text: str) -> int:
    """Rough token estimation for mixed Chinese/English text.

    Heuristic:
      - Chinese characters: ~1.5 chars per token
      - English words: ~1.3 tokens per word (including spaces/punctuation)
      - Code/JSON: ~2.5 chars per token
    Returns a ceiling estimate (overestimate rather than under).
    """
    if not text:
        return 0

    # Count CJK characters (including punctuation)
    cjk_count = len(re.findall(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3000-\u303f\uff00-\uffef]', text))
    non_cjk = len(text) - cjk_count

    # Code-heavy detection: many braces, brackets, semicolons
    code_chars = len(re.findall(r'[{}\[\]();=<>]', text))
    code_ratio = code_chars / max(len(text), 1)

    if code_ratio > 0.05:
        # Code-heavy: ~2.5 chars per token
        return max(1, len(text) // 3)  # Ceiling with 3+ chars/token rule
    elif cjk_count > len(text) * 0.3:
        # Mostly Chinese: ~1.5 chars per token
        cjk_tokens = cjk_count / 1.5
        other_tokens = non_cjk / 4.0  # English ~4 chars/token
        return max(1, int(cjk_tokens + other_tokens + 1))
    else:
        # Mostly English: ~4 chars per token
        return max(1, int(len(text) / 3.5 + 1))


class TokenBudget:
    """Pre-request token budget controller.

    Usage:
        budget = TokenBudget(max_total=16000, max_prompt=12000)
        if budget.can_afford(long_text):
            # safe to include
        else:
            # need to trim or compress

        report = budget.get_usage_report()
    """

    def __init__(self, max_total: int = 16000, max_prompt: int = 12000):
        """Initialize budget.

        Args:
            max_total: Total token budget (deepseek-chat default 16K).
            max_prompt: Maximum tokens allowed for prompt/system context.
        """
        self.max_total = max_total
        self.max_prompt = max_prompt
        self._consumed: list[tuple[str, int]] = []  # (label, token_estimate)
        self._total_consumed = 0

    def estimate(self, text: str) -> int:
        """Estimate token count for a text string."""
        return _estimate_tokens(text)

    def consume(self, label: str, text: str) -> int:
        """Register consumption for a labeled chunk. Returns tokens consumed."""
        tokens = self.estimate(text)
        self._consumed.append((label, tokens))
        self._total_consumed += tokens
        return tokens

    def remaining(self) -> int:
        """Remaining budget for prompt."""
        return max(0, self.max_prompt - self._total_consumed)

    def usage_ratio(self) -> float:
        """Current prompt usage as fraction of max_prompt."""
        if self.max_prompt == 0:
            return 1.0
        return self._total_consumed / self.max_prompt

    def can_afford(self, text: str) -> bool:
        """Check if adding text stays within prompt budget."""
        return (self._total_consumed + self.estimate(text)) <= self.max_prompt

    def get_usage_report(self) -> dict:
        """Return usage statistics."""
        pct = self.usage_ratio() * 100
        status = "OK"
        if pct > 90:
            status = "CRITICAL"
        elif pct > 70:
            status = "WARNING"

        return {
            "max_total": self.max_total,
            "max_prompt": self.max_prompt,
            "consumed_prompt": self._total_consumed,
            "remaining_prompt": self.remaining(),
            "usage_pct": round(pct, 1),
            "status": status,
            "breakdown": [{"label": label, "tokens": t} for label, t in self._consumed],
        }


# ─── Convenience helpers for _inject_session_context ───

def budget_context(
    hermes_ctx: str,
    history_text: str,
    max_prompt: int = 12000,
) -> tuple[str, dict]:
    """Apply token budget to context parts, returning trimmed context + report.

    Returns:
        (final_context_text, budget_report_dict)
    """
    budget = TokenBudget(max_prompt=max_prompt)

    # Always include user profile (hermes_ctx) first
    profile_tokens = budget.consume("hermes_profile", hermes_ctx)

    # If over 70% budget already from profile, skip history
    if budget.usage_ratio() > 0.70:
        if budget.usage_ratio() > 0.90:
            # Over 90%: only include first 500 chars of profile
            truncated = hermes_ctx[:500] + "\n\n[profile truncated — token budget critical]"
            report = budget.get_usage_report()
            report["status"] = "CRITICAL"
            report["usage_pct"] = round(budget.usage_ratio() * 100, 1)
            return truncated, report
        else:
            # Over 70%: profile only, no history
            report = budget.get_usage_report()
            if report["status"] == "OK":
                report["status"] = "WARNING"
            return hermes_ctx + "\n\n[history skipped — token budget warning]", report

    # Include history if budget allows
    if history_text:
        hist_tokens = budget.estimate(history_text)
        if budget.can_afford(history_text):
            budget.consume("conversation_history", history_text)
            parts = [hermes_ctx, history_text]
        else:
            # Trim history to fit
            ratio = budget.remaining() / max(hist_tokens, 1)
            if ratio > 0.3:
                trim_len = int(len(history_text) * ratio * 0.9)
                trimmed = history_text[:trim_len] + "\n\n[history trimmed — token budget]"
                budget.consume("conversation_history_trimmed", trimmed)
                parts = [hermes_ctx, trimmed]
            else:
                parts = [hermes_ctx]
                budget.consume("conversation_history_skipped", "[history skipped — insufficient budget]")
    else:
        parts = [hermes_ctx]

    return "\n\n".join(parts), budget.get_usage_report()
