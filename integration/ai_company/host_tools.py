"""宿主侧受控取数工具层（hsmyzgzx 定制）。

这是一张**白名单工具表** + 一个**执行器**，只做一件事：把「用户可观测的
信件数据」以受控方式交给 AI 编排层。铁律（见 docs/ai-data-analysis-design.md）：

1. 每个工具第一件事就是拿 ``user`` 走 ``permission_center.get_context(user)``；
   本层内不允许出现「不带可见性 WHERE 的 letters 查询」。
2. 全部复用宿主既有只读 service 函数，不新开业务口径；唯一新写 SQL 的是
   ``letter_trend``，且**强制拼 ``build_visibility_where(user)``**，不自行加过滤。
3. 不返回任何可调用对象，也不把数据库连接/密钥外泄给调用方。

公开 API（名字严格固定，供 ai-company 编排层照此调用）：

    ToolError(Exception)                      参数错/未知工具/无权限统一抛它
    list_tools() -> list[dict]                工具目录（name/description/params schema）
    run_tool(user, name, args) -> dict        执行工具，返回信封
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import time
from pathlib import Path
from typing import Any, Callable

from django.conf import settings

from api.common.db import execute_select_all, get_env_value
from api.common.permission_center import PermissionDenied, get_context
from api.common.unit_permissions import build_unit_full_name, query_all_units
from api.modules.config.service import (
    get_categories_payload,
    get_letter_attributes_payload,
    get_letter_channels_payload,
    get_rating_tags_payload,
)
from api.modules.letter.service import (
    build_created_at_range_where,
    build_visibility_where,
    compose_where,
    export_letters_csv,
    get_letter,
    get_letter_list,
    get_letter_status_counts,
)

# ─── 工具异常 ────────────────────────────────────────────────────────────


class ToolError(Exception):
    """工具层统一异常：参数错 / 未知工具 / 无权限，message 为人类可读中文。"""


# ─── 状态枚举（与 config/service.py 的 LETTER_STATUSES 保持一致）────────────

STATUS_NAMES: dict[int, str] = {
    1: "预处理",
    2: "待分县局下发",
    3: "处理中",
    4: "无效",
    5: "待分县局审核",
    6: "待市局审核",
    7: "已办结",
    8: "已回访",
}


# ─── 搜索字段白名单：裁剪超大 JSON 列，防超大返回 ──────────────────────────
# 排除 content / flow_records / *_files / *_records 等重字段，只留轻量标量。
SEARCH_FIELD_WHITELIST: tuple[str, ...] = (
    "id",
    "letter_no",
    "citizen_name",
    "phone",
    "id_card",
    "channel_id",
    "category_id",
    "letter_attribute_id",
    "current_unit_code",
    "current_status",
    "deadline_at",
    "extension_count",
    "district_received_at",
    "handler_received_at",
    "followed_up_at",
    "created_at",
    "updated_at",
    "step1",
    "step2",
    "step3",
)

DEFAULT_SEARCH_FIELDS: list[str] = [
    "id",
    "letter_no",
    "citizen_name",
    "current_status",
    "current_unit_code",
    "created_at",
    "deadline_at",
]


# ─── 信封与 scope 公共件 ───────────────────────────────────────────────────


def _scope_block(ctx: Any, sample_size: int, time_range: str = "全部", note: str = "") -> dict:
    """每个工具返回前统一填充的可见范围说明块。"""
    unit = ctx.operator_unit or {}
    full_name = build_unit_full_name(unit) if unit else "（无单位）"
    role = (ctx.profile or {}).get("role_label") or "民警"

    if ctx.is_city_center():
        rule = "市局民意智感中心：可查看全部信件"
    elif ctx.is_branch_center():
        rule = "分县局民意智感中心：可查看本局及下属单位办理的信件"
    elif ctx.operator_unit_code:
        rule = "普通民警：仅可查看本单位当前办理或本单位处理过的信件"
    else:
        rule = "无单位归属：不可查看任何信件"

    return {
        "visible_rule": rule,
        "user_role": role,
        "user_unit": full_name,
        "time_range": time_range,
        "sample_size": sample_size,
        "note": note,
    }


def _envelope(tool: str, scope: dict, data: Any, summary_hint: str) -> dict:
    return {
        "tool": tool,
        "scope": scope,
        "data": data,
        "summary_hint": summary_hint,
    }


def _status_breakdown(counts: dict[str, int]) -> list[dict]:
    """把 {status_code: count} 转成带中文名的有序列表。"""
    items: list[dict] = []
    for code in sorted(counts, key=lambda k: int(k)):
        code_int = int(code)
        items.append({
            "code": code_int,
            "name": STATUS_NAMES.get(code_int, str(code)),
            "count": int(counts[code]),
        })
    return items


def _unit_name_map() -> dict[str, str]:
    """units 表 system_code -> 中文全名 的映射，供按单位统计换算名称。"""
    mapping: dict[str, str] = {}
    for unit in query_all_units():
        code = str(unit.get("system_code") or "").strip()
        if code:
            mapping[code] = build_unit_full_name(unit)
    return mapping


# ─── 工具实现 ─────────────────────────────────────────────────────────────


def _whoami_scope(user: dict, args: dict) -> dict:
    ctx = get_context(user)
    unit = ctx.operator_unit or {}
    full_name = build_unit_full_name(unit) if unit else "（无单位）"
    role = (ctx.profile or {}).get("role_label") or "民警"
    access_scope = (ctx.profile or {}).get("access_scope") or ""

    if ctx.is_city_center():
        rule = "市局民意智感中心：可查看全部信件"
    elif ctx.is_branch_center():
        rule = "分县局民意智感中心：可查看本局及下属单位办理的信件"
    elif ctx.operator_unit_code:
        rule = "普通民警：仅可查看本单位当前办理或本单位处理过的信件"
    else:
        rule = "无单位归属：不可查看任何信件"

    data = {
        "role": role,
        "unit_name": full_name,
        "unit_code": ctx.operator_unit_code or "",
        "access_scope": access_scope,
        "visibility_rule": rule,
    }
    scope = _scope_block(ctx, sample_size=0, note="仅说明可观测范围，无统计数据")
    return _envelope("whoami_scope", scope, data, f"当前角色：{role}；单位：{full_name}；{rule}")


_DICT_KINDS: dict[str, Callable[[], dict]] = {
    "status": lambda: {"list": [{"code": s["code"], "name": s["name"]} for s in _status_enum()]},
    "channel": get_letter_channels_payload,
    "category": get_categories_payload,
    "attribute": get_letter_attributes_payload,
    "rating": get_rating_tags_payload,
}


def _status_enum() -> list[dict]:
    return [{"code": code, "name": name} for code, name in STATUS_NAMES.items()]


def _dict_lookup(user: dict, args: dict) -> dict:
    kind = str(args.get("kind") or "").strip().lower()
    if kind not in _DICT_KINDS:
        raise ToolError("kind 只能是 status/channel/category/attribute/rating")
    payload = _DICT_KINDS[kind]()
    ctx = get_context(user)
    scope = _scope_block(ctx, sample_size=len(payload.get("list", [])), note="字典数据，与可见范围无关")
    return _envelope("dict_lookup", scope, {"kind": kind, **payload}, f"字典[{kind}] 共 {len(payload.get('list', []))} 项")


def _letter_overview(user: dict, args: dict) -> dict:
    scope_param = str(args.get("scope") or "visible").strip() or "visible"
    if scope_param not in {"visible", "current_unit"}:
        raise ToolError("scope 只能是 visible 或 current_unit")

    result = get_letter_status_counts(user, {"scope": scope_param})
    counts: dict[str, int] = result.get("counts", {})
    total = int(result.get("total", 0))

    ctx = get_context(user)
    time_range = "全部"
    scope_note = "统计范围=可见信件" if scope_param == "visible" else "统计范围=当前办理单位信件"
    scope = _scope_block(ctx, sample_size=total, time_range=time_range, note=scope_note)

    # _status_breakdown 返回的是 [{code,name,count}] 而不是 (code, count) 对，
    # 这里必须按键取值 —— 按元组解包会迭代出 dict 的**键**，直接报
    # "too many values to unpack"（且 counts 为空时被短路，只在真有数据的账号上炸）。
    breakdown = _status_breakdown(counts)
    if breakdown:
        summary = f"共 {total} 件；" + "；".join(
            f"{item['name']} {item['count']} 件" for item in breakdown
        )
    else:
        summary = f"共 {total} 件"

    return _envelope(
        "letter_overview",
        scope,
        {"total": total, "scope": scope_param, "by_status": breakdown},
        summary,
    )


def _letter_search(user: dict, args: dict) -> dict:
    # fields 白名单裁剪：只允许轻量字段，防超大返回。
    fields = _resolve_search_fields(args.get("fields"))
    filters: dict[str, Any] = {}
    if args.get("status") not in (None, ""):
        filters["current_status"] = args["status"]
    if args.get("unit_code") not in (None, ""):
        filters["current_unit_code"] = args["unit_code"]
    created: dict[str, Any] = {}
    if args.get("created_from") not in (None, ""):
        created["start_time"] = args["created_from"]
    if args.get("created_to") not in (None, ""):
        created["end_time"] = args["created_to"]
    if created:
        filters["created_at"] = created

    service_args: dict[str, Any] = {
        "fields": fields,
        "keyword": args.get("keyword") or "",
    }
    if filters:
        service_args["filters"] = filters
    for key in ("page", "limit", "sort"):
        if args.get(key) not in (None, ""):
            service_args[key] = args[key]

    payload = get_letter_list(user, service_args)
    total = int(payload.get("total", 0))

    ctx = get_context(user)
    time_range = _range_label(args.get("created_from"), args.get("created_to"))
    scope = _scope_block(ctx, sample_size=total, time_range=time_range, note="结果已按字段白名单裁剪")

    n = len(payload.get("list", []))
    return _envelope(
        "letter_search",
        scope,
        payload,
        f"命中 {total} 件，本次返回 {n} 件",
    )


def _resolve_search_fields(raw: Any) -> list[str]:
    """把用户/LLM 传来的 fields 裁剪到白名单；空/无效则用默认轻量集合。"""
    if raw in (None, "", []):
        return list(DEFAULT_SEARCH_FIELDS)
    if isinstance(raw, str):
        raw = [item.strip() for item in raw.split(",") if item.strip()]
    if not isinstance(raw, list):
        raise ToolError("fields 必须是数组或逗号分隔字符串")
    picked: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = str(item or "").strip()
        if not name or name in seen:
            continue
        if name in SEARCH_FIELD_WHITELIST:
            picked.append(name)
            seen.add(name)
    return picked or list(DEFAULT_SEARCH_FIELDS)


def _letter_detail(user: dict, args: dict) -> dict:
    letter_no = str(args.get("letter_no") or "").strip()
    if not letter_no:
        raise ToolError("letter_no 不能为空")
    result = get_letter(user, {"letter_no": letter_no})
    letter = result.get("letter") or {}
    ctx = get_context(user)
    scope = _scope_block(ctx, sample_size=1 if letter else 0, note="详情已套可见性校验")
    status = letter.get("current_status")
    status_name = STATUS_NAMES.get(int(status), str(status)) if status is not None else "未知"
    summary = f"信件 {letter_no}：当前状态「{status_name}」"
    return _envelope("letter_detail", scope, result, summary)


def _letter_stats_by_unit(user: dict, args: dict) -> dict:
    service_args: dict[str, Any] = {"group_by_unit": True}
    filters: dict[str, Any] = {}
    if args.get("created_from") not in (None, "") or args.get("created_to") not in (None, ""):
        created: dict[str, Any] = {}
        if args.get("created_from") not in (None, ""):
            created["start_time"] = args["created_from"]
        if args.get("created_to") not in (None, ""):
            created["end_time"] = args["created_to"]
        filters["created_at"] = created
    if filters:
        service_args["filters"] = filters

    result = get_letter_status_counts(user, service_args)
    total = int(result.get("total", 0))
    by_unit: dict[str, dict[str, int]] = result.get("by_unit", {})
    name_map = _unit_name_map()

    units_out: list[dict] = []
    for code in sorted(by_unit):
        status_counts = by_unit[code]
        unit_total = sum(status_counts.values())
        units_out.append({
            "unit_code": code,
            "unit_name": name_map.get(code, "未知单位" if not code else code),
            "total": unit_total,
            "by_status": _status_breakdown(status_counts),
        })

    ctx = get_context(user)
    time_range = _range_label(args.get("created_from"), args.get("created_to"))
    scope = _scope_block(ctx, sample_size=total, time_range=time_range, note="按 current_unit_code 分组，单位名来自 units 表")
    return _envelope(
        "letter_stats_by_unit",
        scope,
        {"total": total, "units": units_out},
        f"共 {total} 件，涉及 {len(units_out)} 个单位",
    )


def _letter_trend(user: dict, args: dict) -> dict:
    granularity = str(args.get("granularity") or "month").strip().lower()
    if granularity not in {"month", "day"}:
        raise ToolError("granularity 只能是 month 或 day")

    # 唯一新写 SQL 的工具：强制拼官方可见性 WHERE，绝不自行加过滤条件。
    conditions, params = build_visibility_where(user)
    if args.get("created_from") not in (None, "") or args.get("created_to") not in (None, ""):
        range_conditions, range_params = build_created_at_range_where({
            "start_time": args.get("created_from"),
            "end_time": args.get("created_to"),
        })
        conditions.extend(range_conditions)
        params.extend(range_params)

    where_sql = compose_where(conditions)
    fmt = "%Y-%m" if granularity == "month" else "%Y-%m-%d"
    rows = execute_select_all(
        f"""
        SELECT DATE_FORMAT(l.created_at, %s) AS bucket, COUNT(*) AS total
        FROM letters l
        {where_sql}
        GROUP BY bucket
        ORDER BY bucket ASC
        """,
        tuple([fmt, *params]),
    )

    buckets = [{"bucket": str(r.get("bucket") or ""), "total": int(r.get("total") or 0)} for r in rows]
    total = sum(b["total"] for b in buckets)

    ctx = get_context(user)
    time_range = _range_label(args.get("created_from"), args.get("created_to"))
    scope = _scope_block(ctx, sample_size=total, time_range=time_range, note="按 created_at 分组，口径与列表/概览一致")
    return _envelope(
        "letter_trend",
        scope,
        {"granularity": granularity, "total": total, "buckets": buckets},
        f"共 {total} 件，{len(buckets)} 个时间段",
    )


def _export_letters(user: dict, args: dict) -> dict:
    export_args = {k: v for k, v in (args or {}).items() if k in ("keyword", "status", "created_from", "created_to", "unit_code")}
    export_args = {k: v for k, v in export_args.items() if v not in (None, "")}
    # export_letters_csv 内部已走 get_letter_list → build_visibility_where。
    if "status" in export_args:
        export_args["filters"] = {"current_status": export_args.pop("status")}
    if "unit_code" in export_args:
        export_args.setdefault("filters", {})["current_unit_code"] = export_args.pop("unit_code")
    if "created_from" in export_args or "created_to" in export_args:
        created: dict[str, Any] = {}
        if export_args.get("created_from"):
            created["start_time"] = export_args.pop("created_from")
        if export_args.get("created_to"):
            created["end_time"] = export_args.pop("created_to")
        export_args.setdefault("filters", {})["created_at"] = created
    # keyword 保持原样透传
    export_args.setdefault("keyword", args.get("keyword") or "")

    filename, csv_text = export_letters_csv(user, export_args)
    data_rows = sum(1 for _ in csv.reader(io.StringIO(csv_text))) - 1 if csv_text else 0
    rows = data_rows if data_rows > 0 else 0
    size = len(csv_text.encode("utf-8"))

    handle = _make_export_handle(user, args)
    export_dir = _export_dir()
    export_dir.mkdir(parents=True, exist_ok=True)
    (export_dir / f"{handle}.csv").write_text(csv_text, encoding="utf-8")

    ctx = get_context(user)
    time_range = _range_label(args.get("created_from"), args.get("created_to"))
    scope = _scope_block(ctx, sample_size=rows, time_range=time_range, note="导出内容=当前可见范围内的信件")
    return _envelope(
        "export_letters",
        scope,
        {"filename": filename, "handle": handle, "rows": rows, "size": size},
        f"已导出 {rows} 件（{size} 字节），下载句柄 {handle}",
    )


# ─── 导出句柄与目录 ───────────────────────────────────────────────────────


def _internal_secret() -> str:
    secret = getattr(settings, "AI_COMPANY_INTERNAL_TOKEN", None) or ""
    if not secret:
        secret = get_env_value("AI_COMPANY_INTERNAL_TOKEN", "")
    if not secret:
        secret = getattr(settings, "SECRET_KEY", "") or "ai-company-internal-dev"
    return secret


def _make_export_handle(user: dict, args: dict) -> str:
    user_pk = str(user.get("id") or "")
    ts = int(time.time())
    canonical = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    message = f"{user_pk}|{canonical}|{ts}"
    return hmac.new(_internal_secret().encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _export_dir() -> Path:
    base = Path(getattr(settings, "BASE_DIR", Path(__file__).resolve().parents[2]))
    return base.parent / ".run" / "ai_exports"


def _range_label(created_from: Any, created_to: Any) -> str:
    frm = str(created_from or "").strip()
    to = str(created_to or "").strip()
    if not frm and not to:
        return "全部"
    return f"{frm or '…'}~{to or '…'}"


# ─── 工具目录 ─────────────────────────────────────────────────────────────

_TOOL_CATALOG: list[dict] = [
    {
        "name": "whoami_scope",
        "description": "返回当前用户的可观测范围说明：角色、单位中文名、可见性规则文字。",
        "params": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "dict_lookup",
        "description": "查询字典：状态/渠道/分类/属性/评分标签。",
        "params": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["status", "channel", "category", "attribute", "rating"],
                    "description": "字典类型：status=状态, channel=渠道, category=分类, attribute=属性, rating=评分标签",
                },
            },
            "required": ["kind"],
        },
    },
    {
        "name": "letter_overview",
        "description": "统计当前用户可观测信件的各状态数量与总数。",
        "params": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["visible", "current_unit"],
                    "description": "统计范围：visible=可见范围(默认)，current_unit=仅当前办理单位",
                },
            },
            "required": [],
        },
    },
    {
        "name": "letter_search",
        "description": "按条件检索当前用户可观测的信件列表（字段已裁剪，返回轻量字段）。",
        "params": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "关键字（匹配编号/姓名/身份证/手机/内容/流转记录）"},
                "status": {"type": "integer", "description": "信件状态码 1-8"},
                "created_from": {"type": "string", "description": "创建时间下限，YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS"},
                "created_to": {"type": "string", "description": "创建时间上限，YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS"},
                "unit_code": {"type": "string", "description": "当前办理单位系统编号"},
                "page": {"type": "integer", "description": "页码，从 1 开始"},
                "limit": {"type": "integer", "description": "每页条数；0 表示不分页返回全部"},
                "sort": {"type": "string", "enum": ["asc", "desc"], "description": "按创建时间排序方向"},
                "fields": {"type": "array", "items": {"type": "string"}, "description": "返回字段白名单（仅允许轻量字段）"},
            },
            "required": [],
        },
    },
    {
        "name": "letter_detail",
        "description": "按编号返回一封可观测信件的全部字段详情。",
        "params": {
            "type": "object",
            "properties": {"letter_no": {"type": "string", "description": "信件编号"}},
            "required": ["letter_no"],
        },
    },
    {
        "name": "letter_stats_by_unit",
        "description": "按单位统计当前用户可观测信件的各状态数量，单位编号换算中文名。",
        "params": {
            "type": "object",
            "properties": {
                "created_from": {"type": "string", "description": "创建时间下限"},
                "created_to": {"type": "string", "description": "创建时间上限"},
            },
            "required": [],
        },
    },
    {
        "name": "letter_trend",
        "description": "按创建时间分组统计当前用户可观测信件数量趋势（月/日）。",
        "params": {
            "type": "object",
            "properties": {
                "created_from": {"type": "string", "description": "创建时间下限"},
                "created_to": {"type": "string", "description": "创建时间上限"},
                "granularity": {"type": "string", "enum": ["month", "day"], "description": "聚合粒度：month=按月(默认)，day=按日"},
            },
            "required": [],
        },
    },
    {
        "name": "export_letters",
        "description": "导出当前用户可观测信件为 CSV 文件，返回下载句柄。",
        "params": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "关键字"},
                "status": {"type": "integer", "description": "信件状态码 1-8"},
                "created_from": {"type": "string", "description": "创建时间下限"},
                "created_to": {"type": "string", "description": "创建时间上限"},
            },
            "required": [],
        },
    },
]

_TOOL_HANDLERS: dict[str, Callable[[dict, dict], dict]] = {
    "whoami_scope": _whoami_scope,
    "dict_lookup": _dict_lookup,
    "letter_overview": _letter_overview,
    "letter_search": _letter_search,
    "letter_detail": _letter_detail,
    "letter_stats_by_unit": _letter_stats_by_unit,
    "letter_trend": _letter_trend,
    "export_letters": _export_letters,
}


# ─── 公开 API ─────────────────────────────────────────────────────────────


def list_tools() -> list[dict]:
    """返回工具目录（不含可调用对象），供 LLM 选参。"""
    return [dict(item) for item in _TOOL_CATALOG]


def run_tool(user: dict, name: str, args: dict) -> dict:
    """执行一个受控工具，返回信封 {tool, scope, data, summary_hint}。

    参数错 / 未知工具 / 无权限统一抛 ``ToolError``（中文 message）。
    """
    if not isinstance(args, dict):
        raise ToolError("参数必须是对象")
    if name not in _TOOL_HANDLERS:
        raise ToolError(f"未知工具: {name}")

    handler = _TOOL_HANDLERS[name]
    try:
        return handler(user, args)
    except ToolError:
        raise
    except PermissionDenied as exc:
        raise ToolError(f"无权限: {exc}") from None
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    except Exception as exc:  # 兜底：任何底层异常都转成中文可读错误，不外泄堆栈
        raise ToolError(f"工具 {name} 执行失败: {exc}") from None
