"""集成测试 — 模拟完整 CEO 工作流"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestGraphBuild:
    def test_ceo_graph_builds_without_error(self):
        """The CEO graph should compile without errors."""
        from src.ceo.graph import build_ceo_graph
        graph = build_ceo_graph()
        assert graph is not None

    def test_all_nodes_registered(self):
        """All required nodes should be in the graph."""
        from src.ceo.graph import build_ceo_graph
        graph = build_ceo_graph()
        nodes = graph.nodes if hasattr(graph, 'nodes') else {}
        required = {"triage", "pm", "execute", "execute_department",
                    "auditor", "pmo", "verify_aggregate", "deliver"}
        missing = required - set(nodes.keys())
        assert not missing, f"Missing nodes: {missing}"


class TestTriageRouting:
    def test_triage_keyword_precheck_code_review(self):
        """Code review tasks should be caught by keyword pre-check."""
        from src.ceo.graph import triage_node, CEOState
        import asyncio

        state: CEOState = {
            "messages": [],
            "user_request": "请对项目代码进行审查和打分",
            "phase": "triage",
            "department": "",
            "plan": None,
            "research_results": None,
            "execution_log": [],
            "score_card": None,
            "final_output": None,
            "error": None,
            "retry_count": 0,
            "pmo_result": None,
            "retry_feedback": None,
            "prd": None,
            "arch_design": None,
        }

        result = asyncio.run(triage_node(state))
        assert result["department"] == "developer", \
            f"Expected developer, got {result['department']}"
        # Code review tasks should go to PM first
        assert result["phase"] == "pm"

    def test_triage_keyword_precheck_deploy(self):
        """Deploy tasks should go to devops."""
        from src.ceo.graph import triage_node, CEOState
        import asyncio

        state: CEOState = {
            "messages": [],
            "user_request": "部署 Docker 容器到生产环境",
            "phase": "triage",
            "department": "",
            "plan": None,
            "research_results": None,
            "execution_log": [],
            "score_card": None,
            "final_output": None,
            "error": None,
            "retry_count": 0,
            "pmo_result": None,
            "retry_feedback": None,
            "prd": None,
            "arch_design": None,
        }

        result = asyncio.run(triage_node(state))
        assert result["department"] == "devops", \
            f"Expected devops, got {result['department']}"


class TestFullWorkflow:
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_simple_hello_world(self):
        """Full CEO workflow for simple task (will call DeepSeek API)."""
        from src.ceo.graph import run_ceo

        result = await run_ceo("写一个简单的 Python 加法函数 add(a, b)")

        assert result is not None
        assert result.get("phase") == "complete"
        output = result.get("final_output", "")
        assert "def add" in output.lower() or "add" in output.lower(), \
            f"Expected add function in output, got: {output[:200]}"


class TestConfigAfterFixes:
    def test_gate_scores_updated(self):
        """Gate scores should be at reasonable levels."""
        from src.config import config
        assert config.gate_final_score <= 70, \
            f"Gate too high: {config.gate_final_score}"
        assert config.gate_final_score >= 50

    def test_all_except_pass_fixed(self):
        """No bare except: pass should remain outside stubs."""
        import ast
        src_dir = os.path.join(os.path.dirname(__file__), "..", "src")

        bare_excepts = []
        for root, dirs, files in os.walk(src_dir):
            dirs[:] = [d for d in dirs if "__pycache__" not in d]
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(root, fname)
                tree = ast.parse(open(fpath).read())
                for node in ast.walk(tree):
                    if isinstance(node, ast.ExceptHandler):
                        # Check if body is just "pass"
                        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                            bare_excepts.append(f"{fpath}:{node.lineno}")

        assert not bare_excepts, \
            f"Found bare except:pass at:\n" + "\n".join(bare_excepts)
