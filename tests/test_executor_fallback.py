"""执行器 fallback 链的参数翻译与"不级联造假错误"行为。

踩坑背景（2026-09-15，容器内真对话暴露）：
    模型调用 `weather(city=衡水)` → 快路取数失败 → fallback 到 web_search，
    但 web_search 的命令要 `{query}`，拿到的是 `{city}` → `.format()` KeyError →
    生成 "Missing parameter 'query' for tool 'web_search'" 这种**与事实无关的假错误**；
    而这条假失败又触发 web_search 自己的 fallback → web_fetch（要 `{url}`）→
    又是一条假错误 "Missing parameter 'url'"。模型只能原样转述，用户看到的就是
    "天气工具连续报错"这种莫名其妙的话。
"""
from __future__ import annotations

import asyncio
import sys

from src.execution import executor as ex


def _patch_cli(router, weather_output: str):
    """把 CLI 执行换成录音桩，返回记录下来的命令列表。"""
    calls: list[str] = []

    async def _fake_execute(cmd: str):
        calls.append(cmd)
        if " weather " in cmd:
            return ex.ToolResult(False, weather_output, "weather failed", ex.ExecutionMode.CLI)
        return ex.ToolResult(True, "SEARCH OK: 衡水 晴 28°C", "", ex.ExecutionMode.CLI)

    router.cli.execute = _fake_execute  # type: ignore[method-assign]
    return calls


def test_weather_fallback_translates_city_into_search_query():
    """weather 的 {city} 必须被翻译成 web_search 的 {query}，否则 fallback 必挂。"""
    router = ex.ExecutionRouter()
    calls = _patch_cli(router, weather_output="未能获取 衡水 的天气数据：中国天气网无编码")

    result = asyncio.run(router.execute_tool("weather", {"city": "衡水"}, prefer_mcp=False))

    assert result.success is True, "fallback 到搜索后应当拿到结果"
    search_calls = [c for c in calls if " search " in c]
    assert search_calls, f"没有发生 search fallback：{calls}"
    assert "衡水" in search_calls[0] and "天气" in search_calls[0], search_calls[0]
    assert not any(" fetch " in c for c in calls), f"不该级联到 web_fetch：{calls}"


def test_fallback_skipped_when_target_params_unmappable(monkeypatch):
    """fallback 需要的占位符凑不齐时，直接返回原失败 —— 不要造假错误、不要级联。"""
    monkeypatch.setitem(
        ex.TOOL_REGISTRY,
        "weather",
        {
            "mcp_server": None,
            "mcp_tool": None,
            "cli_command": f"{sys.executable} -m src.execution._web_tool weather {{city}}",
            # 故意配一个需要 {url} 的 fallback，而上游只有 {city} → 映射不了
            "fallback": "web_fetch",
        },
    )
    router = ex.ExecutionRouter()
    calls = _patch_cli(router, weather_output="未能获取 衡水 的天气数据：中国天气网无编码")

    result = asyncio.run(router.execute_tool("weather", {"city": "衡水"}, prefer_mcp=False))

    assert result.success is False
    assert len(calls) == 1, f"不该级联到别的工具：{calls}"
    assert "Missing parameter" not in f"{result.error or ''}{result.output or ''}", "又生成了假错误"
    assert "未能获取 衡水" in f"{result.error or ''}{result.output or ''}", "应当保留真实失败原因"
