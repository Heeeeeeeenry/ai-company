"""Skill loader — 从 skills/ 目录加载 Agent Skill，按需注入上下文。

支持格式：SKILL.md（Claude Code / OpenClaw / Hermes 兼容）
"""

import os
import re
import yaml
from pathlib import Path
from typing import Optional

SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from SKILL.md. Returns (metadata, body)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except Exception:
        meta = {}
    return meta, parts[2].strip()


def _load_skill(skill_dir: Path) -> Optional[dict]:
    """Load a single skill from its directory. Returns {name, metadata, body, dir} or None."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None
    text = skill_md.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)
    name = meta.get("name", skill_dir.name)
    return {
        "name": name,
        "metadata": meta,
        "body": body,
        "dir": skill_dir,
    }


def list_skills() -> list[dict]:
    """List all installed skills."""
    skills = []
    if not SKILLS_DIR.exists():
        return skills
    for d in sorted(SKILLS_DIR.iterdir()):
        if d.is_dir() and not d.name.startswith("."):
            s = _load_skill(d)
            if s:
                skills.append(s)
    return skills


def find_skill(query: str) -> Optional[dict]:
    """Find a skill matching the user query based on frontmatter description + scope keywords."""
    query_lower = query.lower()
    for skill in list_skills():
        desc = skill.get("metadata", {}).get("description", "")
        scope = skill.get("body", "")
        combined = (desc + " " + scope[:2000]).lower()
        # Check if any significant keyword from the skill matches
        # Use a simple heuristic: count overlapping words
        skill_words = set(re.findall(r"[\w\u4e00-\u9fff]+", combined))
        query_words = set(re.findall(r"[\w\u4e00-\u9fff]+", query_lower))
        overlap = skill_words & query_words
        if len(overlap) >= 3 or _trigger_phrase_match(query_lower, combined):
            return skill
    return None


def _trigger_phrase_match(query: str, scope: str) -> bool:
    """Check if query contains known trigger phrases for a skill scope."""
    # Extract trigger phrases from skill scope (Chinese and English)
    triggers = set()
    for m in re.finditer(r"[\u4e00-\u9fff]{2,8}", scope):
        triggers.add(m.group())
    for m in re.finditer(r"[a-zA-Z]{3,20}", scope):
        triggers.add(m.group().lower())

    for trigger in triggers:
        if len(trigger) >= 3 and trigger in query:
            return True
    return False


def get_skill_context(skill: dict) -> str:
    """Build context string to inject into agent prompt."""
    meta = skill.get("metadata", {})
    name = skill.get("name", "unknown")
    desc = meta.get("description", meta.get("short-description", ""))
    refs_dir = skill.get("dir") / "references"
    refs = []
    if refs_dir.exists():
        refs = [f.name for f in sorted(refs_dir.iterdir()) if f.suffix in (".md", ".json", ".jsonl")]

    lines = [
        f"## Skill: {name}",
        f"Description: {desc}",
        "",
        skill.get("body", ""),
    ]
    if refs:
        lines.append(f"\nAvailable references: {', '.join(refs[:20])}")
    return "\n".join(lines)


def detect_and_load(query: str) -> Optional[str]:
    """Main entry: detect if a skill matches the query, return context if so."""
    skill = find_skill(query)
    if skill:
        return get_skill_context(skill)
    return None
