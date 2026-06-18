"""Dynamic Workflow Planner — generates task-specific agent pipelines at runtime.

Instead of a hardcoded graph, the planner:
1. Receives intent classification (IntentResult) + available Capabilities
2. Generates a WorkflowPlan with ordered steps, dependency DAG, and audit policy
3. Supports fast-path (intent template matching, 0 LLM cost)
4. Supports LLM generation (for complex/novel patterns)
5. Provides backward compatibility with legacy task_type routing

V5 Architecture — P1.1 Enhanced.
"""

import json
import logging
import re
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger("ai_company.workflow")


# ═══════════════════════════════════════════════════════════════════
# Data Classes (P1.1 Enhanced)
# ═══════════════════════════════════════════════════════════════════

@dataclass
class WorkflowStep:
    """A single step in a workflow plan.

    Attributes:
        name: Human-readable step name, e.g. "ShellExec", "LocateWechat".
        agent: The agent responsible for executing this step.
        action: Action type, e.g. "shell_exec", "web_search", "wechat_send".
        params: Parameters for this step (may include user_request, etc.).
        depends_on: Names of steps that must complete before this one.
        verify: Whether this step requires its own verification gate.
    """
    name: str
    agent: str
    action: str
    params: dict = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    verify: bool = False


@dataclass
class WorkflowPlan:
    """Complete workflow execution plan.

    Attributes:
        steps: Ordered list of WorkflowStep.
        skip_audit: If True, the Auditor/PMO nodes are bypassed entirely.
        estimated_time_s: Rough time estimate in seconds.
        reasoning: Human-readable rationale for the plan.
    """
    steps: list[WorkflowStep] = field(default_factory=list)
    skip_audit: bool = False
    estimated_time_s: float = 5.0
    reasoning: str = ""

    # ── Backward-compat aliases for old WorkflowPlan interface ──
    @property
    def task(self) -> str:
        """Backward compat: return first step params['task'] or empty."""
        if self.steps and self.steps[0].params:
            return self.steps[0].params.get("task", "")
        return ""

    @property
    def nodes(self) -> list:
        """Backward compat: expose steps as old-style WorkflowNode list."""
        # Import here to avoid circular import
        from src.workflow.planner import WorkflowNode as _OldNode
        return [
            _OldNode(name=s.name, agent=s.agent, capabilities=[s.action], category="action")
            for s in self.steps
        ]

    @property
    def reviewers(self) -> list[str]:
        """Backward compat: reviewers are embedded in verify steps."""
        return [s.action for s in self.steps if s.verify]


# Old dataclass kept for backward compatibility
@dataclass
class WorkflowNode:
    """Legacy workflow node — kept for backward compatibility."""
    name: str
    agent: str
    capabilities: list[str] = field(default_factory=list)
    category: str = "action"


# ═══════════════════════════════════════════════════════════════════
# Intent → Workflow Template Mapping (Fast-Path, 0 LLM cost)
# ═══════════════════════════════════════════════════════════════════

# Each template is a function (intent_result, capabilities, task_text) → WorkflowPlan
# Intents that skip audit: COMMAND, SEARCH, SOCIAL, GENERAL_CHAT, MEMORY, SYSTEM

INTENT_TEMPLATES: dict[str, dict] = {
    "COMMAND": {
        "steps": [
            {"name": "ShellExec", "agent": "devops", "action": "shell_exec", "verify": False},
            {"name": "Verify", "agent": "devops", "action": "command_verify", "verify": True},
        ],
        "skip_audit": True,
        "estimated_time_s": 3.0,
    },
    "SEARCH": {
        "steps": [
            {"name": "WebSearch", "agent": "researcher", "action": "web_search", "verify": False},
            {"name": "Summarize", "agent": "researcher", "action": "summarize", "verify": False},
            {"name": "Verify", "agent": "researcher", "action": "fact_check", "verify": True},
        ],
        "skip_audit": True,
        "estimated_time_s": 8.0,
    },
    "RESEARCH": {
        "steps": [
            {"name": "WebSearch", "agent": "researcher", "action": "web_search", "verify": False},
            {"name": "WebFetch-1", "agent": "researcher", "action": "web_fetch",
             "depends_on": ["WebSearch"], "verify": False},
            {"name": "WebFetch-2", "agent": "researcher", "action": "web_fetch",
             "depends_on": ["WebSearch"], "verify": False},
            {"name": "Analyze", "agent": "researcher", "action": "analyze",
             "depends_on": ["WebFetch-1", "WebFetch-2"], "verify": False},
            {"name": "Verify", "agent": "researcher", "action": "fact_check",
             "depends_on": ["Analyze"], "verify": True},
        ],
        "skip_audit": False,  # RESEARCH goes through audit for quality
        "estimated_time_s": 25.0,
    },
    "SOCIAL": {
        "steps": [
            {"name": "LocateWechat", "agent": "devops", "action": "vision_analyze", "verify": False},
            {"name": "OpenChat", "agent": "devops", "action": "wechat_open_chat",
             "depends_on": ["LocateWechat"], "verify": False},
            {"name": "SendMessage", "agent": "devops", "action": "wechat_send",
             "depends_on": ["OpenChat"], "verify": False},
            {"name": "VisionVerify", "agent": "devops", "action": "vision_verify",
             "depends_on": ["SendMessage"], "verify": True},
        ],
        "skip_audit": True,
        "estimated_time_s": 15.0,
    },
    "CODING": {
        "steps": [
            {"name": "Requirements", "agent": "developer", "action": "analyze_requirements", "verify": False},
            {"name": "Coding", "agent": "developer", "action": "code_gen",
             "depends_on": ["Requirements"], "verify": False},
            {"name": "Test", "agent": "qa", "action": "run_test",
             "depends_on": ["Coding"], "verify": False},
            {"name": "Review", "agent": "qa", "action": "code_review",
             "depends_on": ["Test"], "verify": False},
            {"name": "Verify", "agent": "qa", "action": "lint_code",
             "depends_on": ["Review"], "verify": True},
        ],
        "skip_audit": False,
        "estimated_time_s": 60.0,
    },
    "CODE_REVIEW": {
        "steps": [
            {"name": "GatherCode", "agent": "developer", "action": "read_file", "verify": False},
            {"name": "AuditCode", "agent": "developer", "action": "code_review",
             "depends_on": ["GatherCode"], "verify": False},
            {"name": "ReportFindings", "agent": "developer", "action": "summarize",
             "depends_on": ["AuditCode"], "verify": True},
        ],
        "skip_audit": False,
        "estimated_time_s": 30.0,
    },
    "CREATIVE": {
        "steps": [
            {"name": "ResearchAudience", "agent": "marketer", "action": "web_search", "verify": False},
            {"name": "DraftContent", "agent": "marketer", "action": "creative_write",
             "depends_on": ["ResearchAudience"], "verify": False},
            {"name": "Verify", "agent": "marketer", "action": "content_review",
             "depends_on": ["DraftContent"], "verify": True},
        ],
        "skip_audit": False,
        "estimated_time_s": 30.0,
    },
    "FILE": {
        "steps": [
            {"name": "GatherContent", "agent": "developer", "action": "gather_content", "verify": False},
            {"name": "GenerateFile", "agent": "developer", "action": "write_file",
             "depends_on": ["GatherContent"], "verify": False},
            {"name": "Verify", "agent": "developer", "action": "file_verify",
             "depends_on": ["GenerateFile"], "verify": True},
        ],
        "skip_audit": True,
        "estimated_time_s": 12.0,
    },
    "SYSTEM": {
        "steps": [
            {"name": "ShellExec", "agent": "devops", "action": "shell_exec", "verify": False},
            {"name": "Verify", "agent": "devops", "action": "system_verify", "verify": True},
        ],
        "skip_audit": True,
        "estimated_time_s": 5.0,
    },
    "MEMORY": {
        "steps": [
            {"name": "MemoryOp", "agent": "developer", "action": "memory_operation", "verify": False},
            {"name": "Verify", "agent": "developer", "action": "memory_verify", "verify": True},
        ],
        "skip_audit": True,
        "estimated_time_s": 3.0,
    },
    "VISION": {
        "steps": [
            {"name": "Capture", "agent": "devops", "action": "screen_capture", "verify": False},
            {"name": "Analyze", "agent": "devops", "action": "vision_analyze",
             "depends_on": ["Capture"], "verify": False},
            {"name": "Verify", "agent": "devops", "action": "vision_verify",
             "depends_on": ["Analyze"], "verify": True},
        ],
        "skip_audit": True,
        "estimated_time_s": 10.0,
    },
    "AUTOMATION": {
        "steps": [
            {"name": "PlanAutomation", "agent": "devops", "action": "plan_automation", "verify": False},
            {"name": "ExecuteAutomation", "agent": "devops", "action": "shell_exec",
             "depends_on": ["PlanAutomation"], "verify": False},
            {"name": "Verify", "agent": "devops", "action": "automation_verify",
             "depends_on": ["ExecuteAutomation"], "verify": True},
        ],
        "skip_audit": False,
        "estimated_time_s": 20.0,
    },
    "GENERAL_CHAT": {
        "steps": [
            {"name": "Chat", "agent": "ceo", "action": "chat", "verify": False},
        ],
        "skip_audit": True,
        "estimated_time_s": 2.0,
    },
}

# Default template for unknown intents
DEFAULT_TEMPLATE: dict = {
    "steps": [
        {"name": "CapabilityDispatch", "agent": "developer", "action": "dispatch", "verify": False},
        {"name": "Verify", "agent": "developer", "action": "generic_verify", "verify": True},
    ],
    "skip_audit": False,
    "estimated_time_s": 10.0,
}


# ═══════════════════════════════════════════════════════════════════
# Agent lookup by action type
# ═══════════════════════════════════════════════════════════════════

_ACTION_AGENT_MAP: dict[str, str] = {
    "shell_exec": "devops",
    "command_verify": "devops",
    "system_verify": "devops",
    "web_search": "researcher",
    "web_fetch": "researcher",
    "summarize": "researcher",
    "analyze": "researcher",
    "fact_check": "researcher",
    "vision_analyze": "devops",
    "screen_capture": "devops",
    "vision_verify": "devops",
    "wechat_send": "devops",
    "wechat_open_chat": "devops",
    "code_gen": "developer",
    "analyze_requirements": "developer",
    "read_file": "developer",
    "write_file": "developer",
    "gather_content": "developer",
    "file_verify": "developer",
    "code_review": "qa",
    "lint_code": "qa",
    "run_test": "qa",
    "creative_write": "marketer",
    "content_review": "marketer",
    "memory_operation": "developer",
    "memory_verify": "developer",
    "chat": "ceo",
    "dispatch": "developer",
    "generic_verify": "developer",
    "plan_automation": "devops",
    "automation_verify": "devops",
}


def _get_agent_for_action(action: str, capabilities: list = None) -> str:
    """Resolve agent name for an action, optionally from capabilities."""
    if capabilities:
        for cap in capabilities:
            if action in cap.tools:
                return cap.agent
    return _ACTION_AGENT_MAP.get(action, "developer")


# ═══════════════════════════════════════════════════════════════════
# WorkflowPlanner (P1.1 Enhanced)
# ═══════════════════════════════════════════════════════════════════

class WorkflowPlanner:
    """Dynamic workflow planner with intent-template fast-path + LLM fallback.

    New interface (V5 P1.1):
        plan(intent: IntentResult, capabilities: list[Capability]) → WorkflowPlan

    Backward-compatible interface:
        plan(task: str) → WorkflowPlan  (old WorkflowPlan with .nodes/.task/.reviewers)
    """

    def __init__(self):
        from src.capability.registry import get_capability_registry
        self.registry = get_capability_registry()

    # ── New P1.1 Interface ────────────────────────────────────

    def plan(self, intent=None, capabilities=None, task: str = "") -> WorkflowPlan:
        """Generate a workflow plan.

        Args:
            intent: IntentResult from IntentRouter (new interface).
            capabilities: List of Capability objects (new interface).
            task: Raw task string (backward-compat interface).

        Returns:
            WorkflowPlan with steps, skip_audit, estimated_time_s, reasoning.

        When called with just `task` (backward compat), uses keyword fast-path.
        When called with `intent` + `capabilities`, uses intent template fast-path.
        Falls back to LLM for complex/unknown patterns in either case.
        """
        # Determine which interface is being used
        if intent is not None and capabilities is not None:
            return self._plan_from_intent(intent, capabilities, task)
        elif task:
            return self._plan_from_task(task)
        else:
            # Default: simple chat
            return WorkflowPlan(
                steps=[WorkflowStep("Chat", "ceo", "chat")],
                skip_audit=True,
                estimated_time_s=2.0,
                reasoning="Default plan: no intent or task provided",
            )

    def _plan_from_intent(self, intent, capabilities, task: str) -> WorkflowPlan:
        """Generate plan from IntentResult + Capability list."""
        intent_name = intent.intent.upper() if hasattr(intent, 'intent') else str(intent).upper()

        # Fast-path: intent template matching
        plan = self._fast_path_intent(intent_name, capabilities, task)
        if plan:
            return plan

        # LLM fallback
        return self._llm_plan(intent_name, capabilities, task)

    def _plan_from_task(self, task: str) -> WorkflowPlan:
        """Generate plan from raw task string (backward compat)."""
        # Fast-path: keyword matching (old behavior)
        plan = self._fast_path_keyword(task)
        if plan:
            return plan

        # LLM fallback
        return self._llm_plan("UNKNOWN", [], task)

    # ── Fast-Path: Intent Template ────────────────────────────

    def _fast_path_intent(self, intent_name: str, capabilities: list, task: str) -> Optional[WorkflowPlan]:
        """Intent-based template fast-path — 0 LLM cost."""
        template = INTENT_TEMPLATES.get(intent_name, DEFAULT_TEMPLATE)
        steps = self._build_steps(template["steps"], capabilities, task)
        return WorkflowPlan(
            steps=steps,
            skip_audit=template.get("skip_audit", False),
            estimated_time_s=template.get("estimated_time_s", 10.0),
            reasoning=f"Intent template: {intent_name} → {len(steps)} steps ({'skip audit' if template.get('skip_audit') else 'with audit'})",
        )

    def _build_steps(self, step_specs: list[dict], capabilities: list, task: str) -> list[WorkflowStep]:
        """Build WorkflowStep list from template specs."""
        steps = []
        for spec in step_specs:
            action = spec["action"]
            agent = _get_agent_for_action(action, capabilities)

            step = WorkflowStep(
                name=spec["name"],
                agent=agent,
                action=action,
                params={"task": task},
                depends_on=list(spec.get("depends_on", [])),
                verify=spec.get("verify", False),
            )
            steps.append(step)
        return steps

    # ── Fast-Path: Keyword Matching (backward compat) ─────────

    def _fast_path_keyword(self, task: str) -> Optional[WorkflowPlan]:
        """Keyword-based workflow generation — backward compatible.

        Returns new-style WorkflowPlan with steps, not old nodes.
        """
        task_lower = task.lower()

        # WeChat send flow
        if re.search(r'微信.*(?:发送|发消息|给.*发)', task_lower):
            return WorkflowPlan(
                steps=[
                    WorkflowStep("VisionCheck", "devops", "vision_analyze"),
                    WorkflowStep("WechatAction", "devops", "wechat_send",
                                 depends_on=["VisionCheck"]),
                    WorkflowStep("SendVerify", "devops", "vision_verify",
                                 depends_on=["WechatAction"], verify=True),
                ],
                skip_audit=True,
                estimated_time_s=15.0,
                reasoning="WeChat send: Observe → Act → Verify closed loop",
            )

        # Local system check
        if re.search(r'检测.*(?:本地|运行|软件|进程)|打开.*(?:微信|应用)|pgrep|osascript', task_lower):
            return WorkflowPlan(
                steps=[
                    WorkflowStep("ScreenCapture", "devops", "screen_capture"),
                    WorkflowStep("ShellExec", "devops", "shell_exec",
                                 depends_on=["ScreenCapture"]),
                ],
                skip_audit=True,
                estimated_time_s=8.0,
                reasoning="Local system: capture + execute shell commands",
            )

        # Coding task
        if re.search(r'写.{0,10}(?:代码|程序|脚本|api|函数|类|模块|组件|页面|应用|工具|网站)|开发|实现|修复|bug\b|fix\b|refactor|重构|implement|create.*(?:api|function|class)|build', task_lower):
            return WorkflowPlan(
                steps=[
                    WorkflowStep("Developer", "developer", "code_gen"),
                    WorkflowStep("Review", "qa", "code_review",
                                 depends_on=["Developer"], verify=True),
                ],
                skip_audit=False,
                estimated_time_s=60.0,
                reasoning="Coding: developer + code review",
            )

        # Research
        if re.search(r'研究|分析|调研|查|搜|股价|金价|新闻|最新', task_lower):
            return WorkflowPlan(
                steps=[
                    WorkflowStep("Researcher", "researcher", "web_search"),
                    WorkflowStep("FactCheck", "researcher", "fact_check",
                                 depends_on=["Researcher"], verify=True),
                ],
                skip_audit=True,
                estimated_time_s=8.0,
                reasoning="Research: web search + fact verification",
            )

        # Document generation
        if re.search(r'pdf|报告|文档|导出|周报|月报', task_lower):
            return WorkflowPlan(
                steps=[
                    WorkflowStep("Researcher", "researcher", "web_search"),
                    WorkflowStep("PDFGenerator", "developer", "write_file",
                                 depends_on=["Researcher"]),
                    WorkflowStep("Verify", "developer", "file_verify",
                                 depends_on=["PDFGenerator"], verify=True),
                ],
                skip_audit=True,
                estimated_time_s=15.0,
                reasoning="Document: research + PDF generation",
            )

        # Simple query
        if re.search(r'^(?:帮我|请|可以)?(?:查|搜|什么是|怎么|为什么|谁)', task_lower):
            return WorkflowPlan(
                steps=[
                    WorkflowStep("Researcher", "researcher", "web_search"),
                ],
                skip_audit=True,
                estimated_time_s=5.0,
                reasoning="Simple query: direct research",
            )

        # Shell commands
        if re.search(r'^(pwd|ls|cd|cat|echo|mkdir|rm|cp|mv|grep|find|curl|wget|ps|top|kill|df|du)', task_lower):
            return WorkflowPlan(
                steps=[
                    WorkflowStep("ShellExec", "devops", "shell_exec"),
                ],
                skip_audit=True,
                estimated_time_s=2.0,
                reasoning="Shell command: direct execution",
            )

        return None

    # ── LLM Fallback ──────────────────────────────────────────

    def _llm_plan(self, intent_name: str, capabilities: list, task: str) -> WorkflowPlan:
        """LLM-driven workflow planning for complex/novel tasks."""
        try:
            from src.ceo.graph import _get_llm
            llm = _get_llm("ceo")

            caps = self.registry.list_all() if hasattr(self.registry, 'list_all') else []
            cap_list = "\n".join(
                f"- {c.name}: {c.description} (agent: {c.agent}, tools: {c.tools})"
                for c in caps
            )

            prompt = f"""You are a workflow planner. Given an intent and task, plan the agent pipeline.

Intent: {intent_name}
Task: {task}

Available capabilities:
{cap_list if cap_list else '(none — use generic steps)'}

Return ONLY JSON:
{{
  "steps": [
    {{"name": "StepName", "agent": "developer|researcher|devops|qa|marketer|ceo", "action": "action_name", "depends_on": [], "verify": false}}
  ],
  "skip_audit": true/false,
  "estimated_time_s": 10.0,
  "reasoning": "why this pipeline"
}}

Rules:
- Every plan should end with a Verify step (verify: true).
- Fast/simple intents (COMMAND, SEARCH, SOCIAL, GENERAL_CHAT, MEMORY, SYSTEM) should skip_audit: true.
- Complex intents (CODING, RESEARCH, FILE, AUTOMATION, CREATIVE) should skip_audit: false.
- depends_on lists step names that must complete before.
"""

            response = llm.invoke(prompt)
            raw = str(response.content)

            # Extract JSON
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw.strip())

            steps = []
            for s in data.get("steps", []):
                steps.append(WorkflowStep(
                    name=s.get("name", "Step"),
                    agent=s.get("agent", "developer"),
                    action=s.get("action", "dispatch"),
                    params={"task": task},
                    depends_on=list(s.get("depends_on", [])),
                    verify=s.get("verify", False),
                ))

            if not steps:
                steps = [
                    WorkflowStep("Researcher", "researcher", "web_search", {"task": task}),
                ]

            return WorkflowPlan(
                steps=steps,
                skip_audit=data.get("skip_audit", False),
                estimated_time_s=data.get("estimated_time_s", 10.0),
                reasoning=data.get("reasoning", f"LLM planned for intent={intent_name}"),
            )
        except Exception as e:
            logger.warning("LLM workflow planning failed: %s", e)
            # Fallback: simple researcher pipeline
            return WorkflowPlan(
                steps=[
                    WorkflowStep("Researcher", "researcher", "web_search", {"task": task}),
                ],
                skip_audit=True,
                estimated_time_s=5.0,
                reasoning="Fallback: LLM planning failed, using simple research",
            )

    # ── compat_plan_from_task_type ────────────────────────────

    def compat_plan_from_task_type(self, task_type: str, department: str = "",
                                   task: str = "") -> WorkflowPlan:
        """Generate a WorkflowPlan compatible with legacy graph.py task_type routing.

        Maps old task_type strings (used in graph.py triage_node) to new-style plans.
        This enables gradual migration without breaking the existing pipeline.

        Args:
            task_type: Legacy task type string
                (COMMAND_EXECUTION, SIMPLE_QUERY, DEVELOPMENT, CODE_REVIEW,
                 DOCUMENT, CREATIVE, RESEARCH, LOCAL_SYSTEM, GENERAL).
            department: The department the task was routed to (devops, developer, etc.).
            task: Original task string for params.

        Returns:
            WorkflowPlan with steps that match the old department-based routing.
        """
        # Map legacy task_type to intent name
        _task_type_to_intent: dict[str, str] = {
            "COMMAND_EXECUTION": "COMMAND",
            "SIMPLE_QUERY": "SEARCH",
            "DEVELOPMENT": "CODING",
            "CODE_REVIEW": "CODE_REVIEW",
            "DOCUMENT": "FILE",
            "CREATIVE": "CREATIVE",
            "RESEARCH": "RESEARCH",
            "LOCAL_SYSTEM": "SYSTEM",
            "GENERAL": "GENERAL_CHAT",
        }

        intent_name = _task_type_to_intent.get(task_type, "GENERAL_CHAT")

        # Use template or default
        template = INTENT_TEMPLATES.get(intent_name, DEFAULT_TEMPLATE)
        steps = self._build_steps(template["steps"], [], task)

        return WorkflowPlan(
            steps=steps,
            skip_audit=template.get("skip_audit", False),
            estimated_time_s=template.get("estimated_time_s", 10.0),
            reasoning=f"Compat plan: {task_type} ({department}) → {intent_name}",
        )

    def get_skip_audit_for_task_type(self, task_type: str) -> bool:
        """Check if a legacy task_type should skip audit.

        Used by graph.py `_route_after_block` / `verify_aggregate_node`
        to decide whether to bypass Auditor+PMO.

        Returns True for: COMMAND_EXECUTION, SIMPLE_QUERY, LOCAL_SYSTEM, GENERAL, DOCUMENT.
        """
        _skip_audit_types = {
            "COMMAND_EXECUTION", "SIMPLE_QUERY", "LOCAL_SYSTEM", "GENERAL", "DOCUMENT",
        }
        return task_type in _skip_audit_types


# ═══════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════

_planner: Optional[WorkflowPlanner] = None


def get_workflow_planner() -> WorkflowPlanner:
    """Return the module-level singleton WorkflowPlanner."""
    global _planner
    if _planner is None:
        _planner = WorkflowPlanner()
    return _planner
