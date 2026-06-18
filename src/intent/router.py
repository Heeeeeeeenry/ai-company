"""Intent Router — 意图路由模块 (V5 Architecture P0.1)

Two-layer classification:
  Layer 1: Keyword fast-path (0 LLM cost, >90% coverage)
  Layer 2: LLM classification via deepseek-chat (ambiguous fallback)

Supports 13 intent types:
  COMMAND, SEARCH, RESEARCH, VISION, SOCIAL,
  MEMORY, CODING, CODE_REVIEW, CREATIVE, SYSTEM,
  FILE, AUTOMATION, GENERAL_CHAT

Usage:
    from src.intent.router import IntentRouter, classify_task

    router = IntentRouter()
    result = router.classify("查金价")
    # IntentResult(intent='SEARCH', confidence=0.95, ...)

    # Backward-compatible (returns old task_type string):
    task_type = classify_task("pwd")
    # "COMMAND_EXECUTION"
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("ai_company.intent")


# ─── Intent List ──────────────────────────────────────────────────

ALL_INTENTS = [
    "COMMAND",
    "SEARCH",
    "RESEARCH",
    "VISION",
    "SOCIAL",
    "MEMORY",
    "CODING",
    "CODE_REVIEW",
    "CREATIVE",
    "SYSTEM",
    "FILE",
    "AUTOMATION",
    "GENERAL_CHAT",
]


# ─── Data Classes ─────────────────────────────────────────────────

@dataclass
class IntentResult:
    """Intent classification result."""
    intent: str                    # COMMAND|SEARCH|RESEARCH|VISION|SOCIAL|MEMORY|CODING|SYSTEM|FILE|AUTOMATION|GENERAL_CHAT
    confidence: float              # 0.0 - 1.0
    params: dict = field(default_factory=dict)   # extracted parameters (e.g. {"command": "ls -la"})
    routing_hint: str = ""        # suggested agent name
    matched_by: str = "keyword"   # "keyword" or "llm"


# ─── Intent → old task_type mapping (backward compat) ─────────────

INTENT_TO_TASK_TYPE: dict[str, str] = {
    "COMMAND":      "COMMAND_EXECUTION",
    "SEARCH":       "SIMPLE_QUERY",
    "RESEARCH":     "SIMPLE_QUERY",
    "VISION":       "GENERAL",
    "SOCIAL":       "LOCAL_SYSTEM",
    "MEMORY":       "GENERAL",
    "CODING":       "DEVELOPMENT",
    "CODE_REVIEW":  "CODE_REVIEW",
    "CREATIVE":     "CREATIVE",
    "SYSTEM":       "LOCAL_SYSTEM",
    "FILE":         "DOCUMENT",
    "AUTOMATION":   "GENERAL",
    "GENERAL_CHAT": "GENERAL",
}


# ─── Keyword Rules (Layer 1, by priority) ─────────────────────────
# Each rule: (pattern, intent, confidence, routing_hint)
# Priority order is critical — first match wins.

KEYWORD_RULES: list[tuple[str, str, float, str]] = [
    # ── P0: COMMAND — shell command detection (HIGHEST priority) ──
    # Pure shell commands starting with known binaries
    (r'^\s*(pwd|ls|cat|cd|cp|mv|rm|mkdir|rmdir|chmod|chown|chgrp|df|du|top|htop|ps|kill|killall'
     r'|echo|printf|grep|egrep|fgrep|find|head|tail|wc|sort|uniq|diff|cmp|patch'
     r'|curl|wget|git|ssh|scp|ping|nslookup|dig|whoami|hostname|date|uptime'
     r'|env|export|source|which|tar|gzip|gunzip|zip|unzip|awk|sed|tr|cut'
     r'|tee|xargs|basename|dirname|readlink|realpath|stat|file|ln|touch|mount|umount'
     r'|python3?\s+[-cm]|node\s+-e|ruby\s+-e|perl\s+-e|bash\s+-c|sh\s+-c'
     r'|docker|kubectl|brew|apt|yum|pip|npm|yarn|cargo|go|rustc|make|cmake'
     r'|systemctl|launchctl|pgrep|pkill|osascript|say|sw_vers)\b',
     "COMMAND", 0.98, "SystemAgent"),

    # ── P1: SYSTEM — system ops (open apps, check processes, system control) ──
    (r'(打开|关闭|启动|停止|重启|检查|查看)\s*(微信|QQ|钉钉|浏览器|终端|Terminal|'
     r'Chrome|Safari|Firefox|VS\s*Code|VSCode|应用|软件|程序|进程|服务)',
     "SYSTEM", 0.90, "SystemAgent"),
    (r'(pgrep|pkill|osascript|applescript|launchctl|systemctl|brew\s+services)',
     "SYSTEM", 0.90, "SystemAgent"),

    # ── P2: SOCIAL — WeChat / messaging ──
    (r'(微信|WeChat|发消息|发送消息|聊天|回复|代聊|发微信|给小号|群聊|朋友圈'
     r'|消息|短信|私信|@某人|@\S+)',
     "SOCIAL", 0.92, "WechatAgent"),

    # ── P3: SEARCH — information lookup ──
    # NOTE: standalone 查 only matches at sentence-start or after
    # punctuation/whitespace to avoid false positives on compounds
    # like 检查/查看/调查. Other search keywords (搜/搜索/查询 etc.)
    # match freely as substrings.
    (r'((?:^|\s|[。，！？、])查|搜|搜索|查询|找一下|帮我查|帮我搜|帮我找'
     r'|什么是|怎么|为什么|谁|什么时候|哪里|多少|几'
     r'|价格|多少钱|天气|新闻|股价|股票|汇率|金价|黄金|油价|行情|最新|实时)',
     "SEARCH", 0.90, "ResearchAgent"),
    (r'^(how|what|when|where|who|why|which)\b',
     "SEARCH", 0.85, "ResearchAgent"),

    # ── P4: CODING — write/develop/fix code ──
    (r'(写代码|写程序|写脚本|开发|实现|修复|bug|fix|重构|refactor|测试|test'
     r'|写.*(?:api|函数|类|模块|组件|页面)|改.*(?:代码|程序|bug)|优化.*(?:代码|性能)'
     r'|部署|deploy|build|编译|compile'
     r'|\b(write|implement|create|build|develop|code)\b.*(?:code|function|class|module|api|程序|代码|脚本|app|application))',
     "CODING", 0.92, "CodingAgent"),

    # ── P4.1: CODE_REVIEW — review/audit code ──
    (r'(代码审查|代码审计|代码打分|code\s*review|review\s*code'
     r'|审计代码|检查代码|审查代码|review\s+this\s+(?:code|PR|pull|patch|diff))',
     "CODE_REVIEW", 0.90, "CodingAgent"),

    # ── P4.2: CREATIVE — copywriting/marketing/SEO ──
    (r'(文案|营销|seo|广告|推广|写文案|创意文案|creative|copywriting|marketing'
     r'|广告语|slogan|宣传|品牌|branding|内容营销|软文|公众号文章)',
     "CREATIVE", 0.88, "CreativeAgent"),

    # ── P5: FILE — file/document operations ──
    (r'(读取|查看|打开|显示|预览)\s*(文件|文档|pdf|PDF|报告|日志|log|txt|csv|json|yaml|xml)',
     "FILE", 0.88, "SystemAgent"),
    (r'(生成|创建|写出|保存|导出|制作)\s*(文件|文档|pdf|PDF|报告|周报|月报|日报|会议纪要|总结|笔记)',
     "FILE", 0.88, "SystemAgent"),

    # ── P6: MEMORY — memory operations ──
    (r'(记住|记下|回忆|之前|上次|上回|存储|记忆|忘记|笔记|备忘录'
     r'|说过|聊过|提过|告诉过|记录一下|帮我记)',
     "MEMORY", 0.88, "MemoryAgent"),

    # ── P7: AUTOMATION — batch/timed tasks ──
    (r'(批量|定时|自动化|自动|周期|定期|每天|每小时|每周|每月|计划任务|cron|schedule)',
     "AUTOMATION", 0.85, "SystemAgent"),

    # ── P8: VISION — vision/screenshot tasks ──
    (r'(截图|屏幕|识别|看图|图片|图像|照片|OCR|文字识别|视觉|vision|扫描)',
     "VISION", 0.85, "VisionAgent"),

    # ── P9: RESEARCH — deep investigation (keyword-triggered) ──
    (r'(研究|分析|调研|对比|竞品|深入|深度|research|analysis|compare|survey|trend'
     r'|论文|paper|文献)',
     "RESEARCH", 0.85, "ResearchAgent"),
]


# ─── IntentRouter ──────────────────────────────────────────────────

class IntentRouter:
    """Two-layer intent classifier.

    Layer 1: regex keyword fast-path, 0 LLM cost.
    Layer 2: LLM classification via deepseek-chat for ambiguous inputs.
    """

    def __init__(self, llm_model: str = "deepseek-chat"):
        """Initialize the router.

        Args:
            llm_model: Model name for LLM classification (Layer 2).
                       Default: deepseek-chat.
        """
        self._llm_model = llm_model
        # Compile regex patterns once for performance
        self._compiled: list[tuple[re.Pattern, str, float, str]] = []
        for pattern, intent, conf, hint in KEYWORD_RULES:
            self._compiled.append((re.compile(pattern, re.IGNORECASE), intent, conf, hint))

    def classify(self, text: str) -> IntentResult:
        """Classify user input into one of 11 intent types.

        Layer 1: keyword match (synchronous, 0 LLM cost).
        Layer 2: LLM classification (for ambiguous inputs).

        Args:
            text: Raw user input string.

        Returns:
            IntentResult with intent, confidence, params, routing_hint, matched_by.
        """
        if not text or not text.strip():
            return IntentResult(
                intent="GENERAL_CHAT",
                confidence=1.0,
                routing_hint="",
                matched_by="keyword",
            )

        text_clean = text.strip()

        # ── Layer 1: Keyword fast-path ──
        for pattern, intent, confidence, hint in self._compiled:
            m = pattern.search(text_clean)
            if m:
                params: dict = {}
                # Extract command for COMMAND intent
                if intent == "COMMAND":
                    params["command"] = text_clean
                elif intent == "SYSTEM":
                    params["target"] = m.group(0)
                elif intent == "SOCIAL":
                    params["target"] = m.group(0)
                elif intent == "SEARCH":
                    params["query"] = text_clean
                elif intent == "CODING":
                    params["task"] = text_clean
                elif intent == "FILE":
                    params["path"] = m.group(0)

                logger.debug(
                    "Keyword match: intent=%s confidence=%.2f hint=%s pattern='%s'",
                    intent, confidence, hint, m.re.pattern[:60],
                )
                return IntentResult(
                    intent=intent,
                    confidence=confidence,
                    params=params,
                    routing_hint=hint,
                    matched_by="keyword",
                )

        # ── Layer 2: LLM classification ──
        return self._classify_llm(text)

    def _classify_llm(self, text: str) -> IntentResult:
        """LLM-based classification for inputs that don't match keywords.

        Uses deepseek-chat via OpenAI-compatible API.
        """
        try:
            client = _get_llm_client()
            if client is None:
                logger.warning("DEEPSEEK_API_KEY not set, falling back to GENERAL_CHAT")
                return IntentResult(
                    intent="GENERAL_CHAT",
                    confidence=0.5,
                    routing_hint="",
                    matched_by="llm",
                )

            prompt = _build_llm_classify_prompt(text)
            response = client.chat.completions.create(
                model=self._llm_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100,
                temperature=0.0,
                timeout=10,
            )

            raw = response.choices[0].message.content.strip()
            intent, confidence = _parse_llm_response(raw)

            return IntentResult(
                intent=intent,
                confidence=confidence,
                routing_hint=_default_hint(intent),
                matched_by="llm",
            )

        except ImportError:
            logger.warning("openai package not installed, LLM classification disabled")
            return IntentResult(
                intent="GENERAL_CHAT",
                confidence=0.3,
                routing_hint="",
                matched_by="llm",
            )
        except Exception as e:
            logger.warning("LLM classification failed: %s, falling back to GENERAL_CHAT", e)
            return IntentResult(
                intent="GENERAL_CHAT",
                confidence=0.3,
                routing_hint="",
                matched_by="llm",
            )


# ─── LLM Prompt & Parsing ─────────────────────────────────────────

def _build_llm_classify_prompt(text: str) -> str:
    """Build a concise classification prompt."""
    return f"""Classify the user's intent into EXACTLY ONE of these categories.
Reply with ONLY the category name and confidence (0.0-1.0), separated by a space.

Categories:
- COMMAND: Shell/terminal commands (pwd, ls, git, docker, curl, etc.)
- SEARCH: Information lookup, fact checking, price/weather/news queries
- RESEARCH: Deep investigation, analysis, comparison, trend research
- VISION: Screenshot, image recognition, OCR, photo analysis
- SOCIAL: WeChat/messaging, sending messages, chatting with contacts
- MEMORY: Remembering, recalling, storing information
- CODING: Writing, fixing, refactoring, building code
- CODE_REVIEW: Reviewing, auditing, checking code quality
- CREATIVE: Copywriting, marketing, SEO, advertising content
- SYSTEM: Opening apps, checking processes, system control
- FILE: Reading, creating files/documents/PDFs/reports
- AUTOMATION: Batch processing, scheduled tasks, cron jobs
- GENERAL_CHAT: Casual conversation, greetings, small talk

User input: "{text[:500]}"

Reply format (e.g.): SEARCH 0.95"""


def _parse_llm_response(raw: str) -> tuple[str, float]:
    """Parse LLM response to (intent, confidence)."""
    raw = raw.strip().upper()
    # Try "INTENT CONFIDENCE" format
    parts = raw.split()
    intent = parts[0] if parts else "GENERAL_CHAT"
    confidence = 0.5
    if len(parts) >= 2:
        try:
            confidence = float(parts[1])
            confidence = max(0.0, min(1.0, confidence))
        except ValueError:
            confidence = 0.5

    # Validate intent
    if intent not in ALL_INTENTS:
        intent = "GENERAL_CHAT"
        confidence = 0.3

    return intent, confidence


def _default_hint(intent: str) -> str:
    """Return a default routing hint for an intent."""
    hints: dict[str, str] = {
        "COMMAND": "SystemAgent",
        "SEARCH": "ResearchAgent",
        "RESEARCH": "ResearchAgent",
        "VISION": "VisionAgent",
        "SOCIAL": "WechatAgent",
        "MEMORY": "MemoryAgent",
        "CODING": "CodingAgent",
        "CODE_REVIEW": "CodingAgent",
        "CREATIVE": "CreativeAgent",
        "SYSTEM": "SystemAgent",
        "FILE": "SystemAgent",
        "AUTOMATION": "SystemAgent",
        "GENERAL_CHAT": "",
    }
    return hints.get(intent, "")


# ─── Backward-Compatible classify_task ────────────────────────────

def classify_task(text: str, department: str = "") -> str:
    """Backward-compatible wrapper returning old task_type string.

    Maps V5 intent types to legacy task_type strings used in graph.py:

        COMMAND       → COMMAND_EXECUTION
        SEARCH        → SIMPLE_QUERY
        RESEARCH      → SIMPLE_QUERY
        CODING        → DEVELOPMENT
        CODE_REVIEW   → CODE_REVIEW
        CREATIVE      → CREATIVE
        SOCIAL        → LOCAL_SYSTEM
        FILE          → DOCUMENT
        SYSTEM        → LOCAL_SYSTEM
        AUTOMATION    → GENERAL
        VISION        → GENERAL
        MEMORY        → GENERAL
        GENERAL_CHAT  → GENERAL

    Args:
        text: User input text.
        department: (unused, kept for API compatibility).

    Returns:
        Legacy task_type string.
    """
    router = _get_router()
    result = router.classify(text)
    task_type = INTENT_TO_TASK_TYPE.get(result.intent, "GENERAL")
    logger.debug("classify_task: '%s' → intent=%s → task_type=%s", text[:60], result.intent, task_type)
    return task_type


# ─── Module-level singletons ──────────────────────────────────────

_router_instance: Optional[IntentRouter] = None
_llm_client: Optional[object] = None


def _get_router() -> IntentRouter:
    """Get or create the module-level IntentRouter singleton."""
    global _router_instance
    if _router_instance is None:
        _router_instance = IntentRouter()
    return _router_instance


def _get_llm_client():
    """Get or create the module-level LLM client singleton.

    Returns the OpenAI client if DEEPSEEK_API_KEY is configured,
    otherwise None.
    """
    global _llm_client
    if _llm_client is None:
        try:
            from openai import OpenAI
            from src.config import config

            api_key = config.deepseek_api_key
            if api_key:
                _llm_client = OpenAI(
                    api_key=api_key,
                    base_url="https://api.deepseek.com/v1",
                )
        except Exception:
            _llm_client = False  # Sentinel: tried and failed
    # _llm_client may be False (sentinel) → return None
    return _llm_client if _llm_client is not False else None
