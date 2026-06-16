"""AI-Company Core Tests — Role Registry, Auditor, Config, Router"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─── Role Registry Tests ──────────────────────────

class TestRoleRegistry:
    def test_core_roles_exist(self):
        """Verify all 6 core execution roles are loaded."""
        from src.departments.roles import role_registry
        expected = {"developer", "qa", "devops", "researcher", "marketer"}
        actual = {r.name for r in role_registry.list_execution()}
        assert expected.issubset(actual), f"Missing: {expected - actual}"

    def test_control_roles_exist(self):
        """Verify control layer roles are loaded."""
        from src.departments.roles import role_registry
        expected = {"pm", "architect"}
        actual = {r.name for r in role_registry.list_control()}
        assert expected.issubset(actual), f"Missing: {expected - actual}"

    def test_developer_has_review_keywords(self):
        """P0 fix: Developer must have code review keywords."""
        from src.departments.roles import role_registry
        dev = role_registry.get("developer")
        assert dev is not None
        assert "审查" in dev.keywords or "review" in dev.keywords, \
            "Developer missing review keywords — routing will be broken"

    def test_best_match_developer_for_code_review(self):
        """Code review tasks should route to developer, not researcher."""
        from src.departments.roles import role_registry
        role, score = role_registry.best_match("审查代码质量并打分")
        assert role is not None
        assert role.name == "developer", \
            f"Expected developer, got {role.name} (score: {score:.2f})"

    def test_duplicate_detection(self):
        """Roles with heavily overlapping keywords should be flagged."""
        from src.departments.roles import Role, role_registry
        dup = Role(
            name="fake_dev",
            display_name="假开发者",
            category="execution",
            description="Fake",
            system_prompt="You are fake.",
            keywords=["代码", "开发", "bug", "Python", "JavaScript", "API", "接口", "函数"],
        )
        duplicates = role_registry.check_duplicate(dup, threshold=0.3)
        assert len(duplicates) > 0, "Should detect overlap with developer"

    def test_dynamic_role_lifecycle(self):
        """Register, promote, and remove a trial role."""
        from src.departments.roles import Role, role_registry
        role = Role(
            name="tester_test",
            display_name="测试员",
            category="execution",
            description="Test role",
            system_prompt="You test things.",
            keywords=["测试专用"],
            status="trial",
            trial_uses=0,
        )
        role_registry.register(role)
        assert role_registry.get("tester_test") is not None
        # 3 successful uses → promoted
        for _ in range(3):
            role_registry.record_use("tester_test", success=True)
        promoted = role_registry.get("tester_test")
        assert promoted.status == "established"
        # Clean up
        role_registry.remove("tester_test")
        assert role_registry.get("tester_test") is None


# ─── Auditor Tests ────────────────────────────────

class TestAuditorDimensions:
    def test_devops_vs_operation_not_identical(self):
        """P1 fix: devops and operation must have different scoring dimensions."""
        from src.verification.auditor import SCORING_DIMENSIONS
        devops = SCORING_DIMENSIONS.get("devops", [])
        operation = SCORING_DIMENSIONS.get("operation", [])

        devops_names = [d["name"] for d in devops]
        operation_names = [d["name"] for d in operation]
        assert devops_names != operation_names, \
            f"P1 BUG: devops and operation dimensions are identical!\ndevops={devops_names}\noperation={operation_names}"

    def test_all_execution_roles_have_dimensions(self):
        """Every execution role should have scoring dimensions or a default."""
        from src.verification.auditor import SCORING_DIMENSIONS, DEFAULT_DIMENSIONS
        from src.departments.roles import role_registry

        for role in role_registry.list_execution():
            dims = SCORING_DIMENSIONS.get(role.name, DEFAULT_DIMENSIONS)
            assert len(dims) > 0, f"No scoring dimensions for {role.name}"

    def test_dimension_weights_sum_to_one(self):
        """All dimension weights should sum to ~1.0."""
        from src.verification.auditor import SCORING_DIMENSIONS
        for dept, dims in SCORING_DIMENSIONS.items():
            total = sum(d["weight"] for d in dims)
            assert abs(total - 1.0) < 0.01, \
                f"{dept} weights sum to {total}, expected 1.0"

    def test_audit_report_to_dict(self):
        """AuditReport serialization should work."""
        from src.verification.auditor import AuditReport, DimensionScore
        report = AuditReport(
            dimensions=[
                DimensionScore(name="测试", score=80, weight=1.0,
                              reasoning="Good", issues=[]),
            ],
            overall_score=80,
            verdict="APPROVE",
            summary="All good",
            suggestions=[],
        )
        d = report.to_dict()
        assert d["overall_score"] == 80
        assert d["verdict"] == "APPROVE"
        assert len(d["dimensions"]) == 1


# ─── Config Tests ─────────────────────────────────

class TestConfig:
    def test_default_models(self):
        """All role models should have valid defaults."""
        from src.config import config
        assert config.get_model_for("ceo").model
        assert config.get_model_for("developer").model
        assert config.get_model_for("review").model

    def test_unknown_role_fallback(self):
        """Unknown role should fall back to CEO model."""
        from src.config import config
        mc = config.get_model_for("nonexistent_role_xyz")
        assert mc.model == config.ceo_model.model

    def test_gate_scores_are_valid(self):
        """Gate scores should be between 0-100."""
        from src.config import config
        assert 0 <= config.gate_final_score <= 100
        assert 0 <= config.gate_code_score <= 100


# ─── MCP Client Tests ─────────────────────────────

class TestMCPClient:
    @pytest.mark.asyncio
    async def test_server_not_configured(self):
        """Unconfigured server should return False."""
        from src.execution.executor import MCPClient
        client = MCPClient()
        result = await client._ensure_server("nonexistent_mcp_xyz")
        assert result is False

    @pytest.mark.asyncio
    async def test_call_tool_unavailable_server(self):
        """Calling a tool on unavailable server returns None."""
        from src.execution.executor import MCPClient
        client = MCPClient()
        result = await client.call_tool("nonexistent_mcp_xyz", "test_tool")
        assert result is None

    @pytest.mark.asyncio
    async def test_shutdown_clean(self):
        """Shutdown should handle empty state gracefully."""
        from src.execution.executor import MCPClient
        client = MCPClient()
        await client.shutdown()
        assert len(client._servers) == 0


# ─── CEO Graph Tests ──────────────────────────────

class TestCEOJSONExtraction:
    def test_extract_simple_json(self):
        from src.ceo.graph import _extract_json
        result = _extract_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_extract_json_from_markdown_fence(self):
        from src.ceo.graph import _extract_json
        result = _extract_json('```json\n{"a": 1}\n```')
        assert result == {"a": 1}

    def test_extract_json_with_surrounding_text(self):
        from src.ceo.graph import _extract_json
        result = _extract_json('Hello world! {"b": 2, "c": 3} Some more text.')
        assert result == {"b": 2, "c": 3}

    def test_extract_nested_braces(self):
        from src.ceo.graph import _extract_json
        result = _extract_json('{"outer": {"inner": [1,2,3]}, "key": "val"}')
        assert result == {"outer": {"inner": [1, 2, 3]}, "key": "val"}

    def test_extract_invalid_raises(self):
        from src.ceo.graph import _extract_json
        with pytest.raises(Exception):
            _extract_json("No JSON here at all!")


# ─── Role Match Tests ─────────────────────────────

class TestRoleMatching:
    def test_code_task_matches_developer(self):
        from src.departments.roles import role_registry
        role, score = role_registry.best_match("写一个 Python Flask API")
        assert role is not None
        assert role.name == "developer"

    def test_test_task_matches_qa(self):
        from src.departments.roles import role_registry
        role, score = role_registry.best_match("写 pytest 单元测试")
        assert role is not None
        assert role.name == "qa"

    def test_deploy_task_matches_devops(self):
        from src.departments.roles import role_registry
        role, score = role_registry.best_match("部署 Docker 容器到 K8s")
        assert role is not None
        assert role.name == "devops"

    def test_research_task_matches_researcher(self):
        from src.departments.roles import role_registry
        role, score = role_registry.best_match("调研一下竞品分析")
        assert role is not None
        assert role.name == "researcher"

    def test_marketing_task_matches_marketer(self):
        from src.departments.roles import role_registry
        role, score = role_registry.best_match("写一篇公众号推广文案")
        assert role is not None
        assert role.name == "marketer"


# ─── Export Verification ──────────────────────────

class TestExports:
    def test_pending_proposal_importable(self):
        from src.memory.store import PendingProposal
        p = PendingProposal(proposal_type="test", data={"k": "v"})
        assert p.proposal_type == "test"

    def test_execution_router_importable(self):
        from src.execution.executor import ExecutionRouter
        router = ExecutionRouter()
        assert router is not None

    def test_auditor_agent_importable(self):
        from src.verification.auditor import AuditorAgent
        auditor = AuditorAgent()
        assert auditor is not None

    def test_department_agent_importable(self):
        from src.departments.agents import DepartmentAgent, dispatch_to_department
        from src.departments.roles import role_registry
        dev_role = role_registry.get("developer")
        agent = DepartmentAgent(dev_role)
        assert agent.name == "developer"
