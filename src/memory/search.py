"""Session Search — Search past conversation history by keyword.

Mirrors Hermes' session_search tool. Searches across all session memory.json
files and returns relevant conversation snippets with timestamps.

Usage:
    from src.memory.search import session_search
    results = session_search("wechat 发送失败")
    # → list of {session_name, timestamp, user, assistant, score}
"""

import json
import os
import re
from datetime import datetime
from typing import Optional


SESSION_DIR = os.path.expanduser("~/.ai-company/sessions")


def _tokenize(text: str) -> set[str]:
    """Simple Chinese+English tokenizer."""
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]", " ", str(text).lower())
    words = [w for w in cleaned.split() if len(w) > 1]
    tokens = set(words)

    # Try jieba for Chinese
    has_chinese = any('\u4e00' <= c <= '\u9fff' for c in cleaned)
    if has_chinese:
        try:
            import jieba
            for w in words:
                if any('\u4e00' <= c <= '\u9fff' for c in w):
                    tokens.update(t for t in jieba.cut(w, cut_all=False) if len(t) > 1)
        except ImportError:
            # Fallback: character bigrams
            for w in words:
                for i in range(len(w) - 1):
                    tokens.add(w[i:i + 2])
    return tokens


def _score_match(query_tokens: set[str], text: str) -> float:
    """Score a text against query tokens using token overlap."""
    if not query_tokens:
        return 0.0
    target_tokens = _tokenize(text)
    if not target_tokens:
        return 0.0
    overlap = len(query_tokens & target_tokens)
    # Jaccard + boost for multiple hits
    jaccard = overlap / len(query_tokens | target_tokens) if query_tokens | target_tokens else 0
    return min(1.0, jaccard + (overlap * 0.1))


def session_search(query: str, limit: int = 5) -> list[dict]:
    """Search past conversations across all sessions.

    Args:
        query: Search keywords
        limit: Max results to return

    Returns:
        List of dicts: {session_name, timestamp, user_msg, assistant_msg, score, session_id}
    """
    if not os.path.isdir(SESSION_DIR):
        return []

    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    all_results = []

    for session_id in os.listdir(SESSION_DIR):
        memory_path = os.path.join(SESSION_DIR, session_id, "memory.json")
        metadata_path = os.path.join(SESSION_DIR, session_id, "metadata.json")

        if not os.path.exists(memory_path):
            continue

        # Get session name
        session_name = session_id[:8]
        if os.path.exists(metadata_path):
            try:
                with open(metadata_path) as f:
                    meta = json.load(f)
                session_name = meta.get("name", session_id[:8])
            except Exception:
                pass

        # Load conversations
        try:
            with open(memory_path) as f:
                data = json.load(f)
            conversations = data.get("conversations", [])
        except (json.JSONDecodeError, IOError):
            continue

        for conv in conversations:
            user_msg = conv.get("user", "")
            assistant_msg = conv.get("assistant", "")

            user_score = _score_match(query_tokens, user_msg)
            asst_score = _score_match(query_tokens, assistant_msg)
            combined = user_score * 0.6 + asst_score * 0.4

            if combined > 0.05:
                all_results.append({
                    "session_id": session_id,
                    "session_name": session_name,
                    "timestamp": conv.get("timestamp", ""),
                    "user": user_msg[:300],
                    "assistant": assistant_msg[:300],
                    "score": round(combined, 4),
                })

    # Sort by score desc, take top N
    all_results.sort(key=lambda x: x["score"], reverse=True)
    return all_results[:limit]


def session_search_recent(limit: int = 5) -> list[dict]:
    """Show recent sessions (no query — like Hermes' session_search without query)."""
    if not os.path.isdir(SESSION_DIR):
        return []

    sessions = []
    for session_id in os.listdir(SESSION_DIR):
        metadata_path = os.path.join(SESSION_DIR, session_id, "metadata.json")
        memory_path = os.path.join(SESSION_DIR, session_id, "memory.json")

        session_name = session_id[:8]
        message_count = 0
        last_active = ""
        preview = ""

        if os.path.exists(metadata_path):
            try:
                with open(metadata_path) as f:
                    meta = json.load(f)
                session_name = meta.get("name", session_id[:8])
                message_count = meta.get("message_count", 0)
                last_active = meta.get("last_active", "")
            except Exception:
                pass

        if os.path.exists(memory_path) and message_count > 0:
            try:
                with open(memory_path) as f:
                    data = json.load(f)
                convs = data.get("conversations", [])
                if convs:
                    last = convs[-1]
                    preview = last.get("user", "")[:80]
            except Exception:
                pass

        sessions.append({
            "session_id": session_id,
            "session_name": session_name,
            "message_count": message_count,
            "last_active": last_active,
            "preview": preview,
        })

    sessions.sort(key=lambda x: x["last_active"], reverse=True)
    return sessions[:limit]
