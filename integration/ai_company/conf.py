"""ai-company 嵌入件：配置读取 + 身份解析。

设计要点（安全相关，改动前先想清楚）：

1. **user_id 只能由服务端推导，绝不信前端。**  前端 JS 无权指定 user_id，
   否则任何人把 user_id 改成别人的就能读别人的会话。
2. **加接入端前缀。** 默认 ``admin:<pk>``。同一个 ai-company 实例未来可能同时
   服务市民端，两边的 pk 会撞号——前缀把命名空间分开。
3. **凭据不过期丢历史。** 历史挂在 conversation_id 上，登录态失效不影响历史。
"""
from __future__ import annotations

from django.conf import settings

from .client import AiCompanyClient

DEFAULTS = {
    # ai-company 后端地址（goudan-api 监听的地址，管理端同机就是 127.0.0.1）
    "AI_COMPANY_BASE_URL": "http://127.0.0.1:8020",
    # 单次对话最长等待（秒）。模型思考链较长，别设太小。
    "AI_COMPANY_TIMEOUT": 120.0,
    # 若 ai-company 前面挂了网关要鉴权，在这里配 token
    "AI_COMPANY_TOKEN": "",
    # user_id 命名空间前缀
    "AI_COMPANY_USER_PREFIX": "admin",
}


def conf(name: str):
    return getattr(settings, name, DEFAULTS[name])


def get_client() -> AiCompanyClient:
    return AiCompanyClient(
        base_url=conf("AI_COMPANY_BASE_URL"),
        timeout=float(conf("AI_COMPANY_TIMEOUT")),
        token=conf("AI_COMPANY_TOKEN"),
    )


def resolve_user_id(request) -> str:
    """由宿主登录态推导 ai-company 的 user_id。

    想自定义（比如用员工号而不是 pk）就在 settings 里给
    ``AI_COMPANY_USER_ID_RESOLVER`` 一个 callable，收到 request 返回字符串：

        def _uid(request):
            return f"admin:{request.user.username}"
        AI_COMPANY_USER_ID_RESOLVER = _uid
    """
    resolver = getattr(settings, "AI_COMPANY_USER_ID_RESOLVER", None)
    if resolver is not None:
        uid = str(resolver(request))
        if not uid:
            raise PermissionError("AI_COMPANY_USER_ID_RESOLVER 返回了空 user_id")
        return uid

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        raise PermissionError("未登录：ai-company 依赖宿主的登录态")
    return f"{conf('AI_COMPANY_USER_PREFIX')}:{user.pk}"
