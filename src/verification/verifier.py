# -*- coding: utf-8 -*-
"""Verifier — 执行后验证层 (P0.3)

职责：根据意图类型选择合适的验证策略，确保操作确实成功。
提供两种接口：
1. 面向新架构: async verify(intent, result, context) → VerifyResult
2. 兼容旧 graph.py: verify_aggregate(intent, output, execution_log) → dict (score_card 格式)
"""

import asyncio
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Optional


# ═══ Python 解释器路径 ═══
_PYTHON_BIN = os.environ.get("PYTHON_BIN", sys.executable)


# ═══ VerifyResult 数据类 ═══

@dataclass
class VerifyResult:
    """验证结果"""
    success: bool                     # 验证是否通过
    score: int                        # 0-100 评分
    detail: str                       # 验证详情描述
    needs_retry: bool = False         # 是否需要重试
    retry_strategy: str = ""          # 重试策略描述
    evidence: dict = field(default_factory=dict)  # 验证证据（截图路径、输出摘要等）

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "score": self.score,
            "detail": self.detail,
            "needs_retry": self.needs_retry,
            "retry_strategy": self.retry_strategy,
            "evidence": self.evidence,
        }


# ═══ Stale 检测关键字 ═══

_STALE_PATTERNS = [
    r"无法获取.*?(?:实时|数据|信息)",
    r"根据已知数据",
    r"cannot\s+(?:access|fetch|retrieve).*?(?:data|price|information)",
    r"我无法提供.*?(?:建议|预测|数据)",
    r"无法访问.*?(?:数据|页面|网站)",
    r"no\s+(?:real.?time|current|live)\s+data",
]

# ═══ Compliance boilerplate 检测关键字 ═══
# LLM ack'd format instruction instead of answering
_BOILERPLATE_PATTERNS = [
    r"收到.*我会严格",
    r"我会.*遵守.*格式",
    r"有什么需要我做的",
    r"好的.*我会.*JSON",
    r"明白了.*我会",
    r"了解.*马上.*格式",
    r"^收到[，,。!\s]*$",
]

# ═══ Crash 检测关键字 ═══
# 使用正则模式替代简单子串匹配，避免将 "No error found" 误判为 crash
_CRASH_PATTERNS = [
    r"\bError:",             # Error: 后跟冒号（真实错误输出）
    r"\bError\[",            # Error[ 后跟方括号
    r"\bTraceback\b",
    r"\bCRASHED\b",
    r"\bFAILED\b",
    r"\bSIGSEGV\b",
    r"\bFatal\b",
    r"\bKilled\b",
    r"\bOOM\b",              # Out of Memory
    r"\bSyntaxError\b",
    r"\bConnectionError\b",
    r"Permission denied",
]


# ═══ Verifier ═══

class Verifier:
    """执行后验证器 — 根据意图类型选择合适验证策略。

    用法:
        verifier = Verifier()
        result = await verifier.verify("COMMAND", {"output": "..."}, {})
        # 兼容旧代码:
        score, decision, needs_audit = verifier.verify_aggregate("COMMAND", "output...", [])
    """

    def __init__(self, python_bin: Optional[str] = None):
        """初始化验证器。

        Args:
            python_bin: Python 解释器路径，默认读取环境变量 PYTHON_BIN
        """
        self._python_bin = python_bin or _PYTHON_BIN

    # ─── 主验证接口 ─────────────────────────────────

    async def verify(
        self,
        intent: str,
        result: dict,
        context: Optional[dict] = None,
    ) -> VerifyResult:
        """根据意图类型选择合适的验证策略。

        Args:
            intent: 意图类型 (COMMAND|SEARCH|RESEARCH|CODING|SOCIAL|...)
            result: 执行结果字典，至少包含 "output" 字段
            context: 可选的上下文信息

        Returns:
            VerifyResult 验证结果
        """
        ctx = context or {}
        strategy = self._get_strategy(intent)
        return await strategy(result, ctx)

    def _get_strategy(self, intent: str):
        """根据意图返回对应验证策略函数。"""
        intent_upper = intent.upper().strip()

        strategies = {
            "COMMAND": self._verify_command,
            "SEARCH": self._verify_search,
            "RESEARCH": self._verify_search,      # 复用 SEARCH 策略
            "CODING": self._verify_coding,
            "SOCIAL": self._verify_social,
            "WECHAT": self._verify_social,        # WeChat 复用 SOCIAL
            "FILE": self._verify_file,
            "SYSTEM": self._verify_system,
            "VISION": self._verify_default,
            "MEMORY": self._verify_default,
            "AUTOMATION": self._verify_default,
            "GENERAL_CHAT": self._verify_default,
        }

        return strategies.get(intent_upper, self._verify_default)

    # ─── 各意图验证策略 ──────────────────────────────

    async def _verify_command(self, result: dict, ctx: dict) -> VerifyResult:
        """COMMAND 验证：
        - 检查 output 非空
        - 检查无 crash/error 关键字
        """
        output = str(result.get("output", ""))
        has_output = bool(output.strip())
        is_crash = self._check_crash(output)

        if has_output and not is_crash:
            return VerifyResult(
                success=True,
                score=95,
                detail="命令执行成功：output 非空且无错误关键字",
                evidence={"output_preview": output[:500]},
            )
        elif is_crash:
            return VerifyResult(
                success=False,
                score=0,
                detail=f"命令执行失败：检测到 crash/error 关键字",
                needs_retry=True,
                retry_strategy="重新执行命令或检查错误日志",
                evidence={"output_preview": output[:500]},
            )
        else:
            return VerifyResult(
                success=False,
                score=0,
                detail="命令执行失败：output 为空",
                needs_retry=True,
                retry_strategy="重试命令执行",
            )

    async def _verify_search(self, result: dict, ctx: dict) -> VerifyResult:
        """SEARCH / RESEARCH 验证：
        - 检查结果是否 stale（LLM 未实际搜索，用训练数据回答）
        """
        output = str(result.get("output", ""))
        has_output = bool(output.strip())
        is_stale = self._check_stale_answer(output)

        if not has_output:
            return VerifyResult(
                success=False,
                score=0,
                detail="搜索结果为空",
                needs_retry=True,
                retry_strategy="重新执行搜索",
            )

        if is_stale:
            return VerifyResult(
                success=False,
                score=20,
                detail="Stale answer：LLM 未获取实时数据，使用了过时/训练数据",
                needs_retry=True,
                retry_strategy="强制调用 web_search / web_fetch 获取实时数据",
                evidence={"output_preview": output[:500]},
            )

        return VerifyResult(
            success=True,
            score=95,
            detail="搜索结果正常：非 stale，包含有效数据",
            evidence={"output_preview": output[:500]},
        )

    async def _verify_coding(self, result: dict, ctx: dict) -> VerifyResult:
        """CODING 验证：
        - 运行 python3.12 -m py_compile 检查语法
        - 提取代码块后进行编译检查
        """
        output = str(result.get("output", ""))
        files = result.get("files", [])
        code_blocks = self._extract_code_blocks(output)

        if not code_blocks and not files:
            # 无代码可验证 — 检查是否有输出
            has_output = bool(output.strip())
            if has_output:
                return VerifyResult(
                    success=True,
                    score=85,
                    detail="编码输出正常（未检测到代码块，跳过语法检查）",
                    evidence={"output_preview": output[:500]},
                )
            return VerifyResult(
                success=False,
                score=0,
                detail="编码输出为空",
                needs_retry=True,
                retry_strategy="重新生成代码",
            )

        # 编译检查每个代码块
        check_results = []
        for i, code in enumerate(code_blocks):
            r = await self._py_compile_check(code, label=f"block_{i}")
            check_results.append(r)

        # 合并结果
        all_passed = all(r.get("success", False) for r in check_results if r.get("checked"))
        failures = [r for r in check_results if not r.get("success", False)]

        if all_passed and check_results:
            return VerifyResult(
                success=True,
                score=85,
                detail=f"语法检查通过（{len(check_results)} 个代码块）",
                evidence={
                    "code_blocks_checked": len(check_results),
                    "all_passed": True,
                },
            )
        elif failures:
            fail_details = "; ".join(
                f"block_{f['label']}: {f.get('error', 'unknown')[:100]}"
                for f in failures
            )
            return VerifyResult(
                success=False,
                score=0,
                detail=f"语法检查失败：{fail_details}",
                needs_retry=True,
                retry_strategy="修复语法错误后重新生成代码",
                evidence={
                    "code_blocks_checked": len(check_results),
                    "failures": len(failures),
                },
            )
        else:
            return VerifyResult(
                success=True,
                score=85,
                detail="编码完成（无代码块需检查）",
            )

    async def _verify_social(self, result: dict, ctx: dict) -> VerifyResult:
        """SOCIAL / WeChat 验证：
        - 检查 result 中是否包含成功标记
        - result.get("success") 或 result.get("sent") 为 True
        """
        # 直接检查 result 中的成功标记
        if result.get("success") or result.get("sent"):
            return VerifyResult(
                success=True,
                score=95,
                detail="社交/微信操作成功：检测到成功标记",
                evidence={
                    "success_flag": True,
                    "contact": result.get("contact", ""),
                    "message_preview": str(result.get("message", ""))[:200],
                },
            )

        # 检查 output 中是否有成功迹象
        output = str(result.get("output", ""))
        success_indicators = ["发送成功", "已发送", "sent", "success", "成功"]
        has_success_text = any(
            indicator.lower() in output.lower()
            for indicator in success_indicators
        )

        if has_success_text:
            return VerifyResult(
                success=True,
                score=90,
                detail="社交/微信操作：输出中包含成功指示词",
                evidence={"output_preview": output[:500]},
            )

        return VerifyResult(
            success=False,
            score=0,
            detail="社交/微信操作失败：未检测到成功标记",
            needs_retry=True,
            retry_strategy="重新执行社交操作，或检查截图确认",
            evidence={"output_preview": output[:500]},
        )

    async def _verify_file(self, result: dict, ctx: dict) -> VerifyResult:
        """FILE 验证：
        - 检查文件路径是否存在（用 os.path.exists）
        """
        output = str(result.get("output", ""))
        file_paths = result.get("files", result.get("paths", []))

        # 从 output 中提取可能的文件路径
        if not file_paths:
            file_paths = self._extract_file_paths(output)

        if not file_paths:
            # 无文件路径 — 检查是否有输出
            has_output = bool(output.strip())
            if has_output:
                return VerifyResult(
                    success=True,
                    score=90,
                    detail="文件操作完成（未指定文件路径，有输出内容）",
                    evidence={"output_preview": output[:500]},
                )
            return VerifyResult(
                success=False,
                score=0,
                detail="文件操作失败：无文件路径且无输出",
                needs_retry=True,
                retry_strategy="指定文件路径后重新操作",
            )

        # 逐一检查文件是否存在
        existing = []
        missing = []
        for fp in file_paths:
            if os.path.exists(fp):
                existing.append(fp)
            else:
                # 尝试相对于工作目录
                alt_path = os.path.join(os.getcwd(), fp)
                if os.path.exists(alt_path):
                    existing.append(fp)
                else:
                    missing.append(fp)

        if missing:
            return VerifyResult(
                success=False,
                score=0,
                detail=f"文件操作失败：以下文件不存在 → {', '.join(missing[:5])}",
                needs_retry=True,
                retry_strategy="重新创建或验证文件路径",
                evidence={"existing": existing, "missing": missing},
            )

        return VerifyResult(
            success=True,
            score=90,
            detail=f"文件验证通过：{len(existing)} 个文件存在",
            evidence={"files": existing},
        )

    async def _verify_system(self, result: dict, ctx: dict) -> VerifyResult:
        """SYSTEM 验证：
        - 检查 output 非空且无 error
        """
        output = str(result.get("output", ""))
        has_output = bool(output.strip())
        is_crash = self._check_crash(output)

        if has_output and not is_crash:
            return VerifyResult(
                success=True,
                score=90,
                detail="系统操作成功：output 非空且无错误",
                evidence={"output_preview": output[:500]},
            )
        elif is_crash:
            return VerifyResult(
                success=False,
                score=0,
                detail="系统操作失败：检测到错误关键字",
                needs_retry=True,
                retry_strategy="检查错误日志后重试",
                evidence={"output_preview": output[:500]},
            )
        else:
            return VerifyResult(
                success=False,
                score=0,
                detail="系统操作失败：output 为空",
                needs_retry=True,
                retry_strategy="重试系统操作",
            )

    async def _verify_default(self, result: dict, ctx: dict) -> VerifyResult:
        """DEFAULT 验证（兜底策略）：
        - 有 output → score=80, APPROVE
        - 无 output → score=0, FAIL
        """
        output = str(result.get("output", ""))
        has_output = bool(output.strip())

        if has_output:
            return VerifyResult(
                success=True,
                score=80,
                detail="默认验证通过：output 非空",
                evidence={"output_preview": output[:500]},
            )
        return VerifyResult(
            success=False,
            score=0,
            detail="默认验证失败：output 为空",
            needs_retry=True,
            retry_strategy="重新执行操作",
        )

    # ─── Vision 预留接口 ─────────────────────────────

    async def verify_with_vision(
        self,
        screenshot_path: str,
        expected_content: str = "",
    ) -> VerifyResult:
        """截图 + Vision 确认（预留接口）。

        当前为 stub 实现，实际使用时需要接入 Vision 能力
        （如 vision_analyze 工具或 GPT-4V）。

        Args:
            screenshot_path: 截图文件路径
            expected_content: 期望在截图中看到的内容

        Returns:
            VerifyResult
        """
        # 检查截图文件是否存在
        if not os.path.exists(screenshot_path):
            return VerifyResult(
                success=False,
                score=0,
                detail=f"视觉验证失败：截图文件不存在 → {screenshot_path}",
                needs_retry=True,
                retry_strategy="重新截图",
                evidence={"screenshot_path": screenshot_path},
            )

        # Stub: 实际使用时调用 Vision API 确认内容
        # TODO: 接入 vision_analyze 工具
        return VerifyResult(
            success=True,
            score=85,
            detail=f"视觉验证（stub）：截图文件存在 → {screenshot_path}",
            evidence={
                "screenshot_path": screenshot_path,
                "vision_status": "stub",
                "note": "需要接入实际 Vision API 进行内容确认",
            },
        )

    # ─── 辅助检测方法 ────────────────────────────────

    @staticmethod
    def _check_stale_answer(output: str) -> bool:
        """检测回答是否为 stale（LLM 未实际搜索，使用训练数据）。

        Args:
            output: 验证的输出文本

        Returns:
            True 如果检测到 stale 模式
        """
        if not output:
            return False
        for pat in _STALE_PATTERNS:
            if re.search(pat, output, re.IGNORECASE):
                return True
        return False

    @staticmethod
    def _check_boilerplate_ack(output: str) -> bool:
        """检测 LLM 是否只输出了合规确认（compliance boilerplate），而非实际回答。

        Args:
            output: 验证的输出文本

        Returns:
            True 如果检测到合规锅炉板模式
        """
        if not output:
            return False
        for pat in _BOILERPLATE_PATTERNS:
            if re.search(pat, output, re.IGNORECASE):
                return True
        return False

    @staticmethod
    def _check_crash(output: str) -> bool:
        """检测输出中是否包含 crash/error 关键字。

        使用正则模式而非简单子串匹配，
        避免 "No error found" / "error rate" 等正常输出被误判。

        Args:
            output: 验证的输出文本

        Returns:
            True 如果检测到 crash 关键字
        """
        if not output:
            return False
        for pat in _CRASH_PATTERNS:
            if re.search(pat, output):
                return True
        return False

    @staticmethod
    def _extract_code_blocks(output: str) -> list[str]:
        """从输出中提取 Python 代码块。

        Args:
            output: 包含代码块的文本

        Returns:
            代码块列表
        """
        # 提取 markdown fence 代码块
        blocks = re.findall(
            r'```(?:python|py)?\s*\n(.*?)```',
            output, re.DOTALL,
        )

        if not blocks:
            # 尝试提取缩进代码
            lines = output.split("\n")
            in_code = False
            current: list[str] = []
            for line in lines:
                if re.match(r'^(def |class |import |from |if |for |while |try |# |    |\t)', line):
                    in_code = True
                    current.append(line)
                elif in_code and line.strip() == "":
                    if current:
                        blocks.append("\n".join(current))
                        current = []
                    in_code = False
                elif in_code:
                    current.append(line)
            if current:
                blocks.append("\n".join(current))

        return blocks

    @staticmethod
    def _extract_file_paths(output: str) -> list[str]:
        """从输出中提取可能的文件路径。

        Args:
            output: 输出文本

        Returns:
            文件路径列表
        """
        paths = []
        # 匹配常见的路径模式
        patterns = [
            r'(?:^|\s)((?:/[\w.-]+)+\.\w+)(?:\s|$)',          # 绝对路径 /a/b/c.txt
            r'(?:^|\s)((?:~?/[\w.-]+)+\.\w+)(?:\s|$)',         # ~/a/b/c.txt
            r'(?:^|\s)([\w.-]+/[\w./-]+\.\w+)(?:\s|$)',         # 相对路径 a/b/c.txt
            r'文件[：:]\s*([^\s，,。]+)',                          # 文件：xxx
            r'path[=:]\s*([^\s，,]+)',                            # path=xxx
        ]
        for pat in patterns:
            matches = re.findall(pat, output)
            for m in matches:
                p = m.strip()
                if p not in paths and len(p) > 2:
                    paths.append(p)

        return paths[:10]  # 最多返回 10 个路径

    async def _py_compile_check(
        self,
        code: str,
        label: str = "code",
    ) -> dict:
        """运行 py_compile 检查 Python 代码语法。

        Args:
            code: Python 代码文本
            label: 代码块标签

        Returns:
            {"success": bool, "label": str, "error": str, "checked": bool}
        """
        # 跳过不包含 Python 特征的内容
        if not any(
            kw in code
            for kw in ("def ", "class ", "import ", "from ", "print", "=")
        ):
            return {"success": True, "label": label, "error": "", "checked": False}

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            delete=False,
            encoding="utf-8",
        ) as f:
            f.write(code)
            tmp_path = f.name

        try:
            proc = await asyncio.create_subprocess_exec(
                self._python_bin,
                "-m", "py_compile",
                tmp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=15,
            )

            if proc.returncode != 0:
                error = stderr.decode("utf-8", errors="replace")[:300]
                return {
                    "success": False,
                    "label": label,
                    "error": error,
                    "checked": True,
                }

            return {
                "success": True,
                "label": label,
                "error": "",
                "checked": True,
            }
        except asyncio.TimeoutError:
            return {
                "success": False,
                "label": label,
                "error": "py_compile 超时 (15s)",
                "checked": True,
            }
        except Exception as e:
            return {
                "success": False,
                "label": label,
                "error": str(e)[:200],
                "checked": True,
            }
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ─── 兼容旧代码接口 ───────────────────────────────

    def verify_aggregate(
        self,
        intent: str,
        output: str,
        execution_log: Optional[list] = None,
    ) -> dict:
        """兼容 graph.py verify_aggregate_node 的 score_card 格式。

        返回 dict，键：score / decision / next_action / needs_audit，
        与 graph.py 中 verify_aggregate_node 返回的 score_card 格式兼容。

        Args:
            intent: 意图类型
            output: 执行输出文本
            execution_log: 执行日志列表（可选）

        Returns:
            dict: {"score": int, "decision": str, "next_action": str, "needs_audit": bool}
            - decision: "APPROVE" | "FAIL"
            - next_action: "deliver"
            - needs_audit: 是否需要 Auditor 进一步审查
        """
        exec_log = execution_log or []

        # ═══ Crash 快速检测（统一使用 _check_crash） ═══
        log_str = " ".join(str(e) for e in exec_log)
        combined = f"{log_str}\n{output}"
        is_crash = self._check_crash(combined)

        if is_crash and len(output.strip()) < 500:
            return {"score": 0, "decision": "FAIL", "next_action": "deliver", "needs_audit": False}

        # ═══ Stale 检测 ═══
        is_stale = self._check_stale_answer(output)

        if is_stale:
            return {"score": 20, "decision": "FAIL", "next_action": "deliver", "needs_audit": False}

        # ═══ 根据意图评分 ═══
        intent_upper = intent.upper().strip()

        if intent_upper in ("COMMAND", "SYSTEM"):
            has_output = bool(output.strip())
            if has_output and not self._check_crash(output):
                return {"score": 95, "decision": "APPROVE", "next_action": "deliver", "needs_audit": False}
            return {"score": 0, "decision": "FAIL", "next_action": "deliver", "needs_audit": False}

        elif intent_upper in ("SEARCH", "RESEARCH"):
            has_output = bool(output.strip())
            if has_output and not is_stale:
                return {"score": 95, "decision": "APPROVE", "next_action": "deliver", "needs_audit": False}
            elif is_stale:
                return {"score": 20, "decision": "FAIL", "next_action": "deliver", "needs_audit": False}
            return {"score": 0, "decision": "FAIL", "next_action": "deliver", "needs_audit": False}

        elif intent_upper == "CODING":
            has_output = bool(output.strip())
            if not has_output:
                return {"score": 0, "decision": "FAIL", "next_action": "deliver", "needs_audit": False}
            # 需要 Auditor 审查（无法在同步接口中运行 py_compile）
            return {"score": 85, "decision": "APPROVE", "next_action": "deliver", "needs_audit": True}

        elif intent_upper in ("SOCIAL", "WECHAT"):
            has_output = bool(output.strip())
            success_indicators = ["发送成功", "已发送", "sent", "success", "成功"]
            has_success = any(
                ind.lower() in output.lower()
                for ind in success_indicators
            )
            if has_success or len(output) > 100:
                return {"score": 95, "decision": "APPROVE", "next_action": "deliver", "needs_audit": False}
            return {"score": 0, "decision": "FAIL", "next_action": "deliver", "needs_audit": True}

        elif intent_upper == "FILE":
            has_output = bool(output.strip())
            if has_output:
                return {"score": 90, "decision": "APPROVE", "next_action": "deliver", "needs_audit": False}
            return {"score": 0, "decision": "FAIL", "next_action": "deliver", "needs_audit": False}

        else:
            # DEFAULT / GENERAL_CHAT / VISION / MEMORY / AUTOMATION
            has_output = bool(output.strip())
            if has_output:
                return {"score": 80, "decision": "APPROVE", "next_action": "deliver", "needs_audit": False}
            return {"score": 0, "decision": "FAIL", "next_action": "deliver", "needs_audit": False}
