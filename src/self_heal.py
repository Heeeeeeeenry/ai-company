"""Self-Healing Engine — auto-detect and fix code errors.

When the AI Company encounters a Python exception or tool failure,
the self-heal engine:
1. Parses the error traceback
2. Identifies the problematic file and line
3. Dispatches the developer agent to read→fix→write→verify
4. Reports whether the fix was applied

Triggered automatically on FAIL verdicts, or manually via CLI /fix.
"""

import os
import re
import logging
import traceback
from typing import Optional

logger = logging.getLogger("ai_company.self_heal")

# Directories the engine is allowed to modify
ALLOWED_DIRS = [
    os.path.expanduser("~/.openclaw/workspace/ai-company/src"),
]


def _is_safe_path(filepath: str) -> bool:
    """Check if a file is within the allowed modification scope."""
    real = os.path.realpath(filepath) if os.path.exists(filepath) else os.path.abspath(filepath)
    return any(real.startswith(os.path.realpath(d)) for d in ALLOWED_DIRS)


def parse_error_from_logs(execution_log: list) -> Optional[dict]:
    """Extract actionable error info from execution logs.

    Returns dict with: file, line, error_type, error_msg, traceback
    or None if no actionable error found.
    """
    # Join all log entries for multi-line traceback matching
    full_text = "\n".join(str(e) for e in execution_log)
    
    # Look for Python traceback format: File "path", line N ... Error: msg
    m = re.search(
        r'File "([^"]+)", line (\d+).*?\n(?:\s*\^+.*?\n)?\s*(\w+(?:Error|Exception|Warning)):\s*(.+)',
        full_text, re.DOTALL
    )
    if m:
        filepath = m.group(1)
        if _is_safe_path(filepath):
            return {
                "file": filepath,
                "line": int(m.group(2)),
                "error_type": m.group(3),
                "error_msg": m.group(4).strip(),
                "full_tb": m.group(0),
            }
    
    # Fallback: simple error pattern without file/line context
    m = re.search(
        r'(\w+(?:Error|Exception)):\s*(.+)',
        full_text
    )
    if m:
        return {
            "file": "unknown",
            "line": 0,
            "error_type": m.group(1),
            "error_msg": m.group(2).strip()[:200],
            "full_tb": full_text[-500:],
        }
    
    # Check for tool failure patterns
    tool_failures = [e for e in execution_log if "FAILED" in str(e) or "CRASHED" in str(e)]
    if tool_failures:
        return {
            "file": "unknown",
            "line": 0,
            "error_type": "ToolFailure",
            "error_msg": str(tool_failures[-1])[:200],
            "full_tb": "\n".join(str(t) for t in tool_failures[-3:]),
        }
    
    return None


async def attempt_repair(error_info: dict, task_context: str = "") -> dict:
    """Dispatch developer to fix a specific error.

    Args:
        error_info: Parsed error dict from parse_error_from_logs()
        task_context: Original user task that triggered the error

    Returns:
        {"fixed": bool, "file": str, "changes": str, "output": str}
    """
    from src.departments.agents import DepartmentAgent
    from src.departments.roles import role_registry
    from src.memory.artifacts import artifact_store
    
    role = role_registry.get("developer")
    if not role:
        return {"fixed": False, "error": "Developer role not found"}
    
    filepath = error_info.get("file", "unknown")
    line_num = error_info.get("line", 0)
    error_type = error_info.get("error_type", "Unknown")
    error_msg = error_info.get("error_msg", "")
    
    if filepath != "unknown" and not _is_safe_path(filepath):
        return {"fixed": False, "error": f"File outside allowed scope: {filepath}"}
    
    repair_task = (
        f"Fix the following error in the AI Company codebase:\n\n"
        f"File: {filepath}\n"
        f"Line: {line_num}\n"
        f"Error: {error_type}: {error_msg}\n\n"
        f"Workflow:\n"
        f"1. read_file to see the code around line {line_num}\n"
        f"2. Identify the root cause\n"
        f"3. Use write_file to apply the minimal fix (only change what's broken)\n"
        f"4. Run lint_code to verify the fix compiles\n"
        f"5. Report what you changed with action=final\n\n"
        f"Original task that triggered this error: {task_context[:200]}\n\n"
        f"IMPORTANT: Make MINIMAL changes. Do not refactor. Only fix the specific error."
    )
    
    logger.info("Self-heal: dispatching developer to fix %s:%d (%s)", filepath, line_num, error_type)
    
    agent = DepartmentAgent(role)
    result = await agent.execute(repair_task, context="Self-heal repair task")
    
    if result.get("success") and result.get("output"):
        # Save repair record
        artifact_store.put(
            "self_heal_last_repair",
            {
                "file": filepath,
                "line": line_num,
                "error": f"{error_type}: {error_msg}",
                "result": result["output"][:2000],
            },
            metadata={"status": "fixed" if result["success"] else "failed"},
        )
        return {
            "fixed": True,
            "file": filepath,
            "changes": result.get("output", "")[:1000],
            "output": result.get("output", ""),
        }
    
    return {
        "fixed": False,
        "file": filepath,
        "error": result.get("error", "Repair attempt failed"),
        "output": result.get("output", ""),
    }


def get_repair_history() -> list[dict]:
    """Return recent repair history."""
    from src.memory.artifacts import artifact_store
    record = artifact_store.get("self_heal_last_repair")
    if record:
        return [record]
    return []
