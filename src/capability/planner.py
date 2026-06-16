"""Capability Planner — LLM-driven capability reasoning.

Instead of fixing role → capability mapping, the CEO now reasons about
what capabilities a task needs. This is the core of "通用能力" —
the system adapts to tasks it has never seen before.

Architecture:
    Task → CapabilityPlanner.analyze(task) → {capability, tools, role_hint}
    → Dynamic capability assignment to agent

Example:
    "分析SpaceX供应链" → [research, file_io] → Researcher
    "写Kuberentes Operator" → [coding, filesystem, vcs, research] → Developer
    "东京寿司店推荐" → [research, file_io] → Researcher
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("ai_company.capability")


@dataclass
class CapabilityPlan:
    """Result of capability analysis for a task."""
    capabilities: list[str]       # Capability names: ["research", "file_io", "coding"]
    reasoning: str                # Why these capabilities?
    role_hint: str                # Best department: "researcher", "developer", etc.
    confidence: float = 0.0       # How confident is the planner? (0-1)


# ─── Static fallback: task patterns → capability mapping ───
# Used when LLM is unavailable or for fast-path routing.
# These patterns cover common task types.

PATTERN_CAPABILITY_MAP = [
    # ── Command execution (HIGHEST priority — never use code pipeline) ──
    {
        "keywords": ["pwd", "ls", "cd", "cat", "echo", "mkdir", "rm ", "cp ",
                     "grep", "find", "head", "tail", "wc", "chmod",
                     "curl", "wget", "ssh", "scp", "ping", "whoami",
                     "date", "uptime", "ps ", "kill", "df ", "du ",
                     "env", "export", "which", "who ", "uname",
                     "执行", "运行", "run ", "execute"],
        "capabilities": ["file_io", "research"],  # minimal — just need to run+report
        "role_hint": "developer",
        "reasoning": "Shell command execution — run and report output, no code needed",
    },
    # Research & fact-finding
    {
        "keywords": ["查", "搜索", "搜", "找", "什么是", "怎么", "为什么",
                     "天气", "股价", "股票", "金价", "新闻", "价格",
                     "出生", "生日", "最新", "今天", "昨天"],
        "capabilities": ["research", "file_io"],
        "role_hint": "researcher",
        "reasoning": "Simple fact-finding or lookup task",
    },
    # Code development
    {
        "keywords": ["写代码", "开发", "实现", "函数", "class", "API",
                     "bug", "修复", "fix", "重构", "refactor",
                     "写.*程序", "编程", "代码"],
        "capabilities": ["coding", "filesystem", "vcs", "research"],
        "role_hint": "developer",
        "reasoning": "Code development or modification task",
    },
    # Code review
    {
        "keywords": ["审查", "review", "检查代码", "代码质量", "评分",
                     "审计", "安全审查"],
        "capabilities": ["coding", "research"],
        "role_hint": "developer",
        "reasoning": "Code review or audit task",
    },
    # Document/PDF generation
    {
        "keywords": ["生成.*pdf", "导出.*pdf", "生成.*文档", "写报告",
                     "生成报告", "写文档", "周报", "月报", "日报",
                     "会议纪要", "写总结", "生成.*文件"],
        "capabilities": ["coding", "research", "filesystem"],
        "role_hint": "developer",
        "reasoning": "Document/PDF generation task",
    },
    # Testing
    {
        "keywords": ["测试", "单测", "集成测试", "e2e", "覆盖率",
                     "pytest", "jest", "test case"],
        "capabilities": ["coding", "research"],
        "role_hint": "qa",
        "reasoning": "Software testing task",
    },
    # DevOps / deployment
    {
        "keywords": ["部署", "deploy", "docker", "k8s", "CI/CD",
                     "服务器", "nginx", "监控", "pipeline"],
        "capabilities": ["filesystem", "research"],
        "role_hint": "devops",
        "reasoning": "DevOps/deployment task",
    },
    # Market / content
    {
        "keywords": ["文案", "推广", "SEO", "广告", "社交媒体",
                     "营销", "公众号", "content", "marketing"],
        "capabilities": ["research", "file_io", "market_data"],
        "role_hint": "marketer",
        "reasoning": "Marketing/content creation task",
    },
]


class CapabilityPlanner:
    """Analyze tasks and determine required capabilities.

    Two modes:
    1. Pattern match (fast, no LLM) — for common task types
    2. LLM reasoning (slower, flexible) — for novel or ambiguous tasks
    """

    def __init__(self, llm=None):
        """Initialize with an optional LLM for deep reasoning."""
        self._llm = llm

    def analyze(self, task: str, available_capabilities: list[str] = None) -> CapabilityPlan:
        """Analyze a task and return required capabilities.

        Args:
            task: The user's task description
            available_capabilities: Optional list of available capability names.
                                    If None, uses all capabilities from registry.

        Returns:
            CapabilityPlan with capabilities, role_hint, and reasoning.
        """
        import re

        task_lower = task.lower()

        # ── Fast-path: pattern matching ──
        for pattern in PATTERN_CAPABILITY_MAP:
            for kw in pattern["keywords"]:
                if re.search(kw, task_lower):
                    caps = pattern["capabilities"]
                    if available_capabilities:
                        caps = [c for c in caps if c in available_capabilities]
                    return CapabilityPlan(
                        capabilities=caps,
                        role_hint=pattern["role_hint"],
                        reasoning=pattern["reasoning"],
                        confidence=0.7,
                    )

        # ── Fallback: generic research ──
        return CapabilityPlan(
            capabilities=["research", "file_io"],
            role_hint="researcher",
            reasoning="No pattern matched — defaulting to research",
            confidence=0.3,
        )

    async def analyze_with_llm(self, task: str,
                                available_capabilities: list[str] = None) -> CapabilityPlan:
        """Use LLM to deeply reason about what capabilities a task needs.

        This is the "真正通用" path — the LLM considers the task holistically
        and suggests capabilities, not just keyword matches.

        Args:
            task: The user's task description
            available_capabilities: List of available capability names

        Returns:
            CapabilityPlan with LLM-generated reasoning.
        """
        from src.departments.agents import CAPABILITIES, ROLE_CAPABILITIES

        # Build capability descriptions for the LLM
        cap_desc = []
        for name, cap in CAPABILITIES.items():
            if available_capabilities and name not in available_capabilities:
                continue
            roles_using = [r for r, caps in ROLE_CAPABILITIES.items()
                          if name in caps and r != "_default"]
            cap_desc.append(
                f"- **{name}** ({cap.display}): {cap.description}\n"
                f"  Tools: {', '.join(cap.tools)}\n"
                f"  Used by: {', '.join(roles_using) if roles_using else 'any'}"
            )

        prompt = f"""Analyze this task and determine what capabilities are needed.

Task: {task}

Available capabilities:
{chr(10).join(cap_desc)}

Respond with JSON:
{{
  "capabilities": ["cap1", "cap2", ...],
  "role_hint": "best_role_name",
  "reasoning": "Why these capabilities are needed"
}}

Rules:
1. Choose MINIMAL capabilities — only what's strictly needed
2. research (web_search, web_fetch) is needed for any task requiring external info
3. coding (run_python, lint_code, etc.) only for tasks that need code execution
4. file_io (read_file, write_file) for tasks that need to read/write files
5. filesystem (read_file, write_file, list_dir) for tasks that need directory browsing
6. vcs (git_commit) only for tasks that need version control
7. market_data only for financial data queries
8. role_hint should be the best department: researcher/developer/qa/devops/marketer

Example:
Task: "写一个Python脚本抓取网页"
→ {{"capabilities": ["coding", "filesystem", "research"], "role_hint": "developer", "reasoning": "Needs coding to write the script, filesystem to save it, research to understand web scraping"}}

Task: "查一下明天北京天气"
→ {{"capabilities": ["research", "file_io"], "role_hint": "researcher", "reasoning": "Simple fact-finding: search for weather data"}}
"""
        if not self._llm:
            return self.analyze(task, available_capabilities)

        try:
            from langchain_core.messages import HumanMessage
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            result = _extract_json(str(response.content))

            caps = result.get("capabilities", ["research", "file_io"])
            if available_capabilities:
                caps = [c for c in caps if c in available_capabilities]

            return CapabilityPlan(
                capabilities=caps,
                role_hint=result.get("role_hint", "researcher"),
                reasoning=result.get("reasoning", "LLM analysis"),
                confidence=0.8,
            )
        except Exception:
            logger.debug("LLM capability analysis failed, using pattern fallback")
            return self.analyze(task, available_capabilities)


# ─── Tool selection from capabilities ───

def resolve_tools(capabilities: list[str]) -> list[str]:
    """Given a list of capability names, return the full tool list.

    Deduplicates tools that appear in multiple capabilities.
    """
    from src.departments.agents import CAPABILITIES
    tools = []
    seen = set()
    for cn in capabilities:
        cap = CAPABILITIES.get(cn)
        if cap:
            for t in cap.tools:
                if t not in seen:
                    tools.append(t)
                    seen.add(t)
    return tools


# ─── JSON extraction (inline to avoid circular imports) ───

def _extract_json(text: str) -> dict:
    """Extract JSON from LLM output, handling markdown fences."""
    import re
    fence_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find the first { ... }
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}
