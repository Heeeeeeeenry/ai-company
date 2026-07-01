# -*- coding: utf-8 -*-
"""CEO Agent - LangGraph Orchestration Engine"""
from typing import TypedDict, Annotated, Optional
from datetime import datetime
import operator

# ═══ Global switches ───
AUDIT_ENABLED = True  # /audit on|off — when False, skip Auditor+PMO for all tasks
import json
import re
import os
import urllib.request

# ═══ P0 Module Integration ═══
from src.intent import classify_task  # V5 intent router (replaces local classify_task)
from src.verification import verify_aggregate as _v5_verify_aggregate
from src.checkpoint import get_checkpoint  # P1: checkpoint 错误恢复

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
    hierarchical_plan: Optional[dict]           # P1.2: HierarchicalPlan serialized (goal, phases, current_phase)
    phase_outputs: Annotated[list, operator.add]  # P1.2: accumulated outputs from each phase


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
    # If raw looks like JSON, strip it entirely — but ONLY if it contains a tool call wrapper
    # (\"action\": \"tool\" or \"action\": \"final\"), not if it's legitimate content
    if raw.strip().startswith('{') and raw.strip().endswith('}'):
        try:
            parsed = json.loads(raw.strip())
            if isinstance(parsed, dict):
                action = parsed.get("action", "")
                if action in ("tool", "final") or action in (
                    "web_search", "web_fetch", "market_series",
                    "read_file", "write_file", "list_dir",
                    "run_python", "run_test", "lint_code",
                ):
                    return "Output format error. Please ask again or use /fix."
        except (json.JSONDecodeError, ValueError):
            pass
        # If it's valid JSON but NOT a tool wrapper, it's legitimate content — return as-is
    # ── Compliance boilerplate detection ──
    # deepseek-v4-pro sometimes outputs compliance ack instead of answer
    _cl = raw.strip()
    _compliance_patterns = [
        r"收到.*我会严格", r"我会.*遵守.*格式", r"有什么需要我做的",
        r"好的.*我会.*JSON", r"明白了.*我会",
        r"了解.*马上.*格式", r"按照.*格式.*回复", r"遵守.*JSON.*格式",
        r"^收到[，,。!\s]*$",
    ]
    if any(re.search(p, _cl) for p in _compliance_patterns):
        return raw  # Don't mask — let upstream handle
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


def _strip_wrapping_quotes(text: str) -> str:
    """Trim whitespace, surrounding quotes, and trailing punctuation."""
    if not text:
        return ""
    text = text.strip().strip('"\'' "“”‘’「」『』")
    return text.strip(" ，,。：:；;")



def _quick_reply(task: str) -> str:
    """Generate a fast direct reply for trivial/short queries without LLM.

    Uses simple keyword matching for common conversational patterns.
    Returns a friendly, context-free response.
    """
    import re as _re
    t = task.strip().lower()
    # Math patterns
    math_m = _re.match(r"^(?:计算)?\s*(\d+)\s*([\+\-\*\/×÷])\s*(\d+)\s*(?:等于几|等于多少|是多少|=\?|\?)?$", t)
    if math_m:
        a, op, b = int(math_m.group(1)), math_m.group(2), int(math_m.group(3))
        op_map = {"+": a + b, "-": a - b, "*": a * b, "×": a * b, "/": a / b if b != 0 else "∞", "÷": a / b if b != 0 else "∞"}
        result = op_map.get(op, "?")
        if isinstance(result, float) and result == int(result):
            result = int(result)
        return f"{a} {op} {b} = {result}"
    # Quadratic equation: ax²+bx+c=0  (also handles x^2, x² forms)
    _quad = _re.match(
        r"^(?:.*?)?"  # optional prefix like "解" "求"
        r"(?:x\^2|x²|x\s*\*\*\s*2)\s*"  # x² term
        r"([+\-]?\s*\d+(?:\.\d+)?)\s*x\s*"  # bx term
        r"([+\-]?\s*\d+(?:\.\d+)?)\s*=\s*0"  # c term
        r"(?:.*?)$", t)
    if _quad:
        b_raw = _quad.group(1).replace(" ", "")
        c_raw = _quad.group(2).replace(" ", "")
        try:
            b_val = float(b_raw) if b_raw else 0.0
            c_val = float(c_raw) if c_raw else 0.0
            disc = b_val * b_val - 4 * c_val  # a=1 discriminant
            if disc < 0:
                real = -b_val / 2
                imag = ((-disc) ** 0.5) / 2
                return f"x₁ = {real:.4f} + {imag:.4f}i\nx₂ = {real:.4f} - {imag:.4f}i"
            elif disc == 0:
                x = -b_val / 2
                return f"x = {x:.4f} (重根)"
            else:
                x1 = (-b_val + disc ** 0.5) / 2
                x2 = (-b_val - disc ** 0.5) / 2
                return f"x₁ = {x1:.4f}\nx₂ = {x2:.4f}"
        except (ValueError, ZeroDivisionError):
            pass  # fall through to normal triage
    # Greetings
    if any(w in t for w in ["你好", "您好", "嗨", "哈喽", "halo", "hello", "hi"]):
        return "👋 你好！有什么可以帮你的？"
    # Bot identity questions
    if _re.search(r"你(?:是谁|叫什么|会什么|能做什么|有什么用|在吗|好吗|聪明吗)", t):
        return "我是 AI-Company 的 CEO 助手，负责分析需求、分派任务、审核结果。有什么需要尽管说！"
    # Thanks
    if any(w in t for w in ["谢谢", "thanks", "thx", "感谢"]):
        return "不客气！😊"
    # Bye
    if any(w in t for w in ["再见", "bye", "88", "拜拜"]):
        return "再见！有需要随时找我 👋"
    # Interjections
    if _re.match(r"^[哦嗯啊哈嘿哎咦哟]{1,3}[！!]*$", t):
        return "😄"
    # Default for short queries
    return "有什么我可以帮你的吗？输入 /help 查看我能做什么。"


def _extract_wechat_send_request(task: str) -> Optional[tuple[str, str]]:
    """Extract (contact, message) from natural-language WeChat send requests."""
    task = (task or "").strip()
    if not task or not re.search(r"微信|wechat|发消息|发微信|发信息|发送消息|发送信息|告诉|发给", task, re.IGNORECASE):
        return None

    # ═══ Combined patterns (contact+message in one regex) ═══
    combined_patterns = [
        # "微信发送消息给CONTACT说 MESSAGE" / "微信给CONTACT发消息说 MESSAGE"
        r"微信(?:发送消息|发消息|发微信)?给\s*([^说，。:：\n]+?)\s*(?:发消息|发微信|发送消息)?\s*说\s*(.+)$",
    ]
    for pattern in combined_patterns:
        match = re.search(pattern, task, re.IGNORECASE)
        if match:
            contact = _strip_wrapping_quotes(match.group(1))
            message = _strip_wrapping_quotes(match.group(2))
            if contact and message:
                return contact, message

    quoted_parts = [
        _strip_wrapping_quotes(m.group(1))
        for m in re.finditer(r'["“”「『](.+?)["””」』]', task)
    ]
    if len(quoted_parts) >= 2 and all(quoted_parts[:2]):
        return quoted_parts[0], quoted_parts[1]

    message = ""
    message_match = None
    message_patterns = [
        # "发微信: MESSAGE" / "发消息: MESSAGE"
        r"(?:发微信|发消息|发送消息|发送信息|发信息)\s*[：:：]\s*(.+)$",
        r"(?:说|内容是|内容为|告诉)\s*[：:：]\s*(.+)$",
        r"(?:发送消息|发消息|发送信息|发信息)\s+(.+)$",
        r"(?:发微信|发消息)\s+(.+)$",
        # Generic colon: "发消息给CONTACT：MESSAGE"
        r"[：:：]\s*(.+)$",
        # Bare text after comma: "给CONTACT发微信，MESSAGE"
        r"[，,。]\s*(.+)$",
    ]
    for pattern in message_patterns:
        match = re.search(pattern, task, re.IGNORECASE)
        if match:
            message_match = match
            message = _strip_wrapping_quotes(match.group(1))
            if message:
                break

    search_text = task[:message_match.start()] if message_match else task
    contact = ""
    contact_patterns = [
        # "给CONTACT发微信" / "给CONTACT发消息" — skip "微信" after 给 (not contact name)
        r"(?:给|发给|发送给|告诉)\s*(?:微信(?:\s*(?:好友|联系人))?\s*)?([^，。:：\n]+?)(?=发微信|发消息|发送消息|发信息|发送信息|$)",
        # "给我的微信好友 CONTACT" / "给微信联系人 CONTACT"
        r"(?:给我的微信(?:好友|联系人)?|给微信(?:好友|联系人)?|给(?:好友|联系人)?)\s*[：:，,\s]*([^，。:：\n]+)",
        # "微信发送消息给CONTACT"
        r"(?:微信(?:发送消息)?给)\s*([^，。:：\n]+)",
        r"(?:联系人)\s*[：:，,\s]*([^，。:：\n]+)",
    ]
    for pattern in contact_patterns:
        match = re.search(pattern, search_text, re.IGNORECASE)
        if match:
            contact = _strip_wrapping_quotes(match.group(1))
            if contact:
                break

    if contact and message:
        return contact, message
    return None


def _extract_wechat_conversation_request(task: str) -> Optional[tuple[str, Optional[int], bool]]:
    """Extract (contact, max_turns, initiate) from natural-language conversation/reply requests.
    
    Matches:
      Passive: "帮我跟XX聊天" / "代聊XX 3轮" / "替我回复XX"
      Active: "和XX闲聊几句" / "跟XX聊聊天" / "用微信和XX聊聊"
    
    initiate=True: AI should send an opening message first, then listen.
    """
    task = (task or "").strip()
    if not task or not re.search(r"聊天|回复|代聊|帮我.*聊|替.*回复|聊几句|自动回复|闲聊|聊聊天|聊聊|唠唠", task, re.IGNORECASE):
        return None
    
    # Detect active initiation keywords
    initiate = bool(re.search(r"闲聊|聊聊天|聊聊|主动|随便聊|唠唠", task))
    
    # Extract max_turns
    max_turns = None
    turns_match = re.search(r"(\d+)\s*(?:轮|句|次|个来回)", task)
    if turns_match:
        max_turns = int(turns_match.group(1))
    
    # Extract contact name (闲随便唠 excluded to prevent capture leakage)
    contact_patterns = [
        # "帮我跟XX聊天" / "替我回复XX" / "帮我回复XX"
        r"(?:帮|替)\s*(?:我\s*)?(?:跟|和|回复)\s*([^\s，。:：\d聊代闲随便唠]+)",
        # "跟XX聊天" / "和XX聊几句" / "用微信和XX闲聊/随便聊聊/唠唠"
        r"(?:跟|和|用微信和|用微信跟)\s*([^\s，。:：\d聊代闲随便唠]+?)\s*(?:聊天|聊几句|闲聊|聊聊|聊聊天|随便聊聊|唠唠|自动回复|$)",
        # "回复XX" / "代聊XX 3轮"
        r"(?:回复|代聊)\s*([^\s，。:：\d聊代闲随便唠]+)",
    ]
    for pattern in contact_patterns:
        match = re.search(pattern, task, re.IGNORECASE)
        if match:
            contact = _strip_wrapping_quotes(match.group(1))
            if contact:
                return contact.strip(), max_turns, initiate
    
    return None


def _debug_report(hypothesis_id: str, location: str, msg: str, data: Optional[dict] = None) -> None:
    # #region debug-point A:wechat-triage-report
    env_path = ".dbg/wechat-send-fail.env"
    server_url = os.environ.get("DEBUG_SERVER_URL", "")
    session_id = os.environ.get("DEBUG_SESSION_ID", "")
    run_id = os.environ.get("DEBUG_RUN_ID", "pre-fix")
    try:
        with open(env_path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("DEBUG_SERVER_URL="):
                    server_url = line.split("=", 1)[1].strip()
                elif line.startswith("DEBUG_SESSION_ID="):
                    session_id = line.split("=", 1)[1].strip()
    except OSError:
        pass
    if not server_url or not session_id:
        return
    payload = {
        "sessionId": session_id,
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": location,
        "msg": msg,
        "data": data or {},
    }
    try:
        request = urllib.request.Request(
            server_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(request, timeout=2).read()
    except Exception:
        pass
    # #endregion


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
    
    # Timing callback (records LLM call duration)
    try:
        from src.utils.timing import TimingCallback
        callbacks.append(TimingCallback(role=role, model=mc.model))
    except ImportError:
        pass
    
    if mc.provider == "deepseek":
        # Reasoner needs more time (can take 30-90s), chat is faster
        is_reasoner = "reasoner" in mc.model.lower()
        # Max tokens per role: developer/researcher need room for long outputs
        _max_tokens_map = {
            "developer": 8192, "researcher": 8192, "marketer": 4096,
            "qa": 4096, "devops": 4096, "pm": 2048, "architect": 2048,
            "ceo": 2048, "review": 2048,
        }
        return ChatOpenAI(
            model=mc.model,
            api_key=config.deepseek_api_key,
            base_url="https://api.deepseek.com/v1",
            timeout=120 if is_reasoner else 45,
            max_retries=1,
            max_tokens=_max_tokens_map.get(role, 4096),
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
    """Decorator: wraps a graph node with crash protection and timing.
    
    If the node raises an unexpected exception, the workflow falls
    through to deliver with a clear error message instead of crashing.
    
    Also records execution time when timer.enabled.
    Emits status line so user knows what's happening.
    """
    # Node labels in Chinese for status display
    _NODE_LABEL = {
        "Triage": "分析意图",
        "PM": "制定计划",
        "Architect": "架构设计",
        "Execute": "准备执行",
        "Department": "执行任务",
        "Auditor": "代码审查",
        "PMO": "合规检查",
        "VerifyAggregate": "结果验证",
        "AutoRepair": "自动修复",
        "Deliver": "生成回复",
    }
    def decorator(fn):
        async def wrapper(state, *args, **kwargs):
            from src.utils.timing import timer
            timer.start(name, "node")
            label = _NODE_LABEL.get(name, name)
            # Emit progress: show phase transition (skip for Deliver — shown at end)
            if name != "Deliver":
                dept = state.get("department", "") if isinstance(state, dict) else getattr(state, "department", "")
                dept_hint = f" ({dept})" if dept and name in ("Department", "Execute") else ""
                print(f"  → {label}{dept_hint}", flush=True)
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
            finally:
                timer.stop(name)
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator


@_safe_node("Triage")
async def triage_node(state: CEOState) -> dict:
    """Node function."""
    from src.utils.timing import timer
    timer.start_task()  # start the overall task timer
    
    task = state.get("user_request", "").strip()
    
    # ═══ Trivial query fast-path: never route to AI pipeline ═══
    # Exact single-word / short phrases
    trivial_exact = {
        "help", "?", "h", "hi", "hello", "hey", "你好", "您好",
        "thanks", "thx", "ok", "好的", "test", "测试",
        "fuck", "shit", "damn", "wtf", "lol", "haha", "哈哈",
        "no", "yes", "yeah", "nope", "yep", "嗯", "哦", "啊",
        "bye", "goodbye", "再见", "88", "886",
        "what", "why", "when", "who", "how",
    }
    task_lower = task.lower()
    if task_lower in trivial_exact or len(task) <= 2:
        return {
            "phase": "deliver",
            "department": "ceo",
            "task_type": "LOCAL_SYSTEM",
            "final_output": "👋 你好！输入 /help 查看可用命令，或直接问我问题。",
            "score_card": {"score": 100, "decision": "APPROVE", "final_score": 100,
                          "next_action": "deliver"},
            "execution_log": ["[TRIAGE] Trivial query → direct reply, skip pipeline"],
        }

    # ═══ Expanded fast-path: pattern-based trivial detection ═══
    # Simple math, short greetings, meta questions — these don't need the pipeline.
    _trivial_patterns = [
        # Simple arithmetic: "1+1", "2*3等于几", "计算3+5"
        r"^(?:计算)?\s*\d+\s*[\+\-\*\/×÷]\s*\d+\s*(?:等于几|等于多少|是多少|=\?|\?)?$",
        # Quadratic equations: "x^2+15.3x+55.1=0", "x²-4x+4=0 求x"
        r".*?x(?:\^2|²|\*\*2)\s*[+\-]\s*\d+(?:\.\d+)?\s*x\s*[+\-]\s*\d+(?:\.\d+)?\s*=\s*0.*",
        # Greetings with particles: "你好吗", "您好啊", "嗨你好"
        r"^(?:你好|您好|嗨|哈喽|halo)(?:吗|啊|呀|哦|呢|！|!)*$",
        # Short meta questions about the bot itself
        r"^(?:你(?:是谁|叫什么|会什么|能做什么|有什么用|在吗|好吗|怎么样|聪明吗))[？?！!]*$",
        # Single-word interjections
        r"^(?:哦|嗯|啊|哈|嘿|哎|咦|哟){1,3}[！!]*$",
        # Very short questions (<10 chars) that look conversational
    ]
    for pat in _trivial_patterns:
        if re.match(pat, task.strip()):
            return {
                "phase": "deliver",
                "department": "ceo",
                "task_type": "LOCAL_SYSTEM",
                "final_output": _quick_reply(task),
                "score_card": {"score": 100, "decision": "APPROVE", "final_score": 100,
                              "next_action": "deliver"},
                "execution_log": [f"[TRIAGE] Fast-path match(pat={pat[:40]}) → direct reply"],
            }

    # Short conversational queries (<15 chars, no code/command keywords)
    # These are likely chitchat, not real tasks.
    if len(task) < 15:
        _task_keywords = ["写", "开发", "实现", "修改", "修复", "bug", "代码", "code",
                         "函数", "接口", "api", "部署", "deploy", "数据库", "查询",
                         "搜索", "查", "搜", "报告", "文档", "分析",
                         # Session/context meta queries — need real processing
                         "对话", "说过", "问过", "之前", "刚才", "历史", "聊天",
                         "回答", "输出", "上一",
                         # Knowledge / fact questions — need memory recall
                         "什么", "谁", "怎么", "为什么", "干嘛", "叫啥",
                         "多大", "几岁", "哪里", "哪个", "多少", "何时"]
        if not any(kw in task for kw in _task_keywords):
            return {
                "phase": "deliver",
                "department": "ceo",
                "task_type": "LOCAL_SYSTEM",
                "final_output": _quick_reply(task),
                "score_card": {"score": 100, "decision": "APPROVE", "final_score": 100,
                              "next_action": "deliver"},
                "execution_log": ["[TRIAGE] Short non-task → fast reply"],
            }
    
    from src.departments.roles import role_registry

    llm = _get_llm("ceo")
    agent_state = get_agent_state("ceo")
    agent_state.set_task(state["user_request"])
    
    # ═══ WeChat Send Fast-Path: direct execution, no agent loop ═══
    # task already defined above with .strip()
    
    # ─── Conversation Fast-Path (AI-powered chat) ───
    conv_request = _extract_wechat_conversation_request(task)
    if conv_request:
        contact, max_turns, initiate = conv_request
        max_turns = max_turns or 3  # default 3 turns
        try:
            from src.wechat.conversation import ConversationManager
            mgr = ConversationManager(contact)
            
            if initiate:
                # Active mode: send an opening message first, then listen
                logger = __import__('logging').getLogger("ai_company")
                logger.info("Active conversation: sending opening message to %s", contact)
                opener = mgr._generate_opener()
                if opener:
                    logger.info("Opening: %s", opener[:60])
                    opener_result = mgr.send(opener)
                    if opener_result.get("success"):
                        mgr.history.append({
                            "sender": "me",
                            "content": opener,
                            "time": __import__('datetime').datetime.now().isoformat(),
                        })
            
            replies = mgr.run(turns=max_turns, poll_interval=3.0)
            
            # Build detailed summary
            sent = [r for r in replies if r["sent"]]
            failed = [r for r in replies if not r["sent"]]
            
            parts = [f"与{contact}对话完成"]
            if sent:
                parts.append(f"已发送{len(sent)}条")
                for r in sent:
                    parts.append(f"  ✓ {r['text'][:40]}")
            if failed:
                parts.append(f"生成但发送失败{len(failed)}条")
                for r in failed:
                    parts.append(f"  ✗ {r['text'][:40]} ({r.get('error','?')})")
            if not replies:
                parts.append("无新消息需要回复")
            
            reply_summary = "\n".join(parts)
            return {
                "phase": "deliver",
                "department": "devops",
                "task_type": "LOCAL_SYSTEM",
                "final_output": reply_summary,
                "score_card": {"score": 95, "decision": "APPROVE", "final_score": 95,
                              "next_action": "deliver"},
                "execution_log": [f"[TRIAGE] Conversation fast-path: {contact}, {len(sent)} sent, {len(failed)} failed"],
            }
        except Exception as e:
            import logging
            logging.getLogger("ai_company").warning(
                "Conversation fast-path failed, falling back: %s", e,
            )
    
    wechat_request = _extract_wechat_send_request(task)
    _debug_report(
        "A",
        "src/ceo/graph.py:triage_node",
        "[DEBUG] Parsed WeChat fast-path request",
        {
            "task": task[:200],
            "wechat_request": list(wechat_request) if wechat_request else None,
        },
    )
    if wechat_request:
        contact, message = wechat_request
        try:
            from src.execution._wechat_tool import send_wechat_message
            result = send_wechat_message(contact, message)
            _debug_report(
                "A",
                "src/ceo/graph.py:triage_node",
                "[DEBUG] WeChat fast-path tool returned",
                {
                    "contact": contact,
                    "message": message[:100],
                    "result": result,
                },
            )
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
        except Exception as e:
            import logging
            logging.getLogger("ai_company").warning(
                "WeChat fast-path failed, falling back to normal routing: %s",
                e,
            )
            _debug_report(
                "A",
                "src/ceo/graph.py:triage_node",
                "[DEBUG] WeChat fast-path raised exception",
                {"error": str(e)[:300]},
            )
    
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
    
    # ═══ V5 IntentRouter: detailed intent classification (for logging only) ═══
    # Skip for fast-path departments (keyword-matched) — only needed for LLM-fallback cases.
    user_req = state.get("user_request", "")
    if match_method.startswith("LLM") or match_method.startswith("Fallback"):
        try:
            from src.intent import IntentRouter
            intent_router = IntentRouter()
            v5_result = intent_router.classify(user_req)
            intent_detail = f"intent={v5_result.intent} conf={v5_result.confidence:.2f} via={v5_result.matched_by}"
        except Exception:
            intent_detail = "V5 intent unavailable"
    else:
        intent_detail = f"V5 skipped (fast-path: {match_method})"
    
    return {
        "phase": next_phase,
        "department": department,
        "workspace_id": workspace_id,
        "task_type": classify_task(state.get("user_request", ""), department),
        "execution_log": [f"[TRIAGE] {match_method} -> {department} | {intent_detail}"],
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


# Keep old name for backwards compat
def _classify_task_type(state: CEOState) -> str:
    return classify_task(
        state.get("user_request", ""),
        state.get("department", "developer"),
    )


def _safe_slug(text: str, max_len: int = 40) -> str:
    """将文本转成安全的短标识符(用于 checkpoint 名称)。"""
    import re
    slug = re.sub(r'[^\w\u4e00-\u9fff]', '-', text.strip())
    slug = re.sub(r'-+', '-', slug)
    slug = slug.strip('-')
    if len(slug) > max_len:
        slug = slug[:max_len]
    return slug or "task"


def _discover_project_files() -> list[str]:
    """发现项目中已存在的可跟踪文件(仅 Python 源文件)。

    扫描项目根目录下的 src/ 和 tests/ 目录。
    返回相对路径列表。
    """
    import glob as _glob
    project_root = os.path.join(os.path.dirname(__file__), "..", "..")
    root = os.path.abspath(project_root)
    files = []
    for pattern in ("src/**/*.py", "tests/**/*.py"):
        for fpath in _glob.glob(pattern, root_dir=root, recursive=True):
            if "__pycache__" not in fpath:
                files.append(fpath)
    return sorted(files)[:200]  # 限制最多 200 个文件


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
    import re
    task = state.get("user_request", "")
    task_lower = task.lower() if task else ""
    
    # Fast-path: skip PM LLM call for document/PDF tasks
    is_doc_task = bool(re.search(
        r"pdf|生成.*文档|写报告|生成报告|写文档|周报|月报|日报|会议纪要|写总结|导出.*pdf|统计.*导出",
        task_lower
    )) if task_lower else False

    if is_doc_task:
        return {
            "phase": "execute",
            "plan": {
                "summary": task,
                "prd": {"summary": task, "features": [], "acceptance_criteria": [], "edge_cases": [], "priority": "P1"},
            },
            "execution_log": ["[PM] Document task -> skip LLM, direct execute"],
        }

    # Fast-path: SIMPLE_QUERY and short tasks don't need a formal PM PRD.
    # The department can handle these directly without acceptance criteria.
    task_type_raw = state.get("task_type", "")
    if task_type_raw in ("SIMPLE_QUERY", "LOCAL_SYSTEM") or len(task) < 30:
        return {
            "phase": "execute",
            "department": state.get("department", "developer"),
            "prd": {
                "summary": task[:100],
                "features": [task[:80]],
                "acceptance_criteria": ["Answer is accurate and concise"],
                "edge_cases": [],
                "priority": "P2",
            },
            "execution_log": [f"[PM] {task_type_raw or 'Short'} task -> skip LLM, fast PRD"],
        }
    
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
    tech_stack = design.get("tech_stack", [])
    stack_first = tech_stack[0] if tech_stack else "N/A"

    return {
        "phase": "execute",
        "arch_design": arch_text,
        "execution_log": [
            f"[Arch] Stack: {stack_first}, "
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
    """Node function. P1.2: supports hierarchical phase execution."""
    from src.departments.agents import dispatch_to_department

    department = state.get("department", "developer")

    # ── P1.2 Hierarchical Plan Management ──
    hplan_data = state.get("hierarchical_plan")
    current_phase = None
    hplan = None

    if hplan_data:
        # 反序列化已有计划
        from src.workflow.planner import HierarchicalPlan, PlanPhase, WorkflowStep
        hplan = _deserialize_hierarchical_plan(hplan_data)
        current_phase = hplan.next_phase()
    else:
        # 生成层次化计划
        from src.workflow.planner import get_workflow_planner, HierarchicalPlan, PlanPhase
        planner = get_workflow_planner()
        task = state.get("user_request", "")
        task_type = state.get("task_type", "GENERAL")
        # 使用兼容接口生成层次化计划
        hplan = planner.compat_hierarchical_plan(task, task_type)
        current_phase = hplan.next_phase()

    if current_phase is None:
        # 所有phase已完成
        if hplan and hplan.all_phases_done():
            return {
                "phase": "audit",
                "execution_log": ["[DEPT] All phases complete"],
                "final_output": _collect_phase_outputs(state),
            }
        # 没有可执行phase（依赖未满足等）
        return {
            "phase": "deliver",
            "final_output": hplan.get_partial_results() if hplan else "No phases to execute",
            "execution_log": ["[DEPT] No executable phase (dependencies unmet)"],
        }

    current_phase.status = "running"
    phase_name = current_phase.name

    # ── Determine department from phase steps ──
    # Use the first step's agent as the primary department
    if current_phase.steps:
        primary_step = current_phase.steps[0]
        effective_department = primary_step.agent
        # Build task from phase steps
        phase_task = f"[Phase: {phase_name}] {state.get('user_request', '')}"
        for step in current_phase.steps:
            if step.params.get("task"):
                phase_task = step.params["task"]
                break
    else:
        effective_department = department
        phase_task = state.get("user_request", "")

    # Build rich context from PM and Architect
    context_parts = []

    # Add phase context
    context_parts.append(
        f"[Hierarchical Phase: {phase_name}]\n"
        f"Goal: {hplan.goal if hplan else phase_task}\n"
        f"Phase steps: {len(current_phase.steps)}\n"
        f"On failure: {current_phase.on_failure} (retries: {current_phase.retry_count}/{current_phase.max_retries})"
    )

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
        task_ctx.add_timeline(f"Dispatched to {effective_department} (phase: {phase_name})")
    # Inject workspace context into agent prompt
    ws_context = task_ctx.get_context()
    if ws_context:
        context_parts.insert(0, f"[Workspace Context — Previous Findings]\n{ws_context}")

    context = "\n\n".join(context_parts) if context_parts else ""

    # ── Capability Discovery: determine what capabilities this task needs ──
    from src.capability import CapabilityPlanner
    cap_planner = CapabilityPlanner()
    cap_plan = cap_planner.analyze(state.get("user_request", ""))
    dynamic_capabilities = cap_plan.capabilities
    # If the planned role differs from what triage selected, use the planner's choice
    if cap_plan.confidence > 0.5 and cap_plan.role_hint:
        effective_department = cap_plan.role_hint
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

    # ── P1 Checkpoint: CODING 任务执行前自动保存状态 ──
    task_type = _classify_task_type(state)
    checkpoint_name = None
    if task_type == "CODING":
        try:
            ck = get_checkpoint()
            project_files = _discover_project_files()
            if project_files:
                checkpoint_name = ck.save(
                    f"auto-coding-{_safe_slug(state.get('user_request', 'task'))}",
                    project_files,
                )
        except Exception:
            checkpoint_name = None

    try:
        result = await dispatch_to_department(
            department=effective_department,
            task=phase_task,
            context=context,
            dynamic_capabilities=dynamic_capabilities,
        )
    except Exception as e:
        import logging
        logging.getLogger("ai_company").exception("Department dispatch failed")

        # ── P1.2: Apply hierarchical failure strategy ──
        if hplan and current_phase:
            strategy = hplan.mark_failed(
                phase_name,
                error=f"{type(e).__name__}: {e}",
            )
            if strategy == "retry":
                return {
                    "phase": "execute",
                    "hierarchical_plan": _serialize_hierarchical_plan(hplan),
                    "execution_log": [f"[DEPT-{phase_name}] RETRY #{current_phase.retry_count}: {e}"],
                    "retry_feedback": f"Phase '{phase_name}' failed: {e}",
                    "workspace_id": workspace_id,
                }
            elif strategy == "skip":
                return {
                    "phase": "execute",
                    "hierarchical_plan": _serialize_hierarchical_plan(hplan),
                    "execution_log": [f"[DEPT-{phase_name}] SKIPPED: {e}"],
                    "phase_outputs": [f"[{phase_name}] SKIPPED: {str(e)[:200]}"],
                    "workspace_id": workspace_id,
                }
            # abort
            return {
                "phase": "deliver",
                "final_output": hplan.get_partial_results(),
                "execution_log": [f"[DEPT-{phase_name}] ABORTED: {e}"],
                "workspace_id": workspace_id,
            }

        error_msg = f"Department '{department}' crashed: {type(e).__name__}"
        if checkpoint_name:
            error_msg += f"\n💡 可用 /checkpoint rollback {checkpoint_name} 恢复到执行前状态"
        return {
            "phase": "deliver",
            "final_output": error_msg,
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
        error_msg = result.get("error", "Department failed")

        # ── P1.2: Apply hierarchical failure strategy ──
        if hplan and current_phase:
            strategy = hplan.mark_failed(
                phase_name,
                error=error_msg,
                partial_output=output[:500] if output else "",
            )
            if strategy == "retry":
                if checkpoint_name:
                    error_msg += f"\n💡 可用 /checkpoint rollback {checkpoint_name} 恢复到执行前状态"
                return {
                    "phase": "execute",
                    "hierarchical_plan": _serialize_hierarchical_plan(hplan),
                    "execution_log": [f"[DEPT-{phase_name}] RETRY #{current_phase.retry_count}: {error_msg[:100]}"],
                    "retry_feedback": f"Phase '{phase_name}' failed: {error_msg}",
                    "workspace_id": workspace_id,
                }
            elif strategy == "skip":
                return {
                    "phase": "execute",
                    "hierarchical_plan": _serialize_hierarchical_plan(hplan),
                    "execution_log": [f"[DEPT-{phase_name}] SKIPPED: {error_msg[:100]}"],
                    "phase_outputs": [f"[{phase_name}] SKIPPED: {error_msg[:200]}"],
                    "workspace_id": workspace_id,
                }
            # abort
            return {
                "phase": "deliver",
                "final_output": hplan.get_partial_results(),
                "execution_log": [f"[DEPT-{phase_name}] ABORTED: {error_msg[:100]}"],
                "workspace_id": workspace_id,
            }

        if checkpoint_name:
            error_msg += f"\n💡 可用 /checkpoint rollback {checkpoint_name} 恢复到执行前状态"
        return {
            "phase": "deliver",
            "final_output": error_msg,
            "execution_log": [f"[DEPT-{department}] FAILED: {str(result.get('error', ''))[:100]}"],
            "workspace_id": workspace_id,
        }

    # ── P1.2: Phase succeeded ──
    if hplan and current_phase:
        hplan.mark_done(phase_name)
        # Check if more phases remain
        next_phase = hplan.next_phase()
        next_phase_info = f" → next: {next_phase.name}" if next_phase else " → all done"

        return {
            "phase": "execute" if next_phase else "audit",
            "hierarchical_plan": _serialize_hierarchical_plan(hplan),
            "execution_log": [
                f"[DEPT-{phase_name}] ✓ Complete (PM={'Y' if prd else 'N'} Arch={'Y' if arch_design else 'N'}){next_phase_info}",
            ] + ([f"[DEPT-{phase_name}] Tool failures: {failed_calls}/{len(tool_calls)} calls"] if tool_calls else []),
            "final_output": output,
            "phase_outputs": [f"[{phase_name}] {output[:500]}"],
            "workspace_id": workspace_id,
            "task_type": classify_task(state.get("user_request", ""), effective_department),
        }

    # Fallback: original non-hierarchical flow
    return {
        "phase": "audit",
        "execution_log": [
            f"[DEPT-{department}] Work completed (PM={'Y' if prd else 'N'} Arch={'Y' if arch_design else 'N'})",
        ] + ([f"[DEPT-{department}] Tool failures: {failed_calls}/{len(tool_calls)} calls"] if tool_calls else []),
        "final_output": output,
        "workspace_id": workspace_id,
        "task_type": classify_task(state.get("user_request", ""), effective_department),
    }


# ── P1.2 Helper Functions ──────────────────────

def _serialize_hierarchical_plan(hplan) -> dict:
    """序列化 HierarchicalPlan 为可存储在state中的dict。"""
    from dataclasses import asdict
    return asdict(hplan)


def _deserialize_hierarchical_plan(data: dict):
    """从dict反序列化 HierarchicalPlan。"""
    from src.workflow.planner import HierarchicalPlan, PlanPhase, WorkflowStep
    phases = []
    for p_data in data.get("phases", []):
        steps = []
        for s_data in p_data.get("steps", []):
            steps.append(WorkflowStep(
                name=s_data.get("name", ""),
                agent=s_data.get("agent", ""),
                action=s_data.get("action", ""),
                params=s_data.get("params", {}),
                depends_on=s_data.get("depends_on", []),
                verify=s_data.get("verify", False),
            ))
        phases.append(PlanPhase(
            name=p_data.get("name", ""),
            steps=steps,
            depends_on=p_data.get("depends_on", []),
            on_failure=p_data.get("on_failure", "abort"),
            max_retries=p_data.get("max_retries", 1),
            status=p_data.get("status", "pending"),
            retry_count=p_data.get("retry_count", 0),
            partial_output=p_data.get("partial_output"),
            error_message=p_data.get("error_message", ""),
        ))
    return HierarchicalPlan(
        goal=data.get("goal", ""),
        phases=phases,
        fallback_plan=data.get("fallback_plan"),
        current_phase=data.get("current_phase", 0),
    )


def _collect_phase_outputs(state: dict) -> str:
    """收集所有phase的输出。"""
    outputs = state.get("phase_outputs", [])
    if not outputs:
        return state.get("final_output", "")
    return "\n\n".join(str(o) for o in outputs)


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


# ─── Legacy task_type → V5 intent mapping (for verifier compatibility) ───

_LEGACY_TO_V5_INTENT = {
    "COMMAND_EXECUTION": "COMMAND",
    "SIMPLE_QUERY": "SEARCH",
    "DEVELOPMENT": "CODING",
    "CODE_REVIEW": "CODE_REVIEW",
    "CREATIVE": "CREATIVE",
    "LOCAL_SYSTEM": "SYSTEM",
    "DOCUMENT": "FILE",
    "GENERAL": "GENERAL_CHAT",
}


@_safe_node("VerifyAggregate")
async def verify_aggregate_node(state: CEOState) -> dict:
    """Node function."""    
    def _is_fail(output: str, exec_log: list) -> bool:
        """Check if output indicates a real failure (not just non-empty)."""
        patterns = [
            # System errors / crashes
            r"Max iterations exhausted", r"CRASHED", r"FAILED",
            # Chinese stale/evasive answers
            r"\u65e0\u6cd5\u83b7\u53d6\u5b9e\u65f6", r"\u6839\u636e\u5df2\u77e5\u6570\u636e",
            # English stale/evasive answers
            r"cannot\s+(?:access|fetch|retrieve)",
            # Python runtime errors
            r"NameError", r"TypeError", r"KeyError", r"ValueError",
            r"AttributeError", r"ImportError", r"ModuleNotFoundError",
            r"SyntaxError", r"IndentationError", r"IndexError",
            r"FileNotFoundError", r"PermissionError", r"OSError",
            # LLM output failures
            r"Internal tool call leaked", r"Output format error",
            r"No valid JSON", r"unable to (?:parse|process|generate)",
            # Empty / near-empty outputs
            r"^\s*$", r"^\s*[{}\[\]]\s*$",
            # Chinese compliance boilerplate (LLM ack instead of answer)
            r"收到.*我会严格", r"我会.*遵守.*格式", r"有什么需要我做的",
            r"好的.*我会.*JSON", r"明白了.*我会",
            r"了解.*马上.*格式", r"按照.*格式.*回复",
            r"遵守.*JSON.*格式", r"收到[，,。!\s]*$",
            # Network / API errors
            r"(?:网络|接口|API|HTTP|连接).*(?:错误|超时|失败|异常)",
            r"(?:timeout|connection\s+(?:error|refused|reset)|500|503|429)",
            # Tool execution failures
            r"(?:tool|工具).*(?:fail|失败|error|错误|crash)",
            r"Maximum retries exceeded", r"Execution failed",
            # Empty search results
            r"(?:no|zero|0)\s+results?\s+(?:found|returned)",
            r"(?:未找到|没有找到|无).*(?:结果|数据|信息)",
            # Hallucination indicators
            r"I don't have (?:access to|the ability to)",
            r"As an AI(?:,| language model)? I (?:cannot|don't|can't)",
        ]
        combined = output + " " + " ".join(str(x) for x in exec_log)
        return any(re.search(p, combined, re.IGNORECASE) for p in patterns)

    score_card = state.get("score_card") or {}
    pmo_result = state.get("pmo_result") or {}
    department = state.get("department", "")
    workspace_id = state.get("workspace_id")  # Preserve workspace across node boundaries
    task_type = state.get("task_type", "")

    # ═══ Fast-lane: COMMAND_EXECUTION or LOCAL_SYSTEM — never audit, just check stdout ═══
    if task_type in ("COMMAND_EXECUTION", "LOCAL_SYSTEM"):
        final_output = str(state.get("final_output", ""))
        _el = state.get("execution_log", [])
        has_output = bool(final_output.strip()) and not _is_fail(final_output, _el)
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
        _el = state.get("execution_log", [])
        has_output = bool(final_output.strip()) and not _is_fail(final_output, _el)
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
            # Compliance boilerplate: LLM ack'd format instruction instead of answering
            r"收到.*我会严格",
            r"我会.*遵守.*格式",
            r"有什么需要我做的",
            r"好的.*我会.*JSON",
            r"明白了.*我会",
            r"了解.*马上.*格式",
r"^收到[，,。!\s]*$",
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
    
    # ═══ General short/non-code fast-lane: skip if no auditor score ═══
    # Covers queries that were routed to verify (audit skipped) but aren't
    # COMMAND/SIMPLE/DOCUMENT/LOCAL_SYSTEM. Applies to short factual queries,
    # simple lookups, and other non-code tasks.
    if not score_card.get("overall_score"):
        task = state.get("user_request", "").strip()
        if len(task) < 30:  # short queries
            code_kw = ["写", "开发", "实现", "修改", "修复", "bug", "代码", "code",
                       "函数", "接口", "api", "部署", "deploy", "数据库", "测试"]
            if not any(kw in task.lower() for kw in code_kw):
                final_output = str(state.get("final_output", ""))
                has_output = bool(final_output.strip())
                return {
                    "phase": "deliver",
                    "workspace_id": workspace_id,
                    "score_card": {"score": 90 if has_output else 0,
                                  "decision": "APPROVE" if has_output else "FAIL",
                                  "final_score": 90 if has_output else 0,
                                  "next_action": "deliver",
                                  "auditor_verdict": "SKIPPED",
                                  "pmo_verdict": "SKIPPED"},
                    "execution_log": ["[CEO-AGGREGATE] Short query -> skip audit, direct deliver"],
                }
    

    # ═══ V5 Verifier Integration: use new verifier for non-fast-lane tasks ═══
    v5_intent = _LEGACY_TO_V5_INTENT.get(task_type, task_type.upper())
    final_output_v5 = str(state.get("final_output", ""))
    exec_log_v5 = state.get("execution_log", [])
    try:
        v5_score_card = _v5_verify_aggregate(v5_intent, final_output_v5, exec_log_v5)
        if not v5_score_card.get("needs_audit", True) and not score_card.get("overall_score"):
            return {
                "phase": "deliver",
                "workspace_id": workspace_id,
                "score_card": {
                    "score": v5_score_card.get("score", 80),
                    "decision": v5_score_card.get("decision", "APPROVE"),
                    "final_score": v5_score_card.get("score", 80),
                    "next_action": "deliver",
                    "auditor_verdict": "SKIPPED",
                    "pmo_verdict": "SKIPPED",
                    "v5_verifier": v5_score_card,
                },
                "execution_log": [f"[CEO-AGGREGATE] V5 verifier: {v5_intent} -> {v5_score_card.get('decision')} (audit skipped)"],
            }
        if v5_intent == "CODING" and v5_score_card.get("score", 0) < 60:
            return {
                "phase": "deliver",
                "workspace_id": workspace_id,
                "score_card": {
                    "score": v5_score_card.get("score", 0),
                    "decision": "FAIL",
                    "final_score": v5_score_card.get("score", 0),
                    "next_action": "deliver",
                    "auditor_verdict": "SKIPPED",
                    "pmo_verdict": "SKIPPED",
                    "v5_verifier": v5_score_card,
                },
                "execution_log": [f"[CEO-AGGREGATE] V5 verifier: CODING task scored {v5_score_card.get('score')} — fast-fail"],
            }
    except Exception:
        pass  # Verifier unavailable — fall through to existing auditor logic
    
    auditor_score = score_card.get("overall_score", 60)
    auditor_verdict = score_card.get("verdict", "APPROVE")
    pmo_score = pmo_result.get("compliance_score", 70)
    pmo_verdict = pmo_result.get("verdict", "PASS")
    
    # ═══ PMO score sanity check: detect LLM output contradiction ═══
    # Sometimes LLM returns positive criteria_failed text but a low score.
    # If all "failed" items read like positive feedback, override the score.
    _pmo_failed = pmo_result.get("criteria_failed", [])
    if _pmo_failed and pmo_score < 60:
        _positive_kw = ["能", "无", "通过", "正确", "包含", "完整", "清晰",
                        "良好", "合理", "安全", "可用", "有效", "满足",
                        "pass", "good", "valid", "correct", "works", "ok"]
        _all_positive = all(
            any(kw in str(item).lower() for kw in _positive_kw)
            for item in _pmo_failed
        )
        if _all_positive:
            import logging
            logging.getLogger("ai_company").warning(
                "PMO score contradiction: score=%s but criteria_failed=%s → overriding to 75",
                pmo_score, _pmo_failed,
            )
            pmo_score = 75
            pmo_verdict = "PASS"
    
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
    
    # ── Token + memory stats for aggregate report ──
    try:
        from src.utils.token_tracker import get_token_tracker
        tracker = get_token_tracker()
        token_stats = tracker.get_stats()
        token_line = ""
        if token_stats.get("total_calls", 0) > 0:
            token_line = (
                f" | Tokens: {token_stats['total_tokens']:,} total "
                f"({token_stats['total_calls']} calls, "
                f"~${token_stats.get('estimated_cost_usd', 0):.4f})"
            )
    except Exception:
        token_line = ""
    
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
            f"[CEO-AGGREGATE] Auditor({auditor_score}) + PMO({pmo_score}) -> Final={final_score}/{config.gate_final_score} -> {decision}{token_line}",
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
    
    # ── Auto Skill Discovery: capture complex successful workflows ──
    if score and int(score or 0) >= 70:
        try:
            from src.learning.auto_discovery import auto_discovery
            from src.learning import skill_library
            ws = TaskContext.load(workspace_id) if workspace_id else None
            tool_calls_data = []
            if ws:
                context_data = ws._read("context.md", "")
                tool_calls_data = _parse_tools_from_timeline(context_data)
            # Also try from execution log
            if not tool_calls_data:
                exec_log = state.get("execution_log", [])
                for entry in exec_log:
                    if isinstance(entry, str) and "Tools:" in entry:
                        tools_str = entry.split("Tools:")[-1].strip()
                        for t in tools_str.split(","):
                            t = t.strip()
                            if t:
                                tool_calls_data.append({"tool": t, "success": True})
            
            if tool_calls_data and len(tool_calls_data) >= 3:
                # Auto-discover: filter → extract → save
                auto_discovery.discover(
                    task=state.get("user_request", ""),
                    tool_calls=tool_calls_data,
                    score=int(score or 0),
                    department=str(dept),
                )
            elif tool_calls_data:
                # Fallback: old capture for simpler tasks
                caps_data = _parse_capabilities_from_timeline(context_data) if ws and context_data else []
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
    
    # ── Timing report ──
    try:
        from src.utils.timing import timer
        timing_report = timer.get_summary()
        if timing_report:
            dept_output = str(dept_output) + timing_report
    except Exception:
        pass

    # ── Token usage stats ──
    try:
        from src.utils.token_tracker import get_token_tracker
        token_tracker = get_token_tracker()
        token_summary = token_tracker.get_summary_text()
        if token_summary:
            dept_output = str(dept_output) + "\n\n" + token_summary
        # Also log per-request token stats
        stats = token_tracker.get_stats()
        if stats.get("total_calls", 0) > 0:
            import logging
            logger = logging.getLogger("ai_company.token")
            logger.info(
                "Request token report: %d calls, %d total tokens, ~$%.4f, "
                "prompt=%d completion=%d",
                stats["total_calls"],
                stats["total_tokens"],
                stats.get("estimated_cost_usd", 0),
                stats["total_prompt_tokens"],
                stats["total_completion_tokens"],
            )
    except Exception:
        pass

    # ── Memory usage report ──
    try:
        from src.utils.token_budget import TokenBudget
        # Estimate memory context size for this request
        final_output = str(state.get("final_output", ""))
        exec_log_str = " ".join(str(x) for x in state.get("execution_log", []))
        messages_str = " ".join(
            str(m.content) if hasattr(m, "content") else str(m)
            for m in state.get("messages", [])
        )
        total_chars = len(final_output) + len(exec_log_str) + len(messages_str)
        budget_check = TokenBudget(max_total=16000)
        est_tokens = budget_check.estimate(messages_str)
        memory_line = (
            f"\n\n📊 Memory: ~{total_chars} chars output+log+msgs, "
            f"est. ~{est_tokens} prompt tokens "
            f"({min(100, round(est_tokens/16000*100))}% of 16K budget)"
        )
        dept_output = str(dept_output) + memory_line
    except Exception:
        pass
    
    # ── Self-improvement: capture task outcome ──
    try:
        from src.evolution.self_improve import get_improver
        imp = get_improver()
        execution_log = state.get("execution_log", [])
        for entry in execution_log:
            entry_str = str(entry)
            if "FAILED" in entry_str or "Crashed" in entry_str or "CRASHED" in entry_str:
                imp.capture_error(entry_str[:300], {
                    "task": state.get("user_request", "")[:200],
                    "department": state.get("department", "unknown"),
                })
        for entry in execution_log:
            if "Tool failures" in str(entry):
                m = re.search(r"(\d+)/(\d+)", str(entry))
                if m:
                    imp.capture_error(f"Tool failure ratio: {m.group(1)}/{m.group(2)}", {
                        "tool_calls": int(m.group(2)),
                        "tool_failures": int(m.group(1)),
                    })
        warning = imp.check_resources()
        if warning:
            dept_output = str(dept_output) + f"\n\n⚠️ {warning}"
    except Exception:
        pass
    
    # ── Self-optimization: spawn background fix if enabled ──
    try:
        from src.evolution.self_optimizer import get_optimizer
        opt = get_optimizer()
        timing_data = None
        try:
            from src.utils.timing import timer
            if timer._records:
                timing_data = timer._records
        except ImportError:
            pass
        opt.on_task_complete(
            task=state.get("user_request", ""),
            execution_log=state.get("execution_log", []),
            timing_data=timing_data,
        )
    except Exception:
        pass
    
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

    # ═══ Short simple tasks skip audit ═══
    # If AUDIT_ENABLED is False, skip auditor/pmo for ALL tasks
    if not AUDIT_ENABLED:
        return "verify"
    
    task = state.get("user_request", "").strip()
    if len(task) < 20:
        code_keywords = ["写", "开发", "实现", "修改", "修复", "bug", "代码", "code",
                         "函数", "接口", "api", "部署", "deploy", "数据库"]
        if not any(kw in task.lower() for kw in code_keywords):
            return "verify"  # skip audit for short non-code queries
    
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


# ═══ Session Memory Context Injection ═══

def _inject_session_context(state: dict) -> None:
    """Load session history + Hermes memory and inject into state.
    
    Injects in Hermes format:
    1. USER PROFILE — compact declarative facts
    2. MEMORY — durable facts with [N%] budget indicator
    3. Recent conversation turns (last 5, compact)
    
    Uses TokenBudget to prevent context overflow, and Compressor
    to summarise long conversation histories.
    """
    import logging
    logger = logging.getLogger("ai_company.memory")

    # ── Hermes Memory (User Profile + Durable Facts) ──
    hermes_ctx = ""
    user_request = state.get("user_request", "")
    try:
        from src.memory.hermes import hermes_memory

        # ── Smart memory injection: semantic search for top 5 relevant ──
        if user_request.strip():
            try:
                relevant = hermes_memory.semantic_search(user_request, top_k=5)
                if relevant:
                    lines = ["## MEMORY (语义检索 Top 5)"]
                    for e in relevant:
                        score_pct = int(e.get("score", 0) * 100)
                        lines.append(f"- [{score_pct}%] {e['text']}")
                    hermes_ctx = "\n".join(lines)
                    logger.debug("Semantic memory: injected %d relevant facts",
                                 len(relevant))
            except Exception:
                pass

        # Fallback: full context if semantic search returned nothing
        if not hermes_ctx:
            hermes_ctx = hermes_memory.get_full_context() or ""
    except Exception:
        pass

    # ── Recent conversation turns (compact, last 5) ──
    history_text = ""
    history_raw = []
    try:
        from src.session import get_session_manager
        from src.session.memory import get_session_memory
        mgr = get_session_manager()
        if mgr.current:
            mem = get_session_memory(mgr.current.id)
            history_raw = mem.get_recent_conversations(5)
            if history_raw:
                turns = []
                for t in history_raw:
                    q = t.get("user", "")[:150]
                    a = t.get("assistant", "")[:150]
                    if q or a:
                        turns.append(f"Q: {q}\nA: {a}")
                if turns:
                    history_text = "## 最近对话 (Recent Conversation)\n" + "\n---\n".join(turns)
    except Exception:
        pass

    # ═══ Token Budget Control ═══
    # If hermes_ctx + history_text exceeds budget, trim intelligently
    if hermes_ctx or history_text:
        try:
            from src.utils.token_budget import budget_context, TokenBudget
            budget_report = None

            # ── Smart compression: if history > 1000 chars and budget tight ──
            if history_raw and len(history_text) > 1000:
                budget = TokenBudget(max_prompt=12000)
                profile_tokens = budget.estimate(hermes_ctx)
                hist_tokens = budget.estimate(history_text)
                if (profile_tokens + hist_tokens) > budget.max_prompt * 0.6:
                    # Budget is tight — compress old turns
                    try:
                        from src.utils.compressor import Compressor
                        compressor = Compressor(keep_recent=3, trigger_chars=1000)
                        history_text = compressor.compress_conversation(
                            history_raw, max_tokens=min(3000, budget.remaining())
                        )
                        logger.debug("Compressed conversation history (budget tight)")
                    except Exception:
                        pass

            # Apply token budget
            final_context, budget_report = budget_context(
                hermes_ctx, history_text, max_prompt=12000
            )

            if final_context.strip():
                from langchain_core.messages import SystemMessage
                state["messages"].insert(0, SystemMessage(content=final_context))

                mem_chars = len(final_context)
                usage_pct = budget_report.get("usage_pct", 0) if budget_report else 0
                status = budget_report.get("status", "OK") if budget_report else "OK"
                state["execution_log"] = [
                    f"[MEMORY] Context loaded: {mem_chars} chars, "
                    f"budget={usage_pct}% ({status})"
                ]
                if status != "OK":
                    logger.warning("Token budget %s: %.1f%% used (%d/%d)",
                                   status, usage_pct,
                                   budget_report.get("consumed_prompt", 0),
                                   budget_report.get("max_prompt", 12000))
        except ImportError:
            # Fallback: original behavior without budget control
            context_parts = []
            if hermes_ctx.strip():
                context_parts.append(hermes_ctx)
            if history_text.strip():
                context_parts.append(history_text)
            if context_parts:
                full_context = "\n\n".join(context_parts)
                from langchain_core.messages import SystemMessage
                state["messages"].insert(0, SystemMessage(content=full_context))
                state["execution_log"] = [f"[MEMORY] Loaded context ({sum(len(p) for p in context_parts)} chars)"]


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
    
    # ═══ Inject session memory context ═══
    _inject_session_context(initial_state)
    
    config_params = {
        "configurable": {
            "thread_id": f"ceo_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        }
    }
    
    final_state = await app.ainvoke(initial_state, config_params)
    return final_state
