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
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_PROVIDER = "oneapi"
STALE_DEEPSEEK_MODELS = {"deepseek-chat", "deepseek-v4-pro"}


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
    provider = str(data.get("provider") or os.getenv("AI_COMPANY_MODEL_PROVIDER") or DEFAULT_PROVIDER)
    model = str(data.get("model") or os.getenv("ONEAPI_MODEL") or os.getenv("ENGRAM_LLM_MODEL") or DEFAULT_MODEL)
    base_url = str(
        data.get("base_url")
        or os.getenv("ONEAPI_BASE_URL")
        or os.getenv("ENGRA_LLM_BASE_URL")
        or DEFAULT_BASE_URL
    )
    if provider == "deepseek" and model.strip().lower() in STALE_DEEPSEEK_MODELS:
        provider = DEFAULT_PROVIDER
        model = DEFAULT_MODEL
    if model.strip().lower() == "deepseek-v4-pro":
        model = DEFAULT_MODEL
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
    return os.getenv("ONEAPI_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or ""


def list_oneapi_models(base_url: str | None = None, api_key: str | None = None, timeout: int = 30) -> list[str]:
    base = (base_url or load_runtime_model().base_url).rstrip("/")
    key = api_key or get_oneapi_key()
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
