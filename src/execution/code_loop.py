# -*- coding: utf-8 -*-
"""代码生成闭环 — py_compile + pytest 自动修复循环

Code → py_compile 语法检查 → pytest 测试验证 → LLM 修复 → 重试
最多 3 轮，失败后返回原始代码 + 错误信息。
"""

import asyncio
import os
import sys
import tempfile
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("ai_company.code_loop")


@dataclass
class CodeLoopResult:
    """代码闭环执行结果"""
    success: bool
    final_code: str
    iterations: int
    errors_fixed: list[str] = field(default_factory=list)
    remaining_errors: list[str] = field(default_factory=list)
    compilation_pass: bool = False
    tests_pass: bool = False
    degradation_reason: str = ""


class CodeLoop:
    """代码生成→检查→修复→重试 闭环

    用法:
        loop = CodeLoop(max_iterations=3)
        result = await loop.run(code, test_code, file_path)

    降级策略:
        - pytest 不可用 → 跳过测试, 只做语法检查
        - LLM 修复 3 次仍失败 → 返回原始代码 + 错误
        - 非 Python 代码 → 跳过整个闭环
    """

    def __init__(self, max_iterations: int = 3, llm_model: str = "deepseek-chat"):
        self.max_iterations = max_iterations
        self.llm_model = llm_model
        self._project_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )

    # ─── Public API ──────────────────────────────────────

    async def run(
        self,
        code: str,
        test_code: str = "",
        file_path: str = None,
    ) -> CodeLoopResult:
        """主循环：语法检查 → 测试验证 → LLM 修复 → 重试

        Args:
            code: Python 源代码
            test_code: 可选的 pytest 测试代码
            file_path: 源代码文件路径（用于 pytest 发现）

        Returns:
            CodeLoopResult 包含最终状态和修复记录
        """
        # 非 Python 代码 → 跳过
        if not self._is_python_code(code):
            return CodeLoopResult(
                success=True,
                final_code=code,
                iterations=0,
                degradation_reason="非Python代码，跳过闭环检查",
            )

        current_code = code
        errors_fixed: list[str] = []
        compilation_pass = False
        tests_pass = False

        # 检查 pytest 是否可用
        pytest_available = self._check_pytest_available()
        degradation_reason = ""

        for iteration in range(1, self.max_iterations + 1):
            logger.debug("CodeLoop iteration %d/%d", iteration, self.max_iterations)

            # ── 第1步: py_compile 语法检查 ──
            syntax_ok, syntax_error = await self._check_syntax(current_code)

            if not syntax_ok:
                logger.info("语法检查失败 (iteration %d): %s", iteration, syntax_error[:100])
                if iteration < self.max_iterations:
                    try:
                        fixed_code = await self._llm_fix(
                            current_code, syntax_error, iteration
                        )
                    except Exception as e:
                        logger.warning("LLM 修复调用异常: %s", e)
                        fixed_code = current_code
                    if fixed_code and fixed_code != current_code:
                        errors_fixed.append(f"[iter {iteration}] 语法错误: {syntax_error[:80]}")
                        current_code = fixed_code
                    # 即使 LLM 返回相同代码，也继续下一轮（可能带不同的错误上下文）
                    continue
                # 最后一次迭代 → 失败
                return CodeLoopResult(
                    success=False,
                    final_code=current_code,
                    iterations=iteration,
                    errors_fixed=errors_fixed,
                    remaining_errors=[syntax_error],
                    compilation_pass=False,
                    tests_pass=False,
                )

            # 语法通过
            compilation_pass = True
            logger.debug("语法检查通过 (iteration %d)", iteration)

            # ── 第2步: pytest 测试验证 (如果有测试代码) ──
            if test_code and pytest_available:
                tests_ok, test_output = await self._run_tests(
                    current_code, test_code, file_path
                )

                if not tests_ok:
                    logger.info("测试失败 (iteration %d): %s", iteration, test_output[:100])
                    if iteration < self.max_iterations:
                        try:
                            fixed_code = await self._llm_fix(
                                current_code,
                                f"测试失败:\n{test_output}",
                                iteration,
                            )
                        except Exception as e:
                            logger.warning("LLM 修复调用异常 (测试): %s", e)
                            fixed_code = current_code
                        if fixed_code and fixed_code != current_code:
                            errors_fixed.append(
                                f"[iter {iteration}] 测试失败: {test_output[:80]}"
                            )
                            current_code = fixed_code
                        # 即使 LLM 返回相同代码，也继续下一轮
                        continue
                    # 最后一次迭代 → 失败
                    return CodeLoopResult(
                        success=False,
                        final_code=current_code,
                        iterations=iteration,
                        errors_fixed=errors_fixed,
                        remaining_errors=[test_output],
                        compilation_pass=True,
                        tests_pass=False,
                    )

                tests_pass = True
                logger.debug("测试通过 (iteration %d)", iteration)
            elif test_code and not pytest_available:
                degradation_reason = "pytest 不可用，仅做语法检查"
                logger.warning(degradation_reason)

            # 语法 + 测试都通过 → 成功
            return CodeLoopResult(
                success=True,
                final_code=current_code,
                iterations=iteration,
                errors_fixed=errors_fixed,
                remaining_errors=[],
                compilation_pass=True,
                tests_pass=tests_pass if test_code else None,
                degradation_reason=degradation_reason,
            )

        # 循环耗尽
        return CodeLoopResult(
            success=False,
            final_code=current_code,
            iterations=self.max_iterations,
            errors_fixed=errors_fixed,
            remaining_errors=["达到最大迭代次数"],
            compilation_pass=compilation_pass,
            tests_pass=tests_pass,
        )

    # ─── Internal Methods ───────────────────────────────

    @staticmethod
    def _is_python_code(code: str) -> bool:
        """检测是否为 Python 代码。

        规则：
        - 包含 Python 关键字/特征 (def, class, import, print, etc.)
        - 不是纯 shell 命令
        - 不是纯自然语言
        """
        if not code or not code.strip():
            return False

        code_stripped = code.strip()

        # Shell 命令特征：以常见 shell 命令开头
        shell_prefixes = (
            "ls ", "cd ", "cat ", "echo ", "pwd ", "rm ", "cp ", "mv ",
            "mkdir ", "rmdir ", "chmod ", "chown ", "grep ", "find ",
            "docker ", "git ", "pip ", "npm ", "yarn ", "curl ", "wget ",
            "python ", "python3 ", "bash ", "sh ", "source ", "export ",
            "sudo ", "apt ", "brew ", "systemctl ", "ps ", "kill ",
        )
        first_line = code_stripped.split("\n")[0].strip().lower()
        for prefix in shell_prefixes:
            if first_line.startswith(prefix):
                # 除非有明确 Python 语法结构
                if not any(
                    kw in code_stripped
                    for kw in ("def ", "class ", "import ", "from ", "print(", "#!")
                ):
                    return False
                break

        # Python 特征关键字
        python_indicators = [
            "def ", "class ", "import ", "from ",
            "print(", "if __name__", "async def",
            "@dataclass", "@pytest", "try:", "except ",
            "with ", "yield", "raise ",
        ]
        if any(indicator in code_stripped for indicator in python_indicators):
            return True

        # 以 #!python 或 # -*- coding 开头
        if first_line.startswith("#!") and "python" in first_line:
            return True
        if "# -*- coding" in code_stripped:
            return True

        # 纯粹的表达式不检查（如 "1+1" 虽然是有效 Python 但不是"代码"）
        return False

    @staticmethod
    def _check_pytest_available() -> bool:
        """检查 pytest 是否可用。"""
        try:
            import pytest  # noqa: F401
            return True
        except ImportError:
            return False

    async def _check_syntax(self, code: str) -> tuple:
        """写入临时文件，用 compile() 做语法检查。

        Returns:
            (pass: bool, error_msg: str)
        """
        tmp_path = None
        try:
            # 使用 compile() 内建函数直接编译，更快
            compile(code, "<code_loop>", "exec")
            return True, ""
        except SyntaxError as e:
            return False, f"SyntaxError: {e.msg} at line {e.lineno}, offset {e.offset}"
        except Exception as e:
            # 回退到 py_compile 方式
            try:
                fd, tmp_path = tempfile.mkstemp(suffix=".py", prefix="code_loop_")
                os.close(fd)
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(code)

                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "py_compile", tmp_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=10
                )
                if proc.returncode == 0:
                    return True, ""
                return False, stderr.decode("utf-8", errors="replace")[:500]
            except asyncio.TimeoutError:
                return False, "py_compile timeout (10s)"
            except Exception as ex:
                return False, f"py_compile error: {ex}"
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

    async def _run_tests(
        self, code: str, test_code: str, file_path: str = None
    ) -> tuple:
        """写入代码 + 测试到临时文件，用 pytest -q 运行。

        Returns:
            (pass: bool, output: str)
        """
        tmp_dir = None
        src_file = None
        test_file = None
        try:
            tmp_dir = tempfile.mkdtemp(prefix="code_loop_test_")

            # 写入源代码
            module_name = "module_under_test"
            src_file = os.path.join(tmp_dir, f"{module_name}.py")
            with open(src_file, "w", encoding="utf-8") as f:
                f.write(code)

            # 写入测试代码
            test_file = os.path.join(tmp_dir, "test_module.py")
            with open(test_file, "w", encoding="utf-8") as f:
                f.write(test_code)

            # 如果提供了原始文件路径，也复制一份到 tmp_dir
            if file_path and os.path.exists(file_path):
                import shutil
                dest = os.path.join(tmp_dir, os.path.basename(file_path))
                shutil.copy2(file_path, dest)

            # 运行 pytest
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pytest", "-q", test_file,
                "--tb=short",
                cwd=tmp_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=30
            )

            output = stdout.decode("utf-8", errors="replace")
            err_output = stderr.decode("utf-8", errors="replace")

            if proc.returncode == 0:
                return True, output[:500]
            else:
                combined = (output + "\n" + err_output).strip()
                return False, combined[:500]

        except asyncio.TimeoutError:
            return False, "pytest timeout (30s)"
        except Exception as e:
            return False, f"pytest error: {e}"
        finally:
            # 清理临时文件
            import shutil
            if tmp_dir and os.path.exists(tmp_dir):
                try:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except OSError:
                    pass

    async def _llm_fix(self, code: str, error: str, attempt: int) -> str:
        """调用 LLM (deepseek-chat) 根据错误修复代码。

        Args:
            code: 当前有错误的代码
            error: 错误信息
            attempt: 当前修复尝试次数

        Returns:
            修复后的代码文本
        """
        import json
        try:
            from src.ceo.graph import _get_llm
            from langchain_core.messages import HumanMessage, SystemMessage

            llm = _get_llm("developer")

            system_prompt = (
                "你是一个代码修复助手。你的任务是修复给定代码中的错误。\n"
                "要求:\n"
                "1. 仔细分析错误信息\n"
                "2. 修复代码中的所有问题\n"
                "3. 保持代码的功能和结构不变\n"
                "4. 只输出修复后的完整代码，不要添加任何解释\n"
                "5. 不要用markdown代码块包裹，直接输出纯代码"
            )

            user_prompt = (
                f"## 错误信息 (第 {attempt} 次修复)\n{error}\n\n"
                f"## 需要修复的代码\n```python\n{code}\n```\n\n"
                f"请输出修复后的完整代码:"
            )

            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_prompt),
            ]

            response = await llm.ainvoke(messages)
            raw = str(response.content)

            # 清理 LLM 输出：去除 markdown 代码块标记
            fixed = self._extract_code_from_llm_output(raw)

            if not fixed or len(fixed) < 10:
                logger.warning("LLM 返回空代码，保留原始代码")
                return code

            logger.debug("LLM 修复完成，代码长度: %d → %d", len(code), len(fixed))
            return fixed

        except Exception as e:
            logger.warning("LLM 修复失败: %s", e)
            return code  # 返回原始代码作为降级

    @staticmethod
    def _extract_code_from_llm_output(raw: str) -> str:
        """从 LLM 输出中提取纯代码。

        处理以下格式:
        - ```python\ncode\n```
        - ```\ncode\n```
        - 纯代码文本
        """
        import re

        raw = raw.strip()

        # 去掉 markdown 代码块
        fence_match = re.match(
            r"```(?:python|py)?\s*\n(.*?)\n```", raw, re.DOTALL
        )
        if fence_match:
            return fence_match.group(1).strip()

        # 以 ``` 开头但无结尾
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:python|py)?\s*\n?", "", raw)
        if raw.endswith("```"):
            raw = raw[:-3].strip()

        return raw


# ─── Convenience: integrate with executor ────────────────

# 全局单例（延迟初始化）
_code_loop_instance: CodeLoop = None


def get_code_loop(max_iterations: int = 3) -> CodeLoop:
    """获取 CodeLoop 单例。"""
    global _code_loop_instance
    if _code_loop_instance is None:
        _code_loop_instance = CodeLoop(max_iterations=max_iterations)
    return _code_loop_instance


async def check_and_fix_code(
    code: str,
    test_code: str = "",
    file_path: str = None,
    max_iterations: int = 3,
) -> dict:
    """便捷函数：检查并修复代码，返回 dict 结果。

    用于 executor.py 中 run_python 执行后自动触发。

    Returns:
        {
            "checked": bool,           # 是否执行了检查
            "passed": bool,            # 最终是否通过
            "iterations": int,         # 修复轮数
            "errors_fixed": list[str], # 修复的错误
            "context": str,            # 供注入到下一轮 LLM 的上下文
        }
    """
    loop = CodeLoop(max_iterations=max_iterations)
    result = await loop.run(code, test_code, file_path)

    # 构建上下文消息
    if not result.iterations:
        context = "代码闭环: 非Python代码，跳过检查"
    elif result.success:
        if result.tests_pass:
            context = f"代码闭环: ✅ 语法+测试通过 (经过 {result.iterations} 轮检查)"
        else:
            context = f"代码闭环: ✅ 语法通过 (经过 {result.iterations} 轮检查)"
        if result.errors_fixed:
            context += f"\n已修复: {', '.join(result.errors_fixed)}"
    else:
        context = f"代码闭环: ❌ 检查失败 (经过 {result.iterations} 轮)\n"
        if result.errors_fixed:
            context += f"已修复: {', '.join(result.errors_fixed)}\n"
        if result.remaining_errors:
            context += f"剩余错误: {', '.join(result.remaining_errors[:3])}"
        if result.degradation_reason:
            context += f"\n降级原因: {result.degradation_reason}"

    return {
        "checked": True,
        "passed": result.success,
        "iterations": result.iterations,
        "errors_fixed": result.errors_fixed,
        "remaining_errors": result.remaining_errors,
        "context": context,
        "degradation_reason": result.degradation_reason,
    }
