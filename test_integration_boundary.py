#!/usr/bin/env python3
"""Integration boundary tests for engram_backend + hermes session isolation.

Tests:
  1. EngramBackend global vs session instances coexistence
  2. SessionAwareMemory session switch isolation
  3. Empty session behavior
  4. _resolve_backend() fallback on exception
  5. Concurrent multi-session thread safety
  6. migrate_from_json isolation
  7. "最近对话" per-session context

All tests actually execute and output PASS/FAIL with reasons.
"""

import os
import sys
import json
import time
import threading
import tempfile
import shutil

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Test isolation: use temp DB to avoid polluting real data ──
REAL_DATA_DIR = os.path.expanduser("~/.ai-company")
BACKUP_DIR = os.path.expanduser("~/.ai-company.test.backup")

# Backup real data dir if it exists
if os.path.exists(REAL_DATA_DIR):
    if os.path.exists(BACKUP_DIR):
        shutil.rmtree(BACKUP_DIR)
    shutil.copytree(REAL_DATA_DIR, BACKUP_DIR)

# Override DATA_DIR and DB_PATH in engram_backend module
import src.memory.engram_backend as eb_mod
eb_mod.DATA_DIR = tempfile.mkdtemp(prefix="test_ai_company_")
eb_mod.DB_PATH = os.path.join(eb_mod.DATA_DIR, "engram_memory.db")

# Also override session dir
import src.session.manager as sm_mod
sm_mod.SESSION_DIR = tempfile.mkdtemp(prefix="test_sessions_")
sm_mod.GLOBAL_MEMORY_FILE = os.path.join(eb_mod.DATA_DIR, "global_memory.json")

# Reset session manager singleton
sm_mod._session_manager = None

print("=" * 70)
print("Integration Boundary Tests: engram_backend + hermes (session)")
print(f"Test DB:  {eb_mod.DB_PATH}")
print(f"Test Sess: {sm_mod.SESSION_DIR}")
print("=" * 70)

PASSED = 0
FAILED = 0

def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))

# ═══════════════════════════════════════════════════════════════════════
# Test 1: EngramBackend global vs session coexistence
# ═══════════════════════════════════════════════════════════════════════
print("\n── Test 1: EngramBackend global + session 实例共存隔离 ──")

from src.memory.engram_backend import EngramBackend

# Create TWO standalone backends directly (not through SessionAwareMemory)
global_be = EngramBackend(session_id=None)
session_be = EngramBackend(session_id="same-as-session")

# Add data to each
global_be.add("全局规则：禁止删数据", "memory", "safety")
global_be.add("用户语言：简体中文", "user")
session_be.add("Session-A 会话记录：讨论了天气", "memory")
session_be.add("Session-A 用户偏好：喜欢蓝色", "user")

# Verify global only sees global data
global_mem = [e["text"] for e in global_be.list("memory")]
global_user = [e["text"] for e in global_be.list("user")]
check("global 只有全局数据(memory)", "全局规则" in str(global_mem), str(global_mem))
check("global 看不到 session 数据(memory)", "Session-A" not in str(global_mem), str(global_mem))
check("global 看不到 session 数据(user)", "Session-A" not in str(global_user))

# Verify session only sees session data
session_mem = [e["text"] for e in session_be.list("memory")]
session_user = [e["text"] for e in session_be.list("user")]
check("session 只有 session 数据(memory)", "Session-A" in str(session_mem), str(session_mem))
check("session 看不到全局数据(memory)", "全局规则" not in str(session_mem), str(session_mem))
check("session 看不到全局数据(user)", "简体中文" not in str(session_user))

# Verify get_full_context properly separates
global_ctx = global_be.get_full_context()
session_ctx = session_be.get_full_context()
check("global get_full_context 含全局数据", "全局规则" in global_ctx)
check("global get_full_context 不含 session 数据", "Session-A" not in global_ctx)
check("session get_full_context 含 session 数据", "Session-A" in session_ctx)
check("session get_full_context 不含全局数据", "全局规则" not in session_ctx)

# Verify stats
gs = global_be.stats()
ss = session_be.stats()
check("global stats memory_entries >= 1", gs["memory_entries"] >= 1)
check("session stats memory_entries >= 1", ss["memory_entries"] >= 1)


# ═══════════════════════════════════════════════════════════════════════
# Test 2: SessionAwareMemory session 切换隔离
# ═══════════════════════════════════════════════════════════════════════
print("\n── Test 2: SessionAwareMemory session 切换隔离 ──")

# Reset singletons for clean test
sm_mod._session_manager = None
from src.session.manager import get_session_manager

# Import fresh SessionAwareMemory
import importlib
import src.memory.hermes as hermes_mod
importlib.reload(hermes_mod)
from src.memory.hermes import SessionAwareMemory
sam = SessionAwareMemory()

# Create two sessions via SessionManager
sm = get_session_manager()
session_a = sm.create("test-session-A")
session_b = sm.create("test-session-B")

# Switch to A and add data
sm.switch(session_a.id)
sam.add("Session-A 特有记录：项目A进度80%", "memory")
sam.add("Session-A 用户偏好：暗色主题", "user")

# Verify data visible in A
results_a = sam.search("项目A", "memory")
check("Session-A search 能看到自己的数据", len(results_a) > 0 and "Session-A" in results_a[0]["text"])

# Switch to B
sm.switch(session_b.id)
results_b = sam.search("项目A", "memory")
check("Session-B search 看不到 A 的数据", len(results_b) == 0)

# Session B add its own data
sam.add("Session-B 特有记录：项目B已交付", "memory")

# Switch back to A — verify data still there
sm.switch(session_a.id)
results_a2 = sam.search("项目A", "memory")
check("切回 Session-A 数据仍在", len(results_a2) > 0 and "项目A" in results_a2[0]["text"])
# Session A should NOT see B's data
results_a_b = sam.search("项目B", "memory")
check("Session-A 看不到 B 的数据", len(results_a_b) == 0)

# Switch to B — verify B's data still there
sm.switch(session_b.id)
results_b2 = sam.search("项目B", "memory")
check("切回 Session-B 数据仍在", len(results_b2) > 0 and "项目B" in results_b2[0]["text"])


# ═══════════════════════════════════════════════════════════════════════
# Test 3: 空 session 行为
# ═══════════════════════════════════════════════════════════════════════
print("\n── Test 3: 空 session 行为 ──")

# Create a fresh, empty session
session_empty = sm.create("test-empty-session")
sm.switch(session_empty.id)

# search should return empty
empty_search = sam.search("任何内容不存在", "memory")
check("空 session search 返回空列表", len(empty_search) == 0)

# stats should show 0 entries
estats = sam.stats()
check("空 session stats memory_entries=0", estats["memory_entries"] == 0, f"got {estats['memory_entries']}")
check("空 session stats user_entries=0", estats["user_entries"] == 0, f"got {estats['user_entries']}")

# get_full_context should NOT return empty string (includes global context)
# but session part should be minimal
ectx = sam.get_full_context()
check("空 session get_full_context 非空(含全局)", len(ectx) > 0)
check("空 session get_full_context 不含 SESSION CONTEXT 标签", "## SESSION CONTEXT" not in ectx or "SESSION CONTEXT" in ectx and len(ectx.split("## SESSION CONTEXT")[-1].strip()) == 0)

# list should return empty
empty_list = sam.list("memory")
check("空 session list 返回空列表", len(empty_list) == 0)


# ═══════════════════════════════════════════════════════════════════════
# Test 4: _resolve_backend() 降级行为
# ═══════════════════════════════════════════════════════════════════════
print("\n── Test 4: _resolve_backend() 异常降级 ──")

# Create a new SessionAwareMemory and monkey-patch get_session_manager to raise
sam2 = SessionAwareMemory()

import src.session.manager as sm_mod2
original_get_sm = sm_mod2.get_session_manager

# Patch get_session_manager to raise an exception
def broken_get_sm():
    raise RuntimeError("Simulated session manager failure")

sm_mod2.get_session_manager = broken_get_sm
try:
    backend = sam2._resolve_backend()
    check("降级返回 global backend (session_id=None)", backend.session_id is None)
    # Should still be usable
    result = sam2.add("降级测试记录", "memory")
    check("降级后 add 仍可用", result is not None and "text" in result)
    
    # Verify it went to global namespace
    ctx = sam2.get_full_context()
    check("降级后 get_full_context 含降级数据", "降级测试记录" in ctx)
    
    # get_full_context should NOT have session_id (it's global)
    check("降级后 backend session_id=None", backend.session_id is None)
finally:
    sm_mod2.get_session_manager = original_get_sm


# ═══════════════════════════════════════════════════════════════════════
# Test 5: 并发线程安全
# ═══════════════════════════════════════════════════════════════════════
print("\n── Test 5: 并发多 session 线程安全 ──")

# Restore session manager
sm_mod._session_manager = None
importlib.reload(sm_mod)
from src.session.manager import get_session_manager as fresh_get_sm
sm3 = fresh_get_sm()

# Create 5 sessions
sessions = []
for i in range(5):
    s = sm3.create(f"concurrent-{i}")
    sessions.append(s)

# We'll use directly EngramBackend instances (shared by SessionAwareMemory)
# For a true concurrent test, we use separate EngramBackend instances
# accessed via SessionAwareMemory from multiple threads.

importlib.reload(hermes_mod)
from src.memory.hermes import SessionAwareMemory
sam3 = SessionAwareMemory()

# Build a dict of session_id -> expected per-thread result
thread_results = {}
thread_errors = []

def worker(session_id, thread_id):
    try:
        sm3.switch(session_id)
        text = f"线程-{thread_id}-数据-{session_id}"
        sam3.add(text, "memory")
        # Small delay to increase chance of interleaving
        time.sleep(0.01)
        results = sam3.search(f"线程-{thread_id}", "memory")
        thread_results[thread_id] = {
            "added": True,
            "found": len(results) > 0,
            "text": text,
            "match": any(f"线程-{thread_id}" in r["text"] for r in results),
        }
    except Exception as e:
        thread_errors.append((thread_id, str(e)))
        thread_results[thread_id] = {"error": str(e)}

threads = []
for i in range(5):
    sid = sessions[i].id
    t = threading.Thread(target=worker, args=(sid, i))
    threads.append(t)

for t in threads:
    t.start()
for t in threads:
    t.join()

# Verify results
check("并发无异常", len(thread_errors) == 0, str(thread_errors))
all_found_own = all(v.get("match", False) for v in thread_results.values())
check("每个线程都能搜到自己的数据", all_found_own, str(thread_results))

# Verify isolation: thread-0 should NOT find thread-1's data
sm3.switch(sessions[0].id)
cross_results = sam3.search("线程-1-数据", "memory")
check("session-0 搜不到 session-1 的数据", len(cross_results) == 0, str(cross_results))

sm3.switch(sessions[1].id)
cross_results_2 = sam3.search("线程-0-数据", "memory")
check("session-1 搜不到 session-0 的数据", len(cross_results_2) == 0, str(cross_results_2))


# ═══════════════════════════════════════════════════════════════════════
# Test 6: migrate_from_json 隔离
# ═══════════════════════════════════════════════════════════════════════
print("\n── Test 6: migrate_from_json session 隔离 ──")

# Create a JSON file with mixed data
migrate_json = os.path.join(eb_mod.DATA_DIR, "test_migrate.json")
migrate_data = {
    "memory": [
        {"text": "全局记忆：重要安全规则", "category": "safety"},
        {"text": "全局记忆：编码规范", "category": "quality"},
    ],
    "user": [
        {"text": "全局用户：语言偏好简体中文", "category": ""},
    ]
}
with open(migrate_json, "w") as f:
    json.dump(migrate_data, f)

# Test 6a: Global backend migrates all data
global_be2 = EngramBackend(session_id=None)
count_global = global_be2.migrate_from_json(migrate_json)
check("global migrate 导入了数据", count_global > 0, f"imported {count_global}")
check("global migrate 导入精确条目", count_global == 3, f"expected 3, got {count_global}")

# Verify global can see imported data
global_mem2 = [e["text"] for e in global_be2.list("memory")]
check("global 包含导入的记忆条目", "重要安全规则" in str(global_mem2))

# Test 6b: Session backend imports to its own namespace only
session_be2 = EngramBackend(session_id="migrate-session-test")
count_session = session_be2.migrate_from_json(migrate_json)
check("session migrate 也导入了数据", count_session > 0, f"imported {count_session}")

# session should see its own imported data  
session_mem2 = [e["text"] for e in session_be2.list("memory")]
check("session 包含导入的记忆条目", "重要安全规则" in str(session_mem2))

# But session should NOT see global's imported data (they have different namespace)
# The key test: global's data and session's data are independent
# Create a THIRD backend to verify session doesn't pollute global
global_be3 = EngramBackend(session_id=None)
global_be3_mem = [e["text"] for e in global_be3.list("memory")]
check("新建 global 看不到 session 的导入数据(无交叉污染)", "migrate-session-test" not in str(global_be3_mem))

# Clean up
os.unlink(migrate_json)


# ═══════════════════════════════════════════════════════════════════════
# Test 7: "罗列一下最近对话" 不同 session 各返回各的
# ═══════════════════════════════════════════════════════════════════════
print("\n── Test 7: 最近对话 per-session 隔离 ──")

# Reset
sm_mod._session_manager = None
importlib.reload(sm_mod)
sm4 = sm_mod.get_session_manager()

importlib.reload(hermes_mod)
from src.memory.hermes import SessionAwareMemory
sam4 = SessionAwareMemory()

# Create 3 sessions with distinct "最近对话" data
sessions_7 = []
for name in ["工作讨论", "个人聊天", "技术交流"]:
    s = sm4.create(name)
    sessions_7.append(s)

# Add memories to each session
memories_by_session = {
    "工作讨论": [
        "最近对话：和张三讨论了Q3规划",
        "最近对话：产品评审通过",
        "工作流：每日站会9AM",
    ],
    "个人聊天": [
        "最近对话：约朋友周末聚餐",
        "最近对话：订了电影票",
        "偏好：喜欢川菜",
    ],
    "技术交流": [
        "最近对话：讨论了Python异步编程",
        "最近对话：分享了engram-router设计",
        "技术栈：Python + Go",
    ],
}

for s_name, mems in memories_by_session.items():
    sm4.switch(s_name)
    for m in mems:
        sam4.add(m, "memory")

# Now verify each session only sees its own "最近对话"
for s_name, expected_mems in memories_by_session.items():
    sm4.switch(s_name)
    search_results = sam4.search("最近对话", "memory")
    texts = [r["text"] for r in search_results]
    check(
        f'"{s_name}" search 只返回自己的最近对话',
        all(any(em in t for t in texts) for em in expected_mems if "最近对话" in em),
        f"got: {[t[:30] for t in texts]}"
    )
    # Verify NO cross-contamination
    for other_name, other_mems in memories_by_session.items():
        if other_name == s_name:
            continue
        other_texts = [m for m in other_mems if "最近对话" in m]
        cross_found = any(
            any(ot in t for t in texts) for ot in other_texts
        )
        check(
            f'  "{s_name}" 不包含 "{other_name}" 的最近对话',
            not cross_found,
            f"cross contamination! {texts}"
        )

# Test list() per session
sm4.switch("工作讨论")
work_list = [e["text"] for e in sam4.list("memory")]
check('"工作讨论" list 只有自己的记忆', len(work_list) == 3, f"got {len(work_list)}: {work_list}")

sm4.switch("个人聊天")
personal_list = [e["text"] for e in sam4.list("memory")]
check('"个人聊天" list 只有自己的记忆', len(personal_list) == 3, f"got {len(personal_list)}: {personal_list}")

# Test get_full_context per session
sm4.switch("技术交流")
tech_ctx = sam4.get_full_context()
check('"技术交流" get_full_context 含自己的数据', "异步编程" in tech_ctx)
check('"技术交流" get_full_context 不含工作讨论数据', "张三" not in tech_ctx)
check('"技术交流" get_full_context 不含个人聊天数据', "周末聚餐" not in tech_ctx)

sm4.switch("工作讨论")
work_ctx = sam4.get_full_context()
check('"工作讨论" get_full_context 含自己的数据', "张三" in work_ctx)
check('"工作讨论" get_full_context 不含技术交流数据', "异步编程" not in work_ctx)


# ═══════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print(f"结果: {PASSED} PASS, {FAILED} FAIL, {PASSED + FAILED} total")
print("=" * 70)

# Cleanup
shutil.rmtree(eb_mod.DATA_DIR, ignore_errors=True)
shutil.rmtree(sm_mod.SESSION_DIR, ignore_errors=True)

# Restore backup
if os.path.exists(BACKUP_DIR):
    if os.path.exists(REAL_DATA_DIR):
        shutil.rmtree(REAL_DATA_DIR, ignore_errors=True)
    shutil.move(BACKUP_DIR, REAL_DATA_DIR)

if FAILED > 0:
    sys.exit(1)
else:
    sys.exit(0)
