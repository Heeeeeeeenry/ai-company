"""ai-company 嵌入件：**内网**路由（AI 受控取数工具层用）。

为什么单独一份、且挂在 ``/api/`` 之外？

管理端的 ``SessionAuthMiddleware`` 只对 ``/api/`` 前缀做登录校验。这条通道的
调用方是 **ai-company 进程**（没有浏览器 cookie），因此不能走中间件；改由
共享 token（``AI_COMPANY_INTERNAL_TOKEN``）鉴权。

安全约束（改之前先读一遍）：

1. 只监听本机；宿主后端绑 ``0.0.0.0`` 时仍应确保该前缀不被公网反代暴露。
2. token 未配置 → 端点直接 401，不存在「没配就放行」。
3. body 里的 ``user_id`` 只是「哪个用户」，宿主会**自己按 pk 重新加载用户**
   并走权限中心；不接受任何其它身份字段。

宿主 ``urls.py`` 里挂载：

    path("ai-internal/", include("ai_company.urls_internal")),
"""
from django.urls import path

from . import views

app_name = "ai_company_internal"

urlpatterns = [
    path("tools/", views.internal_tools_index, name="tools_index"),
    path("tools/<str:tool_name>/", views.internal_tools_run, name="tools_run"),
]
