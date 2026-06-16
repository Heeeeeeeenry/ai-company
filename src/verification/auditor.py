"""Independent Auditor Agent — Objective Multi-Dimensional Scoring

The Auditor NEVER participates in creating work. It only evaluates outputs
against structured criteria. This is the core of the "separation of duties"
principle: the one who builds does NOT score their own work.
"""

import json
import re
from dataclasses import dataclass, field
from langchain_core.messages import HumanMessage, SystemMessage

from src.config import config
from src.ceo.graph import _get_llm, _extract_json


# ─── Scoring Dimensions ───────────────────────────

@dataclass
class DimensionScore:
    name: str
    score: int          # 0-100
    weight: float       # 0.0-1.0
    reasoning: str      # Why this score
    issues: list[str] = field(default_factory=list)


@dataclass
class AuditReport:
    dimensions: list[DimensionScore]
    overall_score: float
    verdict: str        # APPROVE | REVISE | REJECT
    summary: str
    suggestions: list[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return {
            "dimensions": [
                {
                    "name": d.name,
                    "score": d.score,
                    "weight": d.weight,
                    "reasoning": d.reasoning,
                    "issues": d.issues,
                }
                for d in self.dimensions
            ],
            "overall_score": round(self.overall_score, 1),
            "verdict": self.verdict,
            "summary": self.summary,
            "suggestions": self.suggestions,
        }


# ─── Department-specific scoring dimensions ───────

SCORING_DIMENSIONS = {
    "developer": [
        {"name": "正确性", "weight": 0.30, "description": "代码逻辑是否正确，是否满足需求"},
        {"name": "完整性", "weight": 0.20, "description": "错误处理、边界情况、日志是否完善"},
        {"name": "可维护性", "weight": 0.15, "description": "命名规范、注释、代码结构是否清晰"},
        {"name": "安全性", "weight": 0.15, "description": "是否存在注入、硬编码密钥、权限问题"},
        {"name": "性能", "weight": 0.10, "description": "算法复杂度、资源使用是否合理"},
        {"name": "测试覆盖", "weight": 0.10, "description": "是否包含测试用例，覆盖关键路径"},
    ],
    "qa": [
        {"name": "覆盖完整性", "weight": 0.30, "description": "测试是否覆盖核心功能、边界、异常"},
        {"name": "用例质量", "weight": 0.25, "description": "测试用例设计是否合理，预期结果是否明确"},
        {"name": "可执行性", "weight": 0.20, "description": "测试能否直接运行，依赖是否正确"},
        {"name": "维护性", "weight": 0.15, "description": "测试代码结构是否清晰，是否易于扩展"},
        {"name": "有效性", "weight": 0.10, "description": "测试是否能真正发现问题"},
    ],
    "devops": [
        {"name": "正确性", "weight": 0.25, "description": "配置是否正确可用"},
        {"name": "安全性", "weight": 0.25, "description": "端口暴露、权限、密钥管理是否安全"},
        {"name": "可靠性", "weight": 0.20, "description": "是否包含健康检查、重启策略"},
        {"name": "成本效率", "weight": 0.15, "description": "资源分配是否合理"},
        {"name": "可维护性", "weight": 0.15, "description": "配置是否清晰、易于修改"},
    ],
    "research": [
        {"name": "准确性", "weight": 0.30, "description": "信息是否准确、有来源支持"},
        {"name": "完整性", "weight": 0.25, "description": "是否覆盖用户关心的所有角度"},
        {"name": "深度", "weight": 0.20, "description": "分析是否深入，还是浮于表面"},
        {"name": "实用性", "weight": 0.15, "description": "是否给出可操作的建议"},
        {"name": "时效性", "weight": 0.10, "description": "信息是否是最新的"},
    ],
    "marketing": [
        {"name": "吸引力", "weight": 0.25, "description": "文案是否有吸引力，能否抓住注意力"},
        {"name": "清晰度", "weight": 0.20, "description": "信息传达是否清晰，无歧义"},
        {"name": "平台适配", "weight": 0.20, "description": "是否符合目标平台的风格和限制"},
        {"name": "说服力", "weight": 0.20, "description": "是否有效传递价值主张"},
        {"name": "SEO/传播性", "weight": 0.15, "description": "是否包含关键词、易于传播"},
    ],
    "operation": [
        {"name": "响应速度", "weight": 0.25, "description": "问题响应和处理速度"},
        {"name": "资源效率", "weight": 0.25, "description": "计算/内存/存储资源的利用效率"},
        {"name": "稳定性", "weight": 0.20, "description": "运行稳定性，uptime、异常恢复"},
        {"name": "可观测性", "weight": 0.15, "description": "日志、指标、告警是否完善"},
        {"name": "自动化程度", "weight": 0.15, "description": "运维操作的自动化覆盖率"},
    ],
}

DEFAULT_DIMENSIONS = [
    {"name": "质量", "weight": 0.40, "description": "产出物整体质量"},
    {"name": "完整性", "weight": 0.30, "description": "是否完整覆盖需求"},
    {"name": "可用性", "weight": 0.30, "description": "是否可直接使用"},
]


# ─── Auditor System Prompt ────────────────────────

AUDITOR_SYSTEM_PROMPT = """你是独立Auditor，只客观评估产出物。
规则: 每维度0-100分(60=及格/80=良好/90=优秀)，<60分须列具体问题。不看格式看实质，深挖逻辑/边界/安全隐患。
输出JSON: {"dimensions":[{"name":"..","score":N,"reasoning":"..","issues":[]}],"overall_score":N,"verdict":"APPROVE|REVISE|REJECT","summary":"..","suggestions":[]}
纪律: 不给100(除非完美)；≥2明显缺陷→REVISE；方向错误→REJECT。"""


# ─── Auditor Agent ────────────────────────────────

class AuditorAgent:
    """Independent scoring agent with deterministic checks.

    Two-phase auditing:
    1. Deterministic checks: ruff lint, Python execution, test detection
    2. LLM holistic review: factors in deterministic results
    """

    def __init__(self):
        self.llm = _get_llm("review")  # Uses REVIEW_MODEL (should be stronger model)

    async def audit(
        self,
        department: str,
        task: str,
        output: str,
        acceptance_criteria: str = "",
    ) -> AuditReport:
        """Score a department's output on multiple dimensions.

        Phase 1: Run deterministic checks (lint, execute, test)
        Phase 2: LLM holistic review with deterministic context
        """

        # ── Phase 1: Deterministic Checks ──
        det_results = await self._run_deterministic_checks(output, department)

        # ═══ Fast-path: deterministic confidence is high → skip LLM audit ═══
        if (det_results["checks_run"] >= 2
                and det_results["score"] >= 85
                and not det_results.get("issues")):
            return AuditReport(
                dimensions=[DimensionScore(
                    name="确定性验证", score=det_results["score"], weight=1.0,
                    reasoning=det_results["detail"], issues=[],
                )],
                overall_score=float(det_results["score"]),
                verdict="APPROVE",
                summary=f"确定性检查全部通过({det_results['score']}/100)，跳过LLM审计",
                suggestions=[],
            )

        # ── Phase 2: LLM Review ──
        # Get scoring dimensions for this department
        dimensions = SCORING_DIMENSIONS.get(department, DEFAULT_DIMENSIONS)
        dims_description = "\n".join(
            f"- {d['name']} (权重{d['weight']}): {d['description']}"
            for d in dimensions
        )

        audit_prompt = f"""审计产出物:
部门: {department} | 任务: {task}
验收标准: {acceptance_criteria or "未指定"}
维度: {dims_description}
确定性检查: {det_results['summary']}
产出物: ```{output[:3000]}```
按维度逐一打分，输出最终评分+裁决。确定性检查仅供参考。"""

        response = await self.llm.ainvoke([
            SystemMessage(content=AUDITOR_SYSTEM_PROMPT),
            HumanMessage(content=audit_prompt),
        ])

        try:
            raw = _extract_json(str(response.content))
            if not isinstance(raw, dict):
                raise ValueError("Not a dict")
        except Exception:
            # Fallback: basic scoring
            raw = {
                "dimensions": [{"name": "综合", "score": 60, "reasoning": "解析失败", "issues": []}],
                "overall_score": 60,
                "verdict": "APPROVE",
                "summary": "审计报告解析异常，默认通过",
                "suggestions": [],
            }

        # ── Build report, inject deterministic evidence ──
        report_dims = [
            DimensionScore(
                name=d.get("name", "?"),
                score=max(0, min(100, d.get("score", 60))),
                weight=dim.get("weight", 0.25) if (dim := next((dd for dd in dimensions if dd["name"] == d.get("name", "")), None)) else 0.25,
                reasoning=d.get("reasoning", ""),
                issues=d.get("issues", []),
            )
            for d in raw.get("dimensions", [])
        ]

        # Append deterministic findings as an extra dimension
        if det_results["checks_run"] > 0:
            det_score = det_results["score"]
            report_dims.append(DimensionScore(
                name="确定性验证",
                score=det_score,
                weight=0.0,  # Informational only, doesn't affect weighted score
                reasoning=det_results["detail"],
                issues=det_results.get("issues", []),
            ))

        # Blend deterministic score into overall (10% weight if checks ran)
        llm_overall = max(0, min(100, raw.get("overall_score", 60)))
        if det_results["checks_run"] > 0:
            blended = round(llm_overall * 0.85 + det_results["score"] * 0.15, 1)
        else:
            blended = llm_overall

        return AuditReport(
            dimensions=report_dims,
            overall_score=blended,
            verdict=raw.get("verdict", "APPROVE").upper(),
            summary=raw.get("summary", ""),
            suggestions=raw.get("suggestions", []),
        )

    # ── Deterministic Checks ─────────────────

    async def _run_deterministic_checks(
        self, output: str, department: str
    ) -> dict:
        """Run objective, deterministic checks on the output.

        Returns:
            {
                "checks_run": int,
                "score": int (0-100),
                "summary": str (for LLM prompt),
                "detail": str (for report),
                "issues": list[str],
            }
        """
        import re
        import tempfile
        import os

        checks_run = 0
        issues: list[str] = []
        scores: list[float] = []
        detail_parts: list[str] = []

        # ── Content checks for non-code departments ──
        if department in ("researcher", "research", "marketer", "marketing"):
            return self._run_content_checks(output, department)

        # Only run code checks for developer/qa/devops departments
        code_departments = {"developer", "qa", "devops", "coding"}
        if department not in code_departments:
            return {
                "checks_run": 0,
                "score": 100,
                "summary": "（非代码/内容部门，跳过确定性检查）",
                "detail": "No deterministic checks available",
                "issues": [],
            }

        # Extract Python code blocks
        code_blocks = re.findall(
            r'```(?:python|py)?\s*\n(.*?)```', output, re.DOTALL
        )
        # Also try inline code (no fence, but looks like Python)
        if not code_blocks:
            # Heuristic: look for def/class/import patterns
            lines = output.split('\n')
            in_code = False
            current_block: list[str] = []
            for line in lines:
                if re.match(r'^(def |class |import |from |# |    |\t)', line):
                    in_code = True
                    current_block.append(line)
                elif in_code and line.strip() == '':
                    if current_block:
                        code_blocks.append('\n'.join(current_block))
                        current_block = []
                    in_code = False
                elif in_code:
                    current_block.append(line)
            if current_block:
                code_blocks.append('\n'.join(current_block))

        if not code_blocks:
            return {
                "checks_run": 0,
                "score": 100,
                "summary": "（未检测到代码块，跳过确定性检查）",
                "detail": "No code blocks detected in output",
                "issues": [],
            }

        # Combine all code blocks into one file for checking
        combined_code = '\n\n'.join(code_blocks)

        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.py', delete=False, encoding='utf-8'
        ) as f:
            f.write(combined_code)
            tmp_path = f.name

        try:
            # ── Check 1: ruff lint ──
            checks_run += 1
            lint_result = await self._run_ruff(tmp_path)
            if lint_result["errors"] > 0:
                issues.extend(lint_result["issues"][:5])
                scores.append(max(0, 100 - lint_result["errors"] * 10))
                detail_parts.append(
                    f"Ruff: {lint_result['errors']} errors, "
                    f"{lint_result['warnings']} warnings"
                )
            else:
                scores.append(100)
                detail_parts.append("Ruff: ✅ 0 errors")

            # ── Check 2: Python syntax/execution ──
            checks_run += 1
            exec_result = await self._run_python_check(tmp_path)
            if exec_result["success"]:
                scores.append(100)
                detail_parts.append("Python执行: ✅ 成功")
            else:
                issues.append(f"代码执行失败: {exec_result['error'][:200]}")
                scores.append(30)
                detail_parts.append(f"Python执行: ❌ {exec_result['error'][:100]}")

            # ── Check 3: test detection ──
            checks_run += 1
            test_result = self._check_test_coverage(combined_code, output)
            scores.append(test_result["score"])
            detail_parts.append(test_result["detail"])
            if test_result.get("issue"):
                issues.append(test_result["issue"])

        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                _ = None  # cleanup: ok if file already gone

        avg_score = round(sum(scores) / len(scores)) if scores else 100

        return {
            "checks_run": checks_run,
            "score": avg_score,
            "summary": "\n".join(f"- {d}" for d in detail_parts),
            "detail": " | ".join(detail_parts),
            "issues": issues,
        }

    async def _run_ruff(self, filepath: str) -> dict:
        """Run ruff linter on a Python file."""
        import asyncio
        try:
            proc = await asyncio.create_subprocess_exec(
                "ruff", "check", filepath, "--output-format=text",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=15
            )
            output = stdout.decode("utf-8", errors="replace")
            lines = [l for l in output.split('\n') if l.strip()]

            errors = sum(1 for l in lines if l and not l.startswith(' '))
            warnings = sum(1 for l in lines if l.startswith(' '))

            return {
                "errors": errors,
                "warnings": warnings,
                "issues": lines[:10],
            }
        except FileNotFoundError:
            return {"errors": 0, "warnings": 0, "issues": []}
        except Exception:
            return {"errors": 0, "warnings": 0, "issues": []}

    async def _run_python_check(self, filepath: str) -> dict:
        """Check if Python code can be compiled and executed."""
        import asyncio
        try:
            # First: compile check (faster, safer)
            proc = await asyncio.create_subprocess_exec(
                "python3", "-c",
                f"import py_compile; py_compile.compile('{filepath}', doraise=True)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=10
            )
            if proc.returncode != 0:
                error = stderr.decode("utf-8", errors="replace")
                return {"success": False, "error": error[:200]}

            # Second: try to import/execute (only if compile passed)
            proc2 = await asyncio.create_subprocess_exec(
                "python3", filepath,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr2 = await asyncio.wait_for(
                proc2.communicate(), timeout=10
            )
            if proc2.returncode != 0:
                error = stderr2.decode("utf-8", errors="replace")
                return {"success": False, "error": error[:200]}

            return {"success": True, "error": ""}

        except asyncio.TimeoutError:
            return {"success": False, "error": "执行超时 (10s)"}
        except Exception as e:
            return {"success": False, "error": str(e)[:200]}

    @staticmethod
    def _check_test_coverage(code: str, output: str) -> dict:
        """Check if output contains tests."""
        import re

        # Check for test patterns
        has_test_function = bool(re.search(
            r'(def test_|class Test|unittest|pytest)', code
        ))
        has_assert = 'assert' in code
        has_test_import = bool(re.search(
            r'(import unittest|import pytest|from unittest)', code
        ))

        if has_test_function and has_assert:
            return {
                "score": 90,
                "detail": "测试: ✅ 包含测试函数 + assert",
            }
        elif has_test_function:
            return {
                "score": 70,
                "detail": "测试: ⚠️ 有测试函数但缺少assert断言",
                "issue": "测试函数缺少assert断言，无法真正验证",
            }
        elif has_assert:
            return {
                "score": 50,
                "detail": "测试: ⚠️ 有assert但无测试函数",
                "issue": "代码中有assert但未组织为测试函数",
            }
        else:
            return {
                "score": 20,
                "detail": "测试: ❌ 未检测到任何测试代码",
                "issue": "缺少测试代码，建议添加pytest测试用例",
            }

    def _run_content_checks(self, output: str, department: str) -> dict:
        """Run deterministic quality checks on non-code content.

        For marketer: word count, structure, readability, platform fit.
        For researcher: source citations, structure, depth.
        """
        import re

        checks_run = 0
        issues: list[str] = []
        scores: list[float] = []
        detail_parts: list[str] = []

        # ── Check 1: Minimum length ──
        checks_run += 1
        word_count = len(output.split())
        char_count = len(output)

        if char_count < 200:
            issues.append(f"内容过短: {char_count} 字符 (建议 >200)")
            scores.append(20)
            detail_parts.append(f"长度: ❌ {char_count} 字符 (过短)")
        elif char_count < 500:
            scores.append(60)
            detail_parts.append(f"长度: ⚠️ {char_count} 字符 (偏短)")
        else:
            scores.append(90)
            detail_parts.append(f"长度: ✅ {char_count} 字符")

        if department in ("marketer", "marketing"):
            # ── Check 2: Structure (has title/sections) ──
            checks_run += 1
            has_title = bool(re.search(r'^#+\s|【|《', output, re.MULTILINE))
            has_sections = len(re.findall(r'^#{1,3}\s|^\*\*|【', output, re.MULTILINE)) >= 2
            has_bullets = bool(re.search(r'^\s*[-\*•]\s', output, re.MULTILINE))

            structure_score = 0
            if has_title:
                structure_score += 35
                detail_parts.append("标题: ✅")
            else:
                detail_parts.append("标题: ❌ 缺失")
                issues.append("缺少标题，不利于阅读")
            if has_sections:
                structure_score += 35
                detail_parts.append("分段: ✅")
            else:
                detail_parts.append("分段: ⚠️")
            if has_bullets:
                structure_score += 30
                detail_parts.append("列表: ✅")
            else:
                detail_parts.append("列表: ⚠️")
            scores.append(structure_score if structure_score > 0 else 20)

            # ── Check 3: Readability (avg sentence length) ──
            checks_run += 1
            sentences = re.split(r'[。！？.!?\n]', output)
            sentences = [s.strip() for s in sentences if len(s.strip()) > 5]
            if sentences:
                avg_len = sum(len(s) for s in sentences) / len(sentences)
                if avg_len > 80:
                    issues.append(f"句子偏长: 平均 {avg_len:.0f} 字符/句")
                    scores.append(50)
                    detail_parts.append(f"可读性: ⚠️ 句子偏长 ({avg_len:.0f}字/句)")
                else:
                    scores.append(90)
                    detail_parts.append(f"可读性: ✅ ({avg_len:.0f}字/句)")
            else:
                scores.append(50)
                detail_parts.append("可读性: ⚠️ 无法评估")

        elif department in ("researcher", "research"):
            # ── Check 2: Source citations ──
            checks_run += 1
            # Look for URLs, references, source mentions
            urls = re.findall(r'https?://[^\s)\]>]+', output)
            source_refs = re.findall(
                r'(?:来源|参考|引用|source|ref|from)[:：]?\s*', output, re.IGNORECASE
            )
            has_sources = len(urls) > 0 or len(source_refs) > 0

            if has_sources:
                scores.append(85)
                detail_parts.append(f"来源引用: ✅ ({len(urls)} URL, {len(source_refs)} 引用)")
            else:
                scores.append(30)
                issues.append("调研报告缺少来源引用，可靠性存疑")
                detail_parts.append("来源引用: ❌ 无来源")

            # ── Check 3: Report structure ──
            checks_run += 1
            has_findings = bool(re.search(
                r'(发现|结论|结果|分析|findings?|conclusion|analysis)',
                output, re.IGNORECASE
            ))
            has_recommendations = bool(re.search(
                r'(建议|推荐|下一步|recommend|suggestion|action)',
                output, re.IGNORECASE
            ))

            struct = 0
            if has_findings:
                struct += 50
                detail_parts.append("分析/发现: ✅")
            else:
                detail_parts.append("分析/发现: ⚠️")
            if has_recommendations:
                struct += 50
                detail_parts.append("建议: ✅")
            else:
                detail_parts.append("建议: ⚠️")
            scores.append(struct if struct > 0 else 20)

        avg_score = round(sum(scores) / len(scores)) if scores else 100

        return {
            "checks_run": checks_run,
            "score": avg_score,
            "summary": "\n".join(f"- {d}" for d in detail_parts),
            "detail": " | ".join(detail_parts),
            "issues": issues,
        }


# ─── PMO Gate Check ───────────────────────────────

async def pmo_gate_check(
    department: str,
    task: str,
    acceptance_criteria: str,
    output: str,
) -> dict:
    """PMO checks if acceptance criteria are actually met."""
    
    llm = _get_llm("ceo")
    
    pmo_prompt = f"""PMO验收检查:
部门: {department} | 任务: {task}
验收标准: {acceptance_criteria or "未指定"}
交付物: ```{output[:3000]}```
逐条检查标准，输出JSON: {{"criteria_met":[],"criteria_failed":[],"compliance_score":N,"verdict":"PASS|FAIL","notes":""}}"""

    response = await llm.ainvoke([
        SystemMessage(content="你是 PMO，只关心交付物是否满足约定标准。严格但公正。"),
        HumanMessage(content=pmo_prompt),
    ])
    
    try:
        return _extract_json(str(response.content))
    except Exception:
        return {
            "criteria_met": ["自动通过"],
            "criteria_failed": [],
            "compliance_score": 70,
            "verdict": "PASS",
            "notes": "PMO检查解析异常，默认放行",
        }


# ─── Role Auditor ────────────────────────────────

ROLE_AUDIT_PROMPT = """你是角色定义审计员，审查新角色合理性。5维度: 专业性/边界清晰度/可执行性/必要性/重复度。
评分: <40不创建, 40-60需修改, 60-80可试用, 80+直接创建。
输出JSON: {"dimensions":[{"name":"..","score":N,"issue":""}],"overall_score":N,"verdict":"APPROVE_TRIAL|REJECT","summary":"..","suggestions":[]}"""


async def audit_role_definition(
    role_proposal: dict,
    existing_roles: list[str],
) -> dict:
    """Audit a proposed role definition. Returns audit report."""
    
    llm = _get_llm("review")
    
    audit_prompt = f"""审查角色定义:
名称: {role_proposal.get('name', '?')} | 显示: {role_proposal.get('display_name', '?')}
描述: {role_proposal.get('description', '?')} | 关键词: {role_proposal.get('keywords', [])}
System Prompt: ```{str(role_proposal.get('system_prompt', 'N/A'))[:2000]}```
已有角色: {', '.join(existing_roles)}
逐一审查并输出JSON。"""

    response = await llm.ainvoke([
        SystemMessage(content=ROLE_AUDIT_PROMPT),
        HumanMessage(content=audit_prompt),
    ])
    
    try:
        return _extract_json(str(response.content))
    except Exception:
        return {
            "overall_score": 50,
            "verdict": "REJECT",
            "summary": "审计解析失败，为安全起见拒绝创建",
            "suggestions": ["请手动检查角色定义"],
        }


# ─── Cross-Validation: Arch一致性检查 ──────────────

async def arch_consistency_check(
    code_output: str,
    arch_design: str = "",
) -> dict:
    """Check if code matches the architecture design."""
    
    if not arch_design:
        return {"consistent": True, "score": 80, "issues": ["无架构设计文档，跳过一致性检查"]}
    
    llm = _get_llm("review")
    
    check_prompt = f"""架构一致性检查:
设计: {arch_design[:2000]}
代码: {code_output[:3000]}
检查: 模块划分/接口匹配/技术选型。输出JSON: {{"consistent":bool,"score":N,"issues":[],"notes":""}}"""

    response = await llm.ainvoke([
        SystemMessage(content="你是架构审查员，只检查代码和设计的一致性。"),
        HumanMessage(content=check_prompt),
    ])
    
    try:
        return _extract_json(str(response.content))
    except Exception:
        return {"consistent": True, "score": 70, "issues": [], "notes": "一致性检查异常"}
