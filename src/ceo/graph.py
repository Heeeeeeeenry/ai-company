# -*- coding: utf-8 -*-
"""CEO Agent - LangGraph Orchestration Engine"""
from typing import TypedDict, Annotated, Optional
from datetime import datetime
import operator
import json
import re

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.language_models import BaseChatModel

from src.config import config
from src.memory.store import (
    episode_memory, get_agent_state, PendingProposal,
    sync_episode_to_chroma,
)


# ─── State ───────────────────────────────────────

class CEOState(TypedDict):
    messages: Annotated[list, operator.add]  # Conversation history
    user_request: str                          # Original user request
    phase: str                                 # Current workflow phase
    department: str                            # Active department
    plan: Optional[dict]                       # Execution plan
    research_results: Optional[str]            # Gathered context
    execution_log: Annotated[list, operator.add]  # All actions taken
    score_card: Optional[dict]                 # Quality scores (accumulated)
    final_output: Optional[str]                # What we deliver
    error: Optional[str]                       # Error state
    retry_count: int                           # Retry counter
    pmo_result: Optional[dict]                 # PMO compliance check result
    retry_feedback: Optional[str]              # Feedback for retry
    prd: Optional[dict]                        # PM's product requirements doc
    arch_design: Optional[str]                 # Architect's design output
    workspace_id: Optional[str]                # Task workspace ID for context sharing
    task_type: Optional[str]                   # COMMAND_EXECUTION|SIMPLE_QUERY|RESEARCH|CODING|DOCUMENT|CREATIVE|GENERAL


# ─── JSON Extraction Helper ───────────────────

def _extract_json(text: str) -> dict:
    """Robust JSON extraction from LLM output with markdown fences."""
    import re
    # Try markdown code fence first
    fence_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    # Find outermost balanced braces
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                return json.loads(text[start:i+1])
    # Fallback: simple find
    s = text.find("{")
    e = text.rfind("}")
    if s >= 0 and e > s:
        return json.loads(text[s:e+1])
    raise ValueError("No valid JSON object found")


def _clean_output(raw: str) -> str:
    """Strip JSON wrapper if the LLM returned raw JSON as text.
    
    Handles: {"action":"final","output":"..."}, {"action":"tool",...}, 
    and v4-pro malformed formats like {"action":"web_fetch",...}.
    """
    import re
    # Try JSON extraction first
    try:
        parsed = _extract_json(raw)
        if isinstance(parsed, dict):
            action = parsed.get("action", "")
            if action == "final":
                return str(parsed.get("output", raw))
            # Tool call leaked → return a clean error message
            if action == "tool" or action in ("web_search", "web_fetch", "market_series",
                                               "read_file", "write_file", "list_dir",
                                               "run_python", "run_test", "lint_code"):
                return "Internal tool call leaked to output. Retry or use /fix."
    except (ValueError, json.JSONDecodeError):
        pass
    # Regex fallback for "output" field
    m = re.search(r'"output"\s*:\s*"', raw)
    if m:
        start = m.end()
        i = start
        while i < len(raw):
            if raw[i] == '\\' and i + 1 < len(raw):
                i += 2
            elif raw[i] == '"':
                inner = raw[start:i]
                return inner.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"')
            else:
                i += 1
    # If raw looks like JSON, strip it entirely
    if raw.strip().startswith('{') and raw.strip().endswith('}'):
        return "Output format error. Please ask again or use /fix."
    return raw


def _safe_str_list(items: list, key: str = "name") -> list[str]:
    """Normalize a list that may contain dicts or strings to a list of strings."""
    if not items:
        return []
    result = []
    for item in items:
        if isinstance(item, dict):
            result.append(str(item.get(key, item.get(list(item.keys())[0], "?")) if item else "?"))
        else:
            result.append(str(item))
    return result


def _parse_tool_failures(logs: list) -> int:
    """Parse tool failure count from execution logs."""
    import re
    for log in reversed(logs):
        m = re.search(r'Tool failures: (\d+)/(\d+)', str(log))
        if m:
            return int(m.group(1))
    return 0


def _parse_tools_used(logs: list) -> list[str]:
    """Extract tool names used from execution logs."""
    import re
    tools = []
    for log in logs:
        m = re.search(r'Agent called tool: (\w+)', str(log))
        if m:
            tools.append(m.group(1))
    return list(dict.fromkeys(tools))


def _parse_tools_from_timeline(context_md: str) -> list[dict]:
    """Extract tool calls from workspace context.md timeline.

    Timeline entries like: "Tools: web_search, web_fetch"
    """
    import re
    m = re.search(r'Tools: (.+)', context_md)
    if m:
        tools_str = m.group(1)
        tools = [t.strip() for t in tools_str.split(",")]
        return [{"tool": t, "params": "", "success": True} for t in tools]
    return []


def _parse_capabilities_from_timeline(context_md: str) -> list[str]:
    """Extract capability names from workspace context.md timeline.

    Timeline entries like: "Capabilities: research, file_io"
    """
    import re
    m = re.search(r'Capabilities: (.+)', context_md)
    if m:
        return [c.strip() for c in m.group(1).split(",")]
    return []


# ─── LLM Factory ─────────────────────────────────

def _get_llm(role: str = "ceo") -> BaseChatModel:
    """Create LLM instance. DeepSeek uses OpenAI-compatible API.
    Includes token tracking via TokenTracker callback."""
    mc = config.get_model_for(role)
    
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        raise ImportError("langchain-openai required. Run: pip install langchain-openai")
    
    # Token tracking callback (best-effort, non-blocking)
    from src.utils.token_tracker import get_token_tracker
    tracker = get_token_tracker()
    tracker.set_context(role=role)
    callbacks = [tracker]
    
    if mc.provider == "deepseek":
        # Reasoner needs more time (can take 30-90s), chat is faster
        is_reasoner = "reasoner" in mc.model.lower()
        return ChatOpenAI(
            model=mc.model,
            api_key=config.deepseek_api_key,
            base_url="https://api.deepseek.com/v1",
            timeout=120 if is_reasoner else 45,
            max_retries=1,
            callbacks=callbacks,
        )
    elif mc.provider == "openai":
        return ChatOpenAI(
            model=mc.model,
            api_key=config.openai_api_key,
            timeout=45,
            max_retries=1,
            callbacks=callbacks,
        )
    elif mc.provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
            return ChatAnthropic(model=mc.model, api_key=config.anthropic_api_key,
                                callbacks=callbacks)
        except ImportError:
            raise ImportError("langchain-anthropic required for Anthropic models")
    
    raise ValueError(f"Unknown provider: {mc.provider}")


# --- CEO Prompts ---

CEO_SYSTEM_PROMPT = """你是AI公司CEO. 不亲自执行, 只做: 路由意图->规划步骤->分派角色->汇总Auditor+PMO评分->交付.
分派JSON: {"action":"dispatch|ask_user|deliver","department":"...","task":"...","acceptance_criteria":"...","reasoning":"..."}
简洁决断, 一次一个任务."""


# ─── Node Functions ───────────────────────────────

def _safe_node(name: str):
    """Decorator: wraps a graph node with crash protection.
    
    If the node raises an unexpected exception, the workflow falls
    through to deliver with a clear error message instead of crashing.
    """
    def decorator(fn):
        async def wrapper(state, *args, **kwargs):
            try:
                return await fn(state, *args, **kwargs)
            except Exception as e:
                import logging
                logging.getLogger("ai_company").exception("Node '%s' crashed", name)
                return {
                    "phase": "deliver",
                    "final_output": f"[{name}] Crashed: {type(e).__name__}: {e}",
                    "execution_log": [f"[{name}] CRASHED: {type(e).__name__}: {str(e)[:200]}"],
                    "score_card": {"score": 0, "decision": "FAIL", "next_action": "deliver"},
                }
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator


@_safe_node("Triage")
async def triage_node(state: CEOState) -> dict:
    """Node function."""
    from src.departments.roles import role_registry
    import re

    llm = _get_llm("ceo")
    agent_state = get_agent_state("ceo")
    agent_state.set_task(state["user_request"])
    
    # ═══ WeChat Send Fast-Path: direct execution, no agent loop ═══
    task = state.get("user_request", "")
    wechat_match = re.search(
        r'(?:微信|wechat).*?(?:给|发送|发).*?["“”「」](.+?)["“”「」]'
        r'.*?(?:发送|发|说).*?["“”「」](.+?)["“”「」]',
        task
    )
    if wechat_match:
        contact = wechat_match.group(1).strip()
        message = wechat_match.group(2).strip()
        try:
            from src.execution._wechat_tool import send_wechat_message
            result = send_wechat_message(contact, message)
            if result["success"]:
                return {
                    "phase": "deliver",
                    "department": "devops",
                    "task_type": "LOCAL_SYSTEM",
                    "final_output": f"微信消息已发送给 {contact}：{message}",
                    "score_card": {"score": 95, "decision": "APPROVE", "final_score": 95,
                                  "next_action": "deliver"},
                    "execution_log": [f"[TRIAGE] WeChat fast-path -> sent to {contact}"],
                }
            else:
                return {
                    "phase": "deliver",
                    "department": "devops",
                    "task_type": "LOCAL_SYSTEM",
                    "final_output": f"发送失败: {result.get('error', 'Unknown')}",
                    "score_card": {"score": 0, "decision": "FAIL", "final_score": 0,
                                  "next_action": "deliver"},
                    "execution_log": [f"[TRIAGE] WeChat fast-path -> FAILED: {result.get('error')}"],
                }
        except Exception:
            pass  # Fall through to normal routing
    
    # Gather memory context (only for LLM fallback, skip for fast-path)
    memory_context = ""
    # Don't search memory yet — only needed if we fall through to LLM
    
    # Build dynamic role list for the CEO prompt
    exec_roles = role_registry.list_execution()
    role_list = "\n".join(
        f"- {r.name.upper()}: {r.description}"
        for r in exec_roles
    )
    
    # ═══ Fast-path: keyword pre-check (skip LLM for clear intents) ═══
    task_lower = state["user_request"].lower()

    # ── Workspace Context: load previous task data for follow-up queries ──
    workspace_id = state.get("workspace_id")
    workspace_context = ""
    if not workspace_id:
        # Check if this is a follow-up query
        from src.workspace import is_followup_query, TaskContext
        if is_followup_query(task_lower):
            prev = TaskContext.load_latest()
            if prev:
                workspace_id = prev.task_id
                workspace_context = prev.get_context()
                import logging
                logging.getLogger("ai_company").info("Follow-up detected — loaded workspace %s", workspace_id)

    # ── Chitchat / Greeting pre-check (skip entire pipeline) ──
    # Simple greetings and casual conversation don't need any department work.
    # The CEO handles these directly without dispatching.
    chitchat_patterns = [
        # Chinese greetings
        r'^(你好|您好|嗨|哈[喽罗]|嘿|早上好|下午好|晚上好|晚安|早啊|早呀)',
        r'^(hi|hello|hey|hiya|howdy|good morning|good afternoon|good evening)\b',
        # Pure greetings / small talk (no task intent)
        r'^(再见|拜拜|bye|see you|回头见|下次聊)',
        r'^(谢谢|多谢|thanks|thank you|thx)\b',
        r'^(嗯|哦|好[的了]?|ok|okay|知道了|明白了|懂了)\s*$',
        # Self-introduction / identity
        r'(你是谁|你叫什么|你的名字|what is your name|who are you)',
        r'(自我介绍|介绍一下?你自己|介绍.*自己|你是[什么谁]|你能做什么|你有什么功能)',
        # Status / progress check
        r'(还在.*(?:执行|做|跑|处理)|进行.*怎么样|好了没|完成了吗|进度|怎么样了)',
        r'(what.*(?:status|progress)|is it done|are you done)',
    ]
    for pattern in chitchat_patterns:
        if re.search(pattern, task_lower, re.IGNORECASE):
            # Direct CEO response — no department dispatch
            msg = state["user_request"].strip()
            # Self-intro / status
            if re.search(r'(你是谁|自我介绍|介绍.*自己|你能做什么|你有什么功能)', task_lower):
                roles = role_registry.list_all()
                exec_count = sum(1 for r in roles if r.category == "execution")
                ctrl_count = sum(1 for r in roles if r.category == "control")
                reply = (
                    f"🏢 你好！我是 **AI Company** 的 CEO.\n\n"
                    f"我管理着一家虚拟软件公司,团队有 {ctrl_count} 个管理层 + {exec_count} 个执行层 Agent:\n\n"
                    f"**管控层** 负责规划和审查:\n"
                    + "\n".join(f"  • {r.display_name} — {r.description}" for r in roles if r.category == "control")
                    + f"\n\n**执行层** 负责干活:\n"
                    + "\n".join(f"  • {r.display_name} — {r.description}" for r in roles if r.category == "execution")
                    + f"\n\n工作流程:你提需求 -> 我分派 -> PM定标准 -> 部门执行 -> Auditor+PMO打分 -> 交付.\n"
                    f"直接告诉我你要做什么,我来调度！"
                )
            elif re.search(r'(还在.*(?:执行|做|跑|处理)|进行.*怎么样|好了没|完成了吗|进度|怎么样了|status|progress)', task_lower):
                reply = (
                    "👋 我是 CEO,每个任务都是独立执行的.\n\n"
                    "如果上一个任务已经显示结果(● complete),说明已完成.\n"
                    "如果没有显示结果,可能是任务超时或出错.\n\n"
                    "你可以:\n"
                    "  • 重新描述任务,我会再次执行\n"
                    "  • 输入 /status 查看系统状态\n"
                    "  • 输入 /memory 查看任务记录\n"
                    "  • 或直接提出新的任务"
                )
            else:
                reply = '你好！我是 AI Company 的 CEO 🏢,有什么可以帮你的？直接告诉我任务就好.'
            return {
                "phase": "deliver",
                "department": "ceo",
                "final_output": reply,
                "execution_log": [
                    f"[CEO] Chitchat detected: '{msg[:50]}' — direct reply, no dispatch"
                ],
                "score_card": {"score": 100, "decision": "CHITCHAT",
                                "feedback": "Casual conversation handled directly by CEO"},
            }

    fast_department = None
    fast_match = ""

    # Code review / audit -> Developer (not researcher/devops!)
    code_review_kw = ["代码审计", "代码审查", "审查代码", "代码质量", "code review",
                      "代码打分", "代码评分", "审计代码", "review code",
                      "审计.*项目.*代码", "审查.*项目.*质量",
                      "项目.*代码.*审查", "审查.*打分"]
    for kw in code_review_kw:
        if re.search(kw, task_lower):
            fast_department = "developer"
            fast_match = f"CodeReview({kw})"
            break

    # Pure development tasks -> Developer (was missing!)
    if not fast_department:
        dev_kw = [
            r"写.*(?:api|函数|代码|程序|脚本|模块|类|接口)",
            r"实现.*(?:功能|方法|算法|逻辑)",
            r"创建.*(?:api|项目|服务|应用)",
            r"重构", r"修复.*(?:bug|问题)", r"优化.*(?:代码|性能)",
            "implement", "refactor", "build a", "create a",
        ]
        for kw in dev_kw:
            if re.search(kw, task_lower, re.IGNORECASE):
                fast_department = "developer"
                fast_match = f"Dev({kw})"
                break

    # Deployment -> DevOps
    if not fast_department:
        for kw in ["部署", "deploy", "docker", "kubernetes", "k8s", "ci/cd"]:
            if kw in task_lower:
                fast_department = "devops"
                fast_match = f"Deploy({kw})"
                break

    # Testing -> QA
    if not fast_department:
        for kw in ["测试", "test", "pytest", "单测", "单元测试"]:
            if kw in task_lower:
                fast_department = "qa"
                fast_match = f"Test({kw})"
                break

    # ═══ NEW: Blind-spot coverage ───

    # Data analysis -> Researcher (was missing!)
    if not fast_department:
        data_kw = [
            "数据分析", "数据统计", "数据报表",
            "分析数据", "统计分析", "报表", "图表",
            r"统计.*数据", r"分析.*(?:趋势|规律|分布)",
            "data analysis", "analytics",
        ]
        for kw in data_kw:
            if re.search(kw, task_lower):
                fast_department = "researcher"
                fast_match = f"DataAnalysis({kw})"
                break

    # Code explanation / understanding -> Researcher
    if not fast_department:
        explain_kw = [
            "解释代码", "这段代码", "理解代码", "代码含义",
            "代码.*做什么", "代码.*作用", "代码.*逻辑",
            "explain.*code", "what does.*do", "how does.*work",
        ]
        for kw in explain_kw:
            if re.search(kw, task_lower):
                fast_department = "researcher"
                fast_match = f"CodeExplain({kw})"
                break

    # Comparison / benchmark -> Researcher
    if not fast_department:
        compare_kw = [
            "对比", "比较", r"\bvs\b", "优劣", "优缺点",
            "哪个更好", "选哪个", "benchmark",
        ]
        for kw in compare_kw:
            if re.search(kw, task_lower):
                fast_department = "researcher"
                fast_match = f"Compare({kw})"
                break

    # ═══ PDF / File Generation -> Developer ═══
    # Creating files (PDF, export, etc.) requires write_file + run_python.
    if not fast_department:
        pdf_kw = [
            r"生成.*pdf", r"创建.*pdf", r"写.*pdf", r"导出.*pdf",
            r"生成.*文件", r"导出.*(?:文件|报表|excel)",
            "pdf", ".pdf",
        ]
        for kw in pdf_kw:
            if re.search(kw, task_lower):
                fast_department = "developer"
                fast_match = f"PDFGen({kw})"
                break

    # Document / Report generation -> Developer (needs write_file + run_python)
    if not fast_department:
        doc_kw = [
            "写报告", "生成报告", "写文档", "写总结", "写纪要",
            "周报", "日报", "月报", "会议纪要",
            r"生成.*文档", r"写.*(?:文档|报告)",
        ]
        for kw in doc_kw:
            if re.search(kw, task_lower):
                fast_department = "developer"
                fast_match = f"Document({kw})"
                break

    # General knowledge / Q&A -> Researcher
    if not fast_department:
        qa_kw = [
            "什么是", "怎么理解", "如何理解", r"是什么",
            "介绍一下", "介绍一下", "有哪些", "什么区别",
            r"^怎么", r"^如何",
            "what is", "how to", "explain", "define",
        ]
        for kw in qa_kw:
            if re.search(kw, task_lower):
                fast_department = "researcher"
                fast_match = f"Knowledge({kw})"
                break

    # ═══ LOCAL_SYSTEM: detect/check local software, processes, system state -> devops ═══
    if not fast_department:
        local_sys_kw = [
            r"检测.*(?:本地|运行|软件|进程|系统)",
            r"本地.*(?:软件|进程|运行|程序|检测|扫描)",
            r"打开.*(?:微信|QQ|钉钉|应用|软件|程序)",
            r"查看.*(?:置顶|联系人|聊天|微信|QQ)",
            r"有没有.*(?:运行|开启|安装|启动)",
            r"(?:运行|启动).*(?:微信|QQ|钉钉|程序)",
            r"发送.*(?:消息|微信|信息|短信)",
            r"给.*(?:微信|好友|联系人).*发",
            r"(?:微信|QQ|钉钉).*(?:发|消息|信息)",
            r"pgrep|ps\\s|进程列表|进程信息",
            r"(?:macos|mac|系统).*(?:权限|设置|偏好|配置)",
        ]
        for kw in local_sys_kw:
            if re.search(kw, task_lower):
                fast_department = "devops"
                fast_match = f"LocalSys({kw[:30]})"
                break

    # ═══ Simple Lookup (查/搜/最新/股价/天气) -> Researcher ═══
    # Catch simple fact-finding / lookup queries that don't match other departments.
    # These are clearly researcher tasks - skip the LLM triage call.
    if not fast_department:
        lookup_kw = [
            r"查(一下|查询|看|询)?", r"搜(一下|索)?",
            r"股价", "股票", "金价", "银价", "油价", "汇率",
            "天气", "新闻", "最新", "今天", "昨日",
            "出生", "生日", "年龄", "多大",
            "价格", "多少钱",
            "search", "news", "price", "stock", "weather",
        ]
        for kw in lookup_kw:
            if re.search(kw, task_lower):
                fast_department = "researcher"
                fast_match = f"Lookup({kw})"
                break

    # Research -> Researcher
    if not fast_department:
        for kw in ["调研", "竞品", "research", "compare", "对比"]:
            if kw in task_lower:
                fast_department = "researcher"
                fast_match = f"Research({kw})"
                break

    # Marketing -> Marketer
    if not fast_department:
        for kw in ["文案", "推广", "营销", "公众号", "广告"]:
            if kw in task_lower:
                fast_department = "marketer"
                fast_match = f"Market({kw})"
                break

    if fast_department and role_registry.get(fast_department):
        department = fast_department
        dept_role = role_registry.get(department)
        if dept_role and dept_role.category == "execution":
            next_phase = "pm" if department in ("developer", "qa") else "execute"
        else:
            next_phase = "execute"
        match_method = fast_match
    else:
        # ═══ LLM fallback: only for ambiguous tasks ═══
        # Only send first 500 chars for intent detection (speed)
        short_request = state["user_request"][:500]

        # Lazy memory lookup (only when we actually need LLM)
        memory_context = await episode_memory.get_context(
            state["user_request"], limit=2
        )

        intent_prompt = f"""用户请求: {short_request}
{memory_context}
可用执行角色:
{role_list}

判断用户意图,回复一个角色名.多领域或不明确->GENERAL."""

        response = await llm.ainvoke([
            SystemMessage(content=CEO_SYSTEM_PROMPT),
            HumanMessage(content=intent_prompt),
        ])

        intent = str(response.content).strip().lower()

        # Validate against registry
        role = role_registry.get(intent)
        if role and role.category == "execution":
            department = intent
            if department in ("developer", "qa"):
                next_phase = "pm"
            else:
                next_phase = "execute"
            match_method = "LLM"
        elif intent == "general":
            department = "developer"  # Default to developer for general tasks
            next_phase = "pm"
            match_method = "LLM(general->dev)"
        else:
            # Fallback: keyword matching
            best, score = role_registry.best_match(state["user_request"])
            if best and score > 0.15:
                department = best.name
                if department in ("developer", "qa"):
                    next_phase = "pm"
                else:
                    next_phase = "execute"
                match_method = f"Keyword({score:.2f})"
            else:
                department = "developer"  # Default fallback
                next_phase = "pm"
                match_method = f"Fallback->dev(best={score:.2f})"

    agent_state.log_decision(
        f"Routed to {department}",
        f"Match: {match_method}"
    )
    
    return {
        "phase": next_phase,
        "department": department,
        "workspace_id": workspace_id,
        "task_type": classify_task(state.get("user_request", ""), department),
        "execution_log": [f"[TRIAGE] {match_method} -> {department}"],
    }


# PM and Architect are now separate nodes:
# - PM: writes PRD with acceptance criteria (all departments)
# - Architect: designs tech stack/modules (only developer/qa departments)
# ─── Task-Type Profiles (PM 任务类型感知) ───────

_TASK_TYPE_PROFILES = {
    "COMMAND_EXECUTION": {
        "label": "Shell Command Execution",
        "criteria_hint": (
            "DO NOT apply code quality criteria. This is a shell command execution task. "
            "Verify: command executed, stdout captured, exit code reported."
        ),
    },
    "SIMPLE_QUERY": {
        "label": "Simple Fact Lookup",
        "criteria_hint": (
            "DO NOT apply code review criteria. Verify: answer is accurate, source cited, concise format."
        ),
    },
    "DEVELOPMENT": {
        "label": "Software Development",
        "criteria_hint": (
            "Focus acceptance criteria on: functional correctness, code quality, "
            "error handling, security, test coverage, performance. "
            "Each criterion must be SPECIFIC and MEASURABLE."
        ),
    },
    "CODE_REVIEW": {
        "label": "Code Review / Audit",
        "criteria_hint": (
            "Focus acceptance criteria on: problem discovery rate (at least 3 specific issues), "
            "evidence quality (code line references), severity classification (P0/P1/P2), "
            "actionable fix suggestions, coverage of all review dimensions. "
            "DO NOT require writing new code — this is an ANALYSIS task."
        ),
    },
    "RESEARCH": {
        "label": "Research / Investigation",
        "criteria_hint": (
            "Focus acceptance criteria on: source credibility, coverage completeness, "
            "analysis depth, comparative structure, actionable recommendations. "
            "DO NOT require code or technical implementation."
        ),
    },
    "DOCUMENT": {
        "label": "Document Generation",
        "criteria_hint": (
            "Focus on: content quality, structure, audience appropriateness. "
            "DO NOT apply code review criteria."
        ),
    },
    "CREATIVE": {
        "label": "Creative / Marketing Content",
        "criteria_hint": (
            "Focus acceptance criteria on: audience appeal, clarity, "
            "platform format compliance, brand tone consistency, engagement. "
            "DO NOT require API design or code quality criteria."
        ),
    },
    "GENERAL": {
        "label": "General Task",
        "criteria_hint": (
            "Focus acceptance criteria on: task completion, output quality, "
            "relevance to user request, usability of the deliverable."
        ),
    },
}


def classify_task(task: str, department: str = "") -> str:
    """Classify the task TYPE before routing — this is the critical first step.

    Types:
        COMMAND_EXECUTION — shell commands (pwd, ls, cat, curl, etc.)
        SIMPLE_QUERY      — fact lookup with no code needed
        RESEARCH          — investigation requiring web search
        CODING            — write/modify code
        CODE_REVIEW       — audit existing code
        DOCUMENT          — generate documents/PDFs
        CREATIVE          — marketing/content
        GENERAL           — fallback
    """
    import re
    task_lower = task.lower().strip()

    # ── Rule 1: Shell command detection (HIGHEST priority) ──
    # Matches standalone shell commands that should NOT go through code pipeline
    shell_commands = [
        r'^(pwd|ls|cd|cat|echo|mkdir|rm|cp|mv|chmod|grep|find|head|tail|wc|sort|uniq|diff)\b',
        r'^(tar|gzip|gunzip|zip|unzip|curl|wget|ssh|scp|ping|nslookup|whoami|hostname|date|uptime)\b',
        r'^(ps|top|kill|df|du|free|env|export|source|which|where|who)\b',
        r'^(python3?\s+-[cm]|node\s+-e|ruby\s+-e|perl\s+-e|bash\s+-c|sh\s+-c)\b',
    ]
    for pattern in shell_commands:
        if re.search(pattern, task_lower):
            return "COMMAND_EXECUTION"

    # ── Rule 1b: Local system detection (before lookup to avoid "查看" → researcher) ──
    local_sys_patterns = [
        r'检测.*(?:本地|运行|软件|进程|系统)',
        r'本地.*(?:软件|进程|运行|程序|检测)',
        r'打开.*(?:微信|QQ|钉钉|应用|软件)',
        r'查看.*(?:置顶|联系人|聊天)',
        r'pgrep|osascript|applescript',
    ]
    for p in local_sys_patterns:
        if re.search(p, task_lower):
            return "LOCAL_SYSTEM"

    # ── Rule 2: Execute/run a command explicitly ──
    if re.search(r'(执行|运行|run|execute)\s+(pwd|ls|命令|command|shell)', task_lower):
        return "COMMAND_EXECUTION"

    # ── Rule 3: Simple fact lookup (no code needed) ──
    lookup_patterns = [
        r'(?:帮我|请|可以|帮忙|能|能不能|给我)?(?:查|搜|搜索|找|什么是|怎么|为什么|谁|什么时候|哪里|多少|几)',
        r'^(how|what|when|where|who|why|which)\b',
    ]
    for p in lookup_patterns:
        if re.search(p, task_lower, re.IGNORECASE):
            return "SIMPLE_QUERY"

    # ── Rule 4: Code review ──
    review_kw = [r'代码审计', r'代码审查', r'审查代码', r'代码质量',
                 r'code review', r'代码打分', r'审计代码', r'审查.*打分']
    for kw in review_kw:
        if re.search(kw, task_lower):
            return "CODE_REVIEW"

    # ── Rule 5: Document generation ──
    doc_kw = [r'生成.*(?:pdf|文档|报告|文件)', r'写.*(?:文档|报告|总结)',
              r'导出', r'周报', r'月报', r'日报', r'会议纪要']
    for kw in doc_kw:
        if re.search(kw, task_lower):
            return "DOCUMENT"

    # ── Rule 6: Coding (write/implement/develop) ──
    code_kw = [r'写(?:代码|程序|脚本)', r'开发', r'实现', r'bug', r'修复',
               r'重构', r'refactor', r'写.*api', r'写.*函数']
    for kw in code_kw:
        if re.search(kw, task_lower):
            return "CODING"

    # ── Rule 7: Research (web-dependent) ──
    research_kw = [r'研究', r'分析', r'调研', r'对比', r'竞品',
                   r'research', r'compare', r'survey', r'trend']
    for kw in research_kw:
        if re.search(kw, task_lower):
            return "RESEARCH"

    # ── Rule 8: Creative/marketing ──
    creative_kw = [r'文案', r'推广', r'seo', r'广告', r'社交媒体',
                   r'营销', r'公众号', r'marketing', r'copywriting']
    for kw in creative_kw:
        if re.search(kw, task_lower):
            return "CREATIVE"

    # ── Rule 9: Department-based fallback ──
    if department == "researcher":
        return "RESEARCH"
    if department == "marketer":
        return "CREATIVE"
    if department == "qa":
        return "CODING"
    if department == "devops":
        return "CODING"

    return "GENERAL"


# Keep old name for backwards compat
def _classify_task_type(state: CEOState) -> str:
    return classify_task(
        state.get("user_request", ""),
        state.get("department", "developer"),
    )


# ─── PM Node ──────────────────────────────────────

def _get_fallback_criteria(department: str, task_type: str) -> str:
    """Get department-appropriate fallback acceptance criteria."""
    if department == "researcher" or task_type == "RESEARCH":
        return """- 信息来源可靠,有明确引用或来源
- 给出可操作的建议或结论
- 输出格式清晰、结构合理、可直接使用"""
    if department == "marketer" or task_type == "CREATIVE":
        return """- 内容有吸引力,能抓住读者注意力
- 有效传递价值主张
- 输出可直接使用或发布"""
    if task_type == "CODE_REVIEW":
        return """- 发现至少3个具体问题
- 问题按严重程度分类(P0/P1/P2)
- 给出可操作的改进建议
- 覆盖安全性、性能、可维护性等维度"""
    # Default: code/development
    return """- 代码能正常运行,无明显逻辑错误
- 无明显安全漏洞(注入,硬编码密钥等)
- 包含必要的测试用例
- 输出格式符合要求，可直接使用"""


@_safe_node("PM")
async def pm_analyze_node(state: CEOState) -> dict:
    """PM: produces PRD with acceptance criteria."""
    from src.departments.roles import role_registry

    pm_role = role_registry.get("pm")
    llm = _get_llm("pm")

    # Task type classification (zero extra LLM calls)
    task_type = _classify_task_type(state)
    profile = _TASK_TYPE_PROFILES.get(task_type, _TASK_TYPE_PROFILES["GENERAL"])

    pm_prompt = f"""任务: {state["user_request"]}
部门: {state.get("department", "developer")}
类型: {profile["label"]}
{profile["criteria_hint"]}

标准必须具体可量化."""

    response = await llm.ainvoke([
        SystemMessage(content=pm_role.system_prompt),
        HumanMessage(content=pm_prompt),
    ])

    try:
        design = _extract_json(str(response.content))
        if not isinstance(design, dict):
            raise ValueError("Not a dict")
    except Exception:
        design = {
            "summary": state["user_request"],
            "features": ["Implement as requested"],
            "acceptance_criteria": ["Meets basic requirements"],
            "edge_cases": [],
            "priority": "P1",
        }

    prd = {
        "summary": str(design.get("summary", "")),
        "features": [
            f.get("name", str(f)) if isinstance(f, dict) else str(f)
            for f in design.get("features", [])
        ],
        "acceptance_criteria": [
            c.get("criterion", str(c)) if isinstance(c, dict) else str(c)
            for c in design.get("acceptance_criteria", [])
        ],
        "edge_cases": [
            e.get("case", str(e)) if isinstance(e, dict) else str(e)
            for e in design.get("edge_cases", [])
        ],
        "priority": str(design.get("priority", "P1")),
    }

    # Only route to architect for code-heavy departments with substantial tasks
    # Skip architect for document/PDF generation tasks (content, not code)
    task_lower = state.get("user_request", "").lower()
    is_doc_task = bool(re.search(
        r"pdf|生成.*文档|写报告|生成报告|写文档|周报|月报|日报|会议纪要|写总结",
        task_lower
    )) if task_lower else False
    needs_architect = (
        state.get("department", "") in ("developer", "qa")
        and len(state.get("user_request", "")) > 30  # Skip architect for short tasks
        and prd.get("priority", "P1") in ("P0", "P1")  # Skip for P2 trivial tasks
        and not is_doc_task  # Document tasks don't need architecture design
    )
    next_phase = "architect" if needs_architect else "execute"
    criteria_count = len(prd.get("acceptance_criteria", []))

    return {
        "phase": next_phase,
        "department": state.get("department", "developer"),
        "prd": prd,
        "execution_log": [
            f"[PM] PRD: {str(prd.get('summary', 'N/A'))[:80]} ({criteria_count} criteria)"
            + (" -> Architect" if needs_architect else " -> Execute"),
        ],
    }


@_safe_node("Architect")
async def architect_node(state: CEOState) -> dict:
    """Architect: design tech stack and module structure.

    Only invoked for code-heavy departments (developer, qa).
    Skipped for research/marketing tasks.
    """
    from src.departments.roles import role_registry

    arch_role = role_registry.get("architect")
    if not arch_role:
        # No architect role defined -> skip
        return {
            "phase": "execute",
            "arch_design": "{}",
            "execution_log": ["[Arch] No architect role, skipping"],
        }

    llm = _get_llm("architect")
    prd = state.get("prd", {})
    user_request = state.get("user_request", "")

    arch_prompt = f"""需求: {user_request}
部门: {state.get("department", "developer")}
PRD: {prd.get('summary', 'N/A')} | 功能: {prd.get('features', [])} | 验收: {prd.get('acceptance_criteria', [])}

简洁,只写开发需要知道的."""

    response = await llm.ainvoke([
        SystemMessage(content=arch_role.system_prompt),
        HumanMessage(content=arch_prompt),
    ])

    try:
        design = _extract_json(str(response.content))
        if not isinstance(design, dict):
            raise ValueError("Not a dict")
    except Exception:
        design = {
            "tech_stack": ["Python"],
            "module_design": "Single module",
            "guidelines": ["Follow best practices"],
        }

    # Normalize list fields to strings
    design["tech_stack"] = _safe_str_list(design.get("tech_stack", []))
    design["guidelines"] = _safe_str_list(design.get("guidelines", []))
    design["risks"] = _safe_str_list(design.get("risks", []))
    design["key_interfaces"] = _safe_str_list(design.get("key_interfaces", []))

    arch_text = json.dumps(design, ensure_ascii=False, indent=2)

    return {
        "phase": "execute",
        "arch_design": arch_text,
        "execution_log": [
            f"[Arch] Stack: {design.get('tech_stack', ['?'])[0]}, "
            f"Modules: {str(design.get('module_design', 'N/A'))[:60]}"
        ],
    }


@_safe_node("Execute")
async def execute_node(state: CEOState) -> dict:
    """Node function."""

    # Get current step from plan (may be None for simple tasks)
    plan = state.get("plan") or {}
    steps = plan.get("steps", [])
    executed = len(state.get("execution_log", []))
    
    if steps and executed < len(steps):
        current_step = steps[executed]
    else:
        current_step = {
            "department": state.get("department", "coding"),
            "task": state.get("user_request", ""),
            "acceptance_criteria": "Task completed successfully",
        }
    
    # Include retry feedback so the department knows what to fix
    retry_feedback = state.get("retry_feedback", "")
    retry_count = state.get("retry_count", 0)
    
    task_with_context = current_step["task"]
    if retry_feedback:
        task_with_context = f"[RETRY #{retry_count}] Fix the following issues and redo the task:\n{retry_feedback}\n\nOriginal task: {task_with_context}"
    
    dispatch_msg = f"""DISPATCH to {current_step['department'].upper()} Department:

Task: {task_with_context}
Acceptance Criteria: {current_step['acceptance_criteria']}

Output format: Return your work result. Do NOT self-score."""

    agent_state = get_agent_state("ceo")
    agent_state.add_to_working_memory(f"Dispatched to {current_step['department']}: {task_with_context[:100]}")
    
    return {
        "execution_log": [f"[EXECUTE] {'🔄 RETRY' if retry_feedback else '->'} {current_step['department']}: {task_with_context[:100]}"],
    }


@_safe_node("Department")
async def execute_department_node(state: CEOState) -> dict:
    """Node function."""
    from src.departments.agents import dispatch_to_department
    
    department = state.get("department", "developer")
    
    # Build rich context from PM and Architect
    context_parts = []
    
    prd = state.get("prd") or {}  # prd may be None in TypedDict
    if prd:
        # Normalize features to strings (LLM may return dicts)
        raw_features = prd.get('features', [])
        features_str = ', '.join(
            f.get('name', str(f)) if isinstance(f, dict) else str(f)
            for f in raw_features
        ) if raw_features else 'N/A'
        criteria_list = '\n'.join(f"    - {c}" for c in prd.get('acceptance_criteria', []))
        raw_edges = prd.get('edge_cases', [])
        edges_str = ', '.join(
            e.get('case', str(e)) if isinstance(e, dict) else str(e)
            for e in raw_edges
        ) if raw_edges else 'N/A'
        context_parts.append(
            f"[PM Requirements]\n"
            f"Summary: {prd.get('summary', 'N/A')}\n"
            f"Features: {features_str}\n"
            f"Acceptance Criteria:\n{criteria_list}\n"
            f"Edge Cases: {edges_str}"
        )
    
    arch_design = state.get("arch_design", "")
    if arch_design:
        context_parts.append(f"[Architect Design]\n{arch_design[:1500]}")
    
    retry_feedback = state.get("retry_feedback", "")
    if retry_feedback:
        context_parts.insert(0, f"[RETRY - Fix These]\n{retry_feedback}")
    
    # ── Workspace Context: load/create task workspace ──
    from src.workspace import TaskContext
    workspace_id = state.get("workspace_id")
    task_ctx = None
    if workspace_id:
        task_ctx = TaskContext.load(workspace_id)
    if not task_ctx:
        task_ctx = TaskContext()
        workspace_id = task_ctx.create(state.get("user_request", ""))
        task_ctx.add_timeline(f"Dispatched to {department}")
    # Inject workspace context into agent prompt
    ws_context = task_ctx.get_context()
    if ws_context:
        context_parts.insert(0, f"[Workspace Context — Previous Findings]\n{ws_context}")
    
    context = "\n\n".join(context_parts) if context_parts else ""
    
    # ── Capability Discovery: determine what capabilities this task needs ──
    from src.capability import CapabilityPlanner
    planner = CapabilityPlanner()
    cap_plan = planner.analyze(state.get("user_request", ""))
    dynamic_capabilities = cap_plan.capabilities
    # If the planned role differs from what triage selected, use the planner's choice
    effective_department = cap_plan.role_hint if cap_plan.confidence > 0.5 else department
    import logging as _log
    _log.getLogger("ai_company").debug("Capability plan: %s → role=%s caps=%s (confidence=%.2f)",
                 cap_plan.reasoning, effective_department, dynamic_capabilities, cap_plan.confidence)
    
    # Save capabilities to workspace for skill learning
    if task_ctx and dynamic_capabilities:
        task_ctx.add_timeline(f"Capabilities: {', '.join(dynamic_capabilities)}")
    
    # ── Skill Injection: inject learned workflow guidance ──
    from src.learning import skill_library as _sl
    skill_guidance = _sl.inject_context(state.get("user_request", ""))
    if skill_guidance:
        context_parts.insert(0, skill_guidance)
        context = "\n\n".join(context_parts) if context_parts else ""
    
    try:
        result = await dispatch_to_department(
            department=effective_department,
            task=state.get("user_request", ""),
            context=context,
            dynamic_capabilities=dynamic_capabilities,
        )
    except Exception as e:
        import logging
        logging.getLogger("ai_company").exception("Department dispatch failed")
        return {
            "phase": "deliver",
            "final_output": f"Department '{department}' crashed: {type(e).__name__}",
            "execution_log": [f"[DEPT-{department}] CRASHED: {e}"],
            "workspace_id": workspace_id,
        }
    
    output = result.get("output", "")
    success = result.get("success", True)
    tool_calls = result.get("tool_calls", [])
    
    # Save tool calls to workspace for skill learning
    if tool_calls and task_ctx:
        task_ctx.add_timeline(f"Tools: {', '.join(tc.get('tool', '?') for tc in tool_calls)}")
    
    # Calculate tool failure count for evolution tracking
    failed_calls = sum(1 for tc in tool_calls if not tc.get("success", True))
    tools_used = list(dict.fromkeys(tc.get("tool", "?") for tc in tool_calls))

    if not success:
        return {
            "phase": "deliver",
            "final_output": result.get("error", "Department failed"),
            "execution_log": [f"[DEPT-{department}] FAILED: {str(result.get('error', ''))[:100]}"],
            "workspace_id": workspace_id,
        }
    
    return {
        "phase": "audit",
        "execution_log": [
            f"[DEPT-{department}] Work completed (PM={'Y' if prd else 'N'} Arch={'Y' if arch_design else 'N'})",
        ] + ([f"[DEPT-{department}] Tool failures: {failed_calls}/{len(tool_calls)} calls"] if tool_calls else []),
        "final_output": output,
        "workspace_id": workspace_id,
        "task_type": classify_task(state.get("user_request", ""), effective_department),
    }


@_safe_node("Auditor")
async def auditor_node(state: CEOState) -> dict:
    """Node function."""
    from src.verification.auditor import AuditorAgent
    
    department = state.get("department", "coding")
    output = state.get("final_output", "")
    task = state.get("user_request", "")
    
    # ═══ 获取 PM 验收标准(之前被遗漏了！)═══
    prd = state.get("prd") or {}
    acceptance_criteria = "\n".join(
        f"- {c}" for c in prd.get("acceptance_criteria", [])
    )
    
    auditor = AuditorAgent()
    report = await auditor.audit(
        department=department,
        task=task,
        output=output,
        acceptance_criteria=acceptance_criteria,  # ← 传入PM的验收标准！
    )
    
    scores_str = ", ".join(
        f"{d.name}:{d.score}" for d in report.dimensions
    )
    
    return {
        "phase": "pmo",
        "score_card": report.to_dict(),
        "execution_log": [
            f"[AUDITOR] Independently scored: {scores_str}",
            f"[AUDITOR] Overall: {report.overall_score}/100 -> {report.verdict}",
        ],
    }


@_safe_node("PMO")
async def pmo_node(state: CEOState) -> dict:
    """Node function."""
    from src.verification.auditor import pmo_gate_check
    
    department = state.get("department", "developer")
    output = state.get("final_output", "")
    task = state.get("user_request", "")
    
    # Get acceptance criteria from PM's PRD (primary source)
    prd = state.get("prd") or {}
    criteria_list = prd.get("acceptance_criteria", [])
    
    if criteria_list:
        criteria = "\n".join(f"- {c}" for c in criteria_list)
    else:
        # Fallback: from plan steps
        plan = state.get("plan") or {}
        steps = plan.get("steps", [])
        executed = len([l for l in state.get("execution_log", []) if "EXECUTE" in l])
        criteria = ""
        if steps and executed <= len(steps):
            criteria = steps[executed - 1].get("acceptance_criteria", "") if executed > 0 else ""
        # ═══ Task-type-aware fallback criteria ═══
        if not criteria:
            task_type = _classify_task_type(state)
            criteria = _get_fallback_criteria(department, task_type)
    
    pmo_result = await pmo_gate_check(
        department=department,
        task=task,
        acceptance_criteria=criteria,
        output=output,
    )
    
    has_criteria = "YES" if criteria_list else "NO (fallback)"
    
    return {
        "execution_log": [
            f"[PMO] Checked {len(criteria_list)} criteria from PM -> {pmo_result.get('verdict', '?')}",
            f"[PMO] Score: {pmo_result.get('compliance_score', '?')}/100 | Met: {pmo_result.get('criteria_met', [])}",
        ],
        "pmo_result": pmo_result,
    }


@_safe_node("VerifyAggregate")
async def verify_aggregate_node(state: CEOState) -> dict:
    """Node function."""
    
    score_card = state.get("score_card") or {}
    pmo_result = state.get("pmo_result") or {}
    department = state.get("department", "")
    workspace_id = state.get("workspace_id")  # Preserve workspace across node boundaries
    task_type = state.get("task_type", "")

    # ═══ Fast-lane: COMMAND_EXECUTION or LOCAL_SYSTEM — never audit, just check stdout ═══
    if task_type in ("COMMAND_EXECUTION", "LOCAL_SYSTEM"):
        final_output = str(state.get("final_output", ""))
        has_output = bool(final_output.strip())
        return {
            "phase": "deliver",
            "workspace_id": workspace_id,
            "score_card": {
                "score": 95 if has_output else 0,
                "decision": "APPROVE" if has_output else "FAIL",
                "final_score": 95 if has_output else 0,
                "next_action": "deliver",
                "auditor_verdict": "SKIPPED",
                "pmo_verdict": "SKIPPED",
            },
            "execution_log": ["[CEO-AGGREGATE] Command execution -> skip audit, direct deliver"],
        }

    # ═══ Fast-lane: SIMPLE_QUERY — skip audit for fact lookups ═══
    if task_type == "SIMPLE_QUERY":
        final_output = str(state.get("final_output", ""))
        has_output = bool(final_output.strip())
        return {
            "phase": "deliver",
            "workspace_id": workspace_id,
            "score_card": {
                "score": 95 if has_output else 0,
                "decision": "APPROVE" if has_output else "FAIL",
                "final_score": 95 if has_output else 0,
                "next_action": "deliver",
                "auditor_verdict": "SKIPPED",
                "pmo_verdict": "SKIPPED",
            },
            "execution_log": ["[CEO-AGGREGATE] Simple query -> skip audit, direct deliver"],
        }

    # Fast-lane: researcher/marketer/developer-doc skip auditor/pmo — pass directly
    if department in ("researcher", "marketer") and not score_card.get("overall_score"):
        # Check for crash/error — don't give 95 to a crash message
        final_output = str(state.get("final_output", ""))
        execution_log = state.get("execution_log", [])
        is_crash = (
            "CRASHED" in str(execution_log)
            or "crashed" in final_output.lower()
            or "FAILED" in str(execution_log)
            or "failed" in final_output.lower()
        )
        if is_crash:
            return {
                "phase": "deliver",
                "workspace_id": workspace_id,
                "score_card": {"score": 0, "decision": "FAIL", "final_score": 0,
                              "next_action": "deliver", "auditor_verdict": "SKIPPED",
                              "pmo_verdict": "SKIPPED", "feedback": "Department crashed"},
            }
        # ═══ Stale answer detection: no tools called, LLM answered from training data ═══
        # Patterns: "无法获取实时", "根据已知数据源", "cannot access", etc.
        # These mean the LLM skipped tools and produced a hallucinated/evasive answer.
        stale_patterns = [
            r"无法获取.*?(?:实时|数据|信息)",
            r"根据已知数据",
            r"cannot\s+(?:access|fetch|retrieve).*?(?:data|price|information)",
            r"我无法提供.*?(?:建议|预测|数据)",
            r"无法访问.*?(?:数据|页面|网站)",
            r"no\s+(?:real.?time|current|live)\s+data",
        ]
        is_stale = False
        import re as _vestale
        for pat in stale_patterns:
            if _vestale.search(pat, final_output, _vestale.IGNORECASE):
                is_stale = True
                break
        if is_stale:
            return {
                "phase": "deliver",
                "workspace_id": workspace_id,
                "score_card": {
                    "score": 20, "decision": "FAIL", "final_score": 20,
                    "next_action": "deliver", "auditor_verdict": "SKIPPED",
                    "pmo_verdict": "SKIPPED",
                    "feedback": "Stale answer: no real data fetched, LLM answered from training data",
                },
                "execution_log": ["[CEO-AGGREGATE] Stale answer detected -> FAIL (no tool usage)"],
            }
        return {
            "phase": "deliver",
            "workspace_id": workspace_id,
            "score_card": {"score": 95, "decision": "APPROVE", "final_score": 95,
                          "next_action": "deliver", "auditor_verdict": "SKIPPED",
                          "pmo_verdict": "SKIPPED"},
            "execution_log": ["[CEO-AGGREGATE] Researcher/marketer output -> direct deliver"],
        }

    # Fast-lane: document/PDF generation tasks skip audit (content tasks, not code)
    if department == "developer" and not score_card.get("overall_score"):
        import re as _vre
        task = state.get("user_request", "").lower()
        is_doc_task = _vre.search(
            r"pdf|生成.*文档|写报告|生成报告|写文档|周报|月报|日报|会议纪要|写总结",
            task
        )
        if is_doc_task:
            final_output = str(state.get("final_output", ""))
            execution_log = state.get("execution_log", [])
            is_crash = (
                "CRASHED" in str(execution_log)
                or "crashed" in final_output.lower()
            )
            if is_crash:
                return {
                    "phase": "deliver",
                    "workspace_id": workspace_id,
                    "score_card": {"score": 0, "decision": "FAIL", "final_score": 0,
                                  "next_action": "deliver", "auditor_verdict": "SKIPPED",
                                  "pmo_verdict": "SKIPPED", "feedback": "Department crashed"},
                }
            return {
                "phase": "deliver",
                "workspace_id": workspace_id,
                "score_card": {"score": 90, "decision": "APPROVE", "final_score": 90,
                              "next_action": "deliver", "auditor_verdict": "SKIPPED",
                              "pmo_verdict": "SKIPPED"},
                "execution_log": ["[CEO-AGGREGATE] Document/PDF task -> skip audit, direct deliver"],
            }
    
    auditor_score = score_card.get("overall_score", 60)
    auditor_verdict = score_card.get("verdict", "APPROVE")
    pmo_score = pmo_result.get("compliance_score", 70)
    pmo_verdict = pmo_result.get("verdict", "PASS")
    
    # Weighted final score: Auditor 70% + PMO 30%
    final_score = round(auditor_score * 0.7 + pmo_score * 0.3, 1)
    
    # ----- Verdict logic -----
    retry_count = state.get("retry_count", 0)
    
    if auditor_verdict == "REJECT" or pmo_verdict == "FAIL":
        decision = "REJECT"
        next_action = "replan"
    elif auditor_verdict == "REVISE" or pmo_score < 60:
        decision = "REVISE"
        next_action = "revise"
    elif final_score >= config.gate_final_score:
        decision = "APPROVE"
        next_action = "deliver"
    else:
        decision = "REVISE"
        next_action = "revise"
    
    # Hard fail after max retries — never deliver garbage
    max_retries = 1
    if next_action != "deliver" and retry_count >= max_retries:
        decision = "FAIL"
        next_action = "deliver"
        # Collect failure diagnostics for the user
        tool_log = state.get("execution_log", [])
        tool_failures = sum(1 for l in tool_log if "FAILED" in str(l) or "CRASHED" in str(l))
        max_iter_hits = sum(1 for l in tool_log if "Max iterations" in str(l))
        
        # Build specific failure reason
        failure_reasons = []
        if max_iter_hits > 0:
            failure_reasons.append(f"工具循环用尽({max_iter_hits} 次达到上限)-> 搜索可能返回空结果或 LLM 重复无效调用")
        if tool_failures > 0:
            failure_reasons.append(f"{tool_failures} 次工具调用失败 -> 检查网络/DuckDuckGo 可用性")
        if auditor_score < 30:
            failure_reasons.append(f"Audiator 仅 {auditor_score} 分 -> 输出内容质量严重不达标")
        if auditor_score >= 70 and pmo_score == 0:
            failure_reasons.append("内容质量尚可但缺少来源引用 -> 未使用 web_search/web_fetch 获取实时数据")
        
        if not failure_reasons:
            failure_reasons.append("多次重试后 Auditor 和 PMO 均不认可输出质量")
        
        fail_output = (
            f"任务未能通过质量审核({max_retries} 次重试后仍不达标).\n\n"
            f"Audiator: {auditor_score}/100 | PMO: {pmo_score}/100\n"
            f"原因: {'; '.join(failure_reasons)}\n\n"
            f"建议:\n"
            f"• 换更具体的查询方式,如 'web_fetch https://en.wikipedia.org/wiki/SpaceX'\n"
            f"• 对股价类任务,指定数据源如 'web_fetch https://finance.yahoo.com/quote/NVDA'\n"
            f"• 缩小范围:只查 Top 5 而非全部"
        )
    
    # Build feedback for retry
    feedback_parts = []
    if auditor_verdict in ("REVISE", "REJECT"):
        suggestions = score_card.get("suggestions", [])
        suggestions_strs = [str(s) for s in suggestions[:3]] if suggestions else []
        feedback_parts.append(f"Auditor建议: {'; '.join(suggestions_strs)}" if suggestions_strs else f"Auditor: {score_card.get('summary', '需要改进')}")
    if pmo_verdict == "FAIL":
        failed = pmo_result.get("criteria_failed", [])
        failed_strs = [str(f) for f in failed[:3]]
        feedback_parts.append(f"PMO未通过: {', '.join(failed_strs)}")
    retry_feedback = " | ".join(feedback_parts) if feedback_parts else ""
    
    # Build score card
    full_score_card = {
        "score": final_score,  # For backward compatibility
        "decision": decision,
        "next_action": next_action,
        "auditor_score": auditor_score,
        "pmo_score": pmo_score,
        "final_score": final_score,
        "auditor_verdict": auditor_verdict,
        "pmo_verdict": pmo_verdict,
        "feedback": score_card.get("summary", ""),
        "dimensions": score_card.get("dimensions", []),
        "suggestions": score_card.get("suggestions", []),
    }
    
    agent_state = get_agent_state("ceo")
    agent_state.log_decision(
        f"Auditor={auditor_score} + PMO={pmo_score} -> Final={final_score}/{config.gate_final_score} -> {decision}",
        full_score_card.get("feedback", "")
    )
    
    next_retry = retry_count + (0 if next_action == "deliver" else 1)
    
    return {
        "phase": next_action,  # "deliver", "revise", or "replan" -> maps to routing
        "workspace_id": workspace_id,
        "score_card": full_score_card,
        "retry_count": next_retry,
        "retry_feedback": retry_feedback,
        **({"final_output": fail_output} if decision == "FAIL" else {}),
        "execution_log": [
            f"[CEO-AGGREGATE] Auditor({auditor_score}) + PMO({pmo_score}) -> Final={final_score}/{config.gate_final_score} -> {decision}",
        ] + (
            [f"[CEO-AGGREGATE] ❌ FAIL after {max_retries} retries — tools unavailable or task too complex"]
            if decision == "FAIL" else []
        ) + ([f"[CEO-AGGREGATE] Retry #{next_retry}: {retry_feedback}"] if retry_feedback else []),
    }


@_safe_node("AutoRepair")
async def auto_repair_node(state: CEOState) -> dict:
    """Attempt to self-heal when the workflow fails.

    Parses error info from execution logs and dispatches the developer
    to fix the specific error in the codebase.
    """
    import logging
    ll = logging.getLogger("ai_company.self_heal")
    
    score_card = state.get("score_card", {})
    if score_card.get("decision") != "FAIL":
        return {"phase": "deliver", "workspace_id": state.get("workspace_id")}
    
    execution_log = state.get("execution_log", [])
    task = state.get("user_request", "")
    
    from src.self_heal import parse_error_from_logs, attempt_repair
    
    error_info = parse_error_from_logs(execution_log)
    if not error_info:
        ll.info("No actionable error found in logs, skipping self-heal")
        return {
            "phase": "deliver",
            "workspace_id": state.get("workspace_id"),
            "execution_log": ["[AUTO-REPAIR] No actionable error to fix"],
        }
    
    ll.info("Self-heal triggered: %s:%s → %s", 
            error_info.get("file"), error_info.get("line"), error_info.get("error_type"))
    
    try:
        repair_result = await attempt_repair(error_info, task)
    except Exception as e:
        ll.exception("Self-heal crashed")
        return {
            "phase": "deliver",
            "workspace_id": state.get("workspace_id"),
            "execution_log": [f"[AUTO-REPAIR] Repair attempt crashed: {e}"],
        }
    
    if repair_result.get("fixed"):
        return {
            "phase": "deliver",
            "workspace_id": state.get("workspace_id"),
            "execution_log": [
                f"[AUTO-REPAIR] ✅ Fixed {error_info.get('error_type')} in {error_info.get('file')}",
                f"[AUTO-REPAIR] Changes: {str(repair_result.get('changes', ''))[:200]}",
            ],
        }
    
    return {
        "phase": "deliver",
        "workspace_id": state.get("workspace_id"),
        "execution_log": [
            f"[AUTO-REPAIR] ❌ Could not fix {error_info.get('error_type')}: {repair_result.get('error', '?')}",
        ],
    }


@_safe_node("Deliver")
async def deliver_node(state: CEOState) -> dict:
    """Deliver: finalize task, record episodes, sync memory, and handle role promotion."""
    from src.departments.roles import role_registry
    from src.evolution.engine import record_completed_task
    
    agent_state = get_agent_state("ceo")
    
    # Record episode with rich metadata
    score_card = state.get("score_card", {}) or {}
    dept = state.get("department", "")
    episode_meta = {
        "score": score_card.get("final_score", score_card.get("score")),
        "department": dept,
        "verdict": score_card.get("decision") or score_card.get("verdict", ""),
        "retries": state.get("retry_count", 0),
    }
    await episode_memory.add_episode(
        content=f"Completed: {state.get('user_request', '')}",
        role="ceo",
        metadata=episode_meta,
    )

    # Sync to Chroma vector store for semantic search
    await sync_episode_to_chroma({
        "content": f"Task: {state.get('user_request', '')} | Result: {score_card.get('verdict', score_card.get('decision', 'N/A'))} | Score: {score_card.get('final_score', score_card.get('score', 'N/A'))}",
        "role": "ceo",
        "timestamp": datetime.now().isoformat(),
    })

    # Force save episodes to disk
    await episode_memory.force_save()
    
    # Persist token usage stats
    from src.utils.token_tracker import get_token_tracker
    token_tracker = get_token_tracker()
    token_tracker.save()
    
    # Trial role promotion check
    score = score_card.get("final_score", score_card.get("score", 0))
    promotion_msg = ""
    if dept:
        result = role_registry.record_use(dept, success=(score >= 60))
        if result == "promoted":
            role = role_registry.get(dept)
            promotion_msg = f"\n\n🎉 试用角色 **{role.display_name}** 已完成 3 次成功任务，晋升为正式角色！"
    
    # Keep the department output, don't overwrite with agent summary
    # Clean any JSON wrapper that leaked through
    dept_output = state.get("final_output", "")
    dept_output = _clean_output(str(dept_output)) if dept_output else ""
    if promotion_msg:
        dept_output = str(dept_output) + promotion_msg
    
    agent_state.clear_task()
    
    # ── Auto-Evolution: record this task outcome ──
    try:
        score_card = state.get("score_card", {}) or {}
        task_type = score_card.get("task_type", "GENERAL")
        # Determine peak score from execution_log
        peak_score = 0.0
        for log_entry in state.get("execution_log", []):
            if "AUDITOR" in str(log_entry) and "Overall" in str(log_entry):
                # Parse "Overall: XX.X/100"
                import re
                m = re.search(r"Overall:\s*([\d.]+)", str(log_entry))
                if m:
                    peak_score = max(peak_score, float(m.group(1)))
        
        record_completed_task(
            task=state.get("user_request", ""),
            department=state.get("department", "developer"),
            task_type=task_type,
            auditor_score=score_card.get("auditor_score", 0),
            pmo_score=score_card.get("pmo_score", 0),
            final_score=score or 0,
            retries=state.get("retry_count", 0),
            verdict=str(score_card.get("decision") or score_card.get("verdict", "")),
            peak_retry_score=peak_score if peak_score > 0 else (score or 0),
            tool_failures=_parse_tool_failures(state.get("execution_log", [])),
            tools_used=_parse_tools_used(state.get("execution_log", [])),
        )
    except Exception:
        import logging
        logging.getLogger("ai_company.evolution").debug(
            "Failed to record experience", exc_info=True)
    
    # ── Workspace: save results ──
    from src.workspace import TaskContext
    workspace_id = state.get("workspace_id")
    if workspace_id and dept_output:
        try:
            ws = TaskContext.load(workspace_id)
            if ws:
                ws.add_result(
                    output=str(dept_output),
                    score=int(score or 0),
                    department=str(dept),
                )
                ws.add_timeline(f"Delivered by {dept} (score: {score})")
        except Exception:
            pass
    
    # ── Skill Learning: capture successful workflows ──
    if score and int(score or 0) >= 60 and workspace_id:
        try:
            from src.learning import skill_library
            ws = TaskContext.load(workspace_id)
            if ws:
                context_data = ws._read("context.md", "")
                tool_calls_data = _parse_tools_from_timeline(context_data)
                caps_data = _parse_capabilities_from_timeline(context_data)
                if tool_calls_data:
                    skill_library.capture(
                        task=state.get("user_request", ""),
                        department=str(dept),
                        tool_calls=tool_calls_data,
                        capabilities=caps_data,
                        success=True,
                    )
        except Exception:
            import logging
            logging.getLogger("ai_company.learning").debug("Skill capture failed", exc_info=True)
    
    return {
        "phase": "complete",
        "final_output": dept_output,
        "execution_log": [f"[DELIVER] Task complete. Score: {score}"],
        **({} if not promotion_msg else {"execution_log": state.get("execution_log", []) + [f"[DELIVER] 🎉 Trial role promoted to established!"]}),
    }


# ─── Routing ──────────────────────────────────────

def route_after_pm(state: CEOState) -> str:
    """After PM: go to architect for code tasks, execute for others."""
    phase = state.get("phase", "execute")
    if phase == "architect":
        return "architect"
    return "execute"


def route_after_triage(state: CEOState) -> str:
    phase = state.get("phase", "")
    if phase == "deliver":
        return "deliver"
    if phase == "pm":
        return "pm"
    return "execute"


def route_after_department(state: CEOState) -> str:
    """Route based on task TYPE, not just department.

    COMMAND_EXECUTION and SIMPLE_QUERY skip Auditor/PMO entirely.
    Researcher/marketer also skip.
    Document tasks skip.
    """
    task_type = state.get("task_type", "")
    dept = state.get("department", "")

    # ── Never audit command execution, simple queries, or local system ops ──
    if task_type in ("COMMAND_EXECUTION", "SIMPLE_QUERY", "LOCAL_SYSTEM"):
        return "verify"

    # Researcher and marketer just need to return data — skip audit
    if dept in ("researcher", "marketer"):
        return "verify"

    # Document/PDF generation tasks don't need code-quality audit
    if task_type == "DOCUMENT":
        return "verify"

    if dept == "developer":
        import re as _vre2
        task = state.get("user_request", "").lower()
        if _vre2.search(r"pdf|生成.*文档|写报告|生成报告|写文档|周报|月报|日报|会议纪要|写总结", task):
            return "verify"

    return "auditor"


def route_after_aggregate(state: CEOState) -> str:
    """Decide where to go after CEO aggregates scores."""
    card = state.get("score_card", {})
    action = card.get("next_action", "deliver")
    decision = card.get("decision", "")
    if action == "deliver":
        # FAIL → try auto-repair before delivering
        if decision == "FAIL":
            return "auto_repair"
        return "deliver"
    # revise or replan → retry via execute
    return "execute"


# ─── Build Graph ──────────────────────────────────

def build_ceo_graph() -> StateGraph:
    """Construct the CEO LangGraph workflow.

    Flow: Triage -> PM -> Architect -> Execute -> Dept -> Auditor -> PMO -> CEO-Aggregate -> Deliver
              |                                                          v (retry)
              +----------------------------------------------------------+

    PM and Architect are now SEPARATE nodes:
    - PM: writes PRD with acceptance criteria (all departments)
    - Architect: designs tech stack/modules (only developer/qa departments)
    """
    workflow = StateGraph(CEOState)

    # Add nodes
    workflow.add_node("triage", triage_node)
    workflow.add_node("pm", pm_analyze_node)
    workflow.add_node("architect", architect_node)
    workflow.add_node("execute", execute_node)
    workflow.add_node("execute_department", execute_department_node)
    workflow.add_node("auditor", auditor_node)
    workflow.add_node("pmo", pmo_node)
    workflow.add_node("verify_aggregate", verify_aggregate_node)
    workflow.add_node("auto_repair", auto_repair_node)
    workflow.add_node("deliver", deliver_node)
    
    # Set entry
    workflow.set_entry_point("triage")
    
    # Triage → PM / Deliver / Execute
    workflow.add_conditional_edges(
        "triage",
        route_after_triage,
        {"pm": "pm",
         "deliver": "deliver", "execute": "execute"}
    )

    # PM → Architect (code tasks) or Execute (non-code tasks)
    workflow.add_conditional_edges(
        "pm",
        route_after_pm,
        {"architect": "architect", "execute": "execute"}
    )

    # Architect → Execute
    workflow.add_edge("architect", "execute")
    
    # Execute → Department → (Auditor+PMO or skip for simple roles)
    workflow.add_edge("execute", "execute_department")
    workflow.add_conditional_edges(
        "execute_department",
        route_after_department,
        {"auditor": "auditor", "verify": "verify_aggregate"}
    )
    workflow.add_edge("auditor", "pmo")
    workflow.add_edge("pmo", "verify_aggregate")
    
    # CEO Aggregate → Deliver / Auto-Repair / Retry
    workflow.add_conditional_edges(
        "verify_aggregate",
        route_after_aggregate,
        {"deliver": "deliver", "auto_repair": "auto_repair", "execute": "execute"}
    )
    
    # Auto-Repair → Deliver
    workflow.add_edge("auto_repair", "deliver")
    
    # Deliver → END
    workflow.add_edge("deliver", END)
    
    return workflow


async def run_ceo(user_message: str) -> CEOState:
    """Run the CEO workflow on a user message. Returns final state."""
    graph = build_ceo_graph()
    app = graph.compile(checkpointer=MemorySaver())
    
    initial_state: CEOState = {
        "messages": [HumanMessage(content=user_message)],
        "user_request": user_message,
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
    
    config_params = {
        "configurable": {
            "thread_id": f"ceo_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        }
    }
    
    final_state = await app.ainvoke(initial_state, config_params)
    return final_state
