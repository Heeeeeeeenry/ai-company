# -*- coding: utf-8 -*-
"""Verifier 测试 — 覆盖所有 11 种意图的验证策略"""

import asyncio
import os
import sys
import tempfile

import pytest

# 将项目根加入 sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.verification.verifier import Verifier, VerifyResult


# ═══ Fixtures ═══

@pytest.fixture
def verifier():
    """返回 Verifier 实例"""
    return Verifier()


def _run(coro):
    """同步运行 async 函数"""
    return asyncio.run(coro)


# ═══ VerifyResult 数据类测试 ═══

class TestVerifyResult:
    """VerifyResult 数据类单元测试"""

    def test_defaults(self):
        vr = VerifyResult(success=True, score=95, detail="OK")
        assert vr.success is True
        assert vr.score == 95
        assert vr.detail == "OK"
        assert vr.needs_retry is False
        assert vr.retry_strategy == ""
        assert vr.evidence == {}

    def test_failure_with_retry(self):
        vr = VerifyResult(
            success=False,
            score=0,
            detail="失败",
            needs_retry=True,
            retry_strategy="重新执行",
        )
        assert vr.success is False
        assert vr.score == 0
        assert vr.needs_retry is True
        assert vr.retry_strategy == "重新执行"

    def test_to_dict(self):
        vr = VerifyResult(
            success=True,
            score=80,
            detail="通过",
            evidence={"output_preview": "hello"},
        )
        d = vr.to_dict()
        assert d["success"] is True
        assert d["score"] == 80
        assert d["evidence"]["output_preview"] == "hello"


# ═══ 辅助方法测试 ═══

class TestHelperMethods:
    """_check_stale_answer / _check_crash / _check_boilerplate_ack / _extract_code_blocks 测试"""

    def test_check_stale_answer_true(self):
        assert Verifier._check_stale_answer("无法获取实时数据，根据已知数据...") is True
        assert Verifier._check_stale_answer("我无法提供实时建议") is True
        assert Verifier._check_stale_answer("cannot access the data currently") is True

    def test_check_stale_answer_false(self):
        assert Verifier._check_stale_answer("当前金价为 580 元/克") is False
        assert Verifier._check_stale_answer("今天天气晴朗，温度 25°C") is False
        assert Verifier._check_stale_answer("") is False

    def test_check_boilerplate_ack_true(self):
        """合规确认检测 — 应命中"""
        assert Verifier._check_boilerplate_ack("收到，我会严格遵守格式") is True
        assert Verifier._check_boilerplate_ack("好的，我会按照JSON格式回复") is True
        assert Verifier._check_boilerplate_ack("明白了，我会遵守规则") is True
        assert Verifier._check_boilerplate_ack("有什么需要我做的") is True
        assert Verifier._check_boilerplate_ack("了解，马上按照格式回复") is True
        assert Verifier._check_boilerplate_ack("收到") is True

    def test_check_boilerplate_ack_false(self):
        """合规确认检测 — 不应命中正常输出"""
        assert Verifier._check_boilerplate_ack("当前金价 580 元/克") is False
        assert Verifier._check_boilerplate_ack("") is False
        assert Verifier._check_boilerplate_ack("消息发送成功") is False

    def test_check_crash_true(self):
        # Error: pattern (冒号后跟错误信息)
        assert Verifier._check_crash("Error: division by zero") is True
        # Error[ pattern
        assert Verifier._check_crash("Error[123]: invalid index") is True
        # Traceback
        assert Verifier._check_crash("Traceback (most recent call last)") is True
        # CRASHED
        assert Verifier._check_crash("CRASHED at line 42") is True
        # FAILED
        assert Verifier._check_crash("Execution FAILED") is True
        # 新增 crash 关键字
        assert Verifier._check_crash("SIGSEGV: segmentation fault") is True
        assert Verifier._check_crash("Fatal: system error") is True
        assert Verifier._check_crash("Process Killed by signal 9") is True
        assert Verifier._check_crash("OOM: out of memory") is True
        assert Verifier._check_crash("SyntaxError: invalid syntax") is True
        assert Verifier._check_crash("ConnectionError: timeout") is True
        assert Verifier._check_crash("Permission denied") is True

    def test_check_crash_false(self):
        assert Verifier._check_crash("一切正常") is False
        assert Verifier._check_crash("output: hello world") is False
        assert Verifier._check_crash("") is False
        # 关键：不应将 "No error found" 误判为 crash
        assert Verifier._check_crash("No error found in the logs") is False
        assert Verifier._check_crash("The error rate is below 0.1%") is False
        assert Verifier._check_crash("所有测试通过，0 error") is False

    def test_extract_code_blocks_fenced(self):
        output = """
        这是输出：
        ```python
        def hello():
            print("hi")
        ```
        结束
        """
        blocks = Verifier._extract_code_blocks(output)
        assert len(blocks) == 1
        assert "def hello()" in blocks[0]

    def test_extract_code_blocks_multiple(self):
        output = """
        ```python
        def foo():
            pass
        ```
        中间文字
        ```py
        def bar():
            return 1
        ```
        """
        blocks = Verifier._extract_code_blocks(output)
        assert len(blocks) == 2

    def test_extract_code_blocks_none(self):
        output = "这是纯文本，没有代码块"
        blocks = Verifier._extract_code_blocks(output)
        assert blocks == []

    def test_extract_code_blocks_indented_if_for_while_try(self):
        """缩进启发式应识别 if/for/while/try 开头的行"""
        output = """\
这里是一些说明文字。
if x > 0:
    print("positive")
然后是更多文字。
for i in range(10):
    print(i)
结束。
while True:
    break
末尾。
try:
    risky()
except:
    pass
"""
        blocks = Verifier._extract_code_blocks(output)
        # 应该有 4 个缩进块（if, for, while, try）
        # 注意：它们可能会被合并或分割取决于空行
        assert len(blocks) >= 1
        combined = "\n".join(blocks)
        assert "if x > 0:" in combined
        assert "for i in range(10):" in combined
        assert "while True:" in combined
        assert "try:" in combined


# ═══ COMMAND 策略测试 ═══

class TestVerifyCommand:
    """COMMAND 意图验证"""

    def test_command_success(self, verifier):
        result = _run(verifier.verify("COMMAND", {
            "output": "/home/user\nDocuments\nDownloads\n",
        }))
        assert result.success is True
        assert result.score == 95

    def test_command_with_error_keyword(self, verifier):
        result = _run(verifier.verify("COMMAND", {
            "output": "Error: command not found",
        }))
        assert result.success is False
        assert result.score == 0
        assert result.needs_retry is True

    def test_command_with_traceback(self, verifier):
        result = _run(verifier.verify("COMMAND", {
            "output": "Traceback (most recent call last):\n  File...",
        }))
        assert result.success is False
        assert result.score == 0

    def test_command_empty_output(self, verifier):
        result = _run(verifier.verify("COMMAND", {
            "output": "",
        }))
        assert result.success is False
        assert result.score == 0
        assert result.needs_retry is True

    def test_command_whitespace_only(self, verifier):
        result = _run(verifier.verify("COMMAND", {
            "output": "   \n  \t  ",
        }))
        assert result.success is False

    def test_command_no_error_found_not_crash(self, verifier):
        """'No error found' 不应触发 crash 检测"""
        result = _run(verifier.verify("COMMAND", {
            "output": "No error found. All tests passed.",
        }))
        assert result.success is True
        assert result.score == 95


# ═══ SEARCH / RESEARCH 策略测试 ═══

class TestVerifySearch:
    """SEARCH / RESEARCH 意图验证"""

    def test_search_success(self, verifier):
        result = _run(verifier.verify("SEARCH", {
            "output": "当前国际金价为 580 元/克，数据来源：上海黄金交易所",
        }))
        assert result.success is True
        assert result.score == 95

    def test_research_success(self, verifier):
        result = _run(verifier.verify("RESEARCH", {
            "output": "经调研，OpenClaw 是一个智能 Agent 框架，支持多平台...",
        }))
        assert result.success is True
        assert result.score == 95

    def test_search_stale(self, verifier):
        result = _run(verifier.verify("SEARCH", {
            "output": "无法获取实时金价数据，根据已知数据源，黄金价格通常在...",
        }))
        assert result.success is False
        assert result.score == 20
        assert result.needs_retry is True

    def test_research_stale(self, verifier):
        result = _run(verifier.verify("RESEARCH", {
            "output": "我无法提供最新建议，因为我无法访问当前数据",
        }))
        assert result.success is False
        assert result.score == 20

    def test_search_empty(self, verifier):
        result = _run(verifier.verify("SEARCH", {
            "output": "",
        }))
        assert result.success is False
        assert result.score == 0


# ═══ CODING 策略测试 ═══

class TestVerifyCoding:
    """CODING 意图验证"""

    def test_coding_valid_syntax(self, verifier):
        result = _run(verifier.verify("CODING", {
            "output": "这是一个函数：\n```python\ndef add(a, b):\n    return a + b\n```\n",
        }))
        assert result.success is True
        assert result.score == 85

    def test_coding_invalid_syntax(self, verifier):
        result = _run(verifier.verify("CODING", {
            "output": """有语法错误的代码：
            ```python
            def add(a, b)
                return a + b
            ```
            """,
        }))
        assert result.success is False
        assert result.score == 0
        assert result.needs_retry is True

    def test_coding_no_code_blocks(self, verifier):
        result = _run(verifier.verify("CODING", {
            "output": "我已经完成了代码编写，请查看文件。",
        }))
        assert result.success is True
        assert result.score == 85

    def test_coding_empty_output(self, verifier):
        result = _run(verifier.verify("CODING", {
            "output": "",
        }))
        assert result.success is False
        assert result.score == 0

    def test_coding_multiple_blocks_all_valid(self, verifier):
        result = _run(verifier.verify("CODING", {
            "output": "```python\ndef foo():\n    return 1\n```\n```python\ndef bar():\n    return 2\n```\n",
        }))
        assert result.success is True


# ═══ SOCIAL / WeChat 策略测试 ═══

class TestVerifySocial:
    """SOCIAL / WeChat 意图验证"""

    def test_social_success_flag(self, verifier):
        result = _run(verifier.verify("SOCIAL", {
            "output": "消息已发送",
            "success": True,
            "contact": "小明",
            "message": "你好",
        }))
        assert result.success is True
        assert result.score == 95

    def test_wechat_sent_flag(self, verifier):
        result = _run(verifier.verify("SOCIAL", {
            "output": "发送成功",
            "sent": True,
        }))
        assert result.success is True

    def test_social_success_text(self, verifier):
        result = _run(verifier.verify("SOCIAL", {
            "output": "微信消息已发送成功，内容已送达",
        }))
        assert result.success is True
        assert result.score == 90

    def test_social_failure(self, verifier):
        result = _run(verifier.verify("SOCIAL", {
            "output": "发送失败：微信未登录",
        }))
        assert result.success is False
        assert result.score == 0
        assert result.needs_retry is True

    def test_social_empty(self, verifier):
        result = _run(verifier.verify("SOCIAL", {
            "output": "",
        }))
        assert result.success is False


# ═══ FILE 策略测试 ═══

class TestVerifyFile:
    """FILE 意图验证"""

    def test_file_exists(self, verifier):
        # 创建一个临时文件
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test")
            tmp = f.name
        try:
            result = _run(verifier.verify("FILE", {
                "output": f"文件已创建：{tmp}",
                "files": [tmp],
            }))
            assert result.success is True
            assert result.score == 90
        finally:
            os.unlink(tmp)

    def test_file_not_exists(self, verifier):
        result = _run(verifier.verify("FILE", {
            "output": "文件已创建",
            "files": ["/nonexistent/path/file.txt"],
        }))
        assert result.success is False
        assert result.score == 0
        assert result.needs_retry is True

    def test_file_no_paths_with_output(self, verifier):
        result = _run(verifier.verify("FILE", {
            "output": "文件内容已成功读取：Hello World",
        }))
        assert result.success is True
        assert result.score == 90

    def test_file_no_paths_no_output(self, verifier):
        result = _run(verifier.verify("FILE", {
            "output": "",
            "files": [],
        }))
        assert result.success is False
        assert result.score == 0


# ═══ SYSTEM 策略测试 ═══

class TestVerifySystem:
    """SYSTEM 意图验证"""

    def test_system_success(self, verifier):
        result = _run(verifier.verify("SYSTEM", {
            "output": "进程已启动，PID: 12345",
        }))
        assert result.success is True
        assert result.score == 90

    def test_system_error(self, verifier):
        result = _run(verifier.verify("SYSTEM", {
            "output": "Error: 无法启动进程",
        }))
        assert result.success is False
        assert result.score == 0
        assert result.needs_retry is True

    def test_system_empty(self, verifier):
        result = _run(verifier.verify("SYSTEM", {
            "output": "",
        }))
        assert result.success is False
        assert result.score == 0


# ═══ DEFAULT 策略测试 ═══

class TestVerifyDefault:
    """DEFAULT / 兜底策略验证"""

    def test_general_chat(self, verifier):
        result = _run(verifier.verify("GENERAL_CHAT", {
            "output": "你好！今天有什么可以帮你的？",
        }))
        assert result.success is True
        assert result.score == 80

    def test_vision_default(self, verifier):
        result = _run(verifier.verify("VISION", {
            "output": "截图分析：检测到微信界面",
        }))
        assert result.success is True
        assert result.score == 80

    def test_memory_default(self, verifier):
        result = _run(verifier.verify("MEMORY", {
            "output": "已记住你的偏好：语言=中文",
        }))
        assert result.success is True
        assert result.score == 80

    def test_automation_default(self, verifier):
        result = _run(verifier.verify("AUTOMATION", {
            "output": "批量处理完成：共处理 42 个文件",
        }))
        assert result.success is True
        assert result.score == 80

    def test_default_empty(self, verifier):
        result = _run(verifier.verify("GENERAL_CHAT", {
            "output": "",
        }))
        assert result.success is False
        assert result.score == 0
        assert result.needs_retry is True

    def test_unknown_intent_falls_to_default(self, verifier):
        """未知意图应走 default 策略，有输出则通过"""
        result = _run(verifier.verify("UNKNOWN_INTENT", {
            "output": "some result",
        }))
        assert result.success is True
        assert result.score == 80


# ═══ verify_with_vision 预留接口测试 ═══

class TestVerifyWithVision:
    """Vision 预留接口测试"""

    def test_screenshot_not_exists(self, verifier):
        result = _run(verifier.verify_with_vision(
            "/nonexistent/screenshot.png",
        ))
        assert result.success is False
        assert result.score == 0

    def test_screenshot_exists_stub(self, verifier):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"fake png")
            tmp = f.name
        try:
            result = _run(verifier.verify_with_vision(tmp))
            assert result.success is True
            assert result.score == 85
            assert result.evidence.get("vision_status") == "stub"
        finally:
            os.unlink(tmp)


# ═══ verify_aggregate 兼容接口测试 ═══

class TestVerifyAggregate:
    """verify_aggregate 兼容旧代码接口测试"""

    def test_command_aggregate(self, verifier):
        result = verifier.verify_aggregate(
            "COMMAND", "/home/user\nDocuments\n",
        )
        assert result["score"] == 95
        assert result["decision"] == "APPROVE"
        assert result["next_action"] == "deliver"
        assert result["needs_audit"] is False

    def test_command_aggregate_crash(self, verifier):
        result = verifier.verify_aggregate(
            "COMMAND", "Error: not found",
            execution_log=["[Agent] CRASHED"],
        )
        assert result["score"] == 0
        assert result["decision"] == "FAIL"

    def test_search_aggregate_stale(self, verifier):
        result = verifier.verify_aggregate(
            "SEARCH", "无法获取实时数据",
        )
        assert result["score"] == 20
        assert result["decision"] == "FAIL"

    def test_search_aggregate_ok(self, verifier):
        result = verifier.verify_aggregate(
            "SEARCH", "当前金价 580 元/克",
        )
        assert result["score"] == 95
        assert result["decision"] == "APPROVE"

    def test_coding_aggregate(self, verifier):
        result = verifier.verify_aggregate(
            "CODING", "def hello(): return 'hi'",
        )
        assert result["score"] == 85
        assert result["decision"] == "APPROVE"
        assert result["needs_audit"] is True  # CODING 需要 Auditor 审查

    def test_social_aggregate_success(self, verifier):
        result = verifier.verify_aggregate(
            "SOCIAL", "微信消息发送成功！",
        )
        assert result["score"] == 95
        assert result["decision"] == "APPROVE"

    def test_social_aggregate_fail(self, verifier):
        result = verifier.verify_aggregate(
            "SOCIAL", "失败",
        )
        assert result["score"] == 0
        assert result["decision"] == "FAIL"

    def test_file_aggregate(self, verifier):
        result = verifier.verify_aggregate(
            "FILE", "文件已读取：hello world",
        )
        assert result["score"] == 90
        assert result["decision"] == "APPROVE"

    def test_default_aggregate(self, verifier):
        result = verifier.verify_aggregate(
            "GENERAL_CHAT", "你好！",
        )
        assert result["score"] == 80
        assert result["decision"] == "APPROVE"

    def test_default_aggregate_empty(self, verifier):
        result = verifier.verify_aggregate(
            "UNKNOWN", "",
        )
        assert result["score"] == 0
        assert result["decision"] == "FAIL"


# ═══ 所有 11 种意图覆盖测试 ═══

class TestAllIntentsCoverage:
    """确保所有 11 种意图都有覆盖"""

    ALL_INTENTS = [
        "COMMAND",
        "SEARCH",
        "RESEARCH",
        "VISION",
        "SOCIAL",
        "MEMORY",
        "CODING",
        "SYSTEM",
        "FILE",
        "AUTOMATION",
        "GENERAL_CHAT",
    ]

    def test_all_intents_return_verify_result(self, verifier):
        """每种意图都应该返回 VerifyResult 且不抛异常"""
        for intent in self.ALL_INTENTS:
            result = _run(verifier.verify(intent, {
                "output": f"test output for {intent}",
            }))
            assert isinstance(result, VerifyResult), (
                f"intent '{intent}' did not return VerifyResult, "
                f"got {type(result)}"
            )
            assert 0 <= result.score <= 100, (
                f"intent '{intent}' score {result.score} out of range [0, 100]"
            )

    def test_all_intents_with_empty_output(self, verifier):
        """每种意图空输出应返回 FAIL"""
        for intent in self.ALL_INTENTS:
            result = _run(verifier.verify(intent, {
                "output": "",
            }))
            assert result.success is False, (
                f"intent '{intent}' with empty output should fail"
            )
