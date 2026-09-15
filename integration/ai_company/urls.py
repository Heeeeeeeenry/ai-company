"""ai-company 嵌入件：URL 路由。

在宿主项目的 urls.py 里挂一行即可（详见 README）：

    path("ai/", include("ai_company.urls")),
"""
from django.urls import path

from . import views

app_name = "ai_company"

urlpatterns = [
    path("page/", views.page, name="page"),
    path("api/health/", views.health, name="health"),
    path("api/sessions/", views.sessions, name="sessions"),
    path("api/sessions/<str:conversation_id>/", views.session_detail, name="session_detail"),
    path("api/chat/", views.chat, name="chat"),
]
