"""Dynamic Workflow Planner — generates task-specific agent pipelines at runtime.

Instead of a hardcoded graph, the planner:
1. Analyzes the task
2. Queries the Capability Registry for needed capabilities
3. Generates a workflow plan (nodes, edges, reviewers)
4. The DynamicGraphBuilder compiles it into a LangGraph

Supports both fast-path (keyword matching) and LLM-driven planning.
"""

import json, logging, re
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger("ai_company.workflow")


@dataclass
class WorkflowNode:
    name: str           # e.g. "VisionAgent"
    agent: str          # e.g. "devops"
    capabilities: list[str]  # e.g. ["vision_analyze"]
    category: str = "action"  # "planning", "action", "verification", "delivery"

@dataclass
class WorkflowPlan:
    task: str
    nodes: list[WorkflowNode]
    reviewers: list[str]      # e.g. ["vision_verify", "fact_check"]
    reasoning: str = ""
    skip_audit: bool = False   # Fast-lane: skip Auditor/PMO


class WorkflowPlanner:
    """Dynamic workflow planner with keyword fast-path + LLM fallback."""
    
    def __init__(self):
        from src.workflow.registry import get_capability_registry
        self.registry = get_capability_registry()
    
    def plan(self, task: str) -> WorkflowPlan:
        """Generate a workflow plan for the given task."""
        # Fast-path: keyword-based
        plan = self._fast_path(task)
        if plan:
            return plan
        # LLM fallback
        return self._llm_plan(task)
    
    def _fast_path(self, task: str) -> Optional[WorkflowPlan]:
        """Keyword-based workflow generation (fast, no LLM cost)."""
        task_lower = task.lower()
        
        # WeChat send flow: VisionAgent → DesktopAgent → VerifierAgent
        if re.search(r'微信.*(?:发送|发消息|给.*发)', task_lower):
            return WorkflowPlan(
                task=task,
                nodes=[
                    WorkflowNode("VisionCheck", "devops", ["vision_analyze"], "planning"),
                    WorkflowNode("WechatAction", "devops", ["wechat_send", "screen_capture"], "action"),
                    WorkflowNode("SendVerify", "devops", ["vision_verify"], "verification"),
                ],
                reviewers=["vision_verify"],
                reasoning="WeChat send: Observe → Act → Verify closed loop",
                skip_audit=True,
            )
        
        # Local system check: ScreenCapture → ShellExec
        if re.search(r'检测.*(?:本地|运行|软件|进程)|打开.*(?:微信|应用)|pgrep|osascript', task_lower):
            return WorkflowPlan(
                task=task,
                nodes=[
                    WorkflowNode("ScreenCapture", "devops", ["screen_capture"], "planning"),
                    WorkflowNode("ShellExec", "devops", ["shell_exec"], "action"),
                ],
                reviewers=[],
                reasoning="Local system: capture + execute shell commands",
                skip_audit=True,
            )
        
        # Coding task: Developer → QA → CodeReview
        if re.search(r'写代码|开发|实现|修复|bug\b|fix\b|refactor|重构', task_lower):
            return WorkflowPlan(
                task=task,
                nodes=[
                    WorkflowNode("Developer", "developer", ["code_gen", "file_ops"], "action"),
                ],
                reviewers=["code_review"],
                reasoning="Coding: developer + code review",
                skip_audit=False,
            )
        
        # Research: Researcher with fact-checking
        if re.search(r'研究|分析|调研|查|搜|股价|金价|新闻|最新', task_lower):
            return WorkflowPlan(
                task=task,
                nodes=[
                    WorkflowNode("Researcher", "researcher", ["web_search", "web_fetch"], "action"),
                ],
                reviewers=["fact_check"],
                reasoning="Research: web search + fact verification",
                skip_audit=True,
            )
        
        # Document generation
        if re.search(r'pdf|报告|文档|导出|周报|月报', task_lower):
            return WorkflowPlan(
                task=task,
                nodes=[
                    WorkflowNode("Researcher", "researcher", ["web_search"], "action"),
                    WorkflowNode("PDFGenerator", "developer", ["pdf_gen", "file_ops"], "action"),
                ],
                reviewers=["fact_check"],
                reasoning="Document: research + PDF generation",
                skip_audit=True,
            )
        
        # Simple query: direct researcher, no review
        if re.search(r'^(?:帮我|请|可以)?(?:查|搜|什么是|怎么|为什么|谁)', task_lower):
            return WorkflowPlan(
                task=task,
                nodes=[
                    WorkflowNode("Researcher", "researcher", ["web_search"], "action"),
                ],
                reviewers=[],
                reasoning="Simple query: direct research",
                skip_audit=True,
            )
        
        # Shell commands: direct execution
        if re.search(r'^(pwd|ls|cd|cat|echo|mkdir|rm|cp|mv|grep|find|curl|wget|ps|top|kill|df|du)', task_lower):
            return WorkflowPlan(
                task=task,
                nodes=[
                    WorkflowNode("ShellExec", "devops", ["shell_exec"], "action"),
                ],
                reviewers=[],
                reasoning="Shell command: direct execution",
                skip_audit=True,
            )
        
        return None
    
    def _llm_plan(self, task: str) -> WorkflowPlan:
        """LLM-driven workflow planning for complex/novel tasks."""
        try:
            from src.ceo.graph import _get_llm
            llm = _get_llm("ceo")
            
            caps = self.registry.list_all()
            cap_list = "\n".join(
                f"- {c.name}: {c.description} (agent: {c.agent}, tools: {c.tools})"
                for c in caps
            )
            
            prompt = f"""You are a workflow planner. Given a task, plan the agent pipeline.

Available capabilities:
{cap_list}

Task: {task}

Return ONLY JSON:
{{
  "nodes": [
    {{"name": "NodeName", "agent": "developer|researcher|devops|qa|marketer", "capabilities": ["cap1", "cap2"], "category": "action|planning|verification|delivery"}}
  ],
  "reviewers": ["code_review|fact_check|vision_verify"],
  "skip_audit": true/false,
  "reasoning": "why this pipeline"
}}"""
            
            response = llm.invoke(prompt)
            raw = str(response.content)
            
            # Extract JSON
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw.strip())
            
            nodes = [
                WorkflowNode(
                    name=n.get("name", "Agent"),
                    agent=n.get("agent", "developer"),
                    capabilities=n.get("capabilities", []),
                    category=n.get("category", "action"),
                )
                for n in data.get("nodes", [])
            ]
            
            return WorkflowPlan(
                task=task,
                nodes=nodes or [
                    WorkflowNode("Researcher", "researcher", ["web_search"], "action")
                ],
                reviewers=data.get("reviewers", []),
                reasoning=data.get("reasoning", "LLM planned"),
                skip_audit=data.get("skip_audit", False),
            )
        except Exception as e:
            logger.warning("LLM workflow planning failed: %s", e)
            # Fallback: simple researcher pipeline
            return WorkflowPlan(
                task=task,
                nodes=[
                    WorkflowNode("Researcher", "researcher", ["web_search"], "action"),
                ],
                reviewers=[],
                reasoning="Fallback: simple research",
                skip_audit=True,
            )


# Singleton
_planner: Optional[WorkflowPlanner] = None

def get_workflow_planner() -> WorkflowPlanner:
    global _planner
    if _planner is None:
        _planner = WorkflowPlanner()
    return _planner
