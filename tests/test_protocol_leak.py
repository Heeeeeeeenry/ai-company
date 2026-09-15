"""协议泄漏防护：模型说"协议"不能变成用户看的"答案"。

背景（2026-09-15 线上两次，同一病灶两种皮）：
  1) 第一次：`{"action":"final","output":"…"}` 被原样展示 → 用户看到 JSON 壳。
  2) 第二次：模型漂到 XML 形态的工具调用，吐出一行
     `<list_dir><path>/data/.ai-company/workspace</path></list_dir>`
     → 清洗函数只认 JSON，没有对应分支，原文被当成答案。
  实测根因：`_parse_agent_response` 在第 3 轮后把**任何**非 JSON 文本
      `return {"action": "final", "output": raw}`；`_extract_maxiter_output`
      也只是 `re.split(r'{"action": "tool"')` 后取前段 → XML 原样返回。

这里锁住三件事：
  a) XML/JSON 形态的协议都能被识别并剥掉；
  b) 剥完没内容时给可操作文案，绝不回吐协议；
  c) **不误伤正常回答**（散文里提到工具名、含正常标点都不该被削）。
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.execution.executor import (  # noqa: E402
    ExecutionRouter,
    _extract_maxiter_output,
    looks_like_protocol_leak,
    sanitize_for_user,
    strip_protocol_leak,
)

# 用户线上实际看到的那一行（原样）
XML_LEAK = "<list_dir><path>/data/.ai-company/workspace</path></list_dir>"
JSON_LEAK = '{"action":"final","output":"衡水的天气是晴，27.9°C。"}'
PROSE = "衡水今天晴，实时气温 27.9°C，湿度 50%，AQI 85（良）。"


def test_xml_tool_call_is_recognised_as_protocol():
    assert looks_like_protocol_leak(XML_LEAK) is True


def test_json_envelope_is_recognised_as_protocol():
    assert looks_like_protocol_leak(JSON_LEAK) is True


def test_plain_answer_is_not_flagged():
    assert looks_like_protocol_leak(PROSE) is False
    # 散文里正常提到工具名 ≠ 协议
    assert looks_like_protocol_leak("你可以用 list_dir 工具看目录结构。") is False


def test_strip_removes_xml_entirely():
    assert strip_protocol_leak(XML_LEAK).strip() == ""


def test_strip_keeps_prose_and_drops_xml():
    mixed = f"衡水今天晴 27.9°C。{XML_LEAK}"
    cleaned = strip_protocol_leak(mixed)
    assert "衡水" in cleaned and "27.9" in cleaned
    assert "list_dir" not in cleaned and "<path>" not in cleaned


def test_maxiter_output_never_returns_raw_xml():
    """这是线上真正漏出去的那条路径。"""
    out = _extract_maxiter_output(XML_LEAK, [{"tool": "list_dir"}])
    assert "<list_dir" not in out, f"协议仍被回吐: {out!r}"
    assert "path" not in out.replace("路径", ""), out
    assert len(out) > 0, "应给可操作文案而不是空"


def test_maxiter_output_still_unwraps_json_envelope():
    """回归：JSON 壳的既有行为不能被改坏。"""
    out = _extract_maxiter_output(JSON_LEAK, [])
    assert out == "衡水的天气是晴，27.9°C。"


def test_parse_agent_response_refuses_protocol_as_final():
    """第 3 轮后的兜底曾把非 JSON 一律当最终答案 —— 现在必须拒绝。"""
    router = ExecutionRouter()
    parsed = router._parse_agent_response(XML_LEAK, 4)
    assert parsed["action"] != "final", parsed
    assert parsed["action"] == "unknown", parsed


def test_parse_agent_response_still_accepts_plain_answer_on_retry():
    """别把纠偏做成"永远不收敛"：正常长散文在第 3 轮后仍应作为答案放行。

    注：兜底里还有一条**既有**启发式 —— "第 3 轮后 <100 字符且不含关键词"
    会被判成合规废话。故这里刻意用过百字的真实答案，避免用例随长度压线假红，
    也确保测的是"协议防护没有误伤"，而不是那条长度门槛。
    """
    router = ExecutionRouter()
    answer = ("衡水今天白天晴，实时气温 27.9 摄氏度，湿度 50%，空气质量指数 85 属良好，"
              "东南风一级，能见度 30 公里；夜间转多云，最低气温 21 摄氏度，适合户外活动。")
    parsed = router._parse_agent_response(answer, 4)
    assert parsed["action"] == "final", parsed
    assert "衡水" in parsed["output"]


def test_sanitize_for_user_passes_normal_text_through_unchanged():
    assert sanitize_for_user(PROSE) == PROSE


def test_sanitize_for_user_never_leaves_protocol():
    assert "<list_dir" not in sanitize_for_user(XML_LEAK)
    assert '"action"' not in sanitize_for_user(JSON_LEAK)
