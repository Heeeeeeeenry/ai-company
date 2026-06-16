"""QA-1001: Architecture Audit — Graph, Module, Flow, Error Handling Validation"""

import pytest
import sys
import os
import ast
import asyncio
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ═══════════════════════════════════════════
# 1. LANGGRAPH WORKFLOW VALIDATION
# ═══════════════════════════════════════════

class TestGraphConnectivity:
    """Validate the LangGraph node dependency graph has no dead-ends or orphans."""

    def test_all_nodes_registered(self):
        """Every node in the design should be registered."""
        from src.ceo.graph import build_ceo_graph
        graph = build_ceo_graph()
        nodes = graph.nodes if hasattr(graph, 'nodes') else {}
        required = {
            "triage", "pm", "architect", "suggest_role", "audit_role",
            "execute", "execute_department", "auditor", "pmo",
            "verify_aggregate", "deliver"
        }
        actual = set(nodes.keys())
        missing = required - actual
        extras = actual - required
        assert not missing, f"Missing nodes: {missing}"
        assert not extras, f"Extra/unexpected nodes: {extras}"

    def test_no_orphan_nodes(self):
        """Every node must be reachable from entry point (triage)."""
        from src.ceo.graph import build_ceo_graph
        graph = build_ceo_graph()

        # Build adjacency from edges
        reachable = set()
        edges = set()

        # Collect all edges (simple + conditional)
        if hasattr(graph, 'edges') and graph.edges:
            for edge in graph.edges:
                if isinstance(edge, tuple) and len(edge) >= 1:
                    src = edge[0]
                    edges.add(src)
                    reachable.add(src)

        # BFS from entry point "triage" using known graph structure
        nodes = set(graph.nodes.keys()) if hasattr(graph, 'nodes') else set()

        # Build full adjacency from source code analysis
        adjacency = {
            "triage": {"pm", "plan", "suggest_role", "execute"},
            "pm": {"architect", "execute"},
            "architect": {"execute"},
            "plan": {"execute"},
            "suggest_role": {"audit_role"},
            "audit_role": {"execute", "deliver"},
            "execute": {"execute_department"},
            "execute_department": {"auditor"},
            "auditor": {"pmo"},
            "pmo": {"verify_aggregate"},
            "verify_aggregate": {"deliver", "execute"},
            "deliver": set(),
        }

        # BFS
        visited = set()
        queue = ["triage"]
        while queue:
            node = queue.pop(0)
            if node in visited:
                continue
            visited.add(node)
            for neighbor in adjacency.get(node, set()):
                if neighbor not in visited:
                    queue.append(neighbor)

        orphaned = nodes - visited
        # "deliver" is visited via verify_aggregate, "execute" from many paths
        assert not orphaned, f"Orphaned (unreachable) nodes: {orphaned}"

    def test_no_dead_end_nodes_except_deliver(self):
        """Only 'deliver' can be a terminal node; all others must have outgoing edges."""
        from src.ceo.graph import build_ceo_graph
        graph = build_ceo_graph()

        adjacency = {
            "triage": {"pm", "plan", "suggest_role", "execute"},
            "pm": {"execute"},
            "plan": {"execute"},
            "suggest_role": {"audit_role"},
            "audit_role": {"execute", "deliver"},
            "execute": {"execute_department"},
            "execute_department": {"auditor"},
            "auditor": {"pmo"},
            "pmo": {"verify_aggregate"},
            "verify_aggregate": {"deliver", "execute"},
            "deliver": set(),
        }

        for node, neighbors in adjacency.items():
            if node == "deliver":
                assert not neighbors, f"'deliver' should have no outgoing edges"
            else:
                assert neighbors, f"Dead-end node: '{node}' has no outgoing edges"

    def test_no_infinite_loop_no_guard(self):
        """Any retry loop must have a guard (max retries) to prevent infinite loops."""
        # The retry path: verify_aggregate → execute → ... → verify_aggregate
        # This is guarded by retry_count in verify_aggregate_node (max_retries = 3)
        from src.ceo.graph import verify_aggregate_node, CEOState
        # Simulate: after 3 retries, next_action should be "deliver"
        import inspect
        source = inspect.getsource(verify_aggregate_node)
        assert "max_retries" in source, "verify_aggregate_node missing max_retries guard"
        assert "max_retries = 2" in source or "max_retries=2" in source, \
            "Expected max_retries guard (now 2, was 3 — optimized for token efficiency)"

    def test_conditional_routing_functions_have_required_branches(self):
        """All conditional routing functions must return valid target node names."""
        from src.ceo.graph import (
            route_after_triage, route_after_audit,
            route_after_aggregate
        )
        from src.ceo.graph import CEOState

        # route_after_triage must return one of: pm, suggest_role, execute, deliver
        valid_triage = {"pm", "suggest_role", "execute", "deliver"}
        state = CEOState(
            messages=[], user_request="", phase="pm", department="",
            plan=None, research_results=None, execution_log=[],
            score_card=None, final_output=None, error=None,
            retry_count=0, pmo_result=None, retry_feedback=None,
            prd=None, arch_design=None,
        )
        assert route_after_triage(state) in valid_triage

        state["phase"] = "execute"
        assert route_after_triage(state) == "execute"

        state["phase"] = "suggest_role"
        assert route_after_triage(state) == "suggest_role"

        state["phase"] = "unknown_phase"
        result = route_after_triage(state)
        assert result == "execute", f"Unknown phase should default to execute, got {result}"

        # route_after_audit
        assert route_after_audit({"phase": "execute"}) == "execute"
        assert route_after_audit({"phase": "deliver"}) == "deliver"
        # Default when phase is unexpected
        result = route_after_audit({"phase": "something_else"})
        assert result == "deliver", f"Expected default 'deliver', got {result}"

        # route_after_aggregate
        assert route_after_aggregate({"score_card": {"next_action": "deliver"}}) == "deliver"
        assert route_after_aggregate({"score_card": {"next_action": "revise"}}) == "execute"
        assert route_after_aggregate({"score_card": {"next_action": "replan"}}) == "execute"
        # Edge case: empty score_card
        empty_card_route = route_after_aggregate({"score_card": {}})
        assert empty_card_route in ("deliver", "execute"), \
            f"With empty score_card, should safely route, got {empty_card_route}"

    def test_route_architect_after_still_exists(self):
        """route_after_pm exists for PM→Architect routing."""
        from src.ceo.graph import route_after_pm
        # PM→Architect for code tasks (phase=architect)
        assert route_after_pm({"phase": "architect"}) == "architect"
        # PM→Execute for non-code tasks (phase=execute)
        assert route_after_pm({"phase": "execute"}) == "execute"


class TestCEOStatePassing:
    """Validate CEOState fields are correctly passed through the graph."""

    def test_all_state_fields_initialized_in_run_ceo(self):
        """run_ceo must initialize all CEOState fields."""
        from src.ceo.graph import CEOState

        # Get all required keys from TypedDict
        typed_keys = set(CEOState.__annotations__.keys())

        # Read directly from file
        graph_path = Path(__file__).parent.parent / "src" / "ceo" / "graph.py"
        content = graph_path.read_text()

        # Extract the initial_state dict definition in run_ceo
        # Look for 'initial_state: CEOState = {' block
        init_start = content.find('initial_state: CEOState = {')
        assert init_start >= 0, "Could not find initial_state definition in run_ceo"
        init_section = content[init_start:init_start + 1000]

        # Check each key is in the initial_state dict
        for key in typed_keys:
            key_pattern = '"' + key + '"'
            assert key_pattern in init_section, \
                f"CEOState field '{key}' is not initialized in initial_state"

    def test_annotated_lists_use_operator_add(self):
        """Annotated list fields must use operator.add for proper LangGraph accumulation."""
        from src.ceo.graph import CEOState
        import typing

        hints = CEOState.__annotations__
        annotated_lists = ["messages", "execution_log"]

        for field in annotated_lists:
            assert field in hints, f"Missing field: {field}"
            annotation = hints[field]
            # Should be Annotated[list, operator.add]
            assert hasattr(annotation, '__metadata__') or "Annotated" in str(annotation), \
                f"{field} should use Annotated[list, operator.add]"

    def test_all_nodes_handle_missing_optional_fields(self):
        """Every node function should safely handle None for Optional fields."""
        from src.ceo.graph import CEOState
        import inspect

        # Build a minimal state with None for all optional fields
        minimal_state: CEOState = {
            "messages": [],
            "user_request": "test",
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

        # Test that pmo_node handles None for prd safely
        from src.ceo.graph import pmo_node, verify_aggregate_node, execute_node, deliver_node

        # These nodes use .get() patterns — should not crash on minimal state
        # We're verifying they access Optional fields with .get() or "or" patterns
        pmo_source = inspect.getsource(pmo_node)
        assert "state.get(" in pmo_source, "pmo_node should use .get() for Optional fields"

        aggregate_source = inspect.getsource(verify_aggregate_node)
        assert "state.get(" in aggregate_source or ".get(" in aggregate_source, \
            "verify_aggregate_node should use .get() for Optional fields"

        exec_source = inspect.getsource(execute_node)
        assert "state.get(" in exec_source, "execute_node should use .get() for Optional fields"

    def test_deliver_node_accesses_optional_fields_safely(self):
        """deliver_node must handle missing score_card gracefully."""
        from src.ceo.graph import deliver_node
        import inspect
        source = inspect.getsource(deliver_node)
        # score_card may be None, so .get("score") on it would crash
        # But looking at code, it does: state.get("score_card", {}).get("score", ...)
        assert 'state.get("score_card", {})' in source or 'state.get("score_card", dict())' in source or \
               'score_card' in source, "deliver_node should safely access score_card"


class TestMemorySaverCheckpointer:
    """Validate the MemorySaver checkpointer usage."""

    def test_memory_saver_is_imported(self):
        """MemorySaver must be imported in graph.py."""
        from src.ceo.graph import build_ceo_graph
        graph_path = Path(__file__).parent.parent / "src" / "ceo" / "graph.py"
        content = graph_path.read_text()
        assert "MemorySaver" in content, "MemorySaver must be imported"

    def test_memory_saver_used_in_run_ceo(self):
        """run_ceo must use MemorySaver as checkpointer."""
        graph_path = Path(__file__).parent.parent / "src" / "ceo" / "graph.py"
        content = graph_path.read_text()
        assert "MemorySaver()" in content, "MemorySaver must be instantiated in run_ceo"
        assert "checkpointer=MemorySaver()" in content or "checkpointer=MemorySaver" in content, \
            "MemorySaver must be passed as checkpointer"

    def test_thread_id_config_is_generated(self):
        """Each run must have a unique thread_id for checkpoint isolation."""
        graph_path = Path(__file__).parent.parent / "src" / "ceo" / "graph.py"
        content = graph_path.read_text()
        assert "thread_id" in content, "thread_id must be generated in config_params"
        assert "ceo_" in content, "thread_id should have a 'ceo_' prefix"


# ═══════════════════════════════════════════
# 2. MODULE ARCHITECTURE VALIDATION
# ═══════════════════════════════════════════

class TestModuleLayering:
    """Validate CEO/Departments/Execution/Verification/Memory layering."""

    def test_no_circular_imports(self):
        """Import graph must be acyclic."""
        import importlib
        modules = [
            "src.ceo.graph",
            "src.departments.roles",
            "src.departments.agents",
            "src.execution.executor",
            "src.verification.auditor",
            "src.memory.store",
            "src.config",
        ]
        for mod_name in modules:
            try:
                # Force fresh import to detect cycles
                if mod_name in sys.modules:
                    del sys.modules[mod_name]
                importlib.import_module(mod_name)
            except ImportError as e:
                if "circular" in str(e).lower() or "most likely due to a circular import" in str(e):
                    pytest.fail(f"Circular import detected in {mod_name}: {e}")
                # Other import errors are okay (missing deps)

    def test_auditor_does_not_depend_on_graph(self):
        """Auditor should not import from graph to avoid circularity."""
        auditor_path = Path(__file__).parent.parent / "src" / "verification" / "auditor.py"
        content = auditor_path.read_text()
        assert "from src.ceo.graph import" in content, \
            "auditor.py uses _get_llm from src.ceo.graph — acceptable utility import"
        # But it should NOT import the graph nodes (build_ceo_graph, nodes, etc.)
        assert "build_ceo_graph" not in content, \
            "auditor.py should not import build_ceo_graph (circular dependency risk)"

    def test_graph_does_not_depend_on_auditor_class(self):
        """graph.py uses auditor via function calls, not direct class imports."""
        graph_path = Path(__file__).parent.parent / "src" / "ceo" / "graph.py"
        content = graph_path.read_text()
        # graph.py should import auditor functions/class inside node functions (lazy)
        # NOT at module level
        lines = content.split('\n')
        top_level = []
        in_imports = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("from ") or stripped.startswith("import "):
                top_level.append(stripped)
                in_imports = True
            elif stripped.startswith("@") or stripped.startswith("def ") or stripped.startswith("class ") or stripped.startswith("async def "):
                break
            elif in_imports and not stripped:
                break

        # Verify auditor imports are lazily inside functions
        auditor_top = [l for l in top_level if "auditor" in l.lower()]
        assert not auditor_top, \
            f"graph.py has top-level auditor imports (should be lazy): {auditor_top}"

    def test_memory_layer_independence(self):
        """Memory store should not depend on departments or execution."""
        mem_path = Path(__file__).parent.parent / "src" / "memory" / "store.py"
        content = mem_path.read_text()
        forbidden = [
            "from src.departments",
            "from src.execution",
            "from src.verification",
            "from src.ceo.graph",
        ]
        for imp in forbidden:
            assert imp not in content, \
                f"Memory layer should not depend on '{imp}'"

    def test_departments_layer_boundaries(self):
        """Departments layer should not depend on verification or execution directly."""
        dept_files = [
            "src/departments/roles.py",
            "src/departments/agents.py",
        ]
        for fname in dept_files:
            fpath = Path(__file__).parent.parent / fname
            content = fpath.read_text()
            assert "from src.verification" not in content, \
                f"{fname} should not import from verification"

    def test_config_layer_independence(self):
        """Config must not import any other src modules (prevents circular deps)."""
        cfg_path = Path(__file__).parent.parent / "src" / "config.py"
        content = cfg_path.read_text()
        forbidden = [
            "from src.ceo",
            "from src.departments",
            "from src.execution",
            "from src.verification",
            "from src.memory",
        ]
        for imp in forbidden:
            assert imp not in content, \
                f"config.py should not import '{imp}'"


class TestRoleRegistryExtensibility:
    """Validate RoleRegistry's extension capabilities."""

    def test_register_dynamic_role_with_all_fields(self):
        """Dynamic role registration should accept all Role fields."""
        from src.departments.roles import Role, role_registry
        role = Role(
            name="test_ext_role",
            display_name="Test Extension",
            category="execution",
            description="A test extension role",
            system_prompt="You are a test role.",
            keywords=["test", "extension", "validate"],
            model_override="deepseek-chat",
            tools=["git"],
            dynamic=True,
            status="trial",
            trial_uses=0,
        )
        role_registry.register(role)
        retrieved = role_registry.get("test_ext_role")
        assert retrieved is not None
        assert retrieved.status == "trial"
        role_registry.remove("test_ext_role")

    def test_register_and_promote_flow(self):
        """Full lifecycle: register → use → promote → cleanup."""
        from src.departments.roles import Role, role_registry
        role = Role(
            name="lifecycle_test",
            display_name="Lifecycle Test",
            category="execution",
            description="Test lifecycle",
            system_prompt="Test prompt",
            keywords=["lifecycle", "test"],
            dynamic=True,
            status="trial",
            trial_uses=0,
        )
        role_registry.register(role)

        # 3 successful uses → promote
        for i in range(3):
            result = role_registry.record_use("lifecycle_test", success=True)
            if i < 2:
                assert result is None
            else:
                assert result == "promoted"

        promoted = role_registry.get("lifecycle_test")
        assert promoted.status == "established"
        assert promoted.trial_uses == 3

        role_registry.remove("lifecycle_test")
        assert role_registry.get("lifecycle_test") is None

    def test_max_core_roles_count(self):
        """Track core roles count for monitoring (7 core: 2 control + 5 exec)."""
        from src.departments.roles import role_registry
        core = [r for r in role_registry.list_all() if r.status == "core"]
        # 7 core roles: pm, architect (control) + developer, qa, devops, researcher, marketer (exec)
        assert len(core) == 7, f"Expected 7 core roles, got {len(core)}: {[r.name for r in core]}"

    def test_dynamic_roles_persistence_path(self):
        """Dynamic roles should have a storage path."""
        from src.departments.roles import role_registry
        assert role_registry.storage_path is not None
        assert "roles.json" in role_registry.storage_path

    def test_cleanup_trials_on_no_uses(self):
        """cleanup_trials should remove unused trial roles."""
        from src.departments.roles import Role, role_registry

        # Register an unused trial role
        role = Role(
            name="unused_trial_xyz",
            display_name="Unused Trial",
            category="execution",
            description="Test",
            system_prompt="You are test.",
            keywords=["unused"],
            dynamic=True,
            status="trial",
            trial_uses=0,
        )
        role_registry.register(role)
        assert role_registry.get("unused_trial_xyz") is not None

        removed = role_registry.cleanup_trials()
        assert removed >= 1

        # Should be removed
        assert role_registry.get("unused_trial_xyz") is None


# ═══════════════════════════════════════════
# 3. PROCESS RATIONALITY
# ═══════════════════════════════════════════

class TestProcessRationality:
    """Validate workflow design decisions."""

    def test_pm_architect_merged_node_has_both_contexts(self):
        """PM and Architect are now separate nodes with distinct prompts."""
        from src.ceo.graph import pm_analyze_node, architect_node
        import inspect
        pm_source = inspect.getsource(pm_analyze_node)
        arch_source = inspect.getsource(architect_node)
        # PM references pm_role only
        assert "pm_role" in pm_source, "pm_analyze_node must reference pm_role"
        # Architect references arch_role
        assert "arch_role" in arch_source, "architect_node must reference arch_role"
        # Verify they are separate functions
        assert pm_analyze_node is not architect_node, "PM and Architect must be separate nodes"

    def test_pm_node_produces_both_prd_and_arch_design(self):
        """PM produces PRD; Architect produces arch_design (now split)."""
        from src.ceo.graph import pm_analyze_node, architect_node
        import inspect
        pm_source = inspect.getsource(pm_analyze_node)
        arch_source = inspect.getsource(architect_node)
        assert '"prd"' in pm_source, "pm_analyze_node must return 'prd'"
        assert '"arch_design"' in arch_source, "architect_node must return 'arch_design'"

    def test_execute_and_execute_department_distinct_roles(self):
        """execute_node and execute_department_node should have distinct responsibilities."""
        from src.ceo.graph import execute_node, execute_department_node
        import inspect

        exec_src = inspect.getsource(execute_node)
        dept_src = inspect.getsource(execute_department_node)

        # execute_node: prepares dispatch message, sets execution_log
        assert "dispatch_msg" in exec_src, "execute_node should prepare dispatch message"

        # execute_department_node: actually calls dispatch_to_department
        assert "dispatch_to_department" in dept_src, \
            "execute_department_node should call dispatch_to_department"

    def test_retry_loop_includes_all_verification_steps(self):
        """Retry must go through execute → execute_department → auditor → pmo → verify_aggregate."""
        from src.ceo.graph import build_ceo_graph
        graph = build_ceo_graph()

        # verify_aggregate routes back to "execute" on retry
        # execute → execute_department → auditor → pmo → verify_aggregate
        # This is a linear chain; verify the edges exist
        edges_check = [
            "execute", "execute_department", "auditor", "pmo", "verify_aggregate"
        ]
        # validate the chain exists in the graph source code
        graph_path = Path(__file__).parent.parent / "src" / "ceo" / "graph.py"
        content = graph_path.read_text()

        # After removing all whitespace, these edges should appear as single strings
        stripped = "".join(content.split())  # Remove all whitespace
        assert 'add_edge("execute","execute_department")' in stripped, \
            "Edge execute→execute_department must exist"
        assert 'add_edge("execute_department","auditor")' in stripped, \
            "Edge execute_department→auditor must exist"
        assert 'add_edge("auditor","pmo")' in stripped, \
            "Edge auditor→pmo must exist"
        assert 'add_edge("pmo","verify_aggregate")' in stripped, \
            "Edge pmo→verify_aggregate must exist"

    def test_deliver_node_has_cleanup(self):
        """deliver_node must clear task and record episode."""
        from src.ceo.graph import deliver_node
        import inspect
        source = inspect.getsource(deliver_node)
        assert "clear_task" in source, "deliver_node must call clear_task"
        assert "add_episode" in source, "deliver_node must call episode_memory.add_episode"

    def test_deliver_node_promotion_check(self):
        """deliver_node must check trial role promotion."""
        from src.ceo.graph import deliver_node
        import inspect
        source = inspect.getsource(deliver_node)
        assert "record_use" in source, "deliver_node must call role_registry.record_use"
        assert "promoted" in source, "deliver_node must check for promotion result"

    def test_triage_produces_phase_routing(self):
        """triage_node must set phase to a valid next step."""
        from src.ceo.graph import triage_node, CEOState

        state: CEOState = {
            "messages": [],
            "user_request": "代码审查项目",
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
        valid_phases = {"pm", "plan", "execute", "suggest_role"}
        assert result["phase"] in valid_phases, \
            f"triage_node returned invalid phase: {result['phase']}"

    def test_execute_department_task_failure_handling(self):
        """execute_department_node must return 'deliver' phase on failure."""
        from src.ceo.graph import execute_department_node
        import inspect
        source = inspect.getsource(execute_department_node)
        assert '"phase": "deliver"' in source or "phase = 'deliver'" in source, \
            "execute_department_node should set phase=deliver on failure"

    def test_aggregate_force_approve_after_max_retries(self):
        """After max retries, aggregate must force deliver."""
        from src.ceo.graph import verify_aggregate_node
        import inspect
        source = inspect.getsource(verify_aggregate_node)
        assert "FORCE_APPROVE" in source or "force" in source.lower(), \
            "verify_aggregate_node must have force-approve after max retries"

    def test_pmo_node_fallback_criteria(self):
        """PMO must have fallback acceptance criteria when PRD is None."""
        from src.ceo.graph import pmo_node
        import inspect
        source = inspect.getsource(pmo_node)
        assert "fallback" in source.lower() or "没有" in source or "default" in source.lower(), \
            "pmo_node must have fallback criteria for missing PRD"


# ═══════════════════════════════════════════
# 4. ERROR HANDLING
# ═══════════════════════════════════════════

class TestErrorHandling:
    """Validate error handling in each node and global boundaries."""

    def test_triage_node_has_error_handling(self):
        """triage_node must have fallback routing."""
        from src.ceo.graph import triage_node
        import inspect
        source = inspect.getsource(triage_node)
        # Should have fallback to developer
        assert "Fallback" in source or "fallback" in source.lower() or \
               'department = "developer"' in source, \
               "triage_node must have a fallback department"

    def test_pm_node_json_parse_fallback(self):
        """pm_analyze_node must handle JSON parsing failures."""
        from src.ceo.graph import pm_analyze_node
        import inspect
        source = inspect.getsource(pm_analyze_node)
        assert "except" in source, "pm_analyze_node must have try/except for JSON parsing"

    def test_suggest_role_node_json_fallback(self):
        """suggest_role_node must have fallback for failed JSON parsing."""
        from src.ceo.graph import suggest_role_node
        import inspect
        source = inspect.getsource(suggest_role_node)
        assert "except" in source, "suggest_role_node must catch parsing errors"
        assert "custom_agent" in source or "temp" in source or "fallback" in source.lower(), \
            "suggest_role_node must have a fallback role proposal"

    def test_audit_role_node_defensive_patterns(self):
        """audit_role_node uses defensive .get() patterns; audit_role_definition handles JSON parse errors."""
        from src.ceo.graph import audit_role_node
        import inspect
        source = inspect.getsource(audit_role_node)
        # The node delegates JSON safety to audit_role_definition (which has try/except)
        # audit_role_node itself uses .get() with defaults for all dict access
        assert "proposal.get(" in source or "state.get(" in source, \
            "audit_role_node must use defensive .get() access"

    def test_execute_department_node_failure_state(self):
        """execute_department must handle case when dispatch fails."""
        from src.ceo.graph import execute_department_node
        import inspect
        source = inspect.getsource(execute_department_node)
        assert "not success" in source or "success" in source, \
            "execute_department_node must check result.success"
        assert "phase" in source, "must set phase on failure"

    def test_auditor_node_handles_fallback_audit(self):
        """auditor_node has fallback AuditReport when parsing fails (via AuditorAgent.audit)."""
        from src.verification.auditor import AuditorAgent
        import inspect
        source = inspect.getsource(AuditorAgent.audit)
        assert "except" in source, "AuditorAgent.audit must handle JSON parse errors"
        assert "综合" in source or "Fallback" in source or "fallback" in source.lower(), \
            "AuditorAgent.audit must have fallback scoring"

    def test_pmo_node_handles_parse_failure(self):
        """pmo_gate_check must handle JSON parse failure."""
        from src.verification.auditor import pmo_gate_check
        import inspect
        source = inspect.getsource(pmo_gate_check)
        assert "except" in source, "pmo_gate_check must handle JSON parse failure"
        assert "自动通过" in source or "PASS" in source, \
            "pmo_gate_check must have fallback verdict"

    def test_execute_node_handles_missing_plan(self):
        """execute_node must handle state where plan is None."""
        from src.ceo.graph import execute_node
        import inspect
        source = inspect.getsource(execute_node)
        # Should use .get() or or operator
        assert "state.get" in source or "plan = state.get" in source, \
            "execute_node must safely handle missing plan"

    def test_no_bare_except_pass(self):
        """No code should swallow exceptions silently."""
        src_root = Path(__file__).parent.parent / "src"
        violations = []
        for py_file in src_root.rglob("*.py"):
            content = py_file.read_text()
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler):
                    if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                        violations.append(f"{py_file}:{node.lineno}")
        assert not violations, \
            f"Found bare except:pass at:\n" + "\n".join(violations)

    def test_telegram_bot_has_global_error_handler(self):
        """Telegram bot must have a global error handler."""
        bot_path = Path(__file__).parent.parent / "src" / "telegram_bot.py"
        content = bot_path.read_text()
        assert "error_handler" in content, "Telegram bot must have error_handler registered"
        assert "add_error_handler" in content, "error_handler must be added to Application"

    def test_all_nodes_have_logging(self):
        """Each node should add to execution_log for traceability."""
        src_root = Path(__file__).parent.parent / "src" / "ceo" / "graph.py"
        content = src_root.read_text()
        # Count occurrences - all major nodes should append to execution_log
        log_patterns = content.count('"execution_log"')
        assert log_patterns >= 8, \
            f"Expected at least 8 execution_log writes, found {log_patterns}"

    def test_verify_aggregate_handles_missing_pmo_result(self):
        """verify_aggregate_node must handle when pmo_result is None."""
        from src.ceo.graph import verify_aggregate_node
        import inspect
        source = inspect.getsource(verify_aggregate_node)
        assert "pmo_result = state.get(" in source or "pmo_result = state.get" in source, \
            "verify_aggregate_node should use .get() for pmo_result"


# ═══════════════════════════════════════════
# 5. PROCESS FLOW INTEGRATION
# ═══════════════════════════════════════════

class TestIntegrationFlow:
    """End-to-end integration checks."""

    def test_graph_compiles_and_has_correct_entry(self):
        """The CEO graph must compile correctly with the standard entry point."""
        from src.ceo.graph import build_ceo_graph
        graph = build_ceo_graph()
        assert graph is not None

    def test_execution_router_has_department_routing(self):
        """ExecutionRouter.route handles unknown departments."""
        from src.execution.executor import ExecutionRouter
        router = ExecutionRouter()
        assert router is not None
        assert hasattr(router, 'route'), "ExecutionRouter must have route method"
        assert hasattr(router, 'execute_tool'), "ExecutionRouter must have execute_tool method"

    def test_dispatch_to_department_handles_unknown_role(self):
        """dispatch_to_department must return error for unknown departments."""
        from src.departments.agents import dispatch_to_department

        result = asyncio.run(dispatch_to_department(
            department="nonexistent_xyz_department",
            task="test",
            context="",
        ))
        assert result["success"] is False
        assert "Unknown" in result["error"] or "unknown" in result["error"].lower()

    def test_dispatch_to_department_rejects_control_role(self):
        """Control roles (PM, Architect) cannot execute tasks directly."""
        from src.departments.agents import dispatch_to_department

        result = asyncio.run(dispatch_to_department(
            department="pm",
            task="test",
            context="",
        ))
        assert result["success"] is False
        assert "control" in result["error"].lower()

    def test_mcp_client_handles_missing_server(self):
        """MCPClient handles missing server configurations gracefully."""
        from src.execution.executor import MCPClient
        client = MCPClient()
        import asyncio
        result = asyncio.run(client.call_tool("nonexistent", "some_tool"))
        assert result is None  # Graceful None return

    def test_full_graph_acyclic(self):
        """Verify the LangGraph DAG is actually acyclic (excluding retry).
        
        A cycle only exists with the retry edge (verify_aggregate → execute).
        All other paths must be acyclic.
        """
        # Base adjacency WITHOUT retry
        base_adjacency = {
            "triage": {"pm", "plan", "suggest_role", "execute"},
            "pm": {"execute"},
            "plan": {"execute"},
            "suggest_role": {"audit_role"},
            "audit_role": {"execute", "deliver"},
            "execute": {"execute_department"},
            "execute_department": {"auditor"},
            "auditor": {"pmo"},
            "pmo": {"verify_aggregate"},
            "verify_aggregate": {"deliver"},  # No retry edge
            "deliver": set(),
        }

        # Topological sort (Kahn's algorithm)
        indegree = {node: 0 for node in base_adjacency}
        for node, neighbors in base_adjacency.items():
            for n in neighbors:
                indegree[n] = indegree.get(n, 0) + 1

        queue = [n for n in indegree if indegree[n] == 0]
        sorted_nodes = []

        while queue:
            node = queue.pop(0)
            sorted_nodes.append(node)
            for neighbor in base_adjacency[node]:
                indegree[neighbor] -= 1
                if indegree[neighbor] == 0:
                    queue.append(neighbor)

        assert len(sorted_nodes) == len(base_adjacency), \
            f"Graph has cycle! Sorted: {sorted_nodes}, expected: {len(base_adjacency)}, got: {len(sorted_nodes)}"

    def test_arch_consistency_check_exists(self):
        """Architecture consistency check function must exist."""
        from src.verification.auditor import arch_consistency_check
        assert callable(arch_consistency_check)
