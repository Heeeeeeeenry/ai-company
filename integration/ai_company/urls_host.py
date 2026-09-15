"""ai-company 嵌入件：宿主友好的 URL 路由（管理端 /api/ai/ 前缀用）。

为什么要单独一份 urls.py？

民意智感中心管理端的 ``SessionAuthMiddleware`` 只对 ``/api/`` 开头的路径做
登录校验（见 ``api/middleware/session_auth.py`` 的 ``_requires_auth``）。
把嵌入件挂在 ``/api/ai/`` 下，就能白拿「登录校验 + 账号禁用校验 +
``request.session_user`` 注入」，不用自己再写一遍鉴权。

路由映射（挂载方式见宿主 ``backend_django/urls.py``）:

    path("api/ai/", include("ai_company.urls_host"))

端点：

    GET      /api/ai/health/                  后端探活
    GET/POST /api/ai/sessions/                会话列表 / 新建会话
    GET/DEL  /api/ai/sessions/<cid>/          会话历史 / 删除会话
    POST     /api/ai/chat/                    发消息（整段 JSON 返回）
    POST     /api/ai/chat/stream/             发消息（SSE 流式逐字返回）
    GET      /api/ai/files/<handle>/          下载 AI 导出的文件（本人限定）

注意：中间件会解析请求体找 ``order`` 做菜单级鉴权；本嵌入件的请求体
不含 ``order``，``enforce_request`` 会直接放行（没有 order 即不校验），
因此不需要往 ``api_access_rules`` 里加规则。

另有一组**不带 /api/ 前缀**的内网端点（AI 取数工具目录），见
``urls_internal.py`` —— 它们由共享 token 鉴权，不走中间件。
"""
from django.urls import path

from . import views

app_name = "ai_company_host"

urlpatterns = [
    path("health/", views.health, name="health"),
    path("sessions/", views.sessions, name="sessions"),
    path("sessions/<str:conversation_id>/", views.session_detail, name="session_detail"),
    path("chat/", views.chat, name="chat"),
    path("chat/stream/", views.chat_stream, name="chat_stream"),
    path("files/<str:handle>/", views.export_file, name="export_file"),
]
