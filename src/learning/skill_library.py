"""Skill Learning — Automatic workflow capture and reuse.

After a task succeeds, the system captures the tool call sequence
as a reusable "skill". On future tasks, skills are matched against
the query and injected as guidance, skipping the exploration phase.

This is the "experience accumulation" (经验积累) layer that
differentiates truly adaptive agents from fixed-pipeline ones.

Architecture:
    Task → execute → success? → capture_skill(tool_sequence)
    New Task → match_skill(query) → inject guidance → skip exploration

Storage: ~/.ai-company/skills/<name>.json
"""

import json
import os
import re
import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("ai_company.learning")

DEFAULT_SKILL_DIR = os.path.expanduser("~/.ai-company/skills")


@dataclass
class Skill:
    """A learned workflow template."""
    name: str                          # Unique name: "github_trending", "stock_lookup"
    description: str                   # Human-readable description
    triggers: list[str]                # Query patterns that match this skill
    capabilities: list[str]            # Required capabilities
    role_hint: str                     # Best department
    workflow: list[dict]               # Tool call sequence: [{tool, params_pattern, purpose}]
    success_count: int = 0             # Times this skill has worked
    fail_count: int = 0                # Times it failed
    avg_duration_s: float = 0.0        # Average execution time
    created_at: str = ""               # ISO timestamp
    last_used: str = ""                # ISO timestamp
    version: int = 1                   # Skill version (increments on refinement)

    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        return self.success_count / total if total > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "triggers": self.triggers,
            "capabilities": self.capabilities,
            "role_hint": self.role_hint,
            "workflow": self.workflow,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "avg_duration_s": self.avg_duration_s,
            "created_at": self.created_at,
            "last_used": self.last_used,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Skill":
        return cls(**{k: v for k, v in d.items()
                     if k in cls.__dataclass_fields__})


class SkillLibrary:
    """Manages learned skills: capture, match, retrieve, refine."""

    def __init__(self, skill_dir: str = DEFAULT_SKILL_DIR):
        self._dir = skill_dir
        os.makedirs(self._dir, exist_ok=True)

    # ─── CRUD ──────────────────────────────

    def save(self, skill: Skill):
        """Persist a skill to disk."""
        path = self._path(skill.name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(skill.to_dict(), f, ensure_ascii=False, indent=2)
        logger.info("Skill saved: %s (success=%d, fail=%d)",
                     skill.name, skill.success_count, skill.fail_count)

    def load(self, name: str) -> Optional[Skill]:
        """Load a skill by name."""
        path = self._path(name)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return Skill.from_dict(json.load(f))
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Failed to load skill %s: %s", name, e)
            return None

    def list_all(self) -> list[Skill]:
        """List all saved skills."""
        skills = []
        if not os.path.isdir(self._dir):
            return skills
        for fname in os.listdir(self._dir):
            if fname.endswith(".json"):
                skill = self.load(fname.replace(".json", ""))
                if skill:
                    skills.append(skill)
        skills.sort(key=lambda s: s.success_rate(), reverse=True)
        return skills

    def delete(self, name: str) -> bool:
        """Remove a skill."""
        path = self._path(name)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    # ─── Capture ───────────────────────────

    def capture(self, task: str, department: str, tool_calls: list[dict],
                capabilities: list[str], duration_s: float = 0.0,
                success: bool = True) -> Optional[Skill]:
        """Capture a successful task execution as a skill.

        Args:
            task: The original task description
            department: Department that executed it
            tool_calls: List of tool call records: [{tool, params, success}, ...]
            capabilities: Capabilities used
            duration_s: Execution time in seconds
            success: Whether the task succeeded

        Returns:
            The created or updated Skill, or None if too trivial.
        """
        if not tool_calls:
            return None  # No tool calls → nothing to learn

        # Generate a skill name from the task
        skill_name = self._generate_name(task, department)

        # Extract trigger patterns from the task
        triggers = self._extract_triggers(task)

        # Create workflow from tool calls
        workflow = self._build_workflow(tool_calls)

        # Check if this skill already exists
        existing = self.load(skill_name)
        if existing:
            # Update existing skill
            existing.success_count += 1 if success else 0
            existing.fail_count += 0 if success else 1
            existing.avg_duration_s = (
                (existing.avg_duration_s * (existing.success_count + existing.fail_count - 1) + duration_s)
                / (existing.success_count + existing.fail_count)
            )
            existing.last_used = _ts()
            existing.version += 1
            # Merge triggers
            for t in triggers:
                if t not in existing.triggers:
                    existing.triggers.append(t)
            self.save(existing)
            logger.info("Skill updated: %s (v%d)", skill_name, existing.version)
            return existing

        # Create new skill
        skill = Skill(
            name=skill_name,
            description=f"Auto-learned: {task[:100]}",
            triggers=triggers,
            capabilities=capabilities,
            role_hint=department,
            workflow=workflow,
            success_count=1 if success else 0,
            fail_count=0 if success else 1,
            avg_duration_s=duration_s,
            created_at=_ts(),
            last_used=_ts(),
        )
        self.save(skill)
        logger.info("New skill created: %s", skill_name)
        return skill

    # ─── Match ─────────────────────────────

    def match(self, query: str, min_confidence: float = 0.3) -> list[tuple[Skill, float]]:
        """Find skills that match a query.

        Returns list of (skill, confidence) sorted by confidence.

        Matching strategy:
        1. Trigger keyword overlap with query
        2. Higher weight for high-success-rate skills
        3. Bonus for recently used skills
        """
        query_lower = query.lower()
        results = []

        for skill in self.list_all():
            if not skill.triggers:
                continue

            # Count trigger keyword hits
            hits = 0
            total_weight = 0
            for trigger in skill.triggers:
                weight = len(trigger)  # Longer triggers are more specific
                total_weight += weight
                if trigger.lower() in query_lower:
                    # Weighted match: longer triggers count more
                    hits += weight

            if total_weight == 0:
                continue

            raw_score = hits / total_weight

            # Boost by success rate
            sr = skill.success_rate()
            confidence = raw_score * (0.5 + 0.5 * sr)

            if confidence >= min_confidence:
                results.append((skill, confidence))

        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def best_match(self, query: str) -> tuple[Optional[Skill], float]:
        """Find the single best matching skill."""
        matches = self.match(query, min_confidence=0.2)
        return matches[0] if matches else (None, 0.0)

    def inject_context(self, query: str) -> str:
        """Get skill guidance for injection into agent prompt.

        If a matching skill exists, returns guidance text.
        Otherwise returns empty string.
        """
        skill, confidence = self.best_match(query)
        if not skill or confidence < 0.3:
            return ""

        steps = []
        for i, step in enumerate(skill.workflow, 1):
            purpose = step.get("purpose", "")
            tool = step.get("tool", "?")
            steps.append(f"  {i}. {tool}" + (f" — {purpose}" if purpose else ""))

        return (
            f"[Learned Skill: {skill.name}]\n"
            f"Success rate: {skill.success_rate():.0%} ({skill.success_count} uses)\n"
            f"Recommended workflow:\n"
            + "\n".join(steps) +
            f"\n\nFollow this workflow. You can deviate if needed, but this has worked before."
        )

    # ─── Internal ──────────────────────────

    def _path(self, name: str) -> str:
        safe = name.replace("/", "_").replace("..", "_")
        return os.path.join(self._dir, f"{safe}.json")

    def _generate_name(self, task: str, department: str) -> str:
        """Generate a short, descriptive skill name from a task."""
        # Remove common prefixes
        cleaned = re.sub(r'^(帮我|请|麻烦|能不能|可以|来)\s*', '', task)
        # Take first meaningful words
        words = cleaned.split()
        name_words = []
        for w in words[:4]:
            # Skip short function words
            if len(w) <= 1 and w not in ('AI', 'Go', 'C', 'R'):
                continue
            name_words.append(w)
        name = '_'.join(name_words[:3]).lower()
        # Clean non-alphanumeric
        name = re.sub(r'[^a-z0-9_\u4e00-\u9fff]', '', name)
        return f"{department}_{name}"[:64]

    def _extract_triggers(self, task: str) -> list[str]:
        """Extract trigger patterns from a task description."""
        triggers = []

        # Use the full task as one trigger
        triggers.append(task)

        # Extract key phrases (2-4 character Chinese phrases, or English bigrams)
        # Chinese: extract 2-4 char substrings
        chinese_chars = re.findall(r'[\u4e00-\u9fff]{2,4}', task)
        for cc in chinese_chars[:5]:
            if cc not in triggers:
                triggers.append(cc)

        # English: extract keywords
        english_words = re.findall(r'[a-zA-Z]{3,}', task.lower())
        for ew in english_words[:3]:
            if ew not in ['the', 'and', 'for', 'that', 'this', 'with']:
                triggers.append(ew)

        return triggers[:8]  # Keep top triggers

    def _build_workflow(self, tool_calls: list[dict]) -> list[dict]:
        """Build a workflow description from tool call records."""
        workflow = []
        for tc in tool_calls:
            step = {
                "tool": tc.get("tool", "?"),
                "purpose": self._infer_purpose(tc),
                "params_pattern": str(tc.get("params", ""))[:100],
                "success": tc.get("success", True),
            }
            workflow.append(step)
        return workflow

    def _infer_purpose(self, tool_call: dict) -> str:
        """Infer the purpose of a tool call from its tool name and params."""
        tool = tool_call.get("tool", "")
        purpose_map = {
            "web_search": "Find information sources",
            "web_fetch": "Read detailed content from a URL",
            "read_file": "Read file contents",
            "write_file": "Save output to file",
            "run_python": "Execute Python code",
            "run_file": "Run a Python file",
            "run_test": "Execute tests",
            "lint_code": "Check code quality",
            "format_code": "Format code",
            "git_commit": "Commit changes",
            "list_dir": "Explore directory structure",
            "market_series": "Query financial data",
        }
        return purpose_map.get(tool, f"Execute {tool}")


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ─── Global singleton ──────────────────────

skill_library = SkillLibrary()
