# -*- coding: utf-8 -*-
"""Tests for token_budget.py — TokenBudget controller."""

import pytest
from src.utils.token_budget import TokenBudget, budget_context, _estimate_tokens


class TestEstimateTokens:
    """Token estimation accuracy tests."""
    
    def test_empty_string(self):
        assert _estimate_tokens("") == 0
    
    def test_english_text(self):
        # ~4 chars per token for English
        text = "Hello world, this is a test."
        est = _estimate_tokens(text)
        assert 5 <= est <= 15  # 29 chars / ~7 tokens
    
    def test_chinese_text(self):
        # ~1.5 chars per token for Chinese
        text = "你好世界这是一个测试句子用于验证令牌估算"
        est = _estimate_tokens(text)
        assert 10 <= est <= 25  # 20 chars / 1.5 ≈ 13 tokens
    
    def test_code_text(self):
        text = "def foo(x):\n    if x > 0:\n        return x * 2\n    return 0"
        est = _estimate_tokens(text)
        # Code: ~3 chars per token, 58 chars ≈ 19 tokens
        assert 10 <= est <= 30
    
    def test_mixed_text(self):
        text = "用户姓名: Alice, 年龄: 30岁, 城市: Beijing"
        est = _estimate_tokens(text)
        assert 5 <= est <= 25


class TestTokenBudget:
    """TokenBudget class tests."""
    
    def test_init_defaults(self):
        b = TokenBudget()
        assert b.max_total == 16000
        assert b.max_prompt == 12000
    
    def test_init_custom(self):
        b = TokenBudget(max_total=8000, max_prompt=6000)
        assert b.max_total == 8000
        assert b.max_prompt == 6000
    
    def test_estimate(self):
        b = TokenBudget()
        est = b.estimate("Hello world")
        assert est > 0
    
    def test_consume(self):
        b = TokenBudget(max_prompt=500)
        tokens = b.consume("test", "Hello world " * 20)
        assert tokens > 0
        assert b.usage_ratio() > 0
    
    def test_remaining(self):
        b = TokenBudget(max_prompt=500)
        initial = b.remaining()
        b.consume("x", "Hello " * 50)
        assert b.remaining() < initial
    
    def test_can_afford_small_text(self):
        b = TokenBudget(max_prompt=10000)
        assert b.can_afford("Hello world") is True
    
    def test_can_afford_large_text(self):
        b = TokenBudget(max_prompt=100)
        # This should exceed budget
        huge = "x" * 5000
        assert b.can_afford(huge) is False
    
    def test_usage_ratio_zero(self):
        b = TokenBudget()
        assert b.usage_ratio() == 0.0
    
    def test_usage_ratio_full(self):
        b = TokenBudget(max_prompt=100)
        # Consume enough to exceed budget: Chinese chars use ~1.5/token
        b.consume("x", "中" * 200)  # 200 CJK chars / 1.5 ≈ 133 tokens
        assert b.usage_ratio() >= 1.0
    
    def test_get_usage_report(self):
        b = TokenBudget(max_prompt=1000)
        b.consume("profile", "user data here")
        b.consume("history", "conversation turns")
        report = b.get_usage_report()
        assert "max_total" in report
        assert "max_prompt" in report
        assert "consumed_prompt" in report
        assert "remaining_prompt" in report
        assert "usage_pct" in report
        assert "status" in report
        assert "breakdown" in report
        assert len(report["breakdown"]) == 2
    
    def test_status_ok(self):
        b = TokenBudget(max_prompt=10000)
        b.consume("x", "short")
        assert b.get_usage_report()["status"] == "OK"
    
    def test_status_warning(self):
        b = TokenBudget(max_prompt=100)
        b.consume("x", "y" * 250)  # high usage
        report = b.get_usage_report()
        assert report["status"] in ("WARNING", "CRITICAL")


class TestBudgetContext:
    """Integration helper tests."""
    
    def test_small_context_fits(self):
        hermes = "用户信息：张三，男，25岁"
        history = "Q: 你好\nA: 你好！有什么可以帮你？"
        result, report = budget_context(hermes, history, max_prompt=5000)
        assert hermes in result
        assert history in result
        assert report["status"] == "OK"
    
    def test_large_profile_triggers_trim(self):
        # Use Chinese text to make the estimate faster (1.5 chars/token vs 4)
        hermes = "测试" * 10000  # 20000 CJK chars → ~13333 tokens → >100% of 12000
        history = "Q: hi\nA: hello"
        result, report = budget_context(hermes, history, max_prompt=12000)
        # Should trim: profile over 70% budget
        assert report["status"] in ("WARNING", "CRITICAL")
    
    def test_truncation_at_90_percent(self):
        # Use Chinese text to exceed 90% budget quickly
        hermes = "超大" * 15000  # 30000 CJK chars → ~20000 tokens → ~167% of 12000
        history = ""
        result, report = budget_context(hermes, history, max_prompt=12000)
        # Over 90%: should truncate to 500 chars
        assert len(result) <= 600  # 500 + overhead markers
        assert "[profile truncated" in result
    
    def test_no_history_when_empty(self):
        hermes = "用户信息"
        history = ""
        result, report = budget_context(hermes, history, max_prompt=12000)
        assert "用户信息" in result
        assert report["status"] == "OK"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
