"""验证宿主侧受控取数工具层 host_tools.py。

在宿主 ~/dev_admin/backend_django 下运行：

    ~/admin_runtime/python/bin/python3.12 \
        integration/../tests/verify_host_tools.py
    # 实际命令（拷贝到宿主后）：
    #   DJANGO_SETTINGS_MODULE=backend_django.settings \
    #   ~/admin_runtime/python/bin/python3.12 verify_host_tools.py

断言：
  a) 8 个工具都能跑通不报错；
  b) 两个不同单位用户的 letter_overview 总数不同（权限确实在生效）；
  c) 故意传非法 tool 名抛 ToolError；
  d) letter_trend 分组总数 == letter_overview 的总数（口径一致性）。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend_django.settings")

import django

django.setup()

from api.common.db import execute_select_all
from ai_company.host_tools import ToolError, list_tools, run_tool

PASS = 0
FAIL = 0


def check(cond: bool, label: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}")


def load_user(uid: int) -> dict:
    rows = execute_select_all(
        """
        SELECT id, name, nickname, police_number, phone, unit_id, is_active, available_menus
        FROM police_users
        WHERE id = %s AND is_active = 1
        LIMIT 1
        """,
        (uid,),
    )
    if not rows:
        raise SystemExit(f"找不到 active 用户 id={uid}")
    return rows[0]


def visible_total(user: dict) -> int:
    """该用户可见信件总数（走宿主官方口径）。"""
    return int(run_tool(user, "letter_overview", {})["data"].get("total") or 0)


def pick_users() -> tuple[dict, dict]:
    """挑一对可见数差异最大的真实用户。

    不能按 id 硬选：本库里 2/3 号都是分县局账号、可见 0 件，两边都是 0
    既证明不了权限在区分（b 项），letter_detail 也搜不到 letter_no（a 项）。
    """
    rows = execute_select_all(
        """
        SELECT id, name, nickname, police_number, unit_id
        FROM police_users
        WHERE is_active = 1
        ORDER BY id
        LIMIT 20
        """
    )
    scored = []
    for row in rows:
        user = load_user(row["id"])
        scored.append((visible_total(user), user))
    if len(scored) < 2:
        raise SystemExit("active 用户不足 2 个，无法验证 b 项")
    scored.sort(key=lambda item: item[0])
    lo_total, lo_user = scored[0]
    hi_total, hi_user = scored[-1]
    if hi_total == lo_total:
        raise SystemExit(f"所有候选用户可见数都等于 {hi_total}，无法验证权限区分（b 项）")
    print(f"用户 A: id={hi_user['id']} name={hi_user['name']} unit_id={hi_user['unit_id']} 可见={hi_total}")
    print(f"用户 B: id={lo_user['id']} name={lo_user['name']} unit_id={lo_user['unit_id']} 可见={lo_total}")
    return hi_user, lo_user


def main() -> int:
    user_a, user_b = pick_users()
    print("=" * 70)

    # ── a) 8 个工具都能跑通不报错 ──────────────────────────────────────────
    print("\n[a] 8 个工具跑通测试（用户 A）")
    tools = list_tools()
    print(f"  list_tools() 返回 {len(tools)} 个工具")
    check(len(tools) == 8, "工具目录共 8 项")

    expected_names = {
        "whoami_scope",
        "dict_lookup",
        "letter_overview",
        "letter_search",
        "letter_detail",
        "letter_stats_by_unit",
        "letter_trend",
        "export_letters",
    }
    got_names = {t["name"] for t in tools}
    check(got_names == expected_names, "工具名与约定一致")

    # 每个工具目录项都要有 name/description/params。
    catalog_ok = all(
        isinstance(t.get("name"), str)
        and isinstance(t.get("description"), str)
        and isinstance(t.get("params"), dict)
        for t in tools
    )
    check(catalog_ok, "每个目录项含 name/description/params")

    results: dict[str, dict] = {}
    for t in tools:
        name = t["name"]
        # 给每个工具一个最小可跑入参。
        if name == "dict_lookup":
            args = {"kind": "status"}
        elif name == "letter_detail":
            # 先搜一封可见信件拿 letter_no。
            search = run_tool(user_a, "letter_search", {"limit": 1})
            letter = (search["data"].get("list") or [{}])[0]
            args = {"letter_no": letter.get("letter_no") or ""}
        elif name == "export_letters":
            args = {}
        else:
            args = {}
        try:
            envelope = run_tool(user_a, name, args)
            # 信封结构校验。
            has_keys = all(k in envelope for k in ("tool", "scope", "data", "summary_hint"))
            scope_ok = all(
                k in envelope.get("scope", {})
                for k in ("visible_rule", "user_role", "user_unit", "time_range", "sample_size", "note")
            )
            check(has_keys and scope_ok, f"{name} 返回信封+scope 完整")
            results[name] = envelope
        except ToolError as exc:
            check(False, f"{name} 抛 ToolError: {exc}")
            results[name] = {}

    # ── b) 两个用户 letter_overview 总数不同 ──────────────────────────────
    print("\n[b] 权限生效测试（两用户 letter_overview 总数不同）")
    ov_a = run_tool(user_a, "letter_overview", {})
    ov_b = run_tool(user_b, "letter_overview", {})
    total_a = int(ov_a["data"].get("total", 0))
    total_b = int(ov_b["data"].get("total", 0))
    print(f"  用户 A 可见总数 = {total_a}")
    print(f"  用户 B 可见总数 = {total_b}")
    check(total_a != total_b, "两个不同单位用户的总数不同（权限生效）")

    # ── c) 非法 tool 名抛 ToolError ────────────────────────────────────────
    print("\n[c] 非法工具名测试")
    try:
        run_tool(user_a, "no_such_tool", {})
        check(False, "非法工具名应抛 ToolError，却未抛")
    except ToolError as exc:
        check("未知工具" in str(exc), f"非法工具名抛 ToolError: {exc}")

    # 非法 kind 也要抛。
    try:
        run_tool(user_a, "dict_lookup", {"kind": "bogus"})
        check(False, "非法 kind 应抛 ToolError，却未抛")
    except ToolError as exc:
        check("kind" in str(exc), f"非法 kind 抛 ToolError: {exc}")

    # ── d) letter_trend 分组总数 == letter_overview 总数 ───────────────────
    print("\n[d] 口径一致性测试（trend 总数 == overview 总数）")
    trend = run_tool(user_a, "letter_trend", {"granularity": "month"})
    trend_total = int(trend["data"].get("total", 0))
    print(f"  letter_trend 总数 = {trend_total}")
    print(f"  letter_overview 总数 = {total_a}")
    check(trend_total == total_a, f"trend({trend_total}) == overview({total_a})")

    # 顺带验证 export 落盘句柄结构。
    print("\n[额外] export_letters 句柄结构")
    export = results.get("export_letters", {})
    edata = export.get("data", {})
    exp_ok = all(k in edata for k in ("filename", "handle", "rows", "size"))
    check(exp_ok, f"export 返回 {sorted(edata.keys())} 含 filename/handle/rows/size")
    if edata.get("handle"):
        print(f"  handle={edata['handle']} rows={edata.get('rows')} size={edata.get('size')}")

    print("=" * 70)
    print(f"结果：PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
