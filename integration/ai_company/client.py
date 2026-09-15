"""ai-company 嵌入件：Django 侧的 HTTP 客户端。

只依赖标准库（urllib），不引入 requests/httpx，方便挂到任何 Django 项目里。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class AiCompanyError(RuntimeError):
    """后端返回非 2xx 时抛出，``status`` 与 ``detail`` 原样带给调用方。"""

    def __init__(self, status: int, detail: str):
        super().__init__(f"ai-company HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class AiCompanyClient:
    """薄客户端：一个方法对应 ai-company 的一个 HTTP 端点。

    注意：本客户端**不接受**调用方指定 user_id 之外的任何越权参数；
    user_id 由 Django 视图从 ``request.user`` 推导后传入（见 views.py）。
    """

    def __init__(self, base_url: str, timeout: float = 120.0, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token or ""

    # ── 内部 ────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, params=None, body=None) -> dict:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8") or "{}"
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode("utf-8")
                detail = json.loads(raw).get("detail", raw)
            except Exception:
                detail = raw or e.reason
            raise AiCompanyError(e.code, str(detail)) from None
        except urllib.error.URLError as e:
            raise AiCompanyError(503, f"ai-company 不可达: {e.reason}") from None

    # ── 端点 ────────────────────────────────────────────────────────
    def health(self) -> dict:
        return self._request("GET", "/ai/health")

    def chat(self, user_id: str, message: str,
             conversation_id: str | None = None, title: str | None = None) -> dict:
        return self._request("POST", "/ai/chat", body={
            "user_id": user_id,
            "message": message,
            "conversation_id": conversation_id,
            "title": title,
        })

    def list_conversations(self, user_id: str) -> dict:
        return self._request("GET", "/ai/conversations", params={"user_id": user_id})

    def create_conversation(self, user_id: str, title: str = "新对话") -> dict:
        return self._request("POST", "/ai/conversations",
                             body={"user_id": user_id, "title": title})

    def history(self, user_id: str, conversation_id: str) -> dict:
        return self._request("GET", f"/ai/conversations/{conversation_id}/history",
                             params={"user_id": user_id})

    def delete_conversation(self, user_id: str, conversation_id: str) -> dict:
        return self._request("DELETE", f"/ai/conversations/{conversation_id}",
                             params={"user_id": user_id})

    # ── 流式对话（衡水定制：逐字输出）────────────────────────────────

    def open_chat_stream(self, user_id: str, message: str,
                         conversation_id: str | None = None,
                         title: str | None = None):
        """POST /ai/chat/stream，返回**未读完的流式响应对象**。

        与 ``chat()`` 的区别：这里不 ``read()`` 到底，而是把响应体交给调用方
        边读边转发（Django 侧再包一层 StreamingHttpResponse）。
        非 2xx 会在这里就抛 ``AiCompanyError`` —— 必须在开始流式输出*之前*
        拿到状态码，否则前端只能看到一半的 200。
        """
        url = f"{self.base_url}/ai/chat/stream"
        data = json.dumps({
            "user_id": user_id,
            "message": message,
            "conversation_id": conversation_id,
            "title": title,
        }).encode("utf-8")
        headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode("utf-8")
                detail = json.loads(raw).get("detail", raw)
            except Exception:
                detail = raw or e.reason
            raise AiCompanyError(e.code, str(detail)) from None
        except urllib.error.URLError as e:
            raise AiCompanyError(503, f"ai-company 不可达: {e.reason}") from None
