#!/usr/bin/env python3
"""隔离不变量单测（多用户嵌入的前提）。

覆盖三条"绝不允许"：
  I1  请求绑定会话 C 时，读别的会话记忆 = 空（不返回别人的事实）
  I2  service 模式的进程不得自动激活磁盘上的 CLI 会话（mgr.current 必须为 None）
  I3  服务端会话记忆落在独立根目录，与部署者自己的 CLI 会话物理隔离

跑法：<conda python> scripts/test_isolation_scope.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAIL = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


def reload_mods():
    for m in ("src.session.memory", "src.session.manager", "src.session.scope",
              "src.session", "src.memory.artifacts", "src.memory.hermes"):
        sys.modules.pop(m, None)


# ── 准备一个假的 ~/.ai-company：CLI 会话里放着"部署者的隐私" ──────────
home = tempfile.mkdtemp()
base = os.path.join(home, ".ai-company")          # ai-company 数据根
os.makedirs(os.path.join(base, "sessions", "clisess1"), exist_ok=True)
import json
with open(os.path.join(base, "sessions", "clisess1", "memory.json"), "w") as f:
    json.dump({"session_id": "clisess1",
               "memories": {"li_heng_profile": {"value": "SECRET-PRIVATE-PROFILE",
                                                "updated_at": "2026-01-01"}},
               "conversations": [{"user": "SECRET-CHAT", "assistant": "ok"}]}, f)
with open(os.path.join(base, "sessions", "clisess1", "metadata.json"), "w") as f:
    json.dump({"id": "clisess1", "name": "default", "is_active": True,
               "created_at": "2026-01-01", "last_active": "2026-01-01",
               "message_count": 1}, f)

os.environ["HOME"] = home
os.environ["AI_COMPANY_SERVICE_MODE"] = "1"
os.environ["AI_COMPANY_SERVICE_SESSION_DIR"] = os.path.join(base, "service_sessions")
reload_mods()

import src.session.memory as mem_mod
from src.session.scope import scope

check("service_mode() 识别 env", mem_mod.service_mode() is True)
check("服务端记忆根目录与 CLI 分离",
      mem_mod._session_root() == os.path.join(base, "service_sessions"),
      mem_mod._session_root())

# ── I1：跨会话读取必须拿到空 ─────────────────────────────────────────
with scope("conv-C"):
    other = mem_mod.get_session_memory("clisess1")          # 别人的 CLI 会话
    check("I1 跨会话读取不返回他人记忆", other.all() == {}, f"{other.all()}")
    check("I1 跨会话读取不返回他人对话", other.get_recent_conversations(10) == [])
    other.set("x", "y")
    check("I1 跨会话读取不落盘（detached）",
          not os.path.exists(os.path.join(base, "service_sessions", "clisess1", "memory.json")))

    mine = mem_mod.get_session_memory("conv-C")
    mine.set("my_key", "mine")
    check("I1 本会话读取正常可写", mine.get("my_key") == "mine")
    check("I1 本会话落盘到服务根目录",
          os.path.exists(os.path.join(base, "service_sessions", "conv-C", "memory.json")))

# 无作用域（本地 CLI）不受影响 —— 注意：CLI 只在非 service 模式下存在
os.environ["AI_COMPANY_SERVICE_MODE"] = "0"
reload_mods()
import src.session.memory as mem_cli
cli_mem = mem_cli.get_session_memory("clisess1")
check("I1 非 service（本地 CLI）仍可读自己会话（不回归）",
      cli_mem.get("li_heng_profile") == "SECRET-PRIVATE-PROFILE",
      f"{cli_mem.get('li_heng_profile')}")
check("I3 非 service 模式记忆根目录 = CLI 目录",
      mem_cli._session_root() == os.path.join(base, "sessions"),
      mem_cli._session_root())

# ── I2：service 模式不得自动激活磁盘 CLI 会话 ────────────────────────
os.environ["AI_COMPANY_SERVICE_MODE"] = "1"
reload_mods()
from src.session.manager import SessionManager
mgr = SessionManager()
check("I2 service 模式 mgr.current 为 None（不自动激活 CLI 会话）",
      mgr.current is None, f"{getattr(mgr.current, 'id', None)}")
check("I2 service 模式不往磁盘写 default 会话",
      set(os.listdir(os.path.join(base, "sessions"))) == {"clisess1"},
      f"{sorted(os.listdir(os.path.join(base, 'sessions')))}")

# 反证：非 service 模式的老行为仍会自动激活（确认开关有效）
os.environ["AI_COMPANY_SERVICE_MODE"] = "0"
reload_mods()
import src.session.manager as mgr_mod
mgr2 = mgr_mod.SessionManager()
check("I2 非 service 模式仍自动激活（开关生效）",
      mgr2.current is not None and mgr2.current.id == "clisess1",
      f"{getattr(mgr2.current, 'id', None)}")

shutil.rmtree(home, ignore_errors=True)

print("\n" + "=" * 46)
if FAIL:
    print(f"结果: {len(FAIL)} 项失败 -> {FAIL}")
    sys.exit(1)
print("结果: 全部通过 ✅")
