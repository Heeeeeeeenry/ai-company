# -*- coding: utf-8 -*-
"""AI 受控取数编排层（衡水定制）。

职责：把「用户问了一句需要看数据的话」变成「先取数、再基于取到的数成文」，
并且保证成文里的数字**全部可溯源**。

三段式：

    ① 选工具   —— 让模型只做一件事：从宿主给的工具目录里挑一个工具 + 参数
                  （JSON 输出，复用 llm_factory.extract_json；不用 native
                  tool-calling，网关对它的支持不稳）
    ② 执行取数 —— 通过 src.host_tools 调宿主受控接口，拿到 JSON
    ③ 成文     —— 把工具结果**原样注入**提示词，并附「可用数字清单」，
                  要求口径与数字逐字照抄；成文后再跑一遍 verify_numbers，
                  有出处的数字才算合格，没有的单独报出来。

设计取舍：

* 不做 ReAct/多轮自由发挥，固定「取数 → 成文」两跳，行为可预测、可审计。
* 工具返回的原始 JSON 是唯一事实来源（single source of truth），模型只负责
  措辞，不负责算数。
* 工具目录为空（宿主没配 token / 不可达）时**不报错**，返回 unusable 的
  trace，让上层退回普通对话 —— 功能缺失不该表现为对话报错。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from src import host_tools
from src.llm_factory import extract_json, get_llm

logger = logging.getLogger("ai_company.tools")

# 需要「先看数据再回答」的意图词。没有命中就走普通对话，省一次模型调用。
ORCHESTRATE_KEYWORDS = re.compile(
    r"统计|汇总|多少|几件|几封|数量|总数|趋势|变化|对比|占比|比例|排名|最多|最少"
    r"|分析|报表|概览|情况如何|导出|下载|列表|清单|查询|查一下|查查|看看.*(件|量)"
    r"|同比|环比|按月|按单位|按状态|按渠道|本月|上月|今年|去年|季度"
)

MAX_STEPS = 4
ENVELOPE_BUDGET = 4000      # 单个工具结果进提示词的长度上限
DATA_BUDGET = 14000         # 全部工具结果进提示词的长度上限
ANSWER_MAX_TOKENS = 6000

NUM_RE = re.compile(r"\d+(?:\.\d+)?")

ANSWER_SYSTEM_PROMPT = """你是「衡水市民意智感中心」管理端的 AI 数据分析助手。

用户问的是他**有权限看到**的信访/信件数据。系统已经把查询结果放在下面 ——
这就是你唯一的数据来源，你看不到任何别的数据，也没有数据库。

铁律（违反就是事故）：

1. 只允许使用【查询结果】里出现过的数字，必须**逐字照抄**。禁止心算、
   禁止推算、禁止四舍五入改变数值、禁止把「大约/约」加在数字上。
2. 如果查询结果里没有能回答问题的数据，就直接说没有查到，并说明可能原因
   （例如时间范围内无记录）。**不许编造任何数字或示例数据**。
3. 回答完必须附一段「数据口径」，逐条写清：
   - 可见范围（用查询结果里 scope 的原话）
   - 时间范围
   - 状态/条件的定义
   - 样本量（本次统计覆盖多少条记录）
4. 数字类结论用列表或表格逐条列出，每条只讲一个事实。
5. 用业务语言，不要提工具名、接口、SQL、JSON 字段名等实现细节。
6. 如果【可用数字清单】给了数字，你写下的每个数字都应当能在其中找到。

【查询结果】
{data}

【可用数字清单】
{numbers}
"""

PLAN_SYSTEM_PROMPT = """你是数据查询规划器。你唯一的工作是：从工具目录里挑一个工具并给出参数。

规则：

1. 只能输出 JSON，不要解释，不要 markdown 围栏。
2. 输出格式二选一：
   {{"tool": "<工具名>", "args": {{...}}}}
   {{"tool": "none", "reason": "<为什么不需要再查>"}}
3. 工具名必须来自目录，参数必须是目录里声明过的字段，不要自己发明字段。
4. 一次只挑一个工具。如果上一轮结果已经够回答问题，输出 "none"。
5. 时间参数用 YYYY-MM-DD 格式。用户说「本月/上月/今年」时按当前日期 {today} 换算。

【工具目录】
{catalog}

【当前用户可见范围（whoami_scope）】
{scope}
"""


@dataclass
class ToolCall:
    name: str
    args: dict
    envelope: dict = field(default_factory=dict)
    error: str = ""


@dataclass
class ToolTrace:
    """一次取数回合的全部痕迹（成文 + 审计都靠它）。"""

    calls: list[ToolCall] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    scope: dict = field(default_factory=dict)
    data_text: str = ""
    numbers: set[str] = field(default_factory=set)
    usable: bool = False

    @property
    def used_tools(self) -> list[str]:
        return [c.name for c in self.calls if not c.error]


def should_orchestrate(message: str, recent: Sequence[str] = ()) -> bool:
    """粗筛：这句像不像要看数据的话。宁少勿滥 —— 普通闲聊不该多等一次模型。"""
    if not host_tools.enabled():
        return False
    text = (message or "").strip()
    if not text:
        return False
    if ORCHESTRATE_KEYWORDS.search(text):
        return True
    # 上一轮刚取过数，接着问「那按单位呢」这种指代也要能续上。
    return any("[已取数]" in (r or "") for r in list(recent)[-2:])


# ── 数字溯源 ────────────────────────────────────────────────────────

def _norm_number(raw: str) -> str:
    text = str(raw or "").strip()
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _numbers_in(text: str) -> set[str]:
    return {_norm_number(n) for n in NUM_RE.findall(text or "")}


def collect_numbers(data_text: str, extra: str = "") -> set[str]:
    """数据里出现过的数字集合 = 允许模型写出的数字。"""
    return _numbers_in(data_text) | _numbers_in(extra)


def verify_numbers(answer: str, allowed: Iterable[str]) -> list[str]:
    """返回成文里「找不到出处」的数字（保持出现顺序，最多 20 个）。"""
    allow = {_norm_number(a) for a in allowed}
    bad: list[str] = []
    for raw in NUM_RE.findall(answer or ""):
        norm = _norm_number(raw)
        if norm in allow:
            continue
        if norm not in bad:
            bad.append(norm)
        if len(bad) >= 20:
            break
    return bad


# ── 编排 ────────────────────────────────────────────────────────────

def _compact_catalog(tools: list[dict]) -> str:
    lines = []
    for t in tools:
        params = t.get("params") or {}
        props = params.get("properties") or {}
        required = set(params.get("required") or [])
        fields = []
        for name, spec in props.items():
            kind = (spec or {}).get("type", "string")
            flag = "必填" if name in required else "可选"
            desc = (spec or {}).get("description") or ""
            fields.append(f"{name}:{kind}({flag}){(' ' + desc) if desc else ''}")
        lines.append(f"- {t.get('name')}: {t.get('description') or ''}\n  参数: " + ("; ".join(fields) or "无"))
    return "\n".join(lines)


def analysis_llm():
    """成文用的 LLM：ceo 角色配置 + 放大 max_tokens（默认 2048 写不完报表）。"""
    llm = get_llm("ceo")
    try:
        return llm.model_copy(update={"max_tokens": ANSWER_MAX_TOKENS})
    except Exception:  # noqa: BLE001 - 非 pydantic 实现时退回 bind
        try:
            return llm.bind(max_tokens=ANSWER_MAX_TOKENS)
        except Exception:  # noqa: BLE001
            return llm


async def _plan_once(messages: list, catalog_names: set[str]) -> dict | None:
    from datetime import date

    llm = get_llm("ceo")
    try:
        raw = await llm.ainvoke(messages)
    except Exception as exc:  # noqa: BLE001 - 规划失败不该拖垮整轮对话
        logger.warning("工具规划失败: %s", exc)
        return None
    text = getattr(raw, "content", "") or ""
    if isinstance(text, list):  # 多模态 content 分片
        text = "".join(str(p.get("text", "")) for p in text if isinstance(p, dict))
    try:
        data = extract_json(text)
    except ValueError:
        logger.info("规划器没有输出 JSON，按无需取数处理")
        return None
    if not isinstance(data, dict):
        return None
    name = str(data.get("tool") or "").strip()
    if not name or name == "none" or name not in catalog_names:
        return None
    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    return {"tool": name, "args": args}


async def plan_and_fetch(message: str, user_id: str,
                         recent: Sequence[str] = ()) -> ToolTrace:
    """取数回合主流程。任何一步失败都优雅降级（返回 usable=False 的 trace）。"""
    trace = ToolTrace()
    if not host_tools.enabled():
        return trace

    tools = host_tools.list_tools()
    if not tools:
        return trace

    catalog_names = {str(t.get("name")) for t in tools if t.get("name")}

    # 0) 先钉住「可见范围」——它决定口径，也决定模型能不能诚实回答。
    try:
        scope_env = host_tools.run_tool(user_id, "whoami_scope", {})
        trace.scope = scope_env.get("scope") or {}
        trace.calls.append(ToolCall(name="whoami_scope", args={}, envelope=scope_env))
    except host_tools.HostToolsError as exc:
        logger.warning("whoami_scope 失败，放弃取数: %s", exc)
        return trace

    from datetime import date

    system = PLAN_SYSTEM_PROMPT.format(
        today=date.today().isoformat(),
        catalog=_compact_catalog(tools),
        scope=json.dumps(trace.scope, ensure_ascii=False),
    )
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for turn in list(recent)[-4:]:
        if turn:
            messages.append({"role": "user", "content": str(turn)[:800]})
    messages.append({"role": "user", "content": message})

    for _ in range(MAX_STEPS):
        plan = await _plan_once(messages, catalog_names)
        if not plan:
            break
        name, args = plan["tool"], plan["args"]
        call = ToolCall(name=name, args=args)
        try:
            call.envelope = host_tools.run_tool(user_id, name, args)
        except host_tools.HostToolsError as exc:
            # 参数错/越权都归到这里：把错误回灌给规划器，让它换参数或收手。
            call.error = str(exc)
            messages.append({"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)})
            messages.append({"role": "user",
                             "content": f"工具 {name} 调用失败：{exc}。请修正参数，或输出 {{\"tool\":\"none\"}}。"})
            trace.calls.append(call)
            continue
        trace.calls.append(call)
        payload = json.dumps(call.envelope, ensure_ascii=False, default=str)
        messages.append({"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)})
        messages.append({"role": "user", "content": f"工具 {name} 返回：{payload[:ENVELOPE_BUDGET]}"})

    # 打包成文用的数据快照（含失败的调用，成文时要知道「有工具没跑成」）。
    parts: list[str] = []
    for call in trace.calls:
        if call.error:
            parts.append(json.dumps({"tool": call.name, "args": call.args,
                                     "error": call.error}, ensure_ascii=False))
            continue
        parts.append(json.dumps(call.envelope, ensure_ascii=False, default=str))
        data = call.envelope.get("data")
        if isinstance(data, dict) and isinstance(data.get("download"), dict):
            dl = data["download"]
            if dl.get("url"):
                trace.files.append({"url": dl["url"],
                                    "name": dl.get("filename") or "导出文件.csv"})

    trace.data_text = "\n".join(parts)[:DATA_BUDGET]
    trace.numbers = collect_numbers(trace.data_text, extra=message)
    # 只有真的取到数据（至少一个成功的非 whoami 工具）才算可用。
    trace.usable = any(not c.error and c.name != "whoami_scope" for c in trace.calls)
    return trace


def build_answer_messages(trace: ToolTrace, message: str,
                          recent: Sequence[str] = ()) -> list[dict[str, Any]]:
    """成文提示词：数据原样注入 + 数字白名单。"""
    numbers = "、".join(sorted(trace.numbers, key=lambda s: (len(s), s))) or "（无）"
    system = ANSWER_SYSTEM_PROMPT.format(data=trace.data_text or "（空）", numbers=numbers)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for turn in list(recent)[-4:]:
        if turn:
            messages.append({"role": "user", "content": str(turn)[:800]})
    messages.append({"role": "user", "content": message})
    return messages
