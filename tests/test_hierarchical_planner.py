"""P1.2 Hierarchical Planner Tests — 层次化分解 + 失败回溯

覆盖:
- PlanPhase / HierarchicalPlan 数据结构
- 简单任务 1-phase plan_hierarchical
- 复杂任务 LLM 多phase生成 (mocked)
- 失败回溯: retry / skip / abort
- 依赖解析
- 兼容性 compat_hierarchical_plan
- 序列化/反序列化
"""

import sys
import os
import ast
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.workflow.planner import (
    WorkflowPlanner,
    WorkflowPlan,
    WorkflowStep,
    WorkflowNode,
    PlanPhase,
    HierarchicalPlan,
    INTENT_TEMPLATES,
    DEFAULT_TEMPLATE,
    _ACTION_AGENT_MAP,
    _get_agent_for_action,
    get_workflow_planner,
)


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def planner():
    return WorkflowPlanner()


@pytest.fixture
def sample_steps():
    return [
        WorkflowStep("research", "researcher", "web_search", {"task": "test"}),
        WorkflowStep("verify", "researcher", "fact_check", {"task": "test"}, verify=True),
    ]


# ═══════════════════════════════════════════════════════════════════
# 1. AST Syntax Check
# ═══════════════════════════════════════════════════════════════════

class TestHierarchicalAST:
    """确保新增代码没有语法错误。"""

    def test_planner_ast_clean(self):
        path = Path(__file__).parent.parent / "src" / "workflow" / "planner.py"
        source = path.read_text()
        tree = ast.parse(source)
        assert tree is not None

    def test_graph_ast_clean(self):
        path = Path(__file__).parent.parent / "src" / "ceo" / "graph.py"
        source = path.read_text()
        tree = ast.parse(source)
        assert tree is not None


# ═══════════════════════════════════════════════════════════════════
# 2. PlanPhase Data Structure
# ═══════════════════════════════════════════════════════════════════

class TestPlanPhase:
    """验证 PlanPhase 数据结构。"""

    def test_default_values(self):
        phase = PlanPhase(name="test")
        assert phase.name == "test"
        assert phase.steps == []
        assert phase.depends_on == []
        assert phase.on_failure == "abort"
        assert phase.max_retries == 1
        assert phase.status == "pending"
        assert phase.retry_count == 0
        assert phase.partial_output is None
        assert phase.error_message == ""

    def test_with_steps(self, sample_steps):
        phase = PlanPhase(
            name="research_phase",
            steps=sample_steps,
            depends_on=["init"],
            on_failure="retry",
            max_retries=3,
        )
        assert len(phase.steps) == 2
        assert phase.steps[0].name == "research"
        assert phase.depends_on == ["init"]
        assert phase.on_failure == "retry"
        assert phase.max_retries == 3


# ═══════════════════════════════════════════════════════════════════
# 3. HierarchicalPlan Data Structure
# ═══════════════════════════════════════════════════════════════════

class TestHierarchicalPlan:
    """验证 HierarchicalPlan 核心逻辑。"""

    def test_default_values(self):
        plan = HierarchicalPlan(goal="test goal")
        assert plan.goal == "test goal"
        assert plan.phases == []
        assert plan.fallback_plan is None
        assert plan.current_phase == 0

    def test_next_phase_first(self):
        """第一个pending阶段应被返回。"""
        plan = HierarchicalPlan(goal="test", phases=[
            PlanPhase(name="phase1"),
            PlanPhase(name="phase2"),
        ])
        next_p = plan.next_phase()
        assert next_p is not None
        assert next_p.name == "phase1"
        assert plan.current_phase == 0

    def test_next_phase_skips_done(self):
        """已完成阶段应被跳过。"""
        p1 = PlanPhase(name="phase1", status="done")
        p2 = PlanPhase(name="phase2", status="pending")
        plan = HierarchicalPlan(goal="test", phases=[p1, p2])
        next_p = plan.next_phase()
        assert next_p is not None
        assert next_p.name == "phase2"

    def test_next_phase_skips_skipped(self):
        """被跳过的阶段不应再被返回。"""
        p1 = PlanPhase(name="phase1", status="skipped")
        p2 = PlanPhase(name="phase2", status="pending")
        plan = HierarchicalPlan(goal="test", phases=[p1, p2])
        next_p = plan.next_phase()
        assert next_p is not None
        assert next_p.name == "phase2"

    def test_next_phase_dependency_unmet(self):
        """依赖未满足的阶段不应被返回。"""
        p1 = PlanPhase(name="phase1", status="pending")
        p2 = PlanPhase(name="phase2", status="pending", depends_on=["phase1"])
        plan = HierarchicalPlan(goal="test", phases=[p1, p2])
        # phase1 应该被返回（无依赖），phase2 依赖未满足暂不返回
        next_p = plan.next_phase()
        assert next_p is not None
        assert next_p.name == "phase1"
        # Mark phase1 done, now phase2 should be available
        plan.mark_done("phase1")
        next_p = plan.next_phase()
        assert next_p is not None
        assert next_p.name == "phase2"

    def test_next_phase_dependency_skipped(self):
        """被skip的依赖也应该解锁后续阶段。"""
        p1 = PlanPhase(name="phase1", status="skipped")
        p2 = PlanPhase(name="phase2", status="pending", depends_on=["phase1"])
        plan = HierarchicalPlan(goal="test", phases=[p1, p2])
        next_p = plan.next_phase()
        assert next_p is not None
        assert next_p.name == "phase2"

    def test_next_phase_none_when_all_done(self):
        """所有阶段完成时返回None。"""
        p1 = PlanPhase(name="phase1", status="done")
        p2 = PlanPhase(name="phase2", status="done")
        plan = HierarchicalPlan(goal="test", phases=[p1, p2])
        assert plan.next_phase() is None

    def test_mark_done(self):
        phase = PlanPhase(name="phase1")
        plan = HierarchicalPlan(goal="test", phases=[phase])
        plan.mark_done("phase1")
        assert phase.status == "done"

    def test_all_phases_done(self):
        p1 = PlanPhase(name="phase1", status="done")
        p2 = PlanPhase(name="phase2", status="skipped")
        plan = HierarchicalPlan(goal="test", phases=[p1, p2])
        assert plan.all_phases_done() is True

    def test_all_phases_not_done_with_pending(self):
        p1 = PlanPhase(name="phase1", status="done")
        p2 = PlanPhase(name="phase2", status="pending")
        plan = HierarchicalPlan(goal="test", phases=[p1, p2])
        assert plan.all_phases_done() is False

    def test_get_partial_results(self):
        p1 = PlanPhase(name="phase1", status="done")
        p2 = PlanPhase(name="phase2", status="failed",
                       error_message="test error", partial_output="partial data")
        p3 = PlanPhase(name="phase3", status="skipped")
        plan = HierarchicalPlan(goal="test goal", phases=[p1, p2, p3])
        result = plan.get_partial_results()
        assert "test goal" in result
        assert "phase1" in result
        assert "phase2" in result
        assert "test error" in result
        assert "partial data" in result
        assert "phase3" in result


# ═══════════════════════════════════════════════════════════════════
# 4. Failure Backtracking: retry / skip / abort
# ═══════════════════════════════════════════════════════════════════

class TestFailureBacktracking:
    """验证失败回溯三种策略。"""

    def test_retry_within_limit(self):
        """retry策略在重试次数内应返回retry并重置状态。"""
        phase = PlanPhase(name="critical", on_failure="retry", max_retries=3)
        plan = HierarchicalPlan(goal="test", phases=[phase])
        phase.status = "running"
        phase.retry_count = 0

        strategy = plan.mark_failed("critical", error="timeout")
        assert strategy == "retry"
        assert phase.status == "pending"  # 重置等待重试
        assert phase.retry_count == 1
        assert phase.error_message == "timeout"

    def test_retry_exhausted(self):
        """重试次数用尽时，fallback到abort。"""
        phase = PlanPhase(name="critical", on_failure="retry", max_retries=2)
        plan = HierarchicalPlan(goal="test", phases=[phase])
        phase.status = "running"
        phase.retry_count = 2  # 已经重试到上限

        strategy = plan.mark_failed("critical", error="persistent failure")
        # 当 retry_count >= max_retries 时，即使 on_failure=retry 也应该abort
        # 但实际上 mark_failed 检查的是 retry_count < max_retries
        # 所以第二次调用(已到上限)应该返回abort
        assert strategy == "abort"
        assert phase.status == "failed"

    def test_retry_exact_limit(self):
        """重试达到max_retries-1时应允许最后一次重试。"""
        phase = PlanPhase(name="critical", on_failure="retry", max_retries=2)
        plan = HierarchicalPlan(goal="test", phases=[phase])
        phase.status = "running"
        phase.retry_count = 1  # 还可以再重试一次 (1 < 2)

        strategy = plan.mark_failed("critical", error="error")
        assert strategy == "retry"
        assert phase.retry_count == 2
        assert phase.status == "pending"

    def test_skip_strategy(self):
        """skip策略应标记为skipped并返回skip。"""
        phase = PlanPhase(name="optional", on_failure="skip")
        plan = HierarchicalPlan(goal="test", phases=[phase])
        phase.status = "running"

        strategy = plan.mark_failed("optional", error="non-critical error")
        assert strategy == "skip"
        assert phase.status == "skipped"

    def test_abort_strategy(self):
        """abort策略应标记为failed并返回abort。"""
        phase = PlanPhase(name="essential", on_failure="abort")
        plan = HierarchicalPlan(goal="test", phases=[phase])
        phase.status = "running"

        strategy = plan.mark_failed("essential", error="critical error",
                                     partial_output="half done")
        assert strategy == "abort"
        assert phase.status == "failed"
        assert phase.partial_output == "half done"

    def test_default_abort(self):
        """默认策略应为abort。"""
        phase = PlanPhase(name="phase")  # on_failure defaults to "abort"
        plan = HierarchicalPlan(goal="test", phases=[phase])
        phase.status = "running"

        strategy = plan.mark_failed("phase", error="error")
        assert strategy == "abort"

    def test_mark_failed_nonexistent_phase(self):
        """不存在的phase名应返回abort。"""
        plan = HierarchicalPlan(goal="test", phases=[
            PlanPhase(name="real")
        ])
        strategy = plan.mark_failed("nonexistent", error="error")
        assert strategy == "abort"

    def test_retry_preserves_partial_output(self):
        """重试时应保留partial_output供下次参考。"""
        phase = PlanPhase(name="phase1", on_failure="retry", max_retries=1)
        plan = HierarchicalPlan(goal="test", phases=[phase])
        phase.status = "running"

        strategy = plan.mark_failed("phase1", error="fail",
                                     partial_output="some output")
        assert strategy == "retry"
        assert phase.partial_output == "some output"


# ═══════════════════════════════════════════════════════════════════
# 5. Simple Task: 1-Phase plan_hierarchical
# ═══════════════════════════════════════════════════════════════════

class TestSimpleHierarchicalPlan:
    """验证简单意图 → 单阶段计划。"""

    @pytest.fixture(autouse=True)
    def setup(self, planner):
        self.planner = planner

    def test_command_1_phase(self):
        """COMMAND → 1 phase with skip on failure."""
        hplan = self.planner.plan_hierarchical(task="pwd")
        assert isinstance(hplan, HierarchicalPlan)
        assert len(hplan.phases) == 1
        phase = hplan.phases[0]
        assert phase.on_failure == "skip"  # COMMAND skips audit → skip
        assert phase.name in ("command", "general_chat")
        assert len(phase.steps) >= 1

    def test_search_1_phase(self):
        """SEARCH → 1 phase."""
        hplan = self.planner.plan_hierarchical(task="查金价")
        assert len(hplan.phases) == 1

    def test_general_chat_1_phase(self):
        """GENERAL_CHAT → 1 phase."""
        hplan = self.planner.plan_hierarchical(task="你好")
        assert len(hplan.phases) == 1
        assert hplan.phases[0].steps[0].action == "chat"

    def test_phase_contains_steps(self):
        """单阶段计划包含所有原始步骤。"""
        hplan = self.planner.plan_hierarchical(task="pwd")
        phase = hplan.phases[0]
        assert len(phase.steps) >= 1
        assert all(isinstance(s, WorkflowStep) for s in phase.steps)

    def test_skip_audit_maps_to_skip_failure(self):
        """skip_audit=True 的任务 → on_failure=skip。"""
        hplan = self.planner.plan_hierarchical(task="pwd")
        assert hplan.phases[0].on_failure == "skip"

    def test_no_audit_skip_maps_to_abort(self):
        """skip_audit=False 的任务 → on_failure=abort。"""
        # 使用 CODING 意图（skip_audit=False）
        from src.intent.router import IntentResult
        intent = IntentResult(intent="CODING", confidence=0.92)
        hplan = self.planner.plan_hierarchical(task="写一个API接口", intent=intent)
        if len(hplan.phases) == 1:
            assert hplan.phases[0].on_failure == "abort"


# ═══════════════════════════════════════════════════════════════════
# 6. Complex Task: LLM Multi-Phase (Mocked)
# ═══════════════════════════════════════════════════════════════════

class TestLLMHierarchicalPlan:
    """验证LLM多阶段计划生成（使用mock避免真实LLM调用）。"""

    @pytest.fixture(autouse=True)
    def setup(self, planner):
        self.planner = planner

    def test_llm_plan_multiphase_mocked(self):
        """Mock LLM返回多阶段计划。"""
        mock_response = MagicMock()
        mock_response.content = '''```json
{
  "phases": [
    {
      "name": "research",
      "description": "研究需求",
      "steps": [
        {"name": "WebSearch", "agent": "researcher", "action": "web_search", "depends_on": [], "verify": false}
      ],
      "depends_on": [],
      "on_failure": "skip",
      "max_retries": 1
    },
    {
      "name": "coding",
      "description": "编写代码",
      "steps": [
        {"name": "Coding", "agent": "developer", "action": "code_gen", "depends_on": [], "verify": false}
      ],
      "depends_on": ["research"],
      "on_failure": "retry",
      "max_retries": 2
    },
    {
      "name": "verify",
      "description": "验证代码",
      "steps": [
        {"name": "Verify", "agent": "qa", "action": "lint_code", "depends_on": [], "verify": true}
      ],
      "depends_on": ["coding"],
      "on_failure": "abort",
      "max_retries": 1
    }
  ],
  "fallback_plan": null
}
```'''

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = mock_response

        with patch("src.workflow.planner.WorkflowPlanner._llm_plan_hierarchical",
                   return_value=HierarchicalPlan(
                       goal="test task",
                       phases=[
                           PlanPhase(name="research", steps=[
                               WorkflowStep("WebSearch", "researcher", "web_search")
                           ], on_failure="skip"),
                           PlanPhase(name="coding", steps=[
                               WorkflowStep("Coding", "developer", "code_gen")
                           ], depends_on=["research"], on_failure="retry", max_retries=2),
                           PlanPhase(name="verify", steps=[
                               WorkflowStep("Verify", "qa", "lint_code", verify=True)
                           ], depends_on=["coding"], on_failure="abort"),
                       ]
                   )):
            hplan = self.planner.plan_hierarchical(
                task="写一个复杂的机器学习模型",
                intent=type("Intent", (), {"intent": "CODING"})(),
            )

        assert len(hplan.phases) == 3
        assert hplan.phases[0].name == "research"
        assert hplan.phases[1].depends_on == ["research"]
        assert hplan.phases[1].on_failure == "retry"
        assert hplan.phases[1].max_retries == 2
        assert hplan.phases[2].depends_on == ["coding"]
        assert hplan.phases[2].on_failure == "abort"


# ═══════════════════════════════════════════════════════════════════
# 7. Compatibility: compat_hierarchical_plan
# ═══════════════════════════════════════════════════════════════════

class TestCompatHierarchicalPlan:
    """验证 compat_hierarchical_plan 兼容旧版task_type。"""

    @pytest.fixture(autouse=True)
    def setup(self, planner):
        self.planner = planner

    def test_command_execution(self):
        hplan = self.planner.compat_hierarchical_plan("pwd", "COMMAND_EXECUTION")
        assert len(hplan.phases) == 1
        assert hplan.phases[0].on_failure == "skip"
        assert hplan.phases[0].name == "command_execution"

    def test_development(self):
        hplan = self.planner.compat_hierarchical_plan("写API", "DEVELOPMENT")
        assert len(hplan.phases) == 1
        assert hplan.phases[0].on_failure == "abort"
        assert hplan.phases[0].name == "development"

    def test_simple_query(self):
        hplan = self.planner.compat_hierarchical_plan("查金价", "SIMPLE_QUERY")
        assert len(hplan.phases) == 1
        assert hplan.phases[0].on_failure == "skip"

    def test_general(self):
        hplan = self.planner.compat_hierarchical_plan("hello", "GENERAL")
        assert len(hplan.phases) == 1
        assert hplan.phases[0].on_failure == "skip"

    def test_research(self):
        hplan = self.planner.compat_hierarchical_plan("分析竞品", "RESEARCH")
        assert len(hplan.phases) == 1
        assert hplan.phases[0].on_failure == "abort"

    def test_unknown_task_type_defaults(self):
        hplan = self.planner.compat_hierarchical_plan("test", "UNKNOWN_TYPE")
        assert len(hplan.phases) == 1
        assert isinstance(hplan, HierarchicalPlan)

    def test_goal_preserved(self):
        hplan = self.planner.compat_hierarchical_plan("测试任务", "GENERAL")
        assert hplan.goal == "测试任务"


# ═══════════════════════════════════════════════════════════════════
# 8. Serialization / Deserialization
# ═══════════════════════════════════════════════════════════════════

class TestSerialization:
    """验证 HierarchicalPlan 序列化/反序列化。"""

    def test_serialize_deserialize_roundtrip(self):
        """序列化和反序列化应保持语义一致。"""
        from dataclasses import asdict
        from src.workflow.planner import HierarchicalPlan, PlanPhase, WorkflowStep

        original = HierarchicalPlan(
            goal="test goal",
            phases=[
                PlanPhase(
                    name="phase1",
                    steps=[
                        WorkflowStep("step1", "developer", "code_gen",
                                     {"task": "test"}, ["init"], True),
                    ],
                    depends_on=[],
                    on_failure="retry",
                    max_retries=2,
                    status="done",
                    retry_count=0,
                ),
                PlanPhase(
                    name="phase2",
                    steps=[
                        WorkflowStep("step2", "qa", "lint_code"),
                    ],
                    depends_on=["phase1"],
                    on_failure="abort",
                    max_retries=1,
                    status="pending",
                ),
            ],
            fallback_plan="restart from scratch",
            current_phase=0,
        )

        # Serialize
        data = asdict(original)

        # Deserialize
        phases = []
        for p_data in data["phases"]:
            steps = []
            for s_data in p_data["steps"]:
                steps.append(WorkflowStep(
                    name=s_data["name"],
                    agent=s_data["agent"],
                    action=s_data["action"],
                    params=s_data["params"],
                    depends_on=s_data["depends_on"],
                    verify=s_data["verify"],
                ))
            phases.append(PlanPhase(
                name=p_data["name"],
                steps=steps,
                depends_on=p_data["depends_on"],
                on_failure=p_data["on_failure"],
                max_retries=p_data["max_retries"],
                status=p_data["status"],
                retry_count=p_data["retry_count"],
                partial_output=p_data["partial_output"],
                error_message=p_data["error_message"],
            ))

        restored = HierarchicalPlan(
            goal=data["goal"],
            phases=phases,
            fallback_plan=data["fallback_plan"],
            current_phase=data["current_phase"],
        )

        assert restored.goal == original.goal
        assert len(restored.phases) == 2
        assert restored.phases[0].name == "phase1"
        assert restored.phases[0].on_failure == "retry"
        assert restored.phases[0].max_retries == 2
        assert restored.phases[0].status == "done"
        assert restored.phases[1].depends_on == ["phase1"]
        assert restored.phases[1].status == "pending"
        assert restored.fallback_plan == "restart from scratch"


# ═══════════════════════════════════════════════════════════════════
# 9. Backward Compatibility: Existing interfaces still work
# ═══════════════════════════════════════════════════════════════════

class TestBackwardCompat:
    """验证原有 plan() 接口不受影响。"""

    @pytest.fixture(autouse=True)
    def setup(self, planner):
        self.planner = planner

    def test_old_plan_still_works(self):
        """plan(task) 应返回 WorkflowPlan（非HierarchicalPlan）。"""
        plan = self.planner.plan(task="pwd")
        assert isinstance(plan, WorkflowPlan)
        assert not isinstance(plan, HierarchicalPlan)
        assert len(plan.steps) >= 1

    def test_old_plan_with_intent_still_works(self):
        """plan(intent, capabilities) 应返回 WorkflowPlan。"""
        from src.intent.router import IntentResult
        intent = IntentResult(intent="COMMAND", confidence=0.98)
        plan = self.planner.plan(intent=intent, capabilities=[])
        assert isinstance(plan, WorkflowPlan)
        assert not isinstance(plan, HierarchicalPlan)

    def test_compat_plan_from_task_type_still_works(self):
        """compat_plan_from_task_type 返回 WorkflowPlan。"""
        plan = self.planner.compat_plan_from_task_type("COMMAND_EXECUTION")
        assert isinstance(plan, WorkflowPlan)

    def test_get_skip_audit_still_works(self):
        """get_skip_audit_for_task_type 不变。"""
        assert self.planner.get_skip_audit_for_task_type("COMMAND_EXECUTION") is True


# ═══════════════════════════════════════════════════════════════════
# 10. Integration: Full Pipeline Simulation
# ═══════════════════════════════════════════════════════════════════

class TestPipelineIntegration:
    """模拟完整pipeline中的层次化执行。"""

    def test_full_success_flow(self):
        """完整成功流程：research → coding → verify。"""
        hplan = HierarchicalPlan(
            goal="build feature X",
            phases=[
                PlanPhase(name="research", steps=[
                    WorkflowStep("WebSearch", "researcher", "web_search")
                ], on_failure="skip"),
                PlanPhase(name="coding", steps=[
                    WorkflowStep("Coding", "developer", "code_gen")
                ], depends_on=["research"], on_failure="retry", max_retries=2),
                PlanPhase(name="verify", steps=[
                    WorkflowStep("Verify", "qa", "lint_code", verify=True)
                ], depends_on=["coding"], on_failure="abort"),
            ]
        )

        # Phase 1: research
        p1 = hplan.next_phase()
        assert p1.name == "research"
        hplan.mark_done("research")

        # Phase 2: coding
        p2 = hplan.next_phase()
        assert p2.name == "coding"
        hplan.mark_done("coding")

        # Phase 3: verify
        p3 = hplan.next_phase()
        assert p3.name == "verify"
        hplan.mark_done("verify")

        # All done
        assert hplan.next_phase() is None
        assert hplan.all_phases_done()

    def test_failure_skip_flow(self):
        """中间阶段失败→skip→继续执行后续。"""
        hplan = HierarchicalPlan(
            goal="simple task",
            phases=[
                PlanPhase(name="step1", on_failure="abort"),
                PlanPhase(name="step2", on_failure="skip"),
                PlanPhase(name="step3", depends_on=["step2"], on_failure="abort"),
            ]
        )

        # step1 succeeds
        hplan.mark_done("step1")

        # step2 fails → skip
        hplan.phases[1].status = "running"
        strategy = hplan.mark_failed("step2", error="optional step failed")
        assert strategy == "skip"
        assert hplan.phases[1].status == "skipped"

        # step3 should now be available (step2 skipped = dependency satisfied)
        next_p = hplan.next_phase()
        assert next_p is not None
        assert next_p.name == "step3"

    def test_failure_abort_flow(self):
        """第一阶段失败→abort→不执行后续。"""
        hplan = HierarchicalPlan(
            goal="critical task",
            phases=[
                PlanPhase(name="critical_step", on_failure="abort"),
                PlanPhase(name="never_reached", depends_on=["critical_step"]),
            ]
        )

        hplan.phases[0].status = "running"
        strategy = hplan.mark_failed("critical_step", error="fatal error")
        assert strategy == "abort"
        assert hplan.phases[0].status == "failed"

        # never_reached 依赖未满足 → 不应返回
        next_p = hplan.next_phase()
        assert next_p is None or next_p.name != "never_reached"

    def test_failure_retry_then_success_flow(self):
        """失败→retry→重试成功→继续。"""
        hplan = HierarchicalPlan(
            goal="flaky task",
            phases=[
                PlanPhase(name="flaky", on_failure="retry", max_retries=2),
                PlanPhase(name="next", depends_on=["flaky"]),
            ]
        )

        # 第一次失败
        hplan.phases[0].status = "running"
        strategy = hplan.mark_failed("flaky", error="timeout")
        assert strategy == "retry"
        assert hplan.phases[0].status == "pending"  # 重置

        # 重试成功
        hplan.mark_done("flaky")
        assert hplan.phases[0].status == "done"

        # next应可执行
        next_p = hplan.next_phase()
        assert next_p.name == "next"

    def test_partial_results_on_abort(self):
        """abort时get_partial_results包含已完成阶段信息。"""
        hplan = HierarchicalPlan(
            goal="partial task",
            phases=[
                PlanPhase(name="step1", status="done"),
                PlanPhase(name="step2", status="failed",
                          error_message="boom", partial_output="half result"),
                PlanPhase(name="step3", status="pending"),
            ]
        )

        result = hplan.get_partial_results()
        assert "✓" in result or "step1" in result
        assert "failed" in result.lower() or "✗" in result
        assert "boom" in result
