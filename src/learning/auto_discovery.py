"""Auto Skill Discovery — Intelligent workflow capture and generalization.

Unlike the old skill_library.py which captured EVERY successful task indiscriminately
(including pwd, ls, one-off queries), this module:

1. Filters: only captures complex tasks (≥3 tool calls, score ≥70, non-trivial)
2. Extracts: LLM analyzes tool call sequence → generates reusable workflow pattern
3. Generalizes: parameter detection → replace literals with {placeholders}
4. Confirms: prompts user before saving

This mirrors the Hermes Agent approach: LLM-generated template + human approval.

Storage: ~/.ai-company/skills/ (same as skill_library, but with higher quality)
"""

import json, os, re, logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("ai_company.learning")

SKILL_DIR = os.path.expanduser("~/.ai-company/skills")

# ═══ One-off patterns that should NEVER become skills ═══
TRIVIAL_PATTERNS = [
    r"^(pwd|ls|cd|cat|echo|whoami|hostname|date|uptime|which|where)\b",
    r"^(help|hi|hello|hey|你好|您好|谢谢|ok|好的)\b",
    r"^(现在|当前|今天|昨天|明天).{0,10}(时间|日期|几点)",
    r"^查(一下|看)?\s*(时间|日期|天气)", 
    r"^你.{0,5}(功能|能力|是什么|是谁)",
]


def is_capturable(task: str, score: int, tool_calls: list[dict], department: str) -> tuple[bool, str]:
    """Decide if a task should be captured as a skill.

    Returns (should_capture, reason).
    """
    # Rule 1: Score threshold
    if score < 70:
        return False, f"score too low ({score})"

    # Rule 2: Must have meaningful tool calls
    if len(tool_calls) < 3:
        return False, f"too few tool calls ({len(tool_calls)})"

    # Rule 3: Not a one-off command
    task_lower = task.lower().strip()
    for pattern in TRIVIAL_PATTERNS:
        if re.search(pattern, task_lower):
            return False, "trivial one-off task"

    # Rule 4: Not a direct shell command
    if re.match(r"^[a-z]{2,6}(\s+-[a-z]+)*(\s+\S+)?$", task_lower.strip()):
        return False, "shell command"

    # Rule 5: Must have diverse tool types (not all the same tool)
    tools_used = {t.get("tool", t.get("name", "")) for t in tool_calls}
    if len(tools_used) < 2:
        return False, "single tool type"

    # Rule 6: Department should be execution-oriented
    if department in ("ceo", "triage"):
        return False, f"non-execution department ({department})"

    return True, "ok"


def extract_pattern(task: str, tool_calls: list[dict], department: str) -> Optional[dict]:
    """Extract a reusable skill pattern from a successful task.

    Uses LLM (deepseek-chat) to analyze the tool call sequence and generate:
    - name: short descriptive name
    - description: what this skill does
    - triggers: keyword patterns that should activate this skill
    - workflow: parameterized tool call template
    """
    if not tool_calls:
        return None

    # Build tool call summary for LLM
    tool_summary = []
    for t in tool_calls[-8:]:  # last 8 calls
        tool_name = t.get("tool", t.get("name", "?"))
        params = t.get("params", t.get("args", {}))
        success = t.get("success", t.get("ok", True))
        tool_summary.append(f"  {tool_name}({_summarize_params(params)}) → {'OK' if success else 'FAIL'}")

    prompt = f"""Analyze this successful AI agent task and extract a reusable skill template.

Task: {task[:200]}
Department: {department}
Tool calls:
{chr(10).join(tool_summary)}

Return ONLY valid JSON (no markdown fences, no commentary):
{{
  "name": "short_name_like_web_search_or_send_wechat",
  "description": "One-line description in Chinese",
  "triggers": ["keyword1", "keyword2", "keyword3"],
  "workflow": [
    {{"step": 1, "tool": "tool_name", "purpose": "why this step", "params_hint": "key params"}}
  ],
  "role_hint": "{department}",
  "is_reusable": true
}}

Rules:
- name: lowercase_underscore, 2-4 words
- triggers: 3-5 common Chinese+English query patterns users would type
- workflow: preserve the logical sequence, simplify params
- is_reusable: false if this is too specific to one query
- If not reusable (one-off search for specific thing), set is_reusable: false"""

    try:
        import openai
        api_key = os.getenv("DEEPSEEK_API_KEY", "")
        if not api_key:
            return None

        client = openai.OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=500,
            timeout=15,
        )
        text = response.choices[0].message.content or ""

        # Extract JSON
        json_match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if not json_match:
            return None
        return json.loads(json_match.group(0))

    except Exception as e:
        logger.warning("Skill pattern extraction failed: %s", e)
        return None


def generalize_params(workflow: list[dict]) -> list[dict]:
    """Replace literal values with {placeholders} in workflow steps.

    Detects common parameter types:
    - File paths → {path}
    - URLs → {url}
    - Contact names → {contact}
    - Search queries → {query}
    - Numbers → {n}
    """
    for step in workflow:
        hint = step.get("params_hint", "")
        if not hint:
            continue

        # Replace paths
        hint = re.sub(r"/[\w/.~-]+", "{path}", hint)
        # Replace URLs
        hint = re.sub(r"https?://\S+", "{url}", hint)
        # Replace quoted strings
        hint = re.sub(r"'[^']+'", "{value}", hint)
        hint = re.sub(r'"[^"]+"', "{value}", hint)
        # Replace numbers
        hint = re.sub(r"\b\d+\b", "{n}", hint)

        step["params_hint"] = hint

    return workflow


class AutoSkillDiscovery:
    """Intelligent skill capture — quality-filtered + LLM-extracted + user-confirmed."""

    def __init__(self):
        os.makedirs(SKILL_DIR, exist_ok=True)

    def should_capture(self, task: str, score: int, tool_calls: list[dict], department: str) -> bool:
        ok, reason = is_capturable(task, score, tool_calls, department)
        if not ok:
            logger.debug("Skill capture skipped: %s — %s", task[:50], reason)
        return ok

    def discover(self, task: str, tool_calls: list[dict], score: int, department: str) -> Optional[dict]:
        """Run full discovery pipeline: filter → extract → generalize → save.

        Returns the skill dict if captured, None otherwise.
        """
        if not self.should_capture(task, score, tool_calls, department):
            return None

        # Check against existing skills (dedup)
        from src.learning.skill_library import skill_library
        existing, conf = skill_library.best_match(task)
        if existing and conf > 0.5:
            logger.info("Skill already exists: %s (confidence %.2f)", existing.name, conf)
            return None

        # Extract pattern via LLM
        pattern = extract_pattern(task, tool_calls, department)
        if not pattern or not pattern.get("is_reusable"):
            return None

        # Generalize parameters
        if "workflow" in pattern:
            pattern["workflow"] = generalize_params(pattern["workflow"])

        # Save
        self._save(pattern, task, tool_calls, score, department)
        return pattern

    def _save(self, pattern: dict, task: str, tool_calls: list[dict], score: int, department: str):
        """Save discovered skill to disk."""
        name = pattern.get("name", _safe_slug(task))
        path = os.path.join(SKILL_DIR, f"{department}_{name}.json")

        skill_data = {
            "name": name,
            "description": pattern.get("description", ""),
            "triggers": pattern.get("triggers", []),
            "workflow": pattern.get("workflow", []),
            "role_hint": pattern.get("role_hint", department),
            "source_task": task[:200],
            "source_score": score,
            "tool_count": len(tool_calls),
            "created_at": datetime.now().isoformat(),
            "use_count": 0,
            "auto_discovered": True,
        }

        with open(path, "w") as f:
            json.dump(skill_data, f, ensure_ascii=False, indent=2)

        logger.info("Auto-discovered skill: %s (%d tools, score %d)", name, len(tool_calls), score)

    def suggest(self, task: str, tool_calls: list[dict], score: int, department: str) -> Optional[str]:
        """Check if a task is worth capturing and return a suggestion message.

        Returns a user-facing message string, or None if not worth capturing.
        """
        if not is_capturable(task, score, tool_calls, department)[0]:
            return None

        from src.learning.skill_library import skill_library
        existing, conf = skill_library.best_match(task)
        if existing and conf > 0.5:
            return None

        return (
            f"💡 发现可复用工作流 ({len(tool_calls)}步, 评分{score})。"
            f" 输入 /skill save 保存为技能。"
        )


def _summarize_params(params: dict) -> str:
    """Compact param summary for LLM prompt."""
    if not params:
        return ""
    items = []
    for k, v in list(params.items())[:2]:
        v_str = str(v)[:40]
        items.append(f"{k}={v_str}")
    return ", ".join(items)


def _safe_slug(text: str) -> str:
    """Generate a safe filename slug."""
    slug = re.sub(r"[^\w\u4e00-\u9fff]", "_", text.lower())[:40]
    return slug.strip("_") or "untitled"


# ═══ Module singleton ═══
auto_discovery = AutoSkillDiscovery()
