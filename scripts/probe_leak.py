#!/usr/bin/env python3
"""定位跨会话泄漏载体：用唯一 token 打一次，然后全盘 grep 找出被写进了哪里。"""
import json, subprocess, time, urllib.request, urllib.error, os, sys

BASE = "http://127.0.0.1:8020"
TOKEN = "ZXQPROBE" + str(int(time.time()))[-5:]


def chat(uid, msg, cid=None):
    body = {"user_id": uid, "message": msg}
    if cid:
        body["conversation_id"] = cid
    req = urllib.request.Request(BASE + "/ai/chat", data=json.dumps(body).encode(),
                                method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode(errors="replace")}


U = "probe-user-" + TOKEN
print(f"token={TOKEN}")

print("\n--- 1) 会话1 植入唯一事实 ---")
r1 = chat(U, f"请记住：我的暗号是 {TOKEN}。只回一句：记住了。")
C1 = r1.get("conversation_id")
print("C1 =", C1, "| reply:", (r1.get("reply") or r1)[:100])

print("\n--- 2) 立刻全盘 grep token 被写到哪 ---")
out = subprocess.run(["grep", "-rl", TOKEN, os.path.expanduser("~/.ai-company")],
                     capture_output=True, text=True)
print(out.stdout.strip() or "(无命中)")

print("\n--- 3) 新会话问暗号（同用户） ---")
r2 = chat(U, "我的暗号是什么？")
C2 = r2.get("conversation_id")
print("C2 =", C2, "| reply:", (r2.get("reply") or r2)[:200])

print("\n--- 4) 再 grep ---")
out = subprocess.run(["grep", "-rl", TOKEN, os.path.expanduser("~/.ai-company")],
                     capture_output=True, text=True)
print(out.stdout.strip() or "(无命中)")

# 清理
for cid in (C1, C2):
    if cid:
        req = urllib.request.Request(f"{BASE}/ai/conversations/{cid}?user_id={U}", method="DELETE")
        try:
            urllib.request.urlopen(req, timeout=30)
        except Exception:
            pass
print("\ncleaned")
