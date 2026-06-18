"""P1.1 WorkflowPlanner Tests — 动态生成执行计划

覆盖所有意图类型的计划生成 + 语法检查 + 兼容性验证。
"""

import sys
import os
import ast
import re
import pytest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.workflow.planner import (
    WorkflowPlanner,
    WorkflowPlan,
    WorkflowStep,
    WorkflowNode,
    INTENT_TEMPLATES,
    DEFAULT_TEMPLATE,
    _ACTION_AGENT_MAP,
    _get_agent_for_action,
    get_workflow_planner,
)
from src.intent.router import IntentRouter, IntentResult
from src.capability.registry import CapabilityRegistry, Capability


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def planner():
    return WorkflowPlanner()


@pytest.fixture
def router():
    return IntentRouter()


@pytest.fixture
def caps_registry():
    return CapabilityRegistry()


# ═══════════════════════════════════════════════════════════════════
# 1. AST Syntax Check
# ═══════════════════════════════════════════════════════════════════

class TestPlannerAST:
    """AST语法检查 — 确保planner.py没有语法错误。"""

    def test_planner_ast_clean(self):
        path = Path(__file__).parent.parent / "src" / "workflow" / "planner.py"
        source = path.read_text()
        tree = ast.parse(source)
        assert tree is not None

    def test_all_intent_templates_valid(self):
        """检查所有意图模板的结构正确性。"""
        for intent, template in INTENT_TEMPLATES.items():
            assert "steps" in template, f"{intent}: missing 'steps'"
            assert "skip_audit" in template, f"{intent}: missing 'skip_audit'"
            assert "estimated_time_s" in template, f"{intent}: missing 'estimated_time_s'"
            for i, step in enumerate(template["steps"]):
                assert "name" in step, f"{intent} step {i}: missing 'name'"
                assert "action" in step, f"{intent} step {i}: missing 'action'"
                # depends_on references must be valid
                for dep in step.get("depends_on", []):
                    step_names = {s["name"] for s in template["steps"][:i]}
                    assert dep in step_names, (
                        f"{intent} step '{step['name']}' depends on '{dep}' "
                        f"which doesn't appear before it"
                    )

    def test_action_agent_map_coverage(self):
        """检查所有模板中的action都有对应的agent映射。"""
        all_actions = set()
        for template in INTENT_TEMPLATES.values():
            for step in template["steps"]:
                all_actions.add(step["action"])
        for template in [DEFAULT_TEMPLATE]:
            for step in template["steps"]:
                all_actions.add(step["action"])

        for action in all_actions:
            agent = _get_agent_for_action(action, None)
            assert agent, f"Action '{action}' has no agent mapping"
            assert agent in ("devops", "developer", "researcher", "qa", "marketer", "ceo"), (
                f"Action '{action}' has unknown agent '{agent}'"
            )


# ═══════════════════════════════════════════════════════════════════
# 2. All Intent Plan Generation (Fast-Path)
# ═══════════════════════════════════════════════════════════════════

class TestAllIntents:
    """验证所有意图类型的计划生成。"""

    @pytest.fixture(autouse=True)
    def setup(self, planner, caps_registry):
        self.planner = planner
        self.caps_registry = caps_registry

    # ── COMMAND ───────────────────────────────────────────────

    def test_command_intent(self):
        """COMMAND → [ShellExec, Verify], skip_audit=True"""
        intent = IntentResult(intent="COMMAND", confidence=0.98, routing_hint="SystemAgent")
        caps = self.caps_registry.resolve("COMMAND")
        plan = self.planner.plan(intent=intent, capabilities=caps)

        assert isinstance(plan, WorkflowPlan)
        assert len(plan.steps) >= 2
        assert plan.steps[0].name == "ShellExec"
        assert plan.steps[0].action == "shell_exec"
        assert plan.steps[-1].name == "Verify"
        assert plan.steps[-1].verify is True
        assert plan.skip_audit is True
        assert plan.estimated_time_s >= 1.0

    # ── SEARCH ────────────────────────────────────────────────

    def test_search_intent(self):
        """SEARCH → [WebSearch, Summarize, Verify], skip_audit=True"""
        intent = IntentResult(intent="SEARCH", confidence=0.90, routing_hint="ResearchAgent")
        caps = self.caps_registry.resolve("SEARCH")
        plan = self.planner.plan(intent=intent, capabilities=caps)

        assert len(plan.steps) >= 3
        assert plan.steps[0].name == "WebSearch"
        assert plan.steps[1].name == "Summarize"
        assert plan.steps[2].name == "Verify"
        assert plan.steps[2].verify is True
        assert plan.skip_audit is True

    # ── RESEARCH ──────────────────────────────────────────────

    def test_research_intent(self):
        """RESEARCH → [WebSearch, WebFetch×2, Analyze, Verify], skip_audit=False"""
        intent = IntentResult(intent="RESEARCH", confidence=0.85, routing_hint="ResearchAgent")
        caps = self.caps_registry.resolve("RESEARCH")
        plan = self.planner.plan(intent=intent, capabilities=caps)

        assert len(plan.steps) >= 5
        step_names = [s.name for s in plan.steps]
        assert "WebSearch" in step_names
        assert "WebFetch-1" in step_names
        assert "WebFetch-2" in step_names
        assert "Analyze" in step_names
        assert "Verify" in step_names
        # RESEARCH goes through audit
        assert plan.skip_audit is False

        # Verify dependency: WebFetch depends on WebSearch
        fetch1 = next(s for s in plan.steps if s.name == "WebFetch-1")
        assert "WebSearch" in fetch1.depends_on

        # Verify dependency: Analyze depends on both fetches
        analyze = next(s for s in plan.steps if s.name == "Analyze")
        assert "WebFetch-1" in analyze.depends_on
        assert "WebFetch-2" in analyze.depends_on

    # ── SOCIAL (WeChat) ───────────────────────────────────────

    def test_social_intent(self):
        """SOCIAL → [LocateWechat, OpenChat, SendMessage, VisionVerify], skip_audit=True"""
        intent = IntentResult(intent="SOCIAL", confidence=0.92, routing_hint="WechatAgent")
        caps = self.caps_registry.resolve("SOCIAL")
        plan = self.planner.plan(intent=intent, capabilities=caps)

        assert len(plan.steps) >= 4
        step_names = [s.name for s in plan.steps]
        assert "LocateWechat" in step_names
        assert "OpenChat" in step_names
        assert "SendMessage" in step_names
        assert "VisionVerify" in step_names
        assert plan.skip_audit is True

        # Verify dependency chain
        open_chat = next(s for s in plan.steps if s.name == "OpenChat")
        assert "LocateWechat" in open_chat.depends_on

        send_msg = next(s for s in plan.steps if s.name == "SendMessage")
        assert "OpenChat" in send_msg.depends_on

    # ── CODING ────────────────────────────────────────────────

    def test_coding_intent(self):
        """CODING → [Requirements, Coding, Test, Review, Verify], skip_audit=False"""
        intent = IntentResult(intent="CODING", confidence=0.92, routing_hint="CodingAgent")
        caps = self.caps_registry.resolve("CODING")
        plan = self.planner.plan(intent=intent, capabilities=caps)

        assert len(plan.steps) >= 5
        step_names = [s.name for s in plan.steps]
        assert "Requirements" in step_names
        assert "Coding" in step_names
        assert "Test" in step_names
        assert "Review" in step_names
        assert "Verify" in step_names
        assert plan.skip_audit is False
        assert plan.estimated_time_s >= 30.0

    # ── CODE_REVIEW ──────────────────────────────────────────

    def test_code_review_intent(self):
        """CODE_REVIEW → [GatherCode, AuditCode, ReportFindings], skip_audit=False"""
        intent = IntentResult(intent="CODE_REVIEW", confidence=0.90, routing_hint="CodingAgent")
        caps = self.caps_registry.resolve("CODING")
        plan = self.planner.plan(intent=intent, capabilities=caps)

        assert len(plan.steps) >= 3
        step_names = [s.name for s in plan.steps]
        assert "GatherCode" in step_names
        assert "AuditCode" in step_names
        assert "ReportFindings" in step_names
        assert plan.skip_audit is False

    # ── CREATIVE ─────────────────────────────────────────────

    def test_creative_intent(self):
        """CREATIVE → [ResearchAudience, DraftContent, Verify]"""
        intent = IntentResult(intent="CREATIVE", confidence=0.88, routing_hint="CreativeAgent")
        caps = []  # CREATIVE not in INTENT_CAPABILITY_MAP yet
        plan = self.planner.plan(intent=intent, capabilities=caps)

        assert len(plan.steps) >= 3
        assert plan.steps[-1].verify is True

    # ── FILE ─────────────────────────────────────────────────

    def test_file_intent(self):
        """FILE → [GatherContent, GenerateFile, Verify], skip_audit=True"""
        intent = IntentResult(intent="FILE", confidence=0.88, routing_hint="SystemAgent")
        caps = self.caps_registry.resolve("FILE")
        plan = self.planner.plan(intent=intent, capabilities=caps)

        assert len(plan.steps) >= 3
        step_names = [s.name for s in plan.steps]
        assert "GatherContent" in step_names
        assert "GenerateFile" in step_names
        assert "Verify" in step_names
        assert plan.skip_audit is True

    # ── SYSTEM ───────────────────────────────────────────────

    def test_system_intent(self):
        """SYSTEM → [ShellExec, Verify], skip_audit=True"""
        intent = IntentResult(intent="SYSTEM", confidence=0.90, routing_hint="SystemAgent")
        caps = self.caps_registry.resolve("SYSTEM")
        plan = self.planner.plan(intent=intent, capabilities=caps)

        assert len(plan.steps) >= 2
        assert plan.steps[0].name == "ShellExec"
        assert plan.skip_audit is True

    # ── MEMORY ───────────────────────────────────────────────

    def test_memory_intent(self):
        """MEMORY → [MemoryOp, Verify], skip_audit=True"""
        intent = IntentResult(intent="MEMORY", confidence=0.88, routing_hint="MemoryAgent")
        caps = self.caps_registry.resolve("MEMORY")
        plan = self.planner.plan(intent=intent, capabilities=caps)

        assert len(plan.steps) >= 2
        assert plan.steps[0].name == "MemoryOp"
        assert plan.skip_audit is True

    # ── VISION ───────────────────────────────────────────────

    def test_vision_intent(self):
        """VISION → [Capture, Analyze, Verify]"""
        intent = IntentResult(intent="VISION", confidence=0.85, routing_hint="VisionAgent")
        caps = self.caps_registry.resolve("VISION")
        plan = self.planner.plan(intent=intent, capabilities=caps)

        assert len(plan.steps) >= 3
        step_names = [s.name for s in plan.steps]
        assert "Capture" in step_names
        assert "Analyze" in step_names
        assert "Verify" in step_names

    # ── AUTOMATION ───────────────────────────────────────────

    def test_automation_intent(self):
        """AUTOMATION → [PlanAutomation, ExecuteAutomation, Verify]"""
        intent = IntentResult(intent="AUTOMATION", confidence=0.85, routing_hint="SystemAgent")
        caps = self.caps_registry.resolve("AUTOMATION")
        plan = self.planner.plan(intent=intent, capabilities=caps)

        assert len(plan.steps) >= 3
        assert plan.skip_audit is False

    # ── GENERAL_CHAT ─────────────────────────────────────────

    def test_general_chat_intent(self):
        """GENERAL_CHAT → [Chat], skip_audit=True"""
        intent = IntentResult(intent="GENERAL_CHAT", confidence=0.95, routing_hint="")
        caps = self.caps_registry.resolve("GENERAL_CHAT")
        plan = self.planner.plan(intent=intent, capabilities=caps)

        assert len(plan.steps) == 1
        assert plan.steps[0].name == "Chat"
        assert plan.steps[0].action == "chat"
        assert plan.skip_audit is True


# ═══════════════════════════════════════════════════════════════════
# 3. Skip Audit Validation
# ═══════════════════════════════════════════════════════════════════

class TestSkipAudit:
    """验证跳过审计的意图类型。"""

    @pytest.fixture(autouse=True)
    def setup(self, planner, caps_registry):
        self.planner = planner
        self.caps_registry = caps_registry

    def _plan_for_intent(self, intent_name: str) -> WorkflowPlan:
        intent = IntentResult(intent=intent_name, confidence=0.9)
        caps = self.caps_registry.resolve(intent_name)
        return self.planner.plan(intent=intent, capabilities=caps)

    def test_command_skips_audit(self):
        plan = self._plan_for_intent("COMMAND")
        assert plan.skip_audit is True, "COMMAND must skip audit"

    def test_search_skips_audit(self):
        plan = self._plan_for_intent("SEARCH")
        assert plan.skip_audit is True, "SEARCH must skip audit"

    def test_social_skips_audit(self):
        plan = self._plan_for_intent("SOCIAL")
        assert plan.skip_audit is True, "SOCIAL must skip audit"

    def test_system_skips_audit(self):
        plan = self._plan_for_intent("SYSTEM")
        assert plan.skip_audit is True, "SYSTEM must skip audit"

    def test_memory_skips_audit(self):
        plan = self._plan_for_intent("MEMORY")
        assert plan.skip_audit is True, "MEMORY must skip audit"

    def test_general_chat_skips_audit(self):
        plan = self._plan_for_intent("GENERAL_CHAT")
        assert plan.skip_audit is True, "GENERAL_CHAT must skip audit"

    def test_file_skips_audit(self):
        plan = self._plan_for_intent("FILE")
        assert plan.skip_audit is True, "FILE must skip audit (document generation)"

    def test_coding_goes_through_audit(self):
        plan = self._plan_for_intent("CODING")
        assert plan.skip_audit is False, "CODING must go through audit"

    def test_research_goes_through_audit(self):
        plan = self._plan_for_intent("RESEARCH")
        assert plan.skip_audit is False, "RESEARCH must go through audit"

    def test_code_review_goes_through_audit(self):
        plan = self._plan_for_intent("CODE_REVIEW")
        assert plan.skip_audit is False, "CODE_REVIEW must go through audit"

    def test_automation_goes_through_audit(self):
        plan = self._plan_for_intent("AUTOMATION")
        assert plan.skip_audit is False, "AUTOMATION must go through audit"


# ═══════════════════════════════════════════════════════════════════
# 4. Backward Compatibility: compat_plan_from_task_type
# ═══════════════════════════════════════════════════════════════════

class TestCompatPlanFromTaskType:
    """验证 compat_plan_from_task_type 兼容旧版 graph.py。"""

    @pytest.fixture(autouse=True)
    def setup(self, planner):
        self.planner = planner

    def test_command_execution(self):
        plan = self.planner.compat_plan_from_task_type("COMMAND_EXECUTION", "devops", "pwd")
        assert len(plan.steps) >= 2
        assert plan.skip_audit is True
        assert plan.steps[0].action == "shell_exec"

    def test_simple_query(self):
        plan = self.planner.compat_plan_from_task_type("SIMPLE_QUERY", "researcher", "查金价")
        assert len(plan.steps) >= 3
        assert plan.skip_audit is True
        assert plan.steps[0].action == "web_search"

    def test_development(self):
        plan = self.planner.compat_plan_from_task_type("DEVELOPMENT", "developer", "写API")
        assert len(plan.steps) >= 5
        assert plan.skip_audit is False

    def test_code_review(self):
        plan = self.planner.compat_plan_from_task_type("CODE_REVIEW", "developer", "审查代码")
        assert len(plan.steps) >= 3
        assert plan.skip_audit is False

    def test_document(self):
        plan = self.planner.compat_plan_from_task_type("DOCUMENT", "developer", "生成PDF")
        assert len(plan.steps) >= 3
        assert plan.skip_audit is True

    def test_creative(self):
        plan = self.planner.compat_plan_from_task_type("CREATIVE", "marketer", "写文案")
        assert len(plan.steps) >= 3
        assert plan.skip_audit is False

    def test_research(self):
        plan = self.planner.compat_plan_from_task_type("RESEARCH", "researcher", "分析竞品")
        assert len(plan.steps) >= 5
        assert plan.skip_audit is False

    def test_local_system(self):
        plan = self.planner.compat_plan_from_task_type("LOCAL_SYSTEM", "devops", "打开微信")
        assert len(plan.steps) >= 2
        assert plan.skip_audit is True

    def test_general(self):
        plan = self.planner.compat_plan_from_task_type("GENERAL", "", "hello")
        assert len(plan.steps) >= 1
        assert plan.skip_audit is True

    def test_unknown_task_type_defaults(self):
        """未知task_type降级为GENERAL_CHAT。"""
        plan = self.planner.compat_plan_from_task_type("UNKNOWN_TYPE", "", "test")
        assert len(plan.steps) >= 1
        assert plan.skip_audit is True  # GENERAL_CHAT skips audit

    def test_get_skip_audit_for_task_type(self):
        """get_skip_audit_for_task_type 方法。"""
        assert self.planner.get_skip_audit_for_task_type("COMMAND_EXECUTION") is True
        assert self.planner.get_skip_audit_for_task_type("SIMPLE_QUERY") is True
        assert self.planner.get_skip_audit_for_task_type("LOCAL_SYSTEM") is True
        assert self.planner.get_skip_audit_for_task_type("GENERAL") is True
        assert self.planner.get_skip_audit_for_task_type("DOCUMENT") is True
        assert self.planner.get_skip_audit_for_task_type("DEVELOPMENT") is False
        assert self.planner.get_skip_audit_for_task_type("RESEARCH") is False
        assert self.planner.get_skip_audit_for_task_type("CODE_REVIEW") is False
        assert self.planner.get_skip_audit_for_task_type("CREATIVE") is False


# ═══════════════════════════════════════════════════════════════════
# 5. Backward Compatibility: Old plan(task) Interface
# ═══════════════════════════════════════════════════════════════════

class TestBackwardCompat:
    """验证旧的 plan(task) 接口仍然可用。"""

    @pytest.fixture(autouse=True)
    def setup(self, planner):
        self.planner = planner

    def test_plan_with_string(self):
        """plan(task: str) 返回 WorkflowPlan（新版steps格式）。"""
        plan = self.planner.plan(task="pwd")
        assert isinstance(plan, WorkflowPlan)
        assert len(plan.steps) >= 1
        assert plan.steps[0].name == "ShellExec"
        assert plan.skip_audit is True

    def test_plan_wechat_send(self):
        plan = self.planner.plan(task="微信给小号发消息")
        assert isinstance(plan, WorkflowPlan)
        assert len(plan.steps) >= 3
        assert any(s.action == "wechat_send" for s in plan.steps)
        assert plan.skip_audit is True

    def test_plan_coding(self):
        plan = self.planner.plan(task="写一个API")
        assert isinstance(plan, WorkflowPlan)
        assert any(s.action == "code_gen" for s in plan.steps)
        assert plan.skip_audit is False

    def test_plan_research(self):
        plan = self.planner.plan(task="查金价")
        assert isinstance(plan, WorkflowPlan)
        assert plan.steps[0].action == "web_search"

    def test_plan_document(self):
        plan = self.planner.plan(task="生成周报PDF")
        assert isinstance(plan, WorkflowPlan)
        assert any(s.action == "write_file" for s in plan.steps)

    def test_plan_greeting(self):
        """问候语不匹配任何关键词，走LLM fallback或默认。"""
        plan = self.planner.plan(task="你好")
        assert isinstance(plan, WorkflowPlan)
        # Either keyword match or fallback — should have steps
        assert len(plan.steps) >= 1

    def test_plan_empty_task(self):
        """空任务返回默认plan。"""
        plan = self.planner.plan(task="")
        assert isinstance(plan, WorkflowPlan)
        assert plan.skip_audit is True

    def test_plan_no_args(self):
        """无参数返回默认plan。"""
        plan = self.planner.plan()
        assert isinstance(plan, WorkflowPlan)
        assert plan.steps[0].action == "chat"
        assert plan.skip_audit is True


# ═══════════════════════════════════════════════════════════════════
# 6. End-to-End: IntentRouter → WorkflowPlanner
# ═══════════════════════════════════════════════════════════════════

class TestIntegration:
    """集成测试：IntentRouter分类 → WorkflowPlanner生成计划。"""

    @pytest.fixture(autouse=True)
    def setup(self, planner, router, caps_registry):
        self.planner = planner
        self.router = router
        self.caps_registry = caps_registry

    def _classify_and_plan(self, text: str) -> WorkflowPlan:
        intent = self.router.classify(text)
        caps = self.caps_registry.resolve(intent.intent)
        return self.planner.plan(intent=intent, capabilities=caps, task=text)

    def test_pwd_command(self):
        plan = self._classify_and_plan("pwd")
        assert plan.skip_audit is True
        assert plan.steps[0].action == "shell_exec"

    def test_search_gold_price(self):
        plan = self._classify_and_plan("查金价")
        assert plan.skip_audit is True
        assert plan.steps[0].action == "web_search"

    def test_wechat_send(self):
        plan = self._classify_and_plan("给小号发微信说你好")
        assert plan.skip_audit is True
        assert any(s.action == "wechat_send" for s in plan.steps)

    def test_coding_task(self):
        plan = self._classify_and_plan("写一个Flask API")
        assert plan.skip_audit is False
        assert any(s.action == "code_gen" for s in plan.steps)

    def test_file_generation(self):
        plan = self._classify_and_plan("生成项目周报PDF")
        assert plan.skip_audit is True
        assert any(s.action == "write_file" for s in plan.steps)

    def test_system_open_app(self):
        plan = self._classify_and_plan("打开微信")
        assert plan.skip_audit is True
        assert plan.steps[0].action == "shell_exec"

    def test_memory_op(self):
        plan = self._classify_and_plan("记住我的偏好是中文")
        assert plan.skip_audit is True
        assert plan.steps[0].action == "memory_operation"

    def test_deep_research(self):
        plan = self._classify_and_plan("分析OpenClaw框架的优劣势并进行竞品对比")
        assert plan.skip_audit is False  # RESEARCH → through audit
        assert len(plan.steps) >= 5

    def test_chitchat(self):
        plan = self._classify_and_plan("你好啊")
        assert plan.skip_audit is True


# ═══════════════════════════════════════════════════════════════════
# 7. Data Structure Validation
# ═══════════════════════════════════════════════════════════════════

class TestDataStructures:
    """验证数据结构的正确性。"""

    def test_workflow_step_defaults(self):
        step = WorkflowStep("Test", "devops", "shell_exec")
        assert step.name == "Test"
        assert step.agent == "devops"
        assert step.action == "shell_exec"
        assert step.params == {}
        assert step.depends_on == []
        assert step.verify is False

    def test_workflow_step_with_params(self):
        step = WorkflowStep(
            name="Search",
            agent="researcher",
            action="web_search",
            params={"query": "金价"},
            depends_on=["Locate"],
            verify=True,
        )
        assert step.params == {"query": "金价"}
        assert step.depends_on == ["Locate"]
        assert step.verify is True

    def test_workflow_plan_defaults(self):
        plan = WorkflowPlan()
        assert plan.steps == []
        assert plan.skip_audit is False
        assert plan.estimated_time_s == 5.0
        assert plan.reasoning == ""

    def test_workflow_plan_with_steps(self):
        steps = [
            WorkflowStep("A", "devops", "shell_exec"),
            WorkflowStep("B", "researcher", "web_search"),
        ]
        plan = WorkflowPlan(
            steps=steps,
            skip_audit=True,
            estimated_time_s=30.0,
            reasoning="Test plan",
        )
        assert len(plan.steps) == 2
        assert plan.skip_audit is True
        assert plan.estimated_time_s == 30.0

    def test_workflow_node_legacy(self):
        node = WorkflowNode("Legacy", "agent", ["cap1", "cap2"], "action")
        assert node.name == "Legacy"
        assert node.agent == "agent"
        assert node.capabilities == ["cap1", "cap2"]
        assert node.category == "action"


# ═══════════════════════════════════════════════════════════════════
# 8. Planner Singleton
# ═══════════════════════════════════════════════════════════════════

class TestSingleton:
    """验证单例模式。"""

    def test_get_workflow_planner_returns_same_instance(self):
        p1 = get_workflow_planner()
        p2 = get_workflow_planner()
        assert p1 is p2

    def test_singleton_is_workflow_planner(self):
        p = get_workflow_planner()
        assert isinstance(p, WorkflowPlanner)
