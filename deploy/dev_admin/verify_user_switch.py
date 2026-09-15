#!/usr/bin/env python3
"""复现 + 验证：换用户后「conversation not owned by user」。

用户实测报的现象：
  同一个浏览器先登 000000 能正常对话 → 再登 000001 发消息报
  「没能拿到回复：conversation not owned by user」。

机制（根因）：
  悬浮窗把 conversation_id 存在 sessionStorage。sessionStorage 是**按标签页**存的，
  同一个标签页里退出登录再登另一个人，它不会失效 —— 于是 000000 的 cid 被
  000001 带了过去。服务端按 session 推出 admin:<000001 的 police_users.id>，
  拿它去查 000000 的会话 → 归属校验失败 → 403。

本脚本用**真实登录接口**建出两条登录态，把这条链路原样打一遍：
  [0] 000000 聊一句          → 拿到 cid
  [1] 000001 带这个 cid 发消息 → 403 conversation not owned by user（复现用户报错）
  [2] 000001 丢掉 cid 再发     → 200（前端自愈后走的就是这条）
  [3] 两边 /sessions/ 的 user_id 不同 → 前端据此判断「换人了」
  [4] 000001 读 000000 的历史  → 403（隔离成立）
跑完会把自己新建的 user_sessions 行删掉。

用法（在 222.223.144.110 上，用户 my）::

    python3 verify_user_switch.py --ua 000000 --ub 000001 --password 000000
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_e2e import find_db_config, mysql_query, read_env  # noqa: E402

CONTAINER = "dev-admin-my-mysql"


def http(method: str, url: str, cookie: str = "", body: dict | None = None):
    """返回 (status, text, set_cookie)。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), resp.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), exc.headers.get("Set-Cookie", "")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}", ""


def login(base: str, number: str, password: str):
    """走真实登录接口，返回 (session_key, user_id)。"""
    code, text, setck = http(
        "POST", f"{base}/api/auth/",
        body={"order": "login", "args": {"police_number": number, "password": password}},
    )
    if code != 200:
        print(f"    [x] {number} 登录失败 HTTP {code}: {text[:200]}")
        return None
    sk = ""
    for part in setck.split(";"):
        name, _, val = part.strip().partition("=")
        if name == "session_key":
            sk = val
    if not sk:
        print(f"    [x] {number} 登录未拿到 session_key cookie")
        return None
    return sk


def db():
    env = read_env(Path.home() / "dev_admin" / ".env")
    _h, _p, name, user, pwd = find_db_config(env)
    return CONTAINER, user, pwd, name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ua", default="000000")
    ap.add_argument("--ub", default="000001")
    ap.add_argument("--password", default="000000")
    ap.add_argument("--port", default="15173")
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    ok = True
    minted: list[str] = []

    print("=" * 72)
    print(f"复现 / 验证：{args.ua} → {args.ub} 换用户后的会话归属问题")
    print("=" * 72)

    container, user, pwd, name = db()

    def uid_of(number: str) -> str | None:
        rows = mysql_query(container, user, pwd, name,
                           f"SELECT id FROM police_users WHERE police_number='{number}' LIMIT 1")
        return rows[0][0] if rows else None

    print(f"\n[登录] 用真实接口建两条登录态（密码长度 {len(args.password)}，不回显）")
    ka = login(base, args.ua, args.password)
    if not ka:
        return 2
    minted.append(ka)
    kb = login(base, args.ub, args.password)
    if not kb:
        mysql_query(container, user, pwd, name,
                    f"DELETE FROM user_sessions WHERE session_key='{ka}'")
        return 2
    minted.append(kb)
    cookie_a, cookie_b = f"session_key={ka}", f"session_key={kb}"
    id_a, id_b = uid_of(args.ua), uid_of(args.ub)
    print(f"    {args.ua} -> police_users.id={id_a} 即 admin:{id_a}")
    print(f"    {args.ub} -> police_users.id={id_b} 即 admin:{id_b}")
    if id_a == id_b:
        print("[!] 两个警号指向同一条记录，场景不成立")
        return 2

    try:
        print(f"\n[0] {args.ua} 先聊一句（它就有会话了）")
        code, text, _ = http("POST", f"{base}/api/ai/chat/", cookie=cookie_a,
                             body={"message": "请只回答两个字：收到"})
        try:
            cid = json.loads(text).get("conversation_id")
        except Exception:  # noqa: BLE001
            cid = None
        print(f"    HTTP {code}  cid={cid}")
        if code != 200 or not cid:
            print(f"    ❌ 建会话失败：{text[:200]}")
            return 1

        print(f"\n[1] {args.ub} 带着 {args.ua} 的 cid 发消息   ← 复现用户看到的报错")
        code, text, _ = http("POST", f"{base}/api/ai/chat/", cookie=cookie_b,
                             body={"message": "在吗", "conversation_id": cid})
        print(f"    HTTP {code}  {text[:160]}")
        if code == 403 and "not owned" in text:
            print("    ✅ 复现成功 —— 这就是「conversation not owned by user」的来源")
            print("       旧 cid 被带给了另一个人，服务端按会话归属直接拒绝（不是 AI 出错）")
        else:
            print("    ⚠️ 未复现 403")

        print(f"\n[2] {args.ub} 丢掉旧 cid 再发   ← 前端自愈后走的就是这条")
        code, text, _ = http("POST", f"{base}/api/ai/chat/", cookie=cookie_b,
                             body={"message": "在吗", "conversation_id": None})
        try:
            obj = json.loads(text)
        except Exception:  # noqa: BLE001
            obj = {}
        print(f"    HTTP {code}  新 cid={obj.get('conversation_id')}  reply={obj.get('reply')!r}")
        if code == 200 and obj.get("reply"):
            print("    ✅ 兜底可用：丢掉旧 cid 后正常作答")
        else:
            print(f"    ❌ 兜底没通：{text[:200]}")
            ok = False

        print(f"\n[3] 两边 /sessions/ 报的 user_id   ← 前端据此判断「换人了」")
        for tag, ck, expect in ((args.ua, cookie_a, f"admin:{id_a}"),
                                (args.ub, cookie_b, f"admin:{id_b}")):
            code, text, _ = http("GET", f"{base}/api/ai/sessions/", cookie=ck)
            try:
                got = json.loads(text).get("user_id")
            except Exception:  # noqa: BLE001
                got = None
            print(f"    {tag}: HTTP {code} user_id={got} 期望={expect} {'✅' if got == expect else '❌'}")
            if got != expect:
                ok = False

        print(f"\n[4] {args.ub} 读 {args.ua} 的会话历史（应 403）")
        code, text, _ = http("GET", f"{base}/api/ai/sessions/{cid}/", cookie=cookie_b)
        print(f"    HTTP {code}  {text[:120]}")
        if code == 403:
            print("    ✅ 会话列表侧同样拒绝跨用户读取，隔离成立")
        else:
            print("    ❌ 跨用户可读，隔离有洞")
            ok = False
    finally:
        n = 0
        for sk in minted:
            mysql_query(container, user, pwd, name,
                        f"DELETE FROM user_sessions WHERE session_key='{sk}'")
            n += 1
        print(f"\n[清理] 已删除本次新建的 {n} 条会话记录")

    print("\n" + "=" * 72)
    print("结论:", "复现 + 兜底 + 隔离 全部符合预期 ✅" if ok else "存在失败项 ❌")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
