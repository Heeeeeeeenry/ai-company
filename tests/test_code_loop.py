# -*- coding: utf-8 -*-
"""测试代码闭环 — py_compile + pytest 自动修复循环"""

import pytest
import os
import sys
import tempfile
from unittest.mock import AsyncMock, patch, MagicMock

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ─── Fixtures ────────────────────────────────────────


@pytest.fixture
def code_loop():
    """创建 CodeLoop 实例（不调用 LLM）。"""
    from src.execution.code_loop import CodeLoop
    return CodeLoop(max_iterations=3)


@pytest.fixture
def valid_code():
    """无语法错误的有效代码。"""
    return """def add(a, b):
    return a + b

def subtract(a, b):
    return a - b
"""


@pytest.fixture
def syntax_error_code():
    """包含语法错误的代码。"""
    return """def add(a, b):
    return a + b

def broken_function(
    print("this is broken")
"""


@pytest.fixture
def type_error_code():
    """包含类型/逻辑错误的代码（语法正确但运行会出错）。"""
    return """def divide(a, b):
    return a / b

def process(data):
    result = []
    for item in data:
        result.append(item.upper())
    return result

# 调用时如果传 None 会报 AttributeError
if __name__ == "__main__":
    print(process(None))
"""


@pytest.fixture
def logic_error_code():
    """逻辑错误代码：语法正确但行为不正确。"""
    return """def multiply(a, b):
    # BUG: should return a * b, but returns a + b
    return a + b

def is_even(n):
    # BUG: off-by-one
    return n % 2 == 1
"""


# ─── is_python_code tests ──────────────────────────


class TestIsPythonCode:
    """测试 _is_python_code 方法。"""

    def test_detect_python_function(self, code_loop):
        """检测包含 def 的 Python 代码。"""
        assert code_loop._is_python_code("def foo():\n    pass")

    def test_detect_python_class(self, code_loop):
        """检测包含 class 的 Python 代码。"""
        assert code_loop._is_python_code("class MyClass:\n    pass")

    def test_detect_python_import(self, code_loop):
        """检测包含 import 的 Python 代码。"""
        assert code_loop._is_python_code("import os\nprint(os.getcwd())")

    def test_reject_shell_command(self, code_loop):
        """拒绝纯 shell 命令。"""
        assert not code_loop._is_python_code("ls -la /tmp")
        assert not code_loop._is_python_code("docker run nginx")

    def test_reject_empty_code(self, code_loop):
        """拒绝空代码。"""
        assert not code_loop._is_python_code("")
        assert not code_loop._is_python_code("   ")
        assert not code_loop._is_python_code(None)

    def test_reject_natural_language(self, code_loop):
        """拒绝纯自然语言。"""
        assert not code_loop._is_python_code("请帮我写一个 hello world 程序")


# ─── _check_syntax tests ───────────────────────────


class TestCheckSyntax:
    """测试语法检查功能。"""

    @pytest.mark.asyncio
    async def test_valid_syntax_passes(self, code_loop, valid_code):
        """有效代码应该通过语法检查。"""
        ok, err = await code_loop._check_syntax(valid_code)
        assert ok is True
        assert err == ""

    @pytest.mark.asyncio
    async def test_syntax_error_detected(self, code_loop, syntax_error_code):
        """语法错误应该被检测到。"""
        ok, err = await code_loop._check_syntax(syntax_error_code)
        assert ok is False
        assert "SyntaxError" in err or "Error" in err.lower()

    @pytest.mark.asyncio
    async def test_empty_code_syntax(self, code_loop):
        """空代码在 compile() 中应通过（无语句=无错误）。"""
        ok, err = await code_loop._check_syntax("")
        # compile("", ...) 对空字符串会通过
        assert ok is True


# ─── _run_tests tests ──────────────────────────────


class TestRunTests:
    """测试 pytest 运行功能。"""

    @pytest.mark.asyncio
    async def test_passing_tests(self, code_loop):
        """正确的代码 + 正确的测试 = 通过。"""
        code = "def add(a, b):\n    return a + b\n"
        test_code = """
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from module_under_test import add

def test_add():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0
"""
        ok, output = await code_loop._run_tests(code, test_code)
        assert ok is True

    @pytest.mark.asyncio
    async def test_failing_tests(self, code_loop):
        """错误代码 + 正确的测试 = 失败。"""
        code = "def add(a, b):\n    return a - b\n"  # BUG: should be +
        test_code = """
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from module_under_test import add

def test_add():
    assert add(2, 3) == 5
"""
        ok, output = await code_loop._run_tests(code, test_code)
        assert ok is False
        assert "FAIL" in output or "fail" in output.lower() or "AssertionError" in output

    @pytest.mark.asyncio
    async def test_no_test_code_skipped(self, code_loop, valid_code):
        """如果没有测试代码，run 方法应只做语法检查。"""
        result = await code_loop.run(valid_code, test_code="")
        assert result.compilation_pass is True
        # tests_pass 在没有测试代码时为 None
        if result.remaining_errors:
            assert result.success is False
        else:
            # 语法通过 + 无测试 = 成功
            assert result.success is True


# ─── CodeLoop.run integration tests ────────────────


class TestCodeLoopRun:
    """测试完整的 run 方法集成。"""

    @pytest.mark.asyncio
    async def test_valid_code_passes(self, code_loop, valid_code):
        """有效代码应该快速通过（1轮）。"""
        result = await code_loop.run(valid_code)
        assert result.success is True
        assert result.iterations == 1
        assert result.compilation_pass is True

    @pytest.mark.asyncio
    async def test_non_python_skip(self, code_loop):
        """非 Python 代码应跳过整个闭环。"""
        result = await code_loop.run("ls -la /tmp")
        assert result.success is True  # 不做检查，不算失败
        assert result.iterations == 0
        assert "非Python" in result.degradation_reason

    @pytest.mark.asyncio
    async def test_syntax_error_with_mock_llm(self, code_loop, syntax_error_code, valid_code):
        """语法错误 + Mock LLM 返回修复代码 = 在第2轮通过。"""
        # Mock LLM 返回有效代码来修复语法错误
        with patch.object(code_loop, "_llm_fix", new_callable=AsyncMock) as mock_fix:
            mock_fix.return_value = valid_code
            result = await code_loop.run(syntax_error_code)
            assert result.success is True
            assert result.compilation_pass is True
            assert result.iterations >= 1
            assert len(result.errors_fixed) >= 0
            mock_fix.assert_called()

    @pytest.mark.asyncio
    async def test_llm_fix_fails_returns_original(self, code_loop, syntax_error_code):
        """LLM 修复3次均失败 → 返回原始代码+错误。"""
        with patch.object(code_loop, "_llm_fix", new_callable=AsyncMock) as mock_fix:
            # LLM 每次都返回同样的错误代码
            mock_fix.return_value = syntax_error_code
            result = await code_loop.run(syntax_error_code)
            assert result.success is False
            assert result.compilation_pass is False
            assert result.iterations == 3  # 循环在第3轮结束
            assert len(result.remaining_errors) > 0
            # 第1、2轮调用 _llm_fix，第3轮是最后一次不调用
            assert mock_fix.call_count == 2

    @pytest.mark.asyncio
    async def test_max_iterations_enforced(self, code_loop, syntax_error_code):
        """循环上限 max_iterations=3 必须生效。"""
        code_loop.max_iterations = 2
        with patch.object(code_loop, "_llm_fix", new_callable=AsyncMock) as mock_fix:
            mock_fix.return_value = syntax_error_code
            result = await code_loop.run(syntax_error_code)
            assert result.iterations <= 2
            assert mock_fix.call_count <= 2

    @pytest.mark.asyncio
    async def test_type_error_fix(self, code_loop):
        """类型错误：代码试图对 None 调用 .upper() → LLM 修复。"""
        type_error_code = """def process(data):
    return [item.upper() for item in data]

if __name__ == "__main__":
    print(process(None))
"""
        fixed_code = """def process(data):
    if data is None:
        return []
    return [item.upper() for item in data]

if __name__ == "__main__":
    print(process(None))
"""
        # 语法正确，但 LLM 修复后更健壮
        # 这里主要测试的是 LLM 被正确调用
        with patch.object(code_loop, "_llm_fix", new_callable=AsyncMock) as mock_fix:
            mock_fix.return_value = fixed_code
            # 需要测试代码来捕获运行时错误
            test_code = """
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from module_under_test import process

def test_process_none():
    result = process(None)
    assert result == []
"""
            result = await code_loop.run(type_error_code, test_code)
            mock_fix.assert_called()
            # 结果应该成功（修复后代码通过测试）
            assert result.success is True


# ─── Degradation path tests ────────────────────────


class TestDegradationPaths:
    """测试降级策略。"""

    @pytest.mark.asyncio
    async def test_pytest_unavailable_fallback(self, code_loop, valid_code):
        """pytest 不可用 → 跳过测试，只做语法检查。"""
        with patch.object(code_loop, "_check_pytest_available", return_value=False):
            result = await code_loop.run(valid_code, test_code="def test(): pass")
            assert result.success is True
            assert "pytest" in result.degradation_reason.lower() or "pytest" in (result.degradation_reason or "")

    @pytest.mark.asyncio
    async def test_llm_error_returns_original(self, code_loop, syntax_error_code):
        """LLM 调用异常 → 返回原始代码（降级）。"""
        with patch.object(code_loop, "_llm_fix", new_callable=AsyncMock) as mock_fix:
            mock_fix.side_effect = Exception("API connection failed")
            result = await code_loop.run(syntax_error_code)
            # LLM 异常 → 函数返回原始代码
            assert result.success is False
            assert result.compilation_pass is False

    @pytest.mark.asyncio
    async def test_llm_returns_empty_code(self, code_loop, syntax_error_code):
        """LLM 返回空代码 → 保留原始代码。"""
        with patch.object(code_loop, "_llm_fix", new_callable=AsyncMock) as mock_fix:
            mock_fix.return_value = ""
            result = await code_loop.run(syntax_error_code)
            assert result.success is False

    @pytest.mark.asyncio
    async def test_llm_returns_same_code(self, code_loop, syntax_error_code):
        """LLM 返回相同代码 → 不修复，循环退出。"""
        with patch.object(code_loop, "_llm_fix", new_callable=AsyncMock) as mock_fix:
            mock_fix.return_value = syntax_error_code
            result = await code_loop.run(syntax_error_code)
            assert result.success is False
            # 每次 LLM 返回相同代码，但代码未被修改，会继续循环
            # 因为 check_syntax 仍然失败且 iteration < max


# ─── CodeLoopResult tests ──────────────────────────


class TestCodeLoopResult:
    """测试 CodeLoopResult 数据类。"""

    def test_success_result(self):
        from src.execution.code_loop import CodeLoopResult
        r = CodeLoopResult(
            success=True,
            final_code="def foo(): pass",
            iterations=1,
            compilation_pass=True,
            tests_pass=True,
        )
        assert r.success is True
        assert r.final_code == "def foo(): pass"
        assert r.errors_fixed == []
        assert r.remaining_errors == []

    def test_failure_result(self):
        from src.execution.code_loop import CodeLoopResult
        r = CodeLoopResult(
            success=False,
            final_code="def broken(",
            iterations=3,
            errors_fixed=["修复了缩进"],
            remaining_errors=["SyntaxError at line 2"],
            compilation_pass=False,
            tests_pass=False,
        )
        assert r.success is False
        assert r.iterations == 3
        assert len(r.errors_fixed) == 1
        assert len(r.remaining_errors) == 1


# ─── check_and_fix_code convenience function ───────


class TestCheckAndFixCode:
    """测试便捷函数 check_and_fix_code。"""

    @pytest.mark.asyncio
    async def test_valid_code(self):
        """有效代码返回通过。"""
        from src.execution.code_loop import check_and_fix_code
        result = await check_and_fix_code("def foo(): pass")
        assert result["checked"] is True
        assert result["passed"] is True
        assert "context" in result

    @pytest.mark.asyncio
    async def test_non_python_skip(self):
        """非 Python 代码跳过检查。"""
        from src.execution.code_loop import check_and_fix_code
        result = await check_and_fix_code("echo hello")
        assert result["checked"] is True
        # 非 Python 代码跳过, 视为通过
        assert result["passed"] is True
        assert "非Python" in result["degradation_reason"]


# ─── Integration: code_loop in executor ────────────


class TestExecutorIntegration:
    """测试 executor.py 中的 CodeLoop 集成。"""

    def test_code_loop_importable(self):
        """code_loop 模块可以正常导入。"""
        from src.execution.code_loop import CodeLoop, CodeLoopResult, check_and_fix_code
        assert CodeLoop is not None
        assert CodeLoopResult is not None
        assert check_and_fix_code is not None

    @pytest.mark.asyncio
    async def test_executor_import_with_code_loop(self):
        """executor 导入时不会因为 code_loop 导入报错。"""
        # 重新导入以确保 code_loop 集成代码正常运行
        import importlib
        import src.execution.executor as exe
        importlib.reload(exe)
        assert exe.ExecutionRouter is not None
