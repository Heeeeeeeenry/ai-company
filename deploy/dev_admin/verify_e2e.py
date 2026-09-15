#!/usr/bin/env python3
"""dev_admin 侧真实端到端验证：用**真登录态**打通 /api/ai/。

为什么这么做：
  悬浮窗最终跑在浏览器里，请求带的是管理端 cookie（session_key）。
  只测「未登录 401」不足以证明接通，必须拿一个**真实的、未过期的会话**
  打一遍完整链路：SessionAuthMiddleware → request.session_user →
  ai_company.conf 映射出 admin:<pk> → 转发 ai-company 容器 → 回 reply。

做法：
  从 MySQL 里取一条未过期的 user_sessions 记录，用它当 cookie 发请求。
  **session_key 全程不回显**（它是凭证），脚本只打印 HTTP 结果。

用法（在 222.223.144.110 上，用户 my）::

    python3 verify_e2e.py [--port 15173]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENV_FILE = Path.home() / "dev_admin" / ".env"


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def find_db_config(env: dict[str, str]) -> tuple[str, str, str, str, str]:
    """从 .env 里猜 DB 配置（不打印任何值）。"""

    def pick(*names: str, default: str = "") -> str:
        for n in names:
            if env.get(n):
                return env[n]
        return default

    host = pick("DB_HOST", "MYSQL_HOST", "DATABASE_HOST", default="127.0.0.1")
    port = pick("DB_PORT", "MYSQL_PORT", "DATABASE_PORT", default="3306")
    name = pick("DB_NAME", "MYSQL_DATABASE", "DATABASE_NAME", "DB_DATABASE")
    user = pick("DB_USER", "MYSQL_USER", "DATABASE_USER", "DB_USERNAME")
    pwd = pick("DB_PASSWORD", "MYSQL_PASSWORD", "DATABASE_PASSWORD", "DB_PASS")
    return host, port, name, user, pwd


def mysql_query(container: str, user: str, pwd: str, db: str, sql: str) -> list[list[str]]:
    """在 mysql 容器里跑查询；密码走 MYSQL_PWD 环境变量，不进 argv。"""
    import os

    env = dict(os.environ, MYSQL_PWD=pwd) if pwd else dict(os.environ)
    cmd = ["docker", "exec", "-i"]
    if pwd:
        cmd += ["-e", "MYSQL_PWD"]
    cmd += [container, "mysql", "-N", "-B", f"-u{user}", db, "-e", sql]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"mysql 查询失败: {proc.stderr.strip()[:300]}")
    rows = [ln.split("\t") for ln in proc.stdout.splitlines() if ln.strip()]
    return rows


def http(method: str, url: str, cookie: str = "", body: dict | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="15173")
    ap.add_argument("--container", default="dev-admin-my-mysql")
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    ok = True

    print("=" * 68)
    print("dev_admin × ai-company 真实端到端验证")
    print("=" * 68)

    env = read_env(ENV_FILE)
    host, port, name, user, pwd = find_db_config(env)
    print(f"[配置] DB {name or '(未识别)'} @ {host}:{port} user={'*' * len(user) if user else '(未识别)'}")
    if not (name and user):
        print("[x] 没能从 .env 里识别出 DB 配置，无法取会话")
        return 2

    # ── 1. 拿一个未过期的真实会话 ──
    sql = (
        "SELECT s.session_key, s.user_id, u.name "
        "FROM user_sessions s JOIN police_users u ON u.id = s.user_id "
        "WHERE s.expires_at > NOW() AND u.is_active = 1 "
        "ORDER BY s.created_at DESC LIMIT 1"
    )
    try:
        rows = mysql_query(args.container, user, pwd, name, sql)
    except Exception as exc:  # noqa: BLE001
        print(f"[x] {exc}")
        return 2
    if not rows:
        print("[x] 库里没有未过期的活跃会话 —— 请先在浏览器登录一次管理端再跑本脚本")
        return 2
    session_key, uid, uname = rows[0][0], rows[0][1], (rows[0][2] if len(rows[0]) > 2 else "?")
    print(f"[会话] 取到 user_id={uid} name={uname}（session_key 长度 {len(session_key)}，不回显）")
    cookie = f"session_key={session_key}"

    # ── 2. 带 cookie 打 /api/ai/sessions/ → 验证身份映射 ──
    print("\n[1] GET /api/ai/sessions/  （验证 SessionAuthMiddleware → admin:<pk> 映射）")
    code, text = http("GET", f"{base}/api/ai/sessions/", cookie=cookie)
    print(f"    HTTP {code}")
    try:
        obj = json.loads(text)
        resolved = obj.get("user_id")
        n = len(obj.get("conversations") or [])
        print(f"    解析到的 user_id = {resolved}")
        print(f"    该用户会话数     = {n}")
        expect = f"admin:{uid}"
        if resolved == expect:
            print(f"    ✅ 身份映射正确：{resolved} == admin:<police_users.id>")
        else:
            print(f"    ❌ 身份映射不符：期望 {expect}，实际 {resolved}")
            ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"    ❌ 响应不是 JSON：{text[:300]} ({exc})")
        ok = False

    # ── 3. 带 cookie 发一句话 → 验证完整链路 ──
    print("\n[2] POST /api/ai/chat/      （完整链路：宿主登录态 → 容器 → DeepSeek）")
    code, text = http(
        "POST",
        f"{base}/api/ai/chat/",
        cookie=cookie,
        body={"message": "请只回答两个字：在吗"},
    )
    print(f"    HTTP {code}")
    try:
        obj = json.loads(text)
        reply = obj.get("reply")
        print(f"    conversation_id = {obj.get('conversation_id')}")
        print(f"    reply           = {reply!r}")
        if code == 200 and reply:
            print("    ✅ 全链路打通")
        else:
            print(f"    ❌ 未拿到回复：{text[:300]}")
            ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"    ❌ 响应不是 JSON：{text[:300]} ({exc})")
        ok = False

    # ── 4. 不带 cookie → 必须 401/403 ──
    print("\n[3] GET /api/ai/sessions/  （不带 cookie，应被中间件拦下）")
    code, _ = http("GET", f"{base}/api/ai/sessions/")
    print(f"    HTTP {code}")
    if code in (401, 403):
        print("    ✅ 未登录被正确拦截")
    else:
        print("    ❌ 未登录竟然能访问，鉴权有问题")
        ok = False

    print("\n" + "=" * 68)
    print("结论:", "全部通过 ✅" if ok else "存在失败项 ❌")
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
