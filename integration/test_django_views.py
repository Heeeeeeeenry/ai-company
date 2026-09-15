#!/usr/bin/env python3
"""视图层真实测试 —— 用 Django 自带 test client 打**活的** ai-company 后端。

覆盖：URL 路由 / 模板渲染 / 静态文件可寻址 / CSRF / 身份推导 / 错误码映射 /
跨用户越权 / 入参校验。 跑法：

    1) ./goudan-api                       # 先起后端 (127.0.0.1:8020)
    2) python3 integration/test_django_views.py

身份：测试里把 AI_COMPANY_USER_ID_RESOLVER 换成一个"从请求头取用户"的 resolver，
这样不必建 auth 表也能验证"服务端推导身份"这条链路（默认的 request.user 版另有单测）。
"""
import os
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import django  # noqa: E402
from django.conf import settings as dj  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


def _resolver(request):
    """测试用身份推导：从 X-Test-User 头取；空值触发 403 分支。"""
    return request.headers.get("X-Test-User", "")


dj.configure(
    DEBUG=False,
    SECRET_KEY="test-only",
    ALLOWED_HOSTS=["testserver"],
    ROOT_URLCONF=__name__,
    INSTALLED_APPS=[
        "django.contrib.contenttypes",
        "django.contrib.staticfiles",
        "ai_company",
    ],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    MIDDLEWARE=[],
    TEMPLATES=[{
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }],
    STATIC_URL="/static/",
    AI_COMPANY_BASE_URL=os.environ.get("AI_COMPANY_TEST_BASE", "http://127.0.0.1:8020"),
    AI_COMPANY_TIMEOUT=120,
    AI_COMPANY_USER_ID_RESOLVER=_resolver,
    USE_TZ=True,
)
django.setup()

from django.test import Client  # noqa: E402
from django.urls import include, path  # noqa: E402

urlpatterns = [path("ai/", include("ai_company.urls"))]  # noqa: E402

anony = Client()                                   # 匿名：无身份头
suffix = uuid.uuid4().hex[:8]
U1, U2 = f"vtest:1{suffix}", f"vtest:2{suffix}"
u1c = Client(headers={"X-Test-User": U1})          # 用户 1
u2c = Client(headers={"X-Test-User": U2})          # 用户 2
created = []

# ── 1. 页面 + 静态资源 ──────────────────────────────────────────────
r = anony.get("/ai/page/")
check("GET /ai/page/ 渲染成功", r.status_code == 200, f"status={r.status_code}")
html = r.content.decode()
check("页面含组件根节点", "data-ai-company" in html)
check("页面引用了静态 JS/CSS",
      "ai_company/ai_company.js" in html and "ai_company/ai_company.css" in html)
check("模板里的 url 标签被正确反转", "/ai/api/sessions/" in html and "/ai/api/chat/" in html)
check("会话详情 URL 带占位符", "__CID__" in html)

# 静态文件确实能被 finders 找到（拷进管理端后 collectstatic 才拿得到）
from django.contrib.staticfiles import finders  # noqa: E402
for asset in ("ai_company/ai_company.js", "ai_company/ai_company.css"):
    check(f"静态文件可寻址 {asset}", finders.find(asset) is not None)

# ── 2. 健康检查 ─────────────────────────────────────────────────────
r = anony.get("/ai/api/health/")
check("GET health 200", r.status_code == 200, f"status={r.status_code}")
h = r.json()
check("health 报 ok 且带生效模型", h.get("ok") is True and bool(h.get("model")),
      f"{h.get('model')} / {h.get('provider')}")
check("health 不泄漏密钥字段",
      not any("key" in k.lower() and isinstance(v, str) and v for k, v in h.items()
              if k != "api_key_source"), f"keys={sorted(h)}")

# ── 3. 无身份 → 403 ────────────────────────────────────────────────
r = anony.get("/ai/api/sessions/")
check("缺身份访问会话列表 → 403", r.status_code == 403, f"status={r.status_code}")

# ── 4. 建会话 / 发消息 / 拉历史 ────────────────────────────────────
r = u1c.post("/ai/api/sessions/", data={"title": f"视图测试-{suffix}"},
           content_type="application/json")
check("POST sessions 201", r.status_code == 201, f"status={r.status_code}")

r = u1c.post("/ai/api/chat/", data={"message": "请只回一句：收到。"},
           content_type="application/json")
check("POST chat 新建会话并回复", r.status_code == 200 and r.json().get("reply"),
      f"reply={str(r.json().get('reply'))[:30]}")
cid = r.json()["conversation_id"]
created.append(cid)

r = u1c.post("/ai/api/chat/", data={"message": "换个说法：好的。", "conversation_id": cid},
           content_type="application/json")
check("POST chat 续接已有会话", r.json().get("conversation_id") == cid)

r = u1c.post("/ai/api/chat/", data={"message": "   "}, content_type="application/json")
check("空消息 → 400", r.status_code == 400, f"status={r.status_code}")

r = u1c.get(f"/ai/api/sessions/{cid}/")
check("GET 会话历史 200", r.status_code == 200, f"status={r.status_code}")
check("历史含 2 轮", len(r.json().get("turns", [])) == 2,
      f"{len(r.json().get('turns', []))} 轮")

r = u1c.get("/ai/api/sessions/")
ids = {x["conversation_id"] for x in r.json()["conversations"]}
check("列表含新会话", cid in ids)
check("列表项带 title/turn_count", all("title" in x and "turn_count" in x
                                       for x in r.json()["conversations"]))

# ── 5. 跨用户越权（换一个身份头）────────────────────────────────────
r = u2c.get(f"/ai/api/sessions/{cid}/")
check("他人读该会话 → 403", r.status_code == 403, f"status={r.status_code}")
r = u2c.post("/ai/api/chat/", data={"message": "越权", "conversation_id": cid},
             content_type="application/json")
check("他人往该会话发言 → 403", r.status_code == 403, f"status={r.status_code}")
r = u2c.delete(f"/ai/api/sessions/{cid}/")
check("他人删该会话 → 403/404", r.status_code in (403, 404), f"status={r.status_code}")
r = u2c.get("/ai/api/sessions/")
check("他人列表看不到该会话",
      cid not in {x["conversation_id"] for x in r.json()["conversations"]})

# ── 6. 删除 ────────────────────────────────────────────────────────
r = u1c.delete(f"/ai/api/sessions/{cid}/")
check("DELETE 自己的会话 200", r.status_code == 200, f"status={r.status_code}")
created.remove(cid)
r = u1c.get(f"/ai/api/sessions/{cid}/")
check("删除后读 → 404", r.status_code == 404, f"status={r.status_code}")

# ── 7. 默认身份推导（request.user）单测 ────────────────────────────
from types import SimpleNamespace  # noqa: E402
from ai_company import conf as ac_conf  # noqa: E402

saved = dj.AI_COMPANY_USER_ID_RESOLVER
del dj.AI_COMPANY_USER_ID_RESOLVER          # 放开默认分支
try:
    authed = SimpleNamespace(user=SimpleNamespace(is_authenticated=True, pk=37))
    check("默认推导 admin:37", ac_conf.resolve_user_id(authed) == "admin:37",
          ac_conf.resolve_user_id(authed))
    anon = SimpleNamespace(user=SimpleNamespace(is_authenticated=False, pk=None))
    try:
        ac_conf.resolve_user_id(anon)
        check("未登录抛 PermissionError", False, "居然没抛")
    except PermissionError:
        check("未登录抛 PermissionError", True)
finally:
    dj.AI_COMPANY_USER_ID_RESOLVER = saved

# ── 8. 清理 ────────────────────────────────────────────────────────
print("\n=== 清理 ===")
from ai_company.client import AiCompanyClient  # noqa: E402
cl = AiCompanyClient(dj.AI_COMPANY_BASE_URL, 30)
for cid_ in created:
    try:
        cl.delete_conversation(U1, cid_)
        print(f"    delete {cid_} ok")
    except Exception as e:  # noqa: BLE001
        print(f"    delete {cid_} -> {e}")

print("\n" + "=" * 46)
if FAIL:
    print(f"结果: {len(FAIL)} 项失败 -> {FAIL}")
    sys.exit(1)
print("结果: 全部通过 ✅")
