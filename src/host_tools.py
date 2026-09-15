# -*- coding: utf-8 -*-
"""宿主「受控取数工具层」客户端（衡水定制）。

红线（改代码前先读）：

* ai-company **没有任何数据库连接**。所有业务数据只能通过本模块 → 宿主
  ``/ai-internal/tools/`` 拿到；宿主的每个工具内部再走它自己的 service 层
  与权限中心（``letter_visibility_sql``）。因此「AI 能看到什么」永远等于
  「这个登录用户在管理端界面上能看到什么」。
* 反向通道用共享 token（``AI_COMPANY_HOST_INTERNAL_TOKEN``）鉴权；**没配
  token 就是通道关闭**，此时 AI 不会声称自己查了数据，而是照常走普通对话。
* 传给宿主的只有 ``user_id``（``admin:<pk>``，由宿主自己翻译），AI 侧持有的
  任何身份字段都不被信任。

环境变量：

    AI_COMPANY_HOST_BASE_URL       宿主后端地址，默认 http://host.docker.internal:15173
                                   （容器里 127.0.0.1 是容器自己；compose 已加
                                   extra_hosts: host-gateway）
    AI_COMPANY_HOST_INTERNAL_TOKEN 与服务端 AI_COMPANY_INTERNAL_TOKEN 一致
    AI_COMPANY_HOST_TOOLS_TIMEOUT  单次工具调用超时（秒），默认 30
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("ai_company.host_tools")

ENV_BASE_URL = "AI_COMPANY_HOST_BASE_URL"
ENV_TOKEN = "AI_COMPANY_HOST_INTERNAL_TOKEN"
ENV_TIMEOUT = "AI_COMPANY_HOST_TOOLS_TIMEOUT"

DEFAULT_BASE_URL = "http://host.docker.internal:15173"
CATALOG_TTL_SECONDS = 300.0

_catalog_lock = threading.Lock()
_catalog_cache: list[dict] = []
_catalog_at: float = 0.0


class HostToolsError(RuntimeError):
    """宿主工具层不可用 / 调用失败。"""


def host_base_url() -> str:
    return (os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL).strip().rstrip("/")


def internal_token() -> str:
    return (os.environ.get(ENV_TOKEN) or "").strip()


def enabled() -> bool:
    """通道是否可用。token 缺失 = 关闭（绝不裸奔调用）。"""
    return bool(internal_token() and host_base_url())


def _timeout() -> float:
    try:
        return float(os.environ.get(ENV_TIMEOUT) or 30)
    except (TypeError, ValueError):
        return 30.0


def _headers() -> dict:
    return {
        "X-AI-Internal-Token": internal_token(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _http_json(method: str, path: str, payload: dict | None = None) -> dict:
    url = f"{host_base_url()}{path}"
    data = json.dumps(payload or {}).encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")
            detail = json.loads(body).get("detail") or body
        except Exception:  # noqa: BLE001
            detail = body or str(e.reason)
        raise HostToolsError(f"宿主工具层 {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise HostToolsError(f"宿主工具层不可达: {e.reason}") from None
    except (TimeoutError, OSError) as e:
        raise HostToolsError(f"宿主工具层调用失败: {e}") from None
    try:
        return json.loads(raw or "{}")
    except ValueError:
        raise HostToolsError("宿主工具层返回了非 JSON 内容") from None


def list_tools(force: bool = False) -> list[dict]:
    """工具目录（带 TTL 缓存，避免每轮对话都多打一次宿主）。"""
    global _catalog_cache, _catalog_at
    if not enabled():
        return []
    with _catalog_lock:
        if not force and _catalog_cache and (time.time() - _catalog_at) < CATALOG_TTL_SECONDS:
            return list(_catalog_cache)
    try:
        data = _http_json("GET", "/ai-internal/tools/")
        tools = data.get("tools") if isinstance(data, dict) else None
        tools = [t for t in (tools or []) if isinstance(t, dict) and t.get("name")]
    except HostToolsError as exc:
        logger.warning("取工具目录失败: %s", exc)
        return list(_catalog_cache)  # 有陈旧缓存就用，没有就空
    with _catalog_lock:
        _catalog_cache = tools
        _catalog_at = time.time()
    return list(tools)


def run_tool(user_id: str, name: str, args: dict | None = None) -> dict:
    """执行一个工具。args 一律经宿主白名单校验，非法参数宿主会返回 400。"""
    if not enabled():
        raise HostToolsError("宿主工具层未启用")
    body = {"user_id": user_id, "args": args or {}}
    return _http_json("POST", f"/ai-internal/tools/{urllib.parse.quote(str(name))}/", body)
