"""Workspace Memory — Shared per-task workspace across all agents.

Every task gets its own directory under ~/.ai-company/workspace/tasks/.
All agents read from and write to the same workspace, enabling:

1. Context injection: follow-up queries load previous task context
2. Source tracking: where did each piece of data come from?
3. Result persistence: what was discovered, by whom?
4. Cross-agent sharing: researcher finds sources → developer uses them

Usage:
    from src.workspace import TaskContext

    # Create workspace for a new task
    ctx = TaskContext()
    task_id = ctx.create("分析SpaceX供应链")

    # Add sources as work progresses
    ctx.add_source("https://example.com/data", {"type": "web", "data": {...}})

    # Save result
    ctx.add_result("## Analysis\n...")

    # On follow-up: load previous context
    ctx = TaskContext.load(task_id)
    context_str = ctx.get_context()  # Injects into agent prompt
"""

import json
import os
import uuid
import logging
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("ai_company.workspace")

DEFAULT_WORKSPACE = os.path.expanduser("~/.ai-company/workspace")


class TaskContext:
    """Per-task workspace with structured file storage.

    Directory structure:
        tasks/<task_id>/
          context.md      — Original task + execution summary
          sources.json    — Data sources discovered during task
          result.json     — Final output + metadata
          artifacts/      — Generated files (PDFs, images, etc.)
          index.json      — Quick-lookup metadata
    """

    def __init__(self, workspace_root: str = DEFAULT_WORKSPACE):
        self._root = workspace_root
        self._task_id: Optional[str] = None
        self._dir: Optional[str] = None

    # ─── Factory ───────────────────────────────

    def create(self, task: str, category: str = "general") -> str:
        """Create a new task workspace. Returns task_id."""
        self._task_id = _make_task_id()
        self._dir = os.path.join(self._root, "tasks", self._task_id)
        os.makedirs(os.path.join(self._dir, "artifacts"), exist_ok=True)

        # Write context.md — original task
        self._write("context.md", f"# Task\n\n{task}\n\n## Timeline\n\n"
                    f"- **{_ts()}** Created (category: {category})\n")

        # Write index.json — quick metadata
        self._write("index.json", {
            "task_id": self._task_id,
            "created_at": _ts(),
            "category": category,
            "task": task[:200],
            "status": "created",
            "source_count": 0,
        })

        logger.info("Workspace created: %s", self._task_id)
        return self._task_id

    @classmethod
    def load(cls, task_id: str, workspace_root: str = DEFAULT_WORKSPACE) -> Optional["TaskContext"]:
        """Load an existing task workspace."""
        ctx = cls(workspace_root)
        ctx._task_id = task_id
        ctx._dir = os.path.join(workspace_root, "tasks", task_id)
        if not os.path.isdir(ctx._dir):
            return None
        return ctx

    @classmethod
    def load_latest(cls, workspace_root: str = DEFAULT_WORKSPACE) -> Optional["TaskContext"]:
        """Load the most recently created task workspace."""
        tasks_dir = os.path.join(workspace_root, "tasks")
        if not os.path.isdir(tasks_dir):
            return None
        task_ids = sorted(
            [d for d in os.listdir(tasks_dir)
             if os.path.isdir(os.path.join(tasks_dir, d))],
            reverse=True,
        )
        if not task_ids:
            return None
        return cls.load(task_ids[0], workspace_root)

    # ─── Write ─────────────────────────────────

    def add_source(self, url: str, data: dict, agent: str = ""):
        """Record a data source discovered during work."""
        sources = self._read_json("sources.json", [])
        entry = {
            "url": url,
            "accessed_at": _ts(),
            "agent": agent,
            "summary": str(data)[:500],
        }
        sources.append(entry)
        self._write("sources.json", sources)

        # Update index
        idx = self._read_json("index.json", {})
        idx["source_count"] = len(sources)
        self._write("index.json", idx)

    def add_result(self, output: str, score: int = 0, department: str = "",
                    tool_calls: list = None):
        """Save the final task result."""
        result = {
            "completed_at": _ts(),
            "department": department,
            "score": score,
            "output": output[:5000],
            "tool_calls": tool_calls or [],
        }
        self._write("result.json", result)

        # Update timeline in context.md
        context = self._read("context.md", "")
        context += f"- **{_ts()}** Completed by {department} (score: {score})\n"
        self._write("context.md", context)

        # Update index
        idx = self._read_json("index.json", {})
        idx["status"] = "completed"
        idx["score"] = score
        idx["department"] = department
        self._write("index.json", idx)

    def add_timeline(self, event: str):
        """Add a timeline entry to context.md."""
        context = self._read("context.md", "")
        context += f"- **{_ts()}** {event}\n"
        self._write("context.md", context)

    def save_artifact(self, name: str, content: str) -> str:
        """Save a generated file to the task's artifacts directory."""
        path = os.path.join(self._dir, "artifacts", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    # ─── Read ──────────────────────────────────

    def get_context(self) -> str:
        """Get a context string for injection into agent prompts.

        Includes: original task, sources discovered, previous results.
        Used by execute_department_node to give agents situational awareness.
        """
        parts = []

        # Original task
        task_text = self._read("context.md", "")
        if task_text:
            parts.append(task_text[:2000])

        # Sources discovered
        sources = self._read_json("sources.json", [])
        if sources:
            src_lines = ["\n## Data Sources Already Discovered"]
            for s in sources[-10:]:  # Last 10 sources
                src_lines.append(f"- [{s.get('agent', '?')}] {s['url']}")
            parts.append("\n".join(src_lines))

        return "\n\n".join(parts) if parts else ""

    def get_sources(self) -> list[dict]:
        """Get all data sources for this task."""
        return self._read_json("sources.json", [])

    def get_result(self) -> Optional[dict]:
        """Get the final result for this task."""
        return self._read_json("result.json", None)

    def get_task(self) -> str:
        """Get the original task description."""
        context = self._read("context.md", "")
        for line in context.split("\n"):
            if line.startswith("# Task"):
                continue
            if line.startswith("##"):
                break
            # Extract first non-header, non-timeline line
            stripped = line.strip()
            if stripped and not stripped.startswith("-"):
                return stripped
        return ""

    def get_summary(self) -> dict:
        """Get a short summary for the task index."""
        return self._read_json("index.json", {})

    # ─── Query ─────────────────────────────────

    def is_followup(self, query: str) -> bool:
        """Check if a query is a likely follow-up to this task."""
        followup_kw = ["来源", "数据来源", "source", "上一步", "刚才", "上次",
                       "之前", "previous", "再查", "继续", "补充"]
        return any(kw in query for kw in followup_kw)

    @property
    def task_id(self) -> Optional[str]:
        return self._task_id

    @property
    def artifacts_dir(self) -> str:
        if not self._dir:
            return os.path.join(self._root, "tasks")
        return os.path.join(self._dir, "artifacts")

    # ─── Internal ──────────────────────────────

    def _write(self, filename: str, data: Any):
        """Write data to a file in the task workspace."""
        path = os.path.join(self._dir, filename)
        if isinstance(data, (dict, list)):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(str(data))

    def _read(self, filename: str, default: str = "") -> str:
        """Read a text file from the task workspace."""
        path = os.path.join(self._dir, filename)
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def _read_json(self, filename: str, default: Any = None) -> Any:
        """Read a JSON file from the task workspace."""
        path = os.path.join(self._dir, filename)
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return default


# ─── Helpers ──────────────────────────────────

def _make_task_id() -> str:
    """Generate a short, sortable task ID."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    short = uuid.uuid4().hex[:6]
    return f"{ts}_{short}"


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ─── Follow-up Detection ──────────────────────

FOLLOWUP_KEYWORDS = [
    "来源", "数据来源", "source", "上一步", "刚才", "上次",
    "之前的结果", "previous", "再查一下", "继续", "补充",
    "那个", "这个", "它", "怎么查的", "从哪里",
]


def is_followup_query(query: str) -> bool:
    """Detect if a query is likely a follow-up to a previous task."""
    return any(kw in query for kw in FOLLOWUP_KEYWORDS)
