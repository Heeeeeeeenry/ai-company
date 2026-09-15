# -*- coding: utf-8 -*-
"""LLM factory — shared LLM creation and JSON extraction utilities.

Extracted from src/ceo/graph.py to break the dispatcher ↔ graph circular dependency.

Usage:
    from src.llm_factory import get_llm, extract_json

    llm = get_llm("ceo")
    data = extract_json(raw_text)
"""

import json
import re
import logging

from langchain_core.language_models import BaseChatModel

from src.config import config

logger = logging.getLogger("ai_company.llm")


def extract_json(text: str) -> dict:
    """Robust JSON extraction from LLM output with markdown fences."""
    # Try markdown code fence first
    fence_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    # Find outermost balanced braces
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                return json.loads(text[start:i + 1])
    # Fallback: simple find
    s = text.find("{")
    e = text.rfind("}")
    if s >= 0 and e > s:
        return json.loads(text[s:e + 1])
    raise ValueError("No valid JSON object found")


def get_llm(role: str = "ceo", model_tier: str = "") -> BaseChatModel:
    """Create LLM instance. DeepSeek uses OpenAI-compatible API.
    Includes token tracking via TokenTracker callback.

    Args:
        role: Role name for model config lookup (ceo, developer, researcher, etc.)
        model_tier: Optional tier override ("tiny"|"standard"|"premium").
                    When set, overrides the role-based model with the tier-configured model.
    """
    mc = config.get_model_for(role)
    from src.model_config import STALE_DEEPSEEK_MODELS, load_runtime_model
    runtime_model = load_runtime_model()

    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        raise ImportError("langchain-openai required. Run: pip install langchain-openai")

    # Token tracking callback (best-effort, non-blocking)
    from src.utils.token_tracker import get_token_tracker
    tracker = get_token_tracker()
    tracker.set_context(role=role, task_id=model_tier if model_tier else "default")
    callbacks = [tracker]

    # Timing callback (records LLM call duration)
    try:
        from src.utils.timing import TimingCallback
        callbacks.append(TimingCallback(role=role, model=mc.model))
    except ImportError:
        pass

    if mc.provider in {"oneapi", "deepseek"}:
        # Model tier routing: override model name based on tier config
        model_name = mc.model
        if model_tier:
            tier_model = config.get_model_for_tier(model_tier)
            if tier_model:
                model_name = tier_model

        # OneAPI is the configured internal OpenAI-compatible proxy. Older
        # env files may still say deepseek-chat/deepseek-v4-pro; map only
        # those stale model names to the current runtime model. Log at DEBUG
        # to avoid noisy warnings during normal execution.
        if model_name.strip().lower() in STALE_DEEPSEEK_MODELS:
            old_model = model_name
            model_name = runtime_model.model
            if old_model != model_name:
                logger.debug(
                    "Model redirect: %s → %s (stale model replaced with runtime config)",
                    old_model, model_name,
                )
        is_reasoner = "reasoner" in model_name.lower()
        _max_tokens_map = {
            "developer": 8192, "researcher": 8192, "marketer": 4096,
            "qa": 4096, "devops": 4096, "pm": 2048, "architect": 2048,
            "ceo": 2048, "review": 2048,
        }
        # 凭证按端点解析：内网 OneAPI 用 ONEAPI_API_KEY，DeepSeek 官方端点
        # 用 DEEPSEEK_API_KEY。拿错一把 key 会必然 401。
        from src.model_config import resolve_api_key
        api_key = resolve_api_key(runtime_model.base_url, mc.provider) or config.oneapi_api_key
        return ChatOpenAI(
            model=model_name,
            api_key=api_key,
            base_url=runtime_model.base_url,
            timeout=180 if is_reasoner else 60,
            max_retries=2,
            max_tokens=_max_tokens_map.get(role, 4096),
            callbacks=callbacks,
        )
    elif mc.provider == "openai":
        return ChatOpenAI(
            model=mc.model,
            api_key=config.openai_api_key,
            timeout=45,
            max_retries=1,
            callbacks=callbacks,
        )
    elif mc.provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
            return ChatAnthropic(model=mc.model, api_key=config.anthropic_api_key,
                                callbacks=callbacks)
        except ImportError:
            raise ImportError("langchain-anthropic required for Anthropic models")

    raise ValueError(f"Unknown provider: {mc.provider}")
