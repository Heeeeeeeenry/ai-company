"""Intent Router — V5 Architecture P0.1

Two-layer intent classification:
  Layer 1: Keyword fast-path (0 LLM cost)
  Layer 2: LLM classification via deepseek-chat

Supports 13 intent types:
  COMMAND, SEARCH, RESEARCH, VISION, SOCIAL,
  MEMORY, CODING, CODE_REVIEW, CREATIVE, SYSTEM,
  FILE, AUTOMATION, GENERAL_CHAT
"""

from src.intent.router import (
    IntentRouter,
    IntentResult,
    classify_task,
    ALL_INTENTS,
    INTENT_TO_TASK_TYPE,
    KEYWORD_RULES,
)

__all__ = [
    "IntentRouter",
    "IntentResult",
    "classify_task",
    "ALL_INTENTS",
    "INTENT_TO_TASK_TYPE",
    "KEYWORD_RULES",
]
