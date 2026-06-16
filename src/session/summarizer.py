"""Auto-Summarizer — analyze conversations and write persistent memories.

Triggered at session switch, session end (/quit), or periodically.
Uses the LLM to extract key facts, decisions, and user preferences from
recent conversation history, then writes them to session memory.

Also handles cross-session memory: when the user explicitly says something
like "remember this for all sessions" or "this applies globally", it writes
to GlobalMemory instead.
"""

import logging
from typing import Optional
from src.session.memory import get_session_memory, get_global_memory

logger = logging.getLogger("ai_company.summarizer")

# Detection patterns for "save to global memory" intent
GLOBAL_MEMORY_TRIGGERS = [
    "所有会话", "全局", "永久记住", "所有对话",
    "global", "all sessions", "remember forever",
    "通用规则", "通用配置", "所有项目",
]


async def auto_summarize_conversation(
    session_id: str,
    llm=None,
    force: bool = False,
) -> Optional[str]:
    """Summarize recent conversation and write key facts to session memory.
    
    Args:
        session_id: Session to summarize
        llm: Optional LLM instance for summarization (uses CEO model if None)
        force: If True, summarize even with few messages
    
    Returns:
        Summary text if generated, None if skipped
    """
    mem = get_session_memory(session_id)
    conversations = mem.get_recent_conversations(20)
    
    if not conversations:
        return None
    
    # Only summarize if we have enough content or forced
    if len(conversations) < 3 and not force:
        return None
    
    # Build conversation text for LLM
    conv_text_parts = []
    for c in conversations[-10:]:
        conv_text_parts.append(f"User: {c['user'][:300]}")
        conv_text_parts.append(f"Assistant: {c['assistant'][:300]}")
    conv_text = "\n".join(conv_text_parts)
    
    existing_memories = mem.get_summary_context()
    
    prompt = f"""You are a memory curator. Analyze the conversation below and extract key facts.

## Existing Memories
{existing_memories if existing_memories else '(none)'}

## Recent Conversation
{conv_text}

## Instructions
Extract and return a JSON object with:
- "new_facts": list of key facts learned (user preferences, decisions, project details, technical choices)
- "updates": list of existing memories to update (if any contradict)
- "summary": one-sentence summary of what was discussed

Rules:
- Only extract CONCRETE facts, not opinions or temporary state
- User preferences (like "prefers short answers") are HIGH priority
- Technical decisions (like "switched to PostgreSQL") are HIGH priority
- Skip facts already in existing memories
- Return JSON only, no explanation

Format:
{{"new_facts": [{{"key": "fact_key", "value": "fact value"}}], "updates": [], "summary": "..."}}"""

    try:
        if llm is None:
            from src.ceo.graph import _get_llm
            llm = _get_llm("ceo")
        
        response = await llm.ainvoke(prompt)
        raw = str(response.content)
        
        # Parse JSON
        import json
        from src.ceo.graph import _extract_json
        try:
            data = _extract_json(raw)
        except (ValueError, json.JSONDecodeError):
            # Try to parse directly
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Failed to parse summarizer LLM response")
                return None
        
        if not isinstance(data, dict):
            return None
        
        # Write new facts to memory
        new_facts = data.get("new_facts", [])
        for fact in new_facts:
            if isinstance(fact, dict) and "key" in fact:
                mem.set(fact["key"], fact.get("value", ""))
        
        # Apply updates
        updates = data.get("updates", [])
        for update in updates:
            if isinstance(update, dict) and "key" in update:
                mem.set(update["key"], update.get("value", ""))
        
        summary = data.get("summary", "")
        if new_facts or updates:
            logger.info(
                "Auto-summarize: %d new facts, %d updates for session %s",
                len(new_facts), len(updates), session_id,
            )
        
        return summary
        
    except Exception as e:
        logger.warning("Auto-summarize failed: %s", e)
        return None


def detect_global_memory_intent(text: str) -> bool:
    """Check if user message intends global memory storage."""
    text_lower = text.lower()
    return any(trigger in text_lower for trigger in GLOBAL_MEMORY_TRIGGERS)


async def save_to_global_memory(key: str, value: str):
    """Write a fact to global (cross-session) memory."""
    gm = get_global_memory()
    gm.set(key, value)
    logger.info("Global memory updated: %s", key)


def get_session_context_for_prompt(session_id: str) -> str:
    """Build session context to inject into LLM prompts."""
    mem = get_session_memory(session_id)
    gm = get_global_memory()
    
    parts = []
    
    # Global context
    global_ctx = gm.get_context_for_prompt()
    if global_ctx:
        parts.append(global_ctx)
    
    # Session memories
    all_mem = mem.all()
    if all_mem:
        parts.append("## Session Memories")
        for k, v in all_mem.items():
            parts.append(f"- {k}: {str(v)[:200]}")
    
    return "\n".join(parts) if parts else ""
