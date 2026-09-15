"""Runtime model configuration for ai-company.

Provides a Hermes-like model switch layer backed by a small JSON file so the
interactive CLI can switch OneAPI models without restarting or editing shell env.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(os.getenv("AI_COMPANY_MODEL_CONFIG", "~/.ai-company/model_config.json")).expanduser()
DEFAULT_BASE_URL = "https://oneapi-comate.baidu-int.com/v1"
DEFAULT_MODEL = "DeepSeek-V4-Flash"
DEFAULT_PROVIDER = "oneapi"
STALE_DEEPSEEK_MODELS = {"deepseek-chat"}

# ─── 端点识别 ──────────────────────────────────────────────────────────
# ai-company 可以走两条完全不同的 DeepSeek 线路：
#   1. oneapi  — 公司内网 OneAPI 代理，模型名形如 DeepSeek-V4-Flash
#   2. deepseek— DeepSeek 官方 API，模型名形如 deepseek-chat / deepseek-reasoner
# 两者的凭证与模型名不通用，因此必须按端点选择 key，不能拿一把 key 通吃。
ONEAPI_HOST_MARKERS = ("oneapi-comate.baidu-int.com",)
OFFICIAL_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


def is_oneapi_endpoint(base_url: str) -> bool:
    """base_url 是否指向内网 OneAPI 代理。"""
    b = (base_url or "").lower()
    return any(m in b for m in ONEAPI_HOST_MARKERS)


def resolve_api_key(base_url: str, provider: str = "") -> str:
    """按端点返回对应的凭证，避免"官方端点 + 内网 key"这种必然 401 的组合。

    * OneAPI 端点            → ONEAPI_API_KEY（回退 DEEPSEEK_API_KEY）
    * 其他（DeepSeek 官方等） → DEEPSEEK_API_KEY（回退 ONEAPI_API_KEY）
    """
    oneapi = get_oneapi_key()
    deepseek = get_deepseek_key()
    if is_oneapi_endpoint(base_url):
        return oneapi or deepseek
    return deepseek or oneapi


@dataclass
class RuntimeModelConfig:
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL


def _read_json(path: Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        return {}
    return {}


def load_runtime_model(path: Path = CONFIG_PATH) -> RuntimeModelConfig:
    data = _read_json(path)
    provider = str(
        data.get("provider")
        or os.getenv("AI_COMPANY_MODEL_PROVIDER")
        or os.getenv("AI_COMPANY_DEEPSEEK_PROVIDER")
        or DEFAULT_PROVIDER
    )
    model = str(
        data.get("model")
        or os.getenv("AI_COMPANY_MODEL")
        or os.getenv("AI_COMPANY_DEEPSEEK_MODEL")
        or os.getenv("ONEAPI_MODEL")
        or os.getenv("ENGRAM_LLM_MODEL")
        or DEFAULT_MODEL
    )
    base_url = str(
        data.get("base_url")
        or os.getenv("AI_COMPANY_MODEL_BASE_URL")
        or os.getenv("AI_COMPANY_DEEPSEEK_BASE_URL")
        or os.getenv("ONEAPI_BASE_URL")
        or os.getenv("ENGRA_LLM_BASE_URL")
        or ""
    )
    # provider=deepseek 且没显式给 base_url 时，默认指向 DeepSeek 官方 API，
    # 而不是默默继承内网代理地址（否则会拿官方 key 打内网端点，必然 401）。
    if not base_url:
        base_url = OFFICIAL_DEEPSEEK_BASE_URL if provider == "deepseek" else DEFAULT_BASE_URL

    # deepseek-chat 只是内网 OneAPI 上的遗留名（官方侧它是合法模型名）。
    # 因此仅在 OneAPI 端点上做重定向，走官方 API 时原样保留。
    if model.strip().lower() in STALE_DEEPSEEK_MODELS and is_oneapi_endpoint(base_url):
        model = DEFAULT_MODEL
        provider = DEFAULT_PROVIDER
    # NOTE: deepseek-v4-pro is a live, working model on the OneAPI proxy.
    # Do NOT force-replace it with DEFAULT_MODEL — that made every agent
    # loop hang (gpt-5.5 chat times out). Use the configured model as-is.
    return RuntimeModelConfig(provider=provider, model=model, base_url=base_url.rstrip("/"))


def save_runtime_model(model: str, provider: str = DEFAULT_PROVIDER, base_url: str = DEFAULT_BASE_URL,
                       path: Path = CONFIG_PATH) -> RuntimeModelConfig:
    cfg = RuntimeModelConfig(provider=provider, model=model, base_url=base_url.rstrip("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({
        "provider": cfg.provider,
        "model": cfg.model,
        "base_url": cfg.base_url,
    }, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)
    return cfg


def get_oneapi_key() -> str:
    """内网 OneAPI 代理凭证。"""
    return (
        os.getenv("AI_COMPANY_ONEAPI_API_KEY")
        or os.getenv("ONEAPI_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or ""
    )


def get_deepseek_key() -> str:
    """DeepSeek 自有凭证（官方 API，或部署方单独申请的一把 key）。

    AI_COMPANY_DEEPSEEK_API_KEY 优先级最高，让 ai-company 拥有独立于
    Hermes / 其他进程的凭证，便于单独计费、单独轮换、单独限流。
    """
    return (
        os.getenv("AI_COMPANY_DEEPSEEK_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or ""
    )


def list_oneapi_models(base_url: str | None = None, api_key: str | None = None, timeout: int = 30) -> list[str]:
    base = (base_url or load_runtime_model().base_url).rstrip("/")
    key = api_key or resolve_api_key(base)
    if not key:
        raise RuntimeError("ONEAPI_API_KEY/DEEPSEEK_API_KEY is not set")
    req = urllib.request.Request(
        f"{base}/models",
        headers={"Authorization": f"Bearer {key}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    ids: list[str] = []
    for item in payload.get("data", []):
        if isinstance(item, dict):
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id:
                ids.append(model_id)
    return sorted(ids, key=lambda x: ("gpt" not in x.lower(), x.lower()))


def validate_oneapi_model(model: str, base_url: str | None = None, api_key: str | None = None) -> bool:
    return model in list_oneapi_models(base_url=base_url, api_key=api_key)
