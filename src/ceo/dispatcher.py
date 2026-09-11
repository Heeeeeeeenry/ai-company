"""
Thin CEO Dispatcher — 薄CEO调度中心

一人公司架构:
  CEO只做两件事: (1) 匹配角色 (2) 看结果
  干不了的事通过 escalation 回路升级回来, CEO带上下文重新分派。

流程:
  用户请求 → quick_triage(日期/时间/数学/问候→零延迟回复)
           → match_role(两阶段域匹配)
           → [复杂任务] PM规划 → dispatch
           → [简单任务] 直接 dispatch
  
  dispatch → DepartmentAgent.execute()
           → ✅ 成功 → 交付结果
           → 🆘 escalation → re-match(带失败上下文, 最多2轮)
           → 2轮耗尽 → 兜底给 developer
"""

import json
import time
import logging
import random
import re
import subprocess
from typing import Optional, TypedDict
from datetime import datetime, timedelta

from langchain_core.messages import HumanMessage

from src.departments.roles import role_registry
from src.departments.agents import dispatch_to_department

logger = logging.getLogger("ai_company.dispatcher")


class UseLegacyGraph(Exception):
    """Signal that this request should use the legacy LangGraph path."""


# ─── Local date helpers ─────────────────────────────

def _try_system_lunar(date_obj: datetime) -> Optional[str]:
    """Best-effort local lunar lookup. Returns None when not trusted."""
    try:
        subprocess.run(["/usr/bin/calendar", "-A", "7", "-B", "0"], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    return None


# ─── Quick Triage ───────────────────────────────────

def _try_quick_answer(user_message: str) -> Optional[str]:
    """Try to answer without dispatching to any role.

    Handles: greetings, date/day, time, math, jokes — all zero-latency.
    Returns answer string if handled, None if needs role dispatch.
    """
    msg = user_message.strip()
    msg_lower = msg.lower()

    # ── Greetings / identity ──
    if any(g in msg_lower for g in ["你好", "嗨", "哈喽", "hello", "hi", "hey"]):
        return "你好！我是 AI 公司的 CEO。有什么可以帮你的？"
    if any(q in msg_lower for q in ["你是谁", "你能做什么", "你有什么能力", "你会什么"]):
        return (
            "我是 AI 公司的 CEO，团队里有开发、测试、运维、研究员、市场运营等角色。\n"
            "告诉我你需要什么，我来派最合适的人处理。"
        )
    if any(q in msg_lower for q in ["谢谢", "感谢", "thanks"]):
        return "不客气！有需要随时找我。"

    # Very short messages
    if len(msg) <= 2:
        return "有什么我可以帮你的吗？"

    # ── Date / Day ──
    now = datetime.now()
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    today_wd = weekday_cn[now.weekday()]
    today_dt = now.strftime("%Y年%m月%d日")

    if any(p in msg_lower for p in [
        "今天周几", "今天星期几", "今天几号", "今天日期",
        "今天是什么日子", "今天周", "今天星期", "what day",
    ]):
        return f"今天是 **{today_wd}**（{today_dt}）。"
    if any(p in msg_lower for p in ["现在几点", "几点了", "当前时间", "what time"]):
        return f"现在是 **{now.strftime('%H:%M')}**（{today_dt} {today_wd}）。"
    if any(p in msg_lower for p in ["明天周几", "明天星期几", "明天是周几"]):
        t = now + timedelta(days=1)
        return f"明天是 **{weekday_cn[t.weekday()]}**（{t.strftime('%m月%d日')}）。"
    if any(p in msg_lower for p in ["昨天周几", "昨天星期几", "昨天是周几"]):
        t = now - timedelta(days=1)
        return f"昨天是 **{weekday_cn[t.weekday()]}**（{t.strftime('%m月%d日')}）。"
    if any(p in msg_lower for p in ["后天周几", "后天星期几", "后天是周几"]):
        t = now + timedelta(days=2)
        return f"后天是 **{weekday_cn[t.weekday()]}**（{t.strftime('%m月%d日')}）。"

    # ── Lunar queries: local-only fast path, never dispatch to LLM/search first ──
    if any(k in msg for k in ("农历", "阴历")):
        for label, days in [("今天", 0), ("明天", 1), ("后天", 2), ("昨天", -1)]:
            if label in msg:
                target = now + timedelta(days=days)
                lunar = _try_system_lunar(target)
                if lunar:
                    return f"{label}是公历{target.strftime('%Y年%m月%d日')}，对应{lunar}。"
                return (
                    f"{label}是公历{target.strftime('%Y年%m月%d日')}。"
                    "当前本机环境没有可用的本地农历数据源，所以我先不给你瞎报阴历，"
                    "也不会再走 researcher 搜索给你残句。"
                )

    # ── Memory/history lookup: legacy graph has the direct memory_mode path ──
    if any(k in msg for k in ("最近", "罗列", "列出", "回顾", "总结", "说说", "聊聊")) and any(
        k in msg for k in ("对话", "聊天", "记忆", "历史", "之前", "刚才")
    ):
        raise UseLegacyGraph("memory lookup requires legacy memory_mode")

    # ── "是什么日子" — basic date info for today/yesterday ──
    if any(p in msg_lower for p in ["是什么日子", "什么日子", "啥日子"]):
        if "昨天" in msg_lower:
            t = now - timedelta(days=1)
            return f"昨天是 **{weekday_cn[t.weekday()]}**，{t.strftime('%Y年%m月%d日')}。"
        if "明天" in msg_lower:
            t = now + timedelta(days=1)
            return f"明天是 **{weekday_cn[t.weekday()]}**，{t.strftime('%m月%d日')}。"
        return f"今天是 **{today_wd}**，{today_dt}。\n想知道具体节日或农历信息的话，我可以帮你搜一下。"

    # ── Simple math ──
    math_m = re.match(
        r"^(?:算一下?|计算)?\s*(\d+)\s*([+\-*/×÷])\s*(\d+)\s*(?:等于几|等于多少|是多少|=?\?)?$",
        msg_lower,
    )
    if math_m:
        a, op, b = int(math_m.group(1)), math_m.group(2), int(math_m.group(3))
        ops = {"+": a + b, "-": a - b, "*": a * b, "×": a * b,
               "/": a / b if b else "除数不能为0",
               "÷": a / b if b else "除数不能为0"}
        result = ops.get(op, "?")
        if isinstance(result, float) and result == int(result):
            result = int(result)
        return f"{a} {op} {b} = **{result}**"

    # ── Jokes ──
    if any(p in msg_lower for p in ["讲个笑话", "笑话", "来段幽默"]):
        jokes = [
            "程序员最怕什么？—— 需求变更 😄",
            "为什么程序员分不清万圣节和圣诞节？—— 因为 Oct 31 == Dec 25！",
            "一个 SQL 语句走进酒吧，看见两张桌子，问：我能 JOIN 你们吗？",
        ]
        return random.choice(jokes)

    # No quick answer → needs role dispatch
    return None


# ─── Task complexity heuristics ─────────────────────

def _is_complex_task(user_message: str) -> bool:
    """Does this task need PM planning before dispatch?"""
    msg = user_message.lower()
    signals = [
        "从零", "从0", "搭建", "架构设计", "系统设计", "重新设计",
        "整体评估", "全面重构", "跨部门", "多模块",
        "新项目", "新系统", "平台", "from scratch",
        "帮我规划", "帮我设计架构", "全栈",
    ]
    return any(s in msg for s in signals)


# ─── Escalation Protocol ────────────────────────────

class DispatchResult(TypedDict):
    success: bool
    output: str
    role: str
    role_display: str
    escalation: bool
    escalation_reason: str
    escalation_tried: list
    escalation_round: int
    mode: str  # "direct" | "planned" | "escalated" | "fallback"


def _detect_escalation(output: str) -> bool:
    """Check if output signals inability to handle the task."""
    if not output:
        return True
    low = output.lower()
    signals = [
        "搞不定", "无法完成", "做不到", "超出了我的能力",
        "我不擅长", "这不在我的", "建议转给", "更适合",
        "unable to", "cannot", "beyond my", "not capable",
        "escalat", "建议重新分派", "换个角色",
    ]
    return any(s in low for s in signals)


def _parse_escalation(output: str) -> str:
    """Extract short reason from escalation output."""
    for line in output.split("\n"):
        line = line.strip()
        if any(kw in line.lower() for kw in ["原因", "reason", "搞不定", "无法"]):
            return line[:200]
    return output[:200]


async def _do_dispatch(
    role_name: str,
    task: str,
    context: str = "",
    escalation_round: int = 0,
) -> DispatchResult:
    """Dispatch task to a role. Cleans leaked tool-call JSON from output."""
    result = await dispatch_to_department(
        department=role_name,
        task=task,
        context=context,
    )
    output = result.get("output", "") if result.get("success") else result.get("error", "")

    # Clean leaked tool-call JSON (ReAct loop didn't finish → treat as failure)
    if isinstance(output, str) and output.strip().startswith('{"action":"tool"'):
        output = (
            f"[执行中断] 角色 {role_name} 未完成执行。"
            f"最后操作: {output[:200]}"
        )
        result["success"] = False

    return {
        "success": result.get("success", False),
        "output": output,
        "role": result.get("role", role_name),
        "role_display": result.get("role_display", ""),
        "escalation": False,
        "escalation_reason": "",
        "escalation_tried": [],
        "escalation_round": escalation_round,
        "mode": "direct" if not context else "planned",
    }


async def _escalation_loop(
    task: str,
    initial_role: str,
    initial_context: str = "",
    max_rounds: int = 2,
) -> DispatchResult:
    """Dispatch with escalation: re-match on failure, max N rounds."""
    last_result = None
    escalation_ctx = []

    for round_num in range(max_rounds + 1):
        if round_num == 0:
            role_name, context = initial_role, initial_context
        else:
            logger.info(f"Escalation r{round_num}: re-matching")
            enriched = f"{task}\n\n[上下文: 之前的角色搞不定]\n" + "\n".join(escalation_ctx)
            matches = role_registry.match(enriched, max_results=3)

            prev_roles = {last_result["role"]} if last_result else set()
            chosen = None
            for role, score in matches:
                if role.name not in prev_roles:
                    chosen = role
                    break
            if not chosen and matches:
                chosen = matches[0][0]
            role_name = chosen.name if chosen else "developer"
            context = (
                f"[ESCALATION]\n原因: {last_result.get('escalation_reason', '?')}\n"
                + "\n".join(escalation_ctx)
            )

        last_result = await _do_dispatch(role_name, task, context, round_num)

        if last_result["success"]:
            last_result["mode"] = "direct" if round_num == 0 else "escalated"
            return last_result

        output = last_result.get("output", "")
        if _detect_escalation(output):
            escalation_ctx.append(f"- R{round_num}: {_parse_escalation(output)}")
            last_result["escalation"] = True
            last_result["escalation_reason"] = _parse_escalation(output)
            continue
        elif round_num < max_rounds:
            escalation_ctx.append(f"- R{round_num}: {role_name} failed [{output[:150]}]")
            last_result["escalation"] = True
            last_result["escalation_reason"] = output[:200]
            continue
        else:
            break

    # Exhausted → fallback
    logger.warning(f"Escalation exhausted after {max_rounds} rounds → fallback dev")
    fb = await _do_dispatch(
        "developer",
        f"{task}\n\n[兜底: 经过{max_rounds}轮重试]\n" + "\n".join(escalation_ctx),
        "",
        max_rounds + 1,
    )
    fb["mode"] = "fallback"
    return fb


# ─── PM Planning (complex tasks only) ───────────────

async def _plan_complex_task(task: str) -> dict:
    """Quick PM plan for complex tasks. Returns planning context."""
    from src.llm_factory import get_llm, extract_json

    llm = get_llm("pm")
    prompt = f"""你是PM。需求:
{task}

输出简洁JSON计划:
{{"summary":"一句话","features":["f1","f2"],"acceptance_criteria":["c1"],"suggested_role":"角色名"}}
直接输出JSON, 别啰嗦。"""

    try:
        result = await llm.ainvoke([HumanMessage(content=prompt)])
        return extract_json(str(result.content))
    except Exception:
        logger.warning("PM planning failed", exc_info=True)
        return {"summary": task[:100], "features": [], "acceptance_criteria": []}


# ─── Main Entry Point ───────────────────────────────

async def run_ceo(user_message: str) -> dict:
    """薄CEO: 匹配角色 → 分派执行 → 看结果。

    三步: quick_triage → match_role → dispatch (带 escalation 回路)
    """
    t0 = time.time()
    task = user_message.strip()

    # ── Step 1: Quick triage (zero-latency for dates/math/greetings) ──
    quick = _try_quick_answer(task)
    if quick is not None:
        return _make_result(quick, "ceo", "CEO", "quick_reply",
                           f"Quick reply in {time.time() - t0:.0f}s")

    # ── Step 2: Role matching ──
    matches = role_registry.match(task, max_results=3)
    if not matches:
        role_name, match_method = "developer", "fallback-to-dev"
    else:
        role_name, best_score = matches[0][0].name, matches[0][1]
        match_method = f"match({best_score:.2f})"

    logger.info("CEO → %s (%s) | %s", role_name, match_method, task[:80])

    # ── Step 3: Complex? Plan first ──
    mode, context = "direct", ""
    if _is_complex_task(task) and role_name in ("developer", "qa"):
        try:
            plan = await _plan_complex_task(task)
            context = (
                f"[PM Plan]\nSummary: {plan.get('summary', task)}\n"
                f"Features: {', '.join(plan.get('features', []))}\n"
                f"Criteria: {', '.join(plan.get('acceptance_criteria', []))}"
            )
            if plan.get("suggested_role"):
                role_name = plan["suggested_role"]
            mode = "planned"
        except Exception:
            logger.warning("Planning failed → direct", exc_info=True)

    # ── Step 4: Dispatch with escalation ──
    result = await _escalation_loop(task, role_name, context)

    elapsed = time.time() - t0
    logger.info("CEO done: %s/%s in %.1fs", result["role"], result["mode"], elapsed)

    return _make_result(
        result["output"],
        result["role"],
        result["role_display"],
        result["mode"],
        f"[CEO] Matched: {role_name} ({match_method}) | "
        f"Mode: {mode}, Rounds: {result['escalation_round']} | "
        f"{'SUCCESS' if result['success'] else 'FAILED'} in {elapsed:.1f}s",
    )


def _make_result(output: str, role: str, role_display: str,
                 mode: str, log: str) -> dict:
    """Build standard result dict with compatibility fields."""
    return {
        "success": True,
        "output": output,
        "role": role,
        "role_display": role_display,
        "mode": mode,
        "error": None,
        "final_output": output,
        "phase": "deliver",
        "score_card": {"score": 100, "decision": mode},
        "department": role,
        "execution_log": [log],
    }
