#!/usr/bin/env python3
"""真实 HTTP 端到端隔离测试：多用户 × 多会话。

不依赖 requests，只用 stdlib。直接打 127.0.0.1:8020。
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8020"
FAIL = []


def call(method, path, body=None, timeout=180):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}
    except Exception as e:
        return -1, {"error": str(e)}


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAIL.append(name)
    print(f"[{tag}] {name}{('  ' + detail) if detail else ''}", flush=True)


def chat(user_id, message, conversation_id=None):
    t0 = time.time()
    status, payload = call("POST", "/ai/chat", {
        "user_id": user_id, "message": message,
        **({"conversation_id": conversation_id} if conversation_id else {}),
    })
    dt = time.time() - t0
    return status, payload, dt


print("=== 0. health ===")
s, p = call("GET", "/ai/health", timeout=10)
check("health 200", s == 200, f"status={s} body={p}")
check("health 报出模型配置", s == 200 and "model" in json.dumps(p), f"body={p}")

stamp = str(int(time.time()))[-6:]
UA, UB = f"test-userA-{stamp}", f"test-userB-{stamp}"

print("\n=== 1. 用户A 建第一个会话并植入事实 ===")
s, p, dt = chat(UA, "我叫李恒，请记住这个名字。只回一句：好的。")
check("A 首次 chat 200", s == 200, f"status={s} body={str(p)[:200]}  {dt:.1f}s")
A1 = p.get("conversation_id")
check("A 自动创建会话 id", bool(A1), f"A1={A1}")
A1_reply = p.get("reply", "")
print(f"    A1 回复: {A1_reply[:120]}")

print("\n=== 2. 用户A 同会话追问（应有上下文） ===")
s, p, dt = chat(UA, "我叫什么名字？", A1)
check("A 同会话追问 200", s == 200, f"status={s}  {dt:.1f}s")
r = p.get("reply", "")
check("A1 记住 李恒", "李恒" in r, f"reply={r[:160]}")

print("\n=== 3. 用户B 新会话问同名（应隔离，不知道） ===")
s, p, dt = chat(UB, "我叫什么名字？")
check("B 首次 chat 200", s == 200, f"status={s}  {dt:.1f}s")
B1 = p.get("conversation_id")
rb = p.get("reply", "")
check("B 不知道李恒（跨用户隔离）", "李恒" not in rb, f"reply={rb[:200]}")

print("\n=== 4. 用户A 再开新会话问同名（应隔离，不知道） ===")
s, p, dt = chat(UA, "我叫什么名字？")
check("A 第二会话 chat 200", s == 200, f"status={s}  {dt:.1f}s")
A2 = p.get("conversation_id")
ra2 = p.get("reply", "")
check("A2 != A1（多会话）", A2 and A2 != A1, f"A1={A1} A2={A2}")
check("A 新会话不知道李恒（会话间隔离）", "李恒" not in ra2, f"reply={ra2[:200]}")

print("\n=== 5. 会话列表 ===")
s, p = call("GET", f"/ai/conversations?user_id={UA}", timeout=30)
convs = [c["conversation_id"] for c in p.get("conversations", [])] if s == 200 else []
check("A 列表 200", s == 200, f"status={s}")
check("A 列表含 2 个会话", len(convs) >= 2, f"convs={convs}")
s, p = call("GET", f"/ai/conversations?user_id={UB}", timeout=30)
convs_b = [c["conversation_id"] for c in p.get("conversations", [])] if s == 200 else []
check("B 列表不含 A 的会话", A1 not in convs_b and A2 not in convs_b, f"B convs={convs_b}")

print("\n=== 6. 跨用户越权 ===")
s, p = call("GET", f"/ai/conversations/{A1}/history?user_id={UB}", timeout=30)
check("B 读 A 会话历史 -> 403", s == 403, f"status={s} body={str(p)[:120]}")
s, p = call("POST", "/ai/chat", {"user_id": UB, "conversation_id": A1, "message": "偷看一下"})
check("B 往 A 会话发消息 -> 403", s == 403, f"status={s} body={str(p)[:120]}")
s, p = call("DELETE", f"/ai/conversations/{A1}?user_id={UB}", timeout=30)
check("B 删 A 会话 -> 403/404", s in (403, 404), f"status={s} body={str(p)[:120]}")

print("\n=== 7. 历史持久化 ===")
s, p = call("GET", f"/ai/conversations/{A1}/history?user_id={UA}", timeout=30)
hist = p.get("history") or p.get("messages") or p
check("A 能读自己会话历史", s == 200, f"status={s}")
check("A1 历史含植入的事实", "李恒" in json.dumps(hist, ensure_ascii=False),
      f"history={json.dumps(hist, ensure_ascii=False)[:200]}")

print("\n=== 清理测试数据 ===")
for uid, cid in ((UA, A1), (UA, A2), (UB, B1)):
    if cid:
        s, _ = call("DELETE", f"/ai/conversations/{cid}?user_id={uid}", timeout=30)
        print(f"    delete {cid[:12]}... -> {s}")

print("\n" + "=" * 50)
if FAIL:
    print(f"结果: {len(FAIL)} 项失败 -> {FAIL}")
    sys.exit(1)
print("结果: 全部通过 ✅")
