#!/usr/bin/env python3
"""接入件的真实契约测试 —— 不需要 Django，直接打活的 ai-company 服务。

为什么这么测：views.py 只是薄代理，真正要对齐的是 **HTTP 契约**。
本脚本用与 client.py 完全相同的代码路径打真实后端，覆盖：
    健康检查 / 建会话 / 发消息 / 拉历史 / 列会话 / 删会话 / 跨用户越权
跑法：先起服务（./goudan-api），然后
    python3 integration/test_django_client.py
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai_company.client import AiCompanyClient, AiCompanyError  # noqa: E402

BASE = os.environ.get("AI_COMPANY_TEST_BASE", "http://127.0.0.1:8020")
c = AiCompanyClient(BASE, timeout=120)

FAIL = []
CREATED = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


suffix = uuid.uuid4().hex[:8]
U1, U2 = "itest-admin:101", "itest-admin:102"   # 模拟两个管理端用户

# 1. 健康
info = c.health()
check("健康检查可达", info.get("status") == "ok")
check("健康检查回显 model/provider",
      bool(info.get("model")) and bool(info.get("provider")),
      f"{info.get('model')} / {info.get('provider')}")

# 2. U1 建会话 + 说话（带上只有 U1 知道的秘密）
s1 = c.create_conversation(U1, f"契约测试-{suffix}")
CREATED.append((U1, s1["conversation_id"]))
check("建会话返回 conversation_id", bool(s1.get("conversation_id")))

r1 = c.chat(U1, "请只回一句：收到。", conversation_id=s1["conversation_id"])
check("发消息拿到回复", bool(r1.get("reply")), f"{str(r1.get('reply'))[:40]}")
check("回复带回 conversation_id", r1.get("conversation_id") == s1["conversation_id"])

r2 = c.chat(U1, f"记住暗号 ITEST{suffix}。只回一句：记住了。")
CREATED.append((U1, r2["conversation_id"]))
check("省略 conversation_id 时自动新建会话",
      r2.get("conversation_id") and r2["conversation_id"] != s1["conversation_id"])

# 3. U1 历史里能看到自己的两段
h = c.history(U1, s1["conversation_id"])
check("U1 能读自己会话历史", len(h.get("turns", [])) >= 1, f"{len(h.get('turns', []))} 轮")

lst = c.list_conversations(U1)
ids = {x["conversation_id"] for x in lst["conversations"]}
check("U1 会话列表含自己的两个会话",
      s1["conversation_id"] in ids and r2["conversation_id"] in ids)

# 4. U2 看不到、也进不去 U1 的会话
lst2 = c.list_conversations(U2)
ids2 = {x["conversation_id"] for x in lst2["conversations"]}
check("U2 列表不含 U1 的会话", not (ids & ids2), f"U2={len(ids2)} 个")

for name, fn, want in (
    ("U2 读 U1 历史被拒", lambda: c.history(U2, s1["conversation_id"]), 403),
    ("U2 往 U1 会话发言被拒",
     lambda: c.chat(U2, "越权", conversation_id=s1["conversation_id"]), 403),
    ("U2 删 U1 会话被拒", lambda: c.delete_conversation(U2, s1["conversation_id"]), (403, 404)),
):
    try:
        fn()
        check(name, False, "居然成功了")
    except AiCompanyError as e:
        want_set = want if isinstance(want, tuple) else (want,)
        check(name, e.status in want_set, f"status={e.status}")

# 5. 删除 + 幂等
d = c.delete_conversation(U1, s1["conversation_id"])
check("U1 删自己会话成功", d.get("deleted") == s1["conversation_id"])
CREATED.remove((U1, s1["conversation_id"]))
try:
    c.history(U1, s1["conversation_id"])
    check("删掉后读历史应 404", False, "还能读到")
except AiCompanyError as e:
    check("删掉后读历史应 404", e.status == 404, f"status={e.status}")

# 6. 清理
print("\n=== 清理 ===")
for uid, cid in CREATED:
    try:
        c.delete_conversation(uid, cid)
        print(f"    delete {cid} ok")
    except AiCompanyError as e:
        print(f"    delete {cid} -> {e.status}")

print("\n" + "=" * 46)
if FAIL:
    print(f"结果: {len(FAIL)} 项失败 -> {FAIL}")
    sys.exit(1)
print("结果: 全部通过 ✅")
