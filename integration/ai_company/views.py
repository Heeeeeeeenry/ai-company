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

import asyncio
import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from functools import wraps

from django.conf import settings
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from .client import AiCompanyError
from .conf import conf, get_client, resolve_user_id


def maybe_csrf_exempt(view):
    """按宿主约定决定是否豁免 CSRF。

    默认不豁免（Django 原生行为）。当宿主整个 JSON API 面都是
    ``csrf_exempt``、前端又只靠 Cookie 认证、从不发 CSRF token 时
    （例如民意智感中心管理端的 ``/api/llm/``），在宿主 settings 里设
    ``AI_COMPANY_CSRF_EXEMPT = True`` 即可对齐，否则 POST 一律 403。
    """
    if getattr(settings, "AI_COMPANY_CSRF_EXEMPT", False):
        return csrf_exempt(view)
    return view


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

@maybe_csrf_exempt
@require_http_methods(["GET", "POST"])
@embedded
def sessions(request, uid):
    client = get_client()
    if request.method == "GET":
        return JsonResponse(client.list_conversations(uid))
    body = _json_body(request)
    created = client.create_conversation(uid, body.get("title") or "新对话")
    return JsonResponse(created, status=201)


@maybe_csrf_exempt
@require_http_methods(["GET", "DELETE"])
@embedded
def session_detail(request, uid, conversation_id):
    client = get_client()
    if request.method == "GET":
        return JsonResponse(client.history(uid, conversation_id))
    return JsonResponse(client.delete_conversation(uid, conversation_id))


# ─── 对话 ────────────────────────────────────────────────────────────

@maybe_csrf_exempt
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


# ─── 流式对话（衡水定制：逐字输出）──────────────────────────────────

@maybe_csrf_exempt
@require_http_methods(["POST"])
@embedded
def chat_stream(request, uid):
    """把 ai-company 的 SSE 原样转发给浏览器。

    要点：**先拿到上游状态码再开始流**。``open_chat_stream`` 在非 2xx 时直接
    抛错，所以我们能干净地返回 403/500 JSON，而不是吐一半的 200 流。
    """
    body = _json_body(request)
    message = (body.get("message") or "").strip()
    if not message:
        return JsonResponse({"detail": "message 不能为空"}, status=400)
    if len(message) > 8000:
        return JsonResponse({"detail": "message 过长（上限 8000 字）"}, status=400)

    try:
        upstream = get_client().open_chat_stream(
            user_id=uid,
            message=message,
            conversation_id=body.get("conversation_id") or None,
            title=body.get("title") or None,
        )
    except AiCompanyError as exc:
        status = exc.status if 400 <= exc.status < 600 else 502
        return JsonResponse({"detail": exc.detail}, status=status)

    async def _forward():
        """异步迭代器。

        Django 的 ASGI handler 遇到**同步**迭代器会打警告
        （"StreamingHttpResponse must consume synchronous iterators ..."）并丢到
        线程池里逐块取。这里显式 ``to_thread`` 取上游的阻塞块，既消掉警告，也把
        「谁在哪个线程读 socket」写清楚。部署走 uvicorn/asgi.py，见 start.sh。
        """
        it = iter(upstream)
        sentinel = object()

        def _next():
            try:
                return next(it)
            except StopIteration:
                return sentinel

        try:
            while True:
                raw = await asyncio.to_thread(_next)
                if raw is sentinel:
                    break
                yield raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        finally:
            try:
                await asyncio.to_thread(upstream.close)
            except Exception:  # noqa: BLE001 - 关流失败不影响已经发出的内容
                pass

    response = StreamingHttpResponse(_forward(), content_type="text/event-stream; charset=utf-8")
    # nginx 默认会缓冲整段响应，逐字效果会被吃掉 —— 显式关掉。
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


# ─── AI 受控取数工具层（内网反向通道）───────────────────────────────
#
# 衡水定制的硬要求：AI 只能通过**程序接口**拿「当前用户可见」的数据，不得
# 绕过程序直连数据库。因此 ai-company 侧没有任何库连接，它拿到的一切都是
# 这里返回的 JSON —— 而这里的一切又都来自宿主自己的 service 层。
#
# 方向：ai-company --(X-AI-Internal-Token + user_id)--> 宿主
#
# 这组视图挂在 /api/ **之外**（见 urls_internal.py），中间件不参与，所以
# request.session_user 不存在。身份由服务端**按 pk 重新加载**，body 里只有
# 「哪个用户」，没有任何可信的其它身份字段。

INTERNAL_TOKEN_HEADER = "X-AI-Internal-Token"
DOWNLOAD_TTL_SECONDS = 3600


def _internal_token() -> str:
    return str(getattr(settings, "AI_COMPANY_INTERNAL_TOKEN", "") or "")


def _check_internal_token(request) -> bool:
    expected = _internal_token()
    # 没配 token 就彻底关闭这条通道 —— 不开无鉴权的内网端点。
    if not expected:
        return False
    provided = request.headers.get(INTERNAL_TOKEN_HEADER) or ""
    return hmac.compare_digest(provided, expected)


def _uid_pk(user_id):
    """``admin:<pk>`` → 主键；不是本前缀的数字则 None。"""
    text = str(user_id or "").strip()
    prefix = f"{conf('AI_COMPANY_USER_PREFIX')}:"
    if not text.startswith(prefix):
        return None
    tail = text[len(prefix):]
    return int(tail) if tail.isdigit() else None


def _load_user_by_id(pk):
    """按主键加载业务层用户 dict（列与 ``auth.service.get_session_user`` 对齐）。

    走库是**为了解析身份**（AI 侧不知道、也不该知道用户是谁），不是取业务
    数据；信件数据一律由 host_tools 调宿主 service 层拿。
    """
    from api.common.db import execute_select_one

    return execute_select_one(
        """
        SELECT id, password_hash, name, nickname, police_number, phone,
               unit_id, is_active, available_menus, last_login
        FROM police_users
        WHERE id = %s AND is_active = 1
        LIMIT 1
        """,
        (pk,),
    )


def _sign_payload(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    sig = hmac.new(_internal_token().encode("utf-8"), raw.encode("ascii"),
                   hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def _verify_payload(handle: str) -> dict:
    raw, _, sig = str(handle or "").partition(".")
    if not raw or not sig:
        raise PermissionError("无效的下载链接")
    expect = hmac.new(_internal_token().encode("utf-8"), raw.encode("ascii"),
                      hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expect):
        raise PermissionError("无效的下载链接")
    try:
        payload = json.loads(base64.urlsafe_b64decode((raw + "=" * (-len(raw) % 4)).encode("ascii")))
    except Exception:  # noqa: BLE001 - 任何解包失败都视为无效句柄
        raise PermissionError("无效的下载链接") from None
    if int(payload.get("exp") or 0) < time.time():
        raise PermissionError("下载链接已过期，请让 AI 重新导出")
    return payload


@require_http_methods(["GET"])
def internal_tools_index(request):
    """工具目录：给 ai-company 的「这次有哪些工具可用」。"""
    if not _check_internal_token(request):
        return JsonResponse({"detail": "unauthorized"}, status=401)
    from . import host_tools

    return JsonResponse({"tools": host_tools.list_tools()})


@maybe_csrf_exempt
@require_http_methods(["POST"])
def internal_tools_run(request, tool_name):
    """执行一个受控取数工具。

    权限链路：body.user_id → 服务端重载用户 → host_tools 内部调宿主 service
    层（自带 ``letter_visibility_sql`` 口径）。ai-company 全程拿不到 SQL。
    """
    if not _check_internal_token(request):
        return JsonResponse({"detail": "unauthorized"}, status=401)

    body = _json_body(request)
    args = body.get("args") if isinstance(body.get("args"), dict) else {}

    pk = _uid_pk(body.get("user_id"))
    if not pk:
        return JsonResponse({"detail": "invalid user_id"}, status=400)
    user = _load_user_by_id(pk)
    if not user:
        return JsonResponse({"detail": "user not found or inactive"}, status=403)

    from . import host_tools

    try:
        envelope = host_tools.run_tool(user, tool_name, args)
    except host_tools.ToolError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    except PermissionError as exc:
        return JsonResponse({"detail": str(exc)}, status=403)

    # 导出类工具：把内部句柄换成本用户可以下载的 URL。
    if tool_name == "export_letters" and isinstance(envelope.get("data"), dict):
        data = envelope["data"]
        token = _sign_payload({"u": pk, "a": args, "exp": int(time.time()) + DOWNLOAD_TTL_SECONDS})
        data["download"] = {
            "url": f"/api/ai/files/{token}/",
            "filename": data.get("filename") or "信件导出.csv",
        }
    return JsonResponse(envelope)


@require_http_methods(["GET"])
def export_file(request, handle):
    """下载 AI 导出的文件。

    双重校验：① 句柄签名 + 有效期；② 句柄里的 ``u`` 必须等于当前登录用户。
    文件内容由宿主**重新生成**（复用 letter 模块的 export_letters_csv），
    所以即使句柄被转发出去，别人也拿不到不属于自己的数据。
    """
    try:
        uid = resolve_user_id(request)
    except PermissionError as exc:
        return JsonResponse({"detail": str(exc)}, status=403)

    try:
        payload = _verify_payload(handle)
    except PermissionError as exc:
        return JsonResponse({"detail": str(exc)}, status=403)

    pk = _uid_pk(uid)
    if not pk or int(payload.get("u") or 0) != int(pk):
        return JsonResponse({"detail": "无权下载该文件"}, status=403)

    user = _load_user_by_id(pk)
    if not user:
        return JsonResponse({"detail": "账号不可用"}, status=403)

    from api.modules.letter.service import export_letters_csv

    try:
        filename, csv_text = export_letters_csv(user, payload.get("a") or {})
    except (PermissionError, ValueError) as exc:
        return JsonResponse({"detail": str(exc)}, status=403)

    response = HttpResponse(csv_text.encode("utf-8-sig"), content_type="text/csv; charset=utf-8")
    quoted = urllib.parse.quote(str(filename or "信件导出.csv"))
    response["Content-Disposition"] = f"attachment; filename*=UTF-8''{quoted}"
    return response
