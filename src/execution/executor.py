"""Execution Layer - MCP Protocol with CLI Fallback

For each tool, try MCP first. If MCP is unavailable or the tool doesn't
support MCP, automatically fall back to CLI commands.

Architecture:
  Tool Request → MCP Probe → Available? → MCP Call
                          ↓ No
                       CLI Call (with sandbox)
"""

import asyncio
import atexit
import shlex
import json
import os
import sys
from typing import Optional
from dataclasses import dataclass, field
from enum import Enum

from src.config import config

# Track MCPClient instances for graceful cleanup on exit
_active_mcp_clients: list = []


def _cleanup_all_mcp():
    """Best-effort MCP client shutdown on process exit.

    Handles two cases:
    1. No event loop running → create one with asyncio.run()
    2. Event loop already running (e.g. inside async context manager) →
       kill subprocesses directly since we can't await from sync atexit.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — safe to use asyncio.run
        async def _shutdown_all():
            for client in list(_active_mcp_clients):
                try:
                    await client.shutdown()
                except Exception:
                    import logging
                    logging.getLogger("ai_company.mcp").debug("shutdown error", exc_info=True)
        try:
            asyncio.run(_shutdown_all())
        except Exception:
            import logging
            logging.getLogger("ai_company.mcp").debug("atexit cleanup failed", exc_info=True)
    else:
        # Loop already running — do best-effort sync cleanup
        for client in list(_active_mcp_clients):
            for name, proc in list(client._servers.items()):
                try:
                    proc.kill()
                except Exception:
                    import logging
                    logging.getLogger("ai_company.mcp").debug("kill failed", exc_info=True)
            client._servers.clear()


atexit.register(_cleanup_all_mcp)


class ExecutionMode(str, Enum):
    MCP = "mcp"
    CLI = "cli"
    API = "api"
    RPC = "rpc"


@dataclass
class ToolResult:
    success: bool
    output: str
    error: Optional[str] = None
    mode: ExecutionMode = ExecutionMode.CLI
    metadata: dict = field(default_factory=dict)


# ─── MCP Client ───────────────────────────────────

class MCPClient:
    """Model Context Protocol client with proper lifecycle management.

    Manages MCP server subprocesses with:
    - Per-server process tracking and health checks
    - Auto-retry on transient failures (up to 2 retries)
    - Graceful shutdown on cleanup
    - Per-call unique request IDs to avoid collisions
    """

    def __init__(self):
        self._servers: dict[str, asyncio.subprocess.Process] = {}
        self._counter: dict[str, int] = {}  # Per-server request counter
        self._lock = asyncio.Lock()
        _active_mcp_clients.append(self)

    async def _ensure_server(self, server_name: str) -> bool:
        """Connect to an MCP server. Returns True if server is ready."""
        if server_name in self._servers:
            proc = self._servers[server_name]
            if proc.returncode is not None:
                # Server died, clean up
                del self._servers[server_name]
            else:
                return True

        server_cfg = config.mcp_servers.get(server_name)
        if not server_cfg:
            return False

        try:
            cmd = server_cfg.get("command", "")
            args = server_cfg.get("args", [])
            if not cmd:
                return False

            proc = await asyncio.create_subprocess_exec(
                cmd, *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Wait briefly for startup
            try:
                await asyncio.wait_for(
                    self._check_process_alive(proc), timeout=3.0
                )
            except asyncio.TimeoutError:
                # Server process started but readiness check timed out - ok
                import logging
                logging.getLogger("ai_company.mcp").debug(
                    "MCP server '%s' startup wait timed out (may still be ready)", server_name)

            if proc.returncode is not None:
                stderr_data = await proc.stderr.read() if proc.stderr else b""
                raise RuntimeError(
                    f"MCP server '{server_name}' exited immediately: "
                    f"{stderr_data.decode(errors='replace')[:200]}"
                )

            self._servers[server_name] = proc
            self._counter[server_name] = 0
            return True

        except FileNotFoundError:
            return False
        except Exception:
            return False

    async def _check_process_alive(self, proc):
        """Wait until the process exits (used for health check)."""
        await proc.wait()

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict = None,
    ) -> Optional[ToolResult]:
        """Call an MCP tool with retry logic. Returns None if server unavailable."""
        available = await self._ensure_server(server)
        if not available:
            return None

        max_retries = 2
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                result = await self._mcp_invoke(server, tool, arguments or {})
                return ToolResult(
                    success=True,
                    output=result,
                    mode=ExecutionMode.MCP,
                    metadata={"server": server, "tool": tool, "attempt": attempt + 1},
                )
            except (ConnectionError, BrokenPipeError, asyncio.TimeoutError) as e:
                last_error = e
                # Server connection lost, try to reconnect
                if server in self._servers:
                    proc = self._servers.pop(server)
                    try:
                        proc.kill()
                    except Exception:
                        import logging
                        logging.getLogger("ai_company.mcp").debug(
                            "Failed to kill MCP process", exc_info=True)
                if attempt < max_retries:
                    await self._ensure_server(server)
            except Exception as e:
                last_error = e
                break  # Non-retryable error

        return ToolResult(
            success=False,
            output="",
            error=str(last_error) if last_error else "Unknown MCP error",
            mode=ExecutionMode.MCP,
        )

    async def _mcp_invoke(self, server: str, tool: str, args: dict) -> str:
        """Send JSON-RPC request and read response."""
        async with self._lock:
            self._counter[server] = self._counter.get(server, 0) + 1
            request_id = self._counter[server]

        request = json.dumps({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": args,
            },
            "id": request_id,
        })

        proc = self._servers.get(server)
        if not proc or not proc.stdin or proc.returncode is not None:
            raise ConnectionError(f"MCP server '{server}' is not running")

        try:
            proc.stdin.write((request + "\n").encode())
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise ConnectionError(f"Failed to write to MCP server '{server}': {e}")

        try:
            response_line = await asyncio.wait_for(
                proc.stdout.readline(), timeout=30
            )
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(f"MCP server '{server}' response timeout (30s)")

        if not response_line:
            raise ConnectionError(f"MCP server '{server}' closed stdout unexpectedly")

        try:
            response = json.loads(response_line)
        except json.JSONDecodeError as e:
            raise ValueError(f"MCP server '{server}' returned invalid JSON: {e}")

        if "error" in response:
            err = response["error"]
            raise RuntimeError(f"MCP error: {err.get('message', 'Unknown')}")

        return json.dumps(response.get("result", {}))

    async def shutdown(self):
        """Gracefully shut down all MCP server processes."""
        for name, proc in list(self._servers.items()):
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                import logging
                logging.getLogger("ai_company.mcp").debug("stdin close failed", exc_info=True)
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                try:
                    proc.kill()
                except Exception:
                    import logging
                    logging.getLogger("ai_company.mcp").debug("kill during shutdown failed", exc_info=True)
        self._servers.clear()
        self._counter.clear()
        # Remove self from global tracking list to prevent memory leak
        try:
            _active_mcp_clients.remove(self)
        except ValueError:
            _ = None  # Already removed or never registered


# ─── CLI Executor ─────────────────────────────────

class CLIExecutor:
    """Executes tools via CLI commands in a sandbox environment."""

    SANDBOX_IMAGE = "python:3.12-slim"

    async def execute(
        self,
        command: str,
        workdir: str = None,
        timeout: int = 120,
    ) -> ToolResult:
        """Execute a CLI command, optionally in Docker sandbox."""
        if workdir is None:
            workdir = os.getcwd()

        if config.sandbox_provider == "docker":
            return await self._docker_exec(command, workdir, timeout)
        elif config.sandbox_provider == "e2b":
            return await self._e2b_exec(command, workdir, timeout)
        else:
            # Default: local exec
            return await self._local_exec(command, workdir, timeout)

    # SECURITY: shell=True is used ONLY for TOOL_REGISTRY pre-defined commands
    # (not user input). The command string comes from hardcoded TOOL_REGISTRY
    # templates with format() substitution of sanitized parameters.
    # DO NOT pass user-supplied strings directly to this method.
    async def _local_exec(self, command: str, workdir: str, timeout: int) -> ToolResult:
        """Execute locally (for trusted environments)."""
        proc = None
        # Inherit parent env + inject PYTHONPATH so subprocess can find src.* modules
        child_env = os.environ.copy()
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        existing_path = child_env.get("PYTHONPATH", "")
        child_env["PYTHONPATH"] = f"{project_root}:{existing_path}" if existing_path else project_root
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=child_env,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )

            return ToolResult(
                success=proc.returncode == 0,
                output=stdout.decode("utf-8", errors="replace"),
                error=stderr.decode("utf-8", errors="replace") if stderr else None,
                mode=ExecutionMode.CLI,
            )
        except asyncio.TimeoutError:
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            return ToolResult(False, "", "Command timed out", ExecutionMode.CLI)
        except asyncio.CancelledError:
            # User interrupted — kill subprocess and re-raise
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            raise
        except Exception as e:
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            return ToolResult(False, "", str(e), ExecutionMode.CLI)

    # SECURITY: shell=True used only for TOOL_REGISTRY pre-defined commands.
    # Docker provides an additional isolation layer beyond the shell.
    # DO NOT pass user-supplied strings directly to this method.
    async def _docker_exec(self, command: str, workdir: str, timeout: int) -> ToolResult:
        """Execute inside Docker container for isolation."""
        docker_cmd = (
            f"docker run --rm -v {workdir}:/workspace "
            f"-w /workspace {self.SANDBOX_IMAGE} "
            f"bash -c {json.dumps(command)}"
        )
        return await self._local_exec(docker_cmd, workdir, timeout)

    async def _e2b_exec(self, command: str, workdir: str, timeout: int) -> ToolResult:
        """Execute in E2B cloud sandbox."""
        try:
            from e2b_code_interpreter import CodeInterpreter

            sandbox = CodeInterpreter(api_key=config.e2b_api_key)
            execution = sandbox.notebook.exec_cell(command)

            return ToolResult(
                success=not execution.error,
                output=str(execution.text) if execution.text else "",
                error=str(execution.error) if execution.error else None,
                mode=ExecutionMode.API,
                metadata={"sandbox_id": sandbox.sandbox_id},
            )
        except ImportError:
            return ToolResult(False, "", "e2b not installed", ExecutionMode.CLI)
        except Exception as e:
            return ToolResult(False, "", str(e), ExecutionMode.API)


# ─── Tool Registry ────────────────────────────────

# Reserved for future use
TOOL_REGISTRY = {
    # Research tools
    "web_search": {
        "mcp_server": "brave-search",
        "mcp_tool": "search",
        "cli_command": f"{sys.executable} -m src.execution._web_tool search {{query}} 5",
        "fallback": "web_fetch",
    },
    "market_series": {
        "mcp_server": None,
        "mcp_tool": None,
        "cli_command": f"{sys.executable} -m src.execution._web_tool market_series {{query}} 30",
        "fallback": "web_search",
    },
    "web_fetch": {
        "mcp_server": None,
        "mcp_tool": None,
        "cli_command": f"{sys.executable} -m src.execution._web_tool fetch {{url}} 5000",
        "fallback": None,
    },
    # Coding tools
    "run_python": {
        "mcp_server": None,
        "mcp_tool": None,
        "cli_command": f"{sys.executable} -c {{code}}",
        "fallback": None,
        "dangerous": True,  # ⚠️ Requires sandbox isolation
    },
    "run_file": {
        "mcp_server": None,
        "mcp_tool": None,
        "cli_command": f"cd {{workdir}} && {sys.executable} {{path}}",
        "fallback": None,
        "dangerous": True,
    },
    "run_test": {
        "mcp_server": None,
        "mcp_tool": None,
        "cli_command": f"cd {{workdir}} && {sys.executable} -m pytest -v",
        "fallback": None,
    },
    "lint_code": {
        "mcp_server": None,
        "mcp_tool": None,
        "cli_command": f"cd {{workdir}} && (ruff check . 2>/dev/null || ({sys.executable} -c '\nimport os,sys\nok=0;fail=0\nfor r,d,fs in os.walk(\".\"):\n if \"site-packages\" in r or \"__pycache__\" in r: continue\n for f in fs:\n  if not f.endswith(\".py\"): continue\n  p=os.path.join(r,f)\n  try:\n   compile(open(p).read(),p,\"exec\")\n   ok+=1\n  except SyntaxError as e:\n   print(f\"SYNTAX ERROR: {{p}}:{{e.lineno}}: {{e.msg}}\", file=sys.stderr)\n   fail+=1\nprint(f\"LINT: {{ok}} files OK, {{fail}} syntax errors\")\n'))",
        "fallback": None,
    },
    "format_code": {
        "mcp_server": None,
        "mcp_tool": None,
        "cli_command": f"cd {{workdir}} && (ruff format . 2>/dev/null || echo 'ruff not installed, skipping format')",
        "fallback": None,
    },
    "git_commit": {
        "mcp_server": None,
        "mcp_tool": None,
        "cli_command": "cd {workdir} && git add -A && git commit -m {message}",
        "fallback": None,
    },
    # Filesystem tools
    "read_file": {
        "mcp_server": "filesystem",
        "mcp_tool": "read_file",
        "cli_command": "cat {path}",
        "fallback": None,
    },
    "write_file": {
        "mcp_server": "filesystem",
        "mcp_tool": "write_file",
        "cli_command": f"{sys.executable} -c 'import sys; open(sys.argv[1],\"w\").write(sys.stdin.read())' {{path}} << 'HEREDOC_END'\n{{content}}\nHEREDOC_END",
        "fallback": None,
    },
    "list_dir": {
        "mcp_server": "filesystem",
        "mcp_tool": "list_directory",
        "cli_command": "ls -la {path}",
        "fallback": None,
    },
    # System tools
    "wechat_send": {
        "mcp_server": None,
        "mcp_tool": None,
        "cli_command": f"{sys.executable} -m src.execution._wechat_tool send {{contact}} {{message}}",
        "fallback": None,
        "description": "Send a WeChat message to a contact via AppleScript (macOS only)",
    },
}


def _extract_maxiter_output(raw: str, tool_calls: list) -> str:
    """Extract meaningful content from a max-iterations LLM response."""
    import json as _json, re as _re
    # Try to parse as JSON
    try:
        parsed = _json.loads(raw.strip()) if raw.strip().startswith('{') else None
        if parsed:
            # If it's a valid action=final, use the output
            if parsed.get("action") == "final" and parsed.get("output"):
                return parsed["output"]
            # If it's a tool call, note what was attempted
            if parsed.get("action") == "tool":
                tool_name = parsed.get("tool", "?")
                return (
                    f"Unable to complete task after {len(tool_calls)} tool calls.\n"
                    f"Last tool attempted: {tool_name}\n"
                    f"Tools used: {', '.join(dict.fromkeys(tc.get('tool','?') for tc in tool_calls))}"
                )
    except (_json.JSONDecodeError, Exception):
        pass
    # Try to extract text before any tool call JSON
    text_parts = _re.split(r'\{\s*"action"\s*:\s*"tool"', raw)
    text = text_parts[0].strip()
    if len(text) > 20:
        return text[:2000]
    return raw[:500]


# ─── Execution Router ────────────────────────────

class ExecutionRouter:
    """Routes tool calls: MCP first, CLI fallback.

    For each tool call:
    1. Check if MCP server is configured and available
    2. If yes → use MCP
    3. If no → use CLI command
    4. If CLI fails → try registered fallback
    """

    def __init__(self):
        self.mcp = MCPClient()
        self.cli = CLIExecutor()

    @staticmethod
    def _normalize_tool_failure(tool_name: str, result: ToolResult) -> ToolResult:
        """Convert string-encoded web errors into real tool failures."""
        if not result.success:
            return result
        output = (result.output or "").strip()
        if tool_name == "web_search":
            failure_prefixes = (
                "SEARCH FAILED",
                "SEARCH BLOCKED",
                "SEARCH DOWN",
                "No results found",
            )
            if any(output.startswith(prefix) for prefix in failure_prefixes):
                return ToolResult(
                    success=False,
                    output=result.output,
                    error=output.splitlines()[0][:200],
                    mode=result.mode,
                    metadata=result.metadata,
                )
        if tool_name == "web_fetch":
            failure_prefixes = (
                "HTTP ",
                "Network error:",
                "ERROR: Cannot decode response",
            )
            if any(output.startswith(prefix) for prefix in failure_prefixes):
                return ToolResult(
                    success=False,
                    output=result.output,
                    error=output.splitlines()[0][:200],
                    mode=result.mode,
                    metadata=result.metadata,
                )
        return result

    @staticmethod
    def _has_structured_time_series(text: str) -> bool:
        """Detect monthly/date-value lines that are sufficient for a summary answer."""
        import re

        matches = re.findall(r"^\d{4}-\d{2}-\d{2}:\s*[-+]?\d[\d.,]*", text, flags=re.MULTILINE)
        return len(matches) >= 3

    async def route(
        self,
        department: str,
        task: str,
        system_prompt: str,
        workspace_dir: str = "",
        max_iterations: int = 3,
    ) -> dict:
        """Route a department task through the execution layer.

        Uses a ReAct-style tool loop:
        1. LLM receives task + available tools in system prompt
        2. LLM decides: produce final answer OR request a tool
        3. If tool requested → execute → feed result back → repeat
        4. If final answer → return

        Tools available: web_search, run_python, read_file, write_file,
        list_dir, run_test, lint_code, format_code, web_fetch.
        """
        import logging
        from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
        from src.ceo.graph import _get_llm, _extract_json

        logger = logging.getLogger("ai_company.execution")
        llm = _get_llm(department)

        # Build tool-augmented system prompt
        tool_system = self._build_tool_prompt(system_prompt, workspace_dir)

        messages = [
            SystemMessage(content=tool_system),
            HumanMessage(content=f"Task: {task}"),
        ]

        tool_calls_made: list[dict] = []
        consecutive_failures = 0  # Smart degradation counter

        for iteration in range(1, max_iterations + 1):
            logger.debug(
                "Tool loop iteration %d/%d for dept=%s",
                iteration, max_iterations, department,
            )

            # Last iteration: force final answer
            if iteration == max_iterations:
                messages.append(HumanMessage(
                    content=(
                        "This is your LAST iteration. You MUST produce action=final NOW. "
                        "Summarize what you found from tools. Do NOT request more tools. "
                        'Format: {\"action\":\"final\",\"output\":\"your summary with data\"}'
                    )
                ))
            response = await llm.ainvoke(messages)
            raw = str(response.content)

            # Parse LLM response
            parsed = self._parse_agent_response(raw, iteration)

            if parsed["action"] == "final":
                # Done - return final output
                return {
                    "success": True,
                    "output": parsed["output"],
                    "department": department,
                    "mode": "llm+tools",
                    "iterations": iteration,
                    "tool_calls": tool_calls_made,
                }

            elif parsed["action"] == "tool":
                tool_name = parsed.get("tool", "")
                params = parsed.get("params", {})

                # Validate tool exists
                if tool_name not in TOOL_REGISTRY:
                    known = ", ".join(TOOL_REGISTRY.keys())
                    feedback = (
                        f"Unknown tool '{tool_name}'. Available: {known}. "
                        f"Use a listed tool or respond with action=final."
                    )
                    consecutive_failures += 1  # Unknown tool also counts
                else:
                    # Execute the tool
                    logger.info(
                        "Agent called tool: %s with params: %s",
                        tool_name, str(params)[:200],
                    )
                    result = await self.execute_tool(tool_name, params)

                    if result.success:
                        compressed = self._rtk_compress(result.output, 500)
                        if tool_name == "web_fetch" and self._has_structured_time_series(result.output):
                            fast_output = self._rtk_compress(result.output, 1800)
                            return {
                                "success": True,
                                "output": fast_output,
                                "department": department,
                                "mode": "llm+tools(short-circuit)",
                                "iterations": iteration,
                                "tool_calls": tool_calls_made + [{
                                    "tool": tool_name,
                                    "params": str(params)[:200],
                                    "success": True,
                                }],
                            }
                            feedback = (
                                f"Tool '{tool_name}' OK.\n"
                                "You now have structured time-series data with a source.\n"
                                "Do NOT call more tools unless the user explicitly asked for another source.\n"
                                "Respond with action=final and summarize the latest data point and the recent trend.\n\n"
                                f"{compressed}"
                            )
                        else:
                            feedback = (
                                f"Tool '{tool_name}' OK.\n"
                                f"{compressed}"
                            )
                    else:
                        compressed = self._rtk_compress(result.output, 300)
                        feedback = (
                            f"Tool '{tool_name}' FAILED: {result.error or '?'}.\n"
                            f"{compressed}"
                        )

                    tool_calls_made.append({
                        "tool": tool_name,
                        "params": str(params)[:200],
                        "success": result.success,
                    })

                    # ═══ Smart degradation: consecutive tool failures ═══
                    if result.success:
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1

                    # After 3 consecutive tool failures, ask LLM to adapt
                    if consecutive_failures >= 3:
                        logger.warning(
                            "%d consecutive tool failures — suggesting alternatives",
                            consecutive_failures,
                        )
                        feedback += (
                            f"\n\nSeveral tools failed. You can still use other tools. "
                            f"If web_search failed, try web_fetch with a direct URL. "
                            f"If you have partial data, give your best answer with action=final "
                            f"and explain what data is missing."
                        )

                # Feed tool result back to LLM (truncate history to save tokens)
                messages.append(AIMessage(content=raw))
                messages.append(HumanMessage(
                    content=f"[Tool: {tool_name}] {feedback[:500]}"
                ))
                # Keep only system + task + last 6 messages to control token growth
                if len(messages) > 8:
                    messages = messages[:2] + messages[-6:]

            else:
                # Unrecognized format — push back with correction message
                if iteration < 3:
                    # Feed the raw response back so LLM sees its mistake
                    messages.append(AIMessage(content=raw))
                    messages.append(HumanMessage(
                        content=(
                            "YOUR RESPONSE FORMAT IS INVALID. You MUST use strict JSON:\n"
                            '  To call a tool: {"action":"tool","tool":"tool_name","params":{...}}\n'
                            '  For final answer: {"action":"final","output":"your answer"}\n'
                            "Do NOT reply with plain text, markdown, or any other format.\n"
                            "If you need data, call a tool. Do NOT answer from training data."
                        )
                    ))
                    if len(messages) > 10:
                        messages = messages[:2] + messages[-8:]
                    continue
                # Unrecognized format — try harder to extract meaningful content
                if iteration >= 3 and len(raw) > 10:
                    import re as _re2
                    m = _re2.search(r'"output"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"', raw)
                    if m:
                        inner = m.group(1).replace('\\n', '\n').replace('\\"', '"')
                        return {
                            "success": True,
                            "output": inner[:3000],
                            "department": department,
                            "mode": "llm+extracted",
                            "iterations": iteration,
                            "tool_calls": tool_calls_made,
                        }
                    if not raw.strip().startswith('{'):
                        return {
                            "success": True,
                            "output": raw[:3000],
                            "department": department,
                            "mode": "llm+fallback",
                            "iterations": iteration,
                            "tool_calls": tool_calls_made,
                        }
        # Fallback: all iterations exhausted, return last LLM response as dict
        return {
            "success": False,
            "output": raw[:3000],
            "department": department,
            "mode": "llm+exhausted",
            "iterations": max_iterations,
            "tool_calls": tool_calls_made,
            "error": "Max iterations exhausted without final answer",
        }

    def _build_tool_prompt(self, base_system_prompt: str, workspace_dir: str = "") -> str:
        """Augment role system prompt with tool usage instructions."""
        if not workspace_dir:
            workspace_dir = os.environ.get("AI_COMPANY_WORKSPACE", os.getcwd())
        tool_list = "\n".join(
            f"  - **{name}**: {desc['cli_command'][:80]}"
            for name, desc in sorted(TOOL_REGISTRY.items())
        )

        # Inject session context (global + per-session memories)
        session_context = ""
        try:
            from src.session import get_session_manager, get_session_context_for_prompt
            sm = get_session_manager()
            if sm and sm.current:
                ctx = get_session_context_for_prompt(sm.current.id)
                if ctx:
                    session_context = f"\n## Session Context (persistent across conversations)\n{ctx}\n"
        except Exception:
            pass

        return f"""{base_system_prompt}

## Workspace: `{workspace_dir}`
Write files here. workdir for test/lint/git = `{workspace_dir}`.
{session_context}
## Tools
{tool_list}

## Response Format
You MUST reply in STRICT JSON — one of these two formats only:
  {{"action":"final","output":"your complete answer"}}
  {{"action":"tool","tool":"tool_name","params":{{...}}}}

CRITICAL RULES:
1. For any factual/current-data query, you MUST call web_search first. Your training data is stale.
2. NEVER fabricate statistics, rankings, or "as of" data — search for real data or say you cannot find it.
3. First iteration: if the task needs facts, ALWAYS start with a tool call, not an answer.
4. Only use action=final when you have actually gathered data via tools.
Keep responses concise."""

    @staticmethod
    def _rtk_compress(text: str, max_chars: int = 0) -> str:
        """Compress tool output. Falls back to truncation."""
        if not text or len(text) < 200:
            return text
        if max_chars > 0:
            return text[:max_chars]
        return text[:3000]

    # ── Response Parser ────────────────────────

    @staticmethod
    def _parse_agent_response(raw: str, iteration: int = 1) -> dict:
        """Parse LLM response into action dict.

        iteration: current tool-loop iteration (1-based). Non-JSON responses
                   are only auto-accepted as final on iteration >= 3. On the
                   first two iterations, the LLM MUST use the JSON format.

        Returns:
            {"action": "final", "output": "..."}
            {"action": "tool", "tool": "name", "params": {...}}
            {"action": "unknown", "raw": "..."}
        """
        import re

        # Try JSON extraction (handles markdown fences)
        try:
            from src.ceo.graph import _extract_json
            parsed = _extract_json(raw)
            if isinstance(parsed, dict) and "action" in parsed:
                # Normalize v4-pro non-standard formats:
                # {"action":"web_fetch","tool":"web_fetch",...} → {"action":"tool","tool":"web_fetch",...}
                action = parsed.get("action", "")
                if action not in ("final", "tool", "unknown"):
                    if action in TOOL_REGISTRY:
                        # action IS the tool name — normalize
                        return {
                            "action": "tool",
                            "tool": action,
                            "params": parsed.get("params", {}),
                        }
                    # "action" field exists but unknown value — check if "tool" field has the tool name
                    tool_name = parsed.get("tool", "")
                    if tool_name in TOOL_REGISTRY:
                        return {
                            "action": "tool",
                            "tool": tool_name,
                            "params": parsed.get("params", {}),
                        }
                return parsed
        except (ValueError, ImportError):
            _ = None  # intentional fallthrough

        # Try to find JSON-like pattern: {"action": ...}
        json_match = re.search(
            r'\{\s*"action"\s*:\s*"(final|tool)"', raw, re.IGNORECASE
        )
        if json_match:
            # Try balanced-brace extraction
            start = json_match.start()
            depth = 0
            for i in range(start, len(raw)):
                if raw[i] == '{':
                    depth += 1
                elif raw[i] == '}':
                    depth -= 1
                    if depth == 0:
                        try:
                            import json
                            candidate = json.loads(raw[start:i + 1])
                            if isinstance(candidate, dict) and "action" in candidate:
                                return candidate
                        except json.JSONDecodeError:
                            # Partial/incomplete JSON, continue scanning
                            continue

        # Fallback: treat as final ONLY on retries (iteration >= 3).
        # On first two passes, a non-JSON answer means the LLM skipped tools —
        # push back so it gets another chance to use the correct format.
        if iteration >= 3 and len(raw) > 20:
            return {"action": "final", "output": raw}

        return {"action": "unknown", "raw": raw[:500]}

    async def execute_tool(
        self,
        tool_name: str,
        params: dict,
        prefer_mcp: bool = True,
    ) -> ToolResult:
        """Execute a specific tool, with MCP/CLI routing.

        Security: dangerous tools (run_python) require sandbox or consent.
        """

        tool_def = TOOL_REGISTRY.get(tool_name)
        if not tool_def:
            return ToolResult(False, "", f"Unknown tool: {tool_name}")

        # ═══ Security check for dangerous tools ═══
        if tool_def.get("dangerous"):
            import os as _os
            exec_mode = _os.environ.get("PYTHON_EXEC_MODE", "sandbox")
            if exec_mode == "sandbox" and config.sandbox_provider == "local":
                # Local sandbox → warn but allow with timeout limit
                import logging
                logging.getLogger("ai_company.execution").warning(
                    "DANGEROUS tool '%s' running locally without sandbox. "
                    "Set PYTHON_EXEC_MODE=sandbox and configure Docker/E2B for isolation.",
                    tool_name,
                )
                # Add execution limits to params
                if "code" in params:
                    code = params["code"]
                    # Inject safety preamble: max 5s timeout only.
                    # Keep original cwd so commands like pwd/ls return expected paths.
                    safety_prelude = (
                        "import signal, sys, os\n"
                        "signal.alarm(5)\n"
                    )
                    params["code"] = safety_prelude + code

        # Try MCP first
        if prefer_mcp and tool_def["mcp_server"]:
            result = await self.mcp.call_tool(
                tool_def["mcp_server"],
                tool_def["mcp_tool"],
                params,
            )
            if result and result.success:
                return result

        # CLI fallback
        try:
            # SECURITY: sanitize ALL params before shell interpolation
            # LLM-produced parameters are untrusted input — must be shell-escaped
            # EXCEPT: write_file 'content' goes into heredoc where quoting corrupts output
            safe_params = {}
            for k, v in params.items():
                if isinstance(v, str):
                    if tool_name == "write_file" and k == "content":
                        safe_params[k] = v  # Raw — heredoc handles safety
                    else:
                        safe_params[k] = shlex.quote(v)
                else:
                    safe_params[k] = v
            cli_cmd = tool_def["cli_command"].format(**safe_params)
            result = await self.cli.execute(cli_cmd)
        except KeyError as e:
            result = ToolResult(
                False, "",
                f"Missing parameter '{e.args[0]}' for tool '{tool_name}'.",
                ExecutionMode.CLI,
            )
        result = self._normalize_tool_failure(tool_name, result)

        # If CLI failed and there's a fallback, try it
        if not result.success and tool_def.get("fallback"):
            fallback_name = tool_def["fallback"]
            if fallback_name != tool_name:
                # Map params for fallback (e.g. web_search query → web_fetch url)
                fallback_params = dict(params)
                if tool_name == "web_search" and fallback_name == "web_fetch":
                    if "query" in fallback_params and "url" not in fallback_params:
                        fallback_params["url"] = f"https://html.duckduckgo.com/html/?q={fallback_params['query']}"
                return await self.execute_tool(fallback_name, fallback_params, prefer_mcp=False)

        return result

    async def get_available_tools(self) -> dict:
        """List all tools with their availability status."""
        available = {}
        for name, defn in TOOL_REGISTRY.items():
            mcp_available = False
            if defn["mcp_server"]:
                mcp_available = await self.mcp._ensure_server(defn["mcp_server"])

            available[name] = {
                "modes": ["mcp"] if mcp_available else [],
                "cli": True,
                "fallback": defn.get("fallback"),
            }

        return available
