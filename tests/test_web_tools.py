"""Focused tests for web tool fallbacks and failure normalization."""

import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_gold_query_has_curated_fallback_urls():
    from src.execution._web_tool import _curated_search_results

    result = _curated_search_results("gold price monthly data")
    assert "macrotrends.net/1333/historical-gold-prices-100-year-chart" in result
    assert "Gold as an investment - Wikipedia" in result


def test_stock_query_has_curated_tencent_url_when_code_present():
    from src.execution._web_tool import _curated_search_results

    result = _curated_search_results("帮我查下 600519 最近一个月股票价格")
    assert "web.ifzq.gtimg.cn/appstock/app/fqkline/get" in result
    assert "param=sh600519" in result


def test_web_search_stock_query_uses_eastmoney_suggest_when_no_code(monkeypatch):
    from src.execution import _web_tool

    payload = {
        "QuotationCodeTable": {
            "Data": [
                {"Name": "贵州茅台", "Code": "sh600519"},
            ]
        }
    }

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(
        _web_tool.urllib.request,
        "urlopen",
        lambda request, timeout=10: _FakeResponse(),
    )

    result = _web_tool.web_search("贵州茅台 最近一个月股票价格", 5)
    assert "web.ifzq.gtimg.cn/appstock/app/fqkline/get" in result
    assert "param=sh600519" in result


def test_tencent_kline_fetch_formats_daily_points(monkeypatch):
    from src.execution import _web_tool

    payload = {
        "code": 0,
        "msg": "",
        "data": {
            "sh600519": {
                "qfqday": [
                    ["2026-05-30", "100", "110", "111", "99", "12345"],
                    ["2026-06-02", "110", "120", "121", "109", "23456"],
                ]
            }
        },
    }

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(
        _web_tool.urllib.request,
        "urlopen",
        lambda request, timeout=15: _FakeResponse(),
    )

    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,,,30,qfq"
    text = _web_tool._fetch_tencent_kline(url)
    assert text is not None
    assert "sh600519" in text
    assert "2026-05-30: 110" in text
    assert "2026-06-02: 120" in text


def test_market_series_cn_stock_prefers_structured_output(monkeypatch):
    from src.execution import _web_tool

    payload = {
        "code": 0,
        "msg": "",
        "data": {
            "sh600519": {
                "qfqday": [
                    ["2026-06-01", "100", "110", "111", "99", "12345"],
                    ["2026-06-02", "110", "120", "121", "109", "23456"],
                ]
            }
        },
    }

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(
        _web_tool.urllib.request,
        "urlopen",
        lambda request, timeout=15: _FakeResponse(),
    )

    text = _web_tool.market_series("最近一个月 600519 股价", 30)
    assert "2026-06-01:" in text
    assert "2026-06-02:" in text


def test_market_series_gold_uses_macrotrends(monkeypatch):
    from src.execution import _web_tool

    payload = {
        "data": [
            [1746057600000, 3289.55],
            [1748736000000, 3266.41],
            [1751328000000, 3350.00],
        ],
        "metadata": {"name": "Gold Prices"},
    }

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(
        _web_tool.urllib.request,
        "urlopen",
        lambda request, timeout=15: _FakeResponse(),
    )

    text = _web_tool.market_series("最近一个月的金价", 30)
    assert "Gold Prices" in text


def test_web_search_string_failures_are_marked_unsuccessful():
    from src.execution.executor import ExecutionRouter, ToolResult, ExecutionMode

    result = ToolResult(
        success=True,
        output="SEARCH DOWN: DuckDuckGo unreachable.\nUse web_fetch instead.",
        mode=ExecutionMode.CLI,
    )

    normalized = ExecutionRouter._normalize_tool_failure("web_search", result)
    assert normalized.success is False
    assert "SEARCH DOWN" in (normalized.error or "")


def test_web_fetch_string_failures_are_marked_unsuccessful():
    from src.execution.executor import ExecutionRouter, ToolResult, ExecutionMode

    result = ToolResult(
        success=True,
        output="HTTP 404: Not Found for https://example.com/data",
        mode=ExecutionMode.CLI,
    )

    normalized = ExecutionRouter._normalize_tool_failure("web_fetch", result)
    assert normalized.success is False
    assert "HTTP 404" in (normalized.error or "")


def test_macrotrends_monthly_fetch_formats_recent_points(monkeypatch):
    from src.execution import _web_tool

    payload = {
        "data": [
            [1743465600000, 3273.50],
            [1746057600000, 3289.55],
            [1748736000000, 3266.41],
        ],
        "metadata": {"name": "Gold Prices"},
    }

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(
        _web_tool.urllib.request,
        "urlopen",
        lambda request, timeout=15: _FakeResponse(),
    )

    text = _web_tool._fetch_macrotrends_monthly(
        "https://www.macrotrends.net/1333/historical-gold-prices-100-year-chart"
    )
    assert text is not None
    assert "Gold Prices" in text
    assert "2025-06-01: 3266.41" in text
    assert "2025-05-01: 3289.55" in text


def test_eastmoney_kline_fetch_formats_daily_points(monkeypatch):
    from src.execution import _web_tool

    payload = {
        "data": {
            "name": "贵州茅台",
            "code": "600519",
            "klines": [
                "2026-05-30,100,110,111,99,12345,0,0,0,0,0",
                "2026-06-02,110,120,121,109,23456,0,0,0,0,0",
            ],
        }
    }

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(
        _web_tool.urllib.request,
        "urlopen",
        lambda request, timeout=15: _FakeResponse(),
    )

    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        "?secid=1.600519&klt=101&fqt=1&lmt=30"
    )
    text = _web_tool._fetch_eastmoney_kline(url)
    assert text is not None
    assert "贵州茅台 600519" in text
    assert "2026-05-30: 110" in text
    assert "2026-06-02: 120" in text


def test_eastmoney_html_stock_page_is_upgraded_to_kline_api(monkeypatch):
    from src.execution import _web_tool

    payload = {
        "data": {
            "name": "贵州茅台",
            "code": "600519",
            "klines": [
                "2026-06-02,110,120,121,109,23456,0,0,0,0,0",
            ],
        }
    }

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(
        _web_tool.urllib.request,
        "urlopen",
        lambda request, timeout=15: _FakeResponse(),
    )

    text = _web_tool._fetch_eastmoney_kline("https://www.eastmoney.com/stock/600519.html")
    assert text is not None
    assert "2026-06-02: 120" in text


def test_sina_stock_page_is_upgraded_to_kline_api(monkeypatch):
    from src.execution import _web_tool

    payload = {
        "data": {
            "name": "贵州茅台",
            "code": "600519",
            "klines": [
                "2026-06-02,110,120,121,109,23456,0,0,0,0,0",
            ],
        }
    }

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(
        _web_tool.urllib.request,
        "urlopen",
        lambda request, timeout=15: _FakeResponse(),
    )

    text = _web_tool._fetch_eastmoney_kline(
        "https://finance.sina.com.cn/realstock/company/sh600519/nc.shtml"
    )
    assert text is not None
    assert "2026-06-02: 120" in text


def test_structured_time_series_detection():
    from src.execution.executor import ExecutionRouter

    text = (
        "Gold Prices\n"
        "2025-06-01: 3266.41\n"
        "2025-05-01: 3289.55\n"
        "2025-04-01: 3273.50\n"
    )

    assert ExecutionRouter._has_structured_time_series(text) is True
