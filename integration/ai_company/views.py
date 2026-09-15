"""ai-company 嵌入件：Django 代理视图。

职责边界（一句话）：**只做"把宿主的登录态翻译成 user_id，再转发给
ai-company"这一件事**，绝不自己存会话、也绝不接受前端传来的 user_id。

四个 JSON 端点（前缀由你的 urls.py 决定，见 README）：

    GET    api/sessions/                     会话列表
    POST   api/sessions/                     新建会话
    GET    api/sessions/<cid>/               会话历史
    DELETE api/sessions/<cid>/               删除会话
    POST   api/chat/                        发消息

外加一个 page/ 视图，渲染自带的对话页面（也可只把模板嵌到已有页面里）。
"""
from __future__ import annotations

import json
from functools import wraps

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from .client import AiCompanyError
from .conf import get_client, resolve_user_id


def _json_body(request) -> dict:
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def embedded(view):
    """统一处理身份解析 + 后端异常 → JSON 错误。

    视图函数签名拿到的是 ``(request, uid, *args)``；uid 永远来自服务端推导。
    """

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            uid = resolve_user_id(request)
        except PermissionError as exc:
            return JsonResponse({"detail": str(exc)}, status=403)
        try:
            return view(request, uid, *args, **kwargs)
        except AiCompanyError as exc:
            status = exc.status if 400 <= exc.status < 600 else 502
            return JsonResponse({"detail": exc.detail}, status=status)

    return wrapper


# ─── 页面 ────────────────────────────────────────────────────────────

@ensure_csrf_cookie
def page(request):
    """渲染自带对话页。整页可用，也可以只 {% include %} 其中的片段。"""
    return render(request, "ai_company/chat.html")


# ─── 会话 ────────────────────────────────────────────────────────────

@require_http_methods(["GET", "POST"])
@embedded
def sessions(request, uid):
    client = get_client()
    if request.method == "GET":
        return JsonResponse(client.list_conversations(uid))
    body = _json_body(request)
    created = client.create_conversation(uid, body.get("title") or "新对话")
    return JsonResponse(created, status=201)


@require_http_methods(["GET", "DELETE"])
@embedded
def session_detail(request, uid, conversation_id):
    client = get_client()
    if request.method == "GET":
        return JsonResponse(client.history(uid, conversation_id))
    return JsonResponse(client.delete_conversation(uid, conversation_id))


# ─── 对话 ────────────────────────────────────────────────────────────

@require_http_methods(["POST"])
@embedded
def chat(request, uid):
    body = _json_body(request)
    message = (body.get("message") or "").strip()
    if not message:
        return JsonResponse({"detail": "message 不能为空"}, status=400)
    if len(message) > 8000:
        return JsonResponse({"detail": "message 过长（上限 8000 字）"}, status=400)
    reply = get_client().chat(
        user_id=uid,
        message=message,
        conversation_id=body.get("conversation_id") or None,
        title=body.get("title") or None,
    )
    return JsonResponse(reply)


# ─── 健康检查（给运维用，不暴露密钥）────────────────────────────────

@require_http_methods(["GET"])
def health(request):
    try:
        info = get_client().health()
    except AiCompanyError as exc:
        return JsonResponse({"ok": False, "detail": exc.detail}, status=503)
    return JsonResponse({"ok": True, **info})
