"""Department Agents — Role-based task execution with tool support

Each execution agent is driven by a Role from the registry.
The CEO dispatches tasks to the best-matching role.
Agents now have access to real tools: file I/O, code execution, web search, etc.
Results are saved as artifacts for cross-agent sharing.
"""

import os
import logging
from dataclasses import dataclass
from typing import Optional
from langchain_core.messages import HumanMessage, SystemMessage

from src.config import config
from src.execution.executor import ExecutionRouter
from src.departments.roles import role_registry, Role

logger = logging.getLogger("ai_company.departments")


# ─── Capability Registry ──────────────────────
# Named capabilities that group tools by purpose.
# Roles are assigned capabilities, not raw tools.
# This prevents agents from calling inappropriate tools
# (e.g. Researcher calling list_dir or run_python).

@dataclass
class Capability:
    name: str              # "research", "coding", "filesystem"
    display: str           # "Research & Search"
    description: str       # What this capability does
    tools: list[str]       # Tool names from TOOL_REGISTRY
    
    def to_prompt(self) -> str:
        return f"**{self.display}**: {self.description}\n  Tools: {', '.join(self.tools)}"


CAPABILITIES = {
    "research": Capability(
        name="research",
        display="Research & Search",
        description="Search the web, fetch web pages, query market data",
        tools=["web_search", "web_fetch", "market_series"],
    ),
    "coding": Capability(
        name="coding",
        display="Code Development",
        description="Read/write files, run Python code, run tests, lint and format code",
        tools=["read_file", "write_file", "list_dir", "run_python",
               "run_file", "run_test", "lint_code", "format_code"],
    ),
    "file_io": Capability(
        name="file_io",
        display="File Read/Write",
        description="Read and write files (no directory browsing)",
        tools=["read_file", "write_file"],
    ),
    "filesystem": Capability(
        name="filesystem",
        display="File System",
        description="Read/write files, list directories, explore project structure",
        tools=["read_file", "write_file", "list_dir"],
    ),
    "vcs": Capability(
        name="vcs",
        display="Version Control",
        description="Git commit and version control operations",
        tools=["git_commit"],
    ),
    "market_data": Capability(
        name="market_data",
        display="Market Data",
        description="Query structured financial market data (K-lines, prices)",
        tools=["market_series"],
    ),
    "messaging": Capability(
        name="messaging",
        display="Messaging",
        description="Send messages via WeChat, iMessage, etc.",
        tools=["wechat_send"],
    ),
}

# ─── Role → Capability assignments ──────────
# Each role gets capabilities appropriate to its job.
# No role gets ALL capabilities — this prevents tool misuse.

ROLE_CAPABILITIES = {
    "developer": ["coding", "filesystem", "vcs", "research"],
    "qa": ["coding", "research"],
    "devops": ["filesystem", "research", "coding", "messaging"],  # coding for run_python, messaging for wechat
    "researcher": ["research", "file_io", "market_data"],
    "marketer": ["research", "file_io", "market_data"],
    "_default": ["file_io", "research"],
}

# Flatten: role → tool names (derived from capabilities)
def _build_role_tools() -> dict[str, list[str]]:
    result = {}
    for role, cap_names in ROLE_CAPABILITIES.items():
        tools = []
        for cn in cap_names:
            cap = CAPABILITIES.get(cn)
            if cap:
                for t in cap.tools:
                    if t not in tools:
                        tools.append(t)
        result[role] = tools
    return result

ROLE_TOOLS = _build_role_tools()


class DepartmentAgent:
    """A role-based department worker with real tool access.

    Uses a ReAct loop: LLM decides → call tool → get result → LLM decides → ...
    Until it produces a final answer or hits max iterations.
    Outputs are auto-saved as artifacts for follow-up queries.
    """

    # Default workspace for project work (configurable via env)
    _default_workspace = os.environ.get(
        "AI_COMPANY_WORKSPACE",
        os.path.join(os.path.dirname(__file__), "..", "..")
    )

    def __init__(self, role: Role):
        self.role = role
        self.executor = ExecutionRouter()
        # Researcher needs more iterations for search→fetch→analyze workflow
        # Developer needs more for code review: explore→read→analyze→fix→verify
        default_iter = {
            "researcher": "4",
            "developer": "8",
        }.get(role.name, "5")
        try:
            self.max_iterations = int(os.environ.get("TOOL_MAX_ITERATIONS", default_iter))
        except (ValueError, TypeError):
            self.max_iterations = int(default_iter)

    @property
    def name(self) -> str:
        return self.role.name

    def _get_allowed_tools(self, dynamic_capabilities: list[str] = None) -> list[str]:
        """Get the list of tools this role is allowed to use.

        If dynamic_capabilities is provided, resolve tools from those
        capability names (bypassing static ROLE_CAPABILITIES).
        """
        if dynamic_capabilities:
            from src.capability import resolve_tools
            return resolve_tools(dynamic_capabilities)
        return ROLE_TOOLS.get(self.role.name, ROLE_TOOLS["_default"])

    async def execute(self, task: str, context: str = "",
                     dynamic_capabilities: list[str] = None) -> dict:
        """Execute a task for this role using tool-augmented LLM loop.

        Args:
            task: The task description
            context: Additional context (PM requirements, etc.)
            dynamic_capabilities: Optional override for capabilities.
                If provided, these capability names determine the tools used,
                bypassing the static ROLE_CAPABILITIES mapping.

        Results are auto-saved as artifacts so follow-up queries
        (e.g. "data sources?") can retrieve them without re-executing.
        """
        from src.memory.artifacts import artifact_store

        allowed_tools = self._get_allowed_tools(dynamic_capabilities)

        # Inject relevant artifacts into context for follow-up queries
        artifact_context = self._load_relevant_artifacts(task, context)
        enriched_context = context
        if artifact_context:
            enriched_context = context + "\n\n" + artifact_context if context else artifact_context

        # Inject known URLs from knowledge base (skip web_search, fetch directly)
        if self.role.name == "researcher":
            url_context = self._inject_url_knowledge(task)
            if url_context:
                enriched_context = enriched_context + "\n\n" + url_context if enriched_context else url_context

        # Build enriched system prompt with tool-awareness and role context
        full_prompt = self._build_prompt(enriched_context, allowed_tools)

        logger.info(
            "Dispatching to %s (tools: %s)",
            self.role.name, ", ".join(allowed_tools),
        )

        result = await self.executor.route(
            department=self.role.name,
            task=task,
            system_prompt=full_prompt,
            workspace_dir=self._default_workspace,
            max_iterations=self.max_iterations,
        )

        # Add role metadata
        result["role"] = self.role.name
        result["role_display"] = self.role.display_name
        result["tools_available"] = allowed_tools

        # Auto-save output as artifact for future reference (best-effort)
        if result.get("success") and result.get("output"):
            try:
                artifact_store.put(
                    f"{self.role.name}_last_output",
                    {
                        "task": task,
                        "output": result["output"][:5000],
                        "tool_calls": [
                            tc.get("tool", "?") for tc in (result.get("tool_calls") or [])
                        ],
                    },
                    metadata={"role": self.role.name, "mode": result.get("mode", "?")},
                )
            except Exception:
                logger.debug("Failed to save artifact for %s", self.role.name, exc_info=True)

        return result

    def _load_relevant_artifacts(self, task: str, context: str) -> str:
        """Load artifacts relevant to the current task and return context string."""
        from src.memory.artifacts import artifact_store

        task_lower = task.lower()
        # Follow-up queries about sources, data, or previous results
        # Use word-boundary matching to avoid false positives (e.g. "data" matching "database")
        followup_kw = [
            "来源", "数据来源", "source", "上一步", "刚才", "上次",
            "之前的结果", "previous result",
        ]
        if any(kw in task_lower for kw in followup_kw):
            artifacts = artifact_store.list_all()
            if artifacts:
                lines = ["## Previously saved artifacts (use these — do NOT re-search):"]
                for a in artifacts[:5]:
                    record = artifact_store.get(a["key"])
                    if record:
                        preview = str(record.get("data", ""))[:300]
                        lines.append(f"- **{a['key']}** ({a['saved_at']}): {preview}")
                return "\n".join(lines)
        return ""

    def _inject_url_knowledge(self, task: str) -> str:
        """Look up known URLs for this query and return context string."""
        try:
            from src.execution.url_kb import find_urls
            urls = find_urls(task)
            if urls:
                url_list = "\n".join(f"  - {u}" for u in urls)
                return (
                    f"## Known data sources for this query:\n"
                    f"{url_list}\n\n"
                    f"Try web_fetch on these URLs FIRST. Only use web_search if these fail."
                )
        except Exception:
            pass
        return ""

    def _build_prompt(self, context: str, allowed_tools: list[str]) -> str:
        """Build role-specific system prompt with tool awareness."""
        tools_str = ", ".join(allowed_tools)

        # Build capability descriptions for the prompt
        cap_names = ROLE_CAPABILITIES.get(self.role.name, ROLE_CAPABILITIES["_default"])
        cap_descriptions = "\n".join(
            CAPABILITIES[cn].to_prompt()
            for cn in cap_names if cn in CAPABILITIES
        )

        # Tool-specific guidance based on role
        tool_guidance = self._get_tool_guidance()

        return f"""{self.role.system_prompt}

## Execution Context
Your available tools: {tools_str}
{tool_guidance}

## Your Capabilities
{cap_descriptions}

Context from CEO/PM:
{context if context else "No additional context provided."}

IMPORTANT: You have REAL tools. If Known data sources are provided, use web_fetch on them directly. Otherwise call web_search. Only use action=final when you have actual data."""

    def _get_tool_guidance(self) -> str:
        """Get role-specific guidance on HOW to use tools."""
        guidance = {
            "developer": (
                "Writing mode: 1) Explore with list_dir/read_file, "
                "2) Write code with write_file, "
                "3) Test with run_python or run_test, "
                "4) Check with lint_code, "
                "5) Format with format_code, "
                "6) Commit with git_commit.\n"
                "Review mode: 1) list_dir to see structure, "
                "2) read_file key files (start with config, main, entry points), "
                "3) Check for bugs/security/performance issues, "
                "4) Write findings with write_file, "
                "5) Report with action=final (use format: file:line → issue)."
            ),
            "qa": (
                "Workflow: 1) Read source with read_file, "
                "2) Write tests with write_file, "
                "3) Run tests with run_test, "
                "4) Report results."
            ),
            "researcher": (
                "Workflow: 1) Search with web_search, "
                "2) Deep-read with web_fetch, "
                "3) Synthesize findings. "
                "If artifact context is provided above, use those results "
                "directly — do NOT re-execute web_search for the same data."
            ),
            "marketer": (
                "Workflow: 1) Research with web_search, "
                "2) Write content with write_file, "
                "3) Verify readability."
            ),
            "devops": (
                "Workflow: 1) Explore with list_dir/read_file, "
                "2) Write configs with write_file, "
                "3) Test deployment setup. "
                "For WeChat/iMessage: use wechat_send(contact, message) directly."
            ),
        }
        extra = guidance.get(self.role.name, "")
        return f"Guidance: {extra}" if extra else ""


async def dispatch_to_department(
    department: str,
    task: str,
    context: str = "",
    dynamic_capabilities: list[str] = None,
) -> dict:
    """CEO calls this to dispatch work to a role.

    Looks up the role in the registry. If not found, falls back
    to a generic agent and suggests creating the role.

    Args:
        dynamic_capabilities: Optional capability names for dynamic tool selection.
    """

    role = role_registry.get(department)

    if role is None:
        # Unknown department → attempt dynamic creation via AgentFactory
        from src.factory import agent_factory
        new_role = agent_factory.create(
            task=task,
            capabilities=dynamic_capabilities,
            domain_hint="",
        )
        # Register the new role
        role = role_registry.register(new_role)
        logger.info("Dynamic agent created: %s for task: %s", role.name, task[:80])

    if role.category != "execution":
        return {
            "success": False,
            "error": f"Role '{department}' is a control role, not an execution role.",
            "output": (
                f"[BLOCKED] {role.display_name} is a control role "
                f"and cannot execute tasks directly."
            ),
            "score": 0,
        }

    agent = DepartmentAgent(role)
    result = await agent.execute(task, context, dynamic_capabilities=dynamic_capabilities)
    return result


def get_available_roles() -> dict:
    """Return all available roles grouped by category."""
    return {
        "control": [r.display_name for r in role_registry.list_control()],
        "execution": [r.display_name for r in role_registry.list_execution()],
    }
