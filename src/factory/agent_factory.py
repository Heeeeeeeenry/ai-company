"""Dynamic Agent Factory — Create specialized agents on-the-fly.

When the CEO encounters a task that doesn't match any existing role,
the AgentFactory reasons about what kind of specialist is needed and
creates a new Role dynamically.

This is the "最强" layer — borrowed from Manus/OpenAI Deep Research:
instead of a fixed set of agents, the CEO spawns specialists as needed.

Lifecycle:
    Created → trial (0 uses)
    trial + 3 successes → established (permanent)
    trial + 0 successes after cleanup → removed

Integration:
    triage_node → no good match → AgentFactory.create(task, capabilities)
    → role_registry.register(new_role) → dispatch

Storage:
    Dynamic roles are persisted to roles.json by role_registry.
    Skills are captured by SkillLibrary on successful execution.
"""

import re
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger("ai_company.factory")


class AgentFactory:
    """Creates specialized Role objects from task analysis.

    Two modes:
    1. Template-based (fast, no LLM) — for common domain patterns
    2. LLM-driven (deep) — for novel domains
    """

    def __init__(self, llm=None):
        self._llm = llm

    def create(self, task: str, capabilities: list[str] = None,
               domain_hint: str = "") -> dict:
        """Create a new Role from a task description.

        Args:
            task: What the user wants done
            capabilities: What capabilities the agent needs
            domain_hint: Optional domain classification

        Returns:
            Role dict ready for role_registry.register()
        """
        from src.departments.roles import Role

        # Generate role identity
        role_name = self._generate_name(task, domain_hint)
        display_name = self._generate_display_name(task, domain_hint)
        domain_description = self._infer_domain(task, domain_hint)

        # Build system prompt
        system_prompt = self._build_system_prompt(task, domain_description, capabilities or [])

        # Default capabilities for unknown domain
        if not capabilities:
            capabilities = ["research", "file_io"]

        role = Role(
            name=role_name,
            display_name=display_name,
            category="execution",
            description=domain_description,
            system_prompt=system_prompt,
            keywords=self._extract_keywords(task, domain_description),
            dynamic=True,
            status="trial",
            trial_uses=0,
            created_at=datetime.now().isoformat(),
        )

        return role

    async def create_with_llm(self, task: str,
                               available_capabilities: list[str] = None) -> dict:
        """Use LLM to deeply reason about what kind of agent is needed.

        The LLM considers the task holistically and proposes a specialist
        role with the right name, description, and system prompt.
        """
        from src.departments.roles import Role
        from src.capability.planner import _extract_json

        cap_list = ", ".join(available_capabilities) if available_capabilities else "all"

        prompt = f"""Design a specialist AI agent for this task:

Task: {task}
Available capabilities: {cap_list}

Respond with JSON:
{{
  "role_name": "short_lowercase_name",
  "display_name": "Human Readable Name",
  "domain": "One-line description of expertise needed",
  "system_prompt": "Agent system prompt with workflow guidance",
  "keywords": ["kw1", "kw2", "kw3"]
}}

Rules:
1. role_name: lowercase English, use underscores (max 20 chars)
2. display_name: Chinese + English, descriptive
3. domain: What this specialist knows (e.g., "航天供应链分析", "中医方案评估")
4. system_prompt: Include role description, workflow steps, and output format
5. keywords: 3-5 Chinese/English keywords for matching
"""

        if not self._llm:
            return self.create(task, available_capabilities)

        try:
            from langchain_core.messages import HumanMessage
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            result = _extract_json(str(response.content))

            role = Role(
                name=result.get("role_name", self._generate_name(task)),
                display_name=result.get("display_name", self._generate_display_name(task)),
                category="execution",
                description=result.get("domain", self._infer_domain(task)),
                system_prompt=result.get("system_prompt", self._build_system_prompt(task, "", [])),
                keywords=result.get("keywords", self._extract_keywords(task)),
                dynamic=True,
                status="trial",
                trial_uses=0,
                created_at=datetime.now().isoformat(),
            )
            logger.info("LLM-created agent: %s", role.name)
            return role

        except Exception:
            logger.debug("LLM agent creation failed, using template")
            return self.create(task, available_capabilities)

    # ─── Internal helpers ──────────────────

    def _generate_name(self, task: str, domain_hint: str = "") -> str:
        """Generate a short, unique role name."""
        if domain_hint:
            safe = re.sub(r'[^a-z0-9_]', '', domain_hint.lower().replace(' ', '_'))
            return f"dyn_{safe}"[:25]

        # Extract key English/number tokens
        tokens = re.findall(r'[a-zA-Z0-9]{2,}', task)
        if tokens:
            return f"dyn_{'_'.join(tokens[:3]).lower()}"[:25]

        # Fallback: hash-based
        import hashlib
        h = hashlib.md5(task.encode()).hexdigest()[:8]
        return f"dyn_{h}"

    def _generate_display_name(self, task: str, domain_hint: str = "") -> str:
        """Generate a human-readable display name."""
        if domain_hint:
            return f"领域专家 ({domain_hint})"

        # Extract first 10 meaningful Chinese chars
        chinese = re.findall(r'[\u4e00-\u9fff]+', task)
        if chinese:
            tag = ''.join(chinese)[:10]
            return f"领域专家 ({tag})"

        return f"领域专家 (Generalist)"

    def _infer_domain(self, task: str, domain_hint: str = "") -> str:
        """Infer the domain from the task description."""
        if domain_hint:
            return domain_hint

        # Domain patterns
        domain_patterns = [
            (r'航天|spacex|火箭|卫星|nasa', '航天与空间技术分析'),
            (r'供应链|supply.chain|采购|物流', '供应链分析'),
            (r'中医|中药|慢性肾炎|肾病|方剂|辨证', '中医药方案评估'),
            (r'金融|股票|投资|理财|基金|债券|期货', '金融投资分析'),
            (r'法律|合同|诉讼|律师|法规', '法律咨询分析'),
            (r'医疗|诊断|药品|临床|手术|治疗', '医疗健康分析'),
            (r'教育|学习|课程|培训|考试', '教育方案设计'),
            (r'Kubernetes|k8s|容器|docker|云原生', '云原生架构'),
            (r'机器学习|深度学习|神经网络|ML|AI|模型', 'AI/ML技术'),
            (r'芯片|半导体|集成电路|CPU|GPU', '半导体技术分析'),
            (r'游戏|game|电竞|esport', '游戏产业分析'),
        ]

        for pattern, domain in domain_patterns:
            if re.search(pattern, task, re.IGNORECASE):
                return domain

        return '通用领域研究'

    def _extract_keywords(self, task: str, domain: str = "") -> list[str]:
        """Extract meaningful keywords from the task."""
        keywords = []

        # Domain words
        if domain:
            for word in re.findall(r'[\u4e00-\u9fff]{2,}', domain):
                if word not in keywords:
                    keywords.append(word)

        # Task-specific keywords
        # Chinese 2-4 char phrases
        for word in re.findall(r'[\u4e00-\u9fff]{2,4}', task):
            if word not in keywords and len(word) >= 2:
                keywords.append(word)

        # English terms
        for word in re.findall(r'[A-Za-z]{3,}', task):
            if word.lower() not in ['the', 'and', 'for', 'that']:
                keywords.append(word.lower())

        return keywords[:8]

    def _build_system_prompt(self, task: str, domain: str,
                             capabilities: list[str]) -> str:
        """Build a role-appropriate system prompt."""
        cap_desc = ", ".join(capabilities) if capabilities else "research tools"

        return f"""你是一位{domain or '通用领域'}的专家。

## 任务
{task}

## 可用能力
{cap_desc}

## 工作流程
1. 搜索收集相关信息
2. 分析验证信息的可靠性
3. 给出有依据的结论

## 输出要求
- 关键发现 + 来源引用
- 如有不确定处，诚实说明
- 简洁明了，避免冗余

## 禁止
- 编造数据
- 凭训练记忆回答实时性问题
- 超出能力范围的操作
"""


# ─── Singleton ──────────────────────────────

agent_factory = AgentFactory()
