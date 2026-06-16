"""Session Memory — per-session persistent memory + global shared memory.

Session memory: ~/.ai-company/sessions/<session_id>/memory.json
Global memory: ~/.ai-company/global_memory.json

Session memory is unique to each session (conversation-specific facts).
Global memory is shared across all sessions (principles, configs, rules).
"""

import json
import os
from datetime import datetime
from typing import Any, Optional
from threading import Lock

# Paths
GLOBAL_MEMORY_FILE = os.path.expanduser("~/.ai-company/global_memory.json")
SESSION_DIR = os.path.expanduser("~/.ai-company/sessions")

def _session_memory_path(session_id: str) -> str:
    return os.path.join(SESSION_DIR, session_id, "memory.json")


class SessionMemory:
    """Per-session key-value memory store. Auto-persists on write.
    
    Usage:
        mem = SessionMemory(session_id)
        mem.set("user_preference", "prefers short answers")
        value = mem.get("user_preference")
        mem.record_conversation(user_msg, assistant_msg)  # append to history
    """
    
    MAX_HISTORY = 50  # Keep last N conversation turns
    
    def __init__(self, session_id: str):
        self.session_id = session_id
        self._data: dict = {}
        self._conversations: list[dict] = []
        self._lock = Lock()
        self._load()
    
    def _load(self):
        path = _session_memory_path(self.session_id)
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                self._data = data.get("memories", {})
                self._conversations = data.get("conversations", [])
            except (json.JSONDecodeError, IOError):
                self._data = {}
                self._conversations = []
    
    def _save(self):
        path = _session_memory_path(self.session_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._lock:
            data = {
                "session_id": self.session_id,
                "updated_at": datetime.now().isoformat(),
                "memories": self._data,
                "conversations": self._conversations[-self.MAX_HISTORY:],
            }
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def set(self, key: str, value: Any):
        """Store a memory item."""
        with self._lock:
            self._data[key] = {
                "value": value,
                "updated_at": datetime.now().isoformat(),
            }
        self._save()
    
    def get(self, key: str) -> Optional[Any]:
        """Retrieve a memory item."""
        item = self._data.get(key)
        return item["value"] if item else None
    
    def delete(self, key: str):
        """Delete a memory item."""
        with self._lock:
            self._data.pop(key, None)
        self._save()
    
    def all(self) -> dict:
        """Get all memories as simple dict."""
        return {k: v["value"] for k, v in self._data.items()}
    
    def record_conversation(self, user_msg: str, assistant_msg: str):
        """Record a conversation turn."""
        with self._lock:
            self._conversations.append({
                "timestamp": datetime.now().isoformat(),
                "user": user_msg[:2000],
                "assistant": assistant_msg[:2000],
            })
        self._save()
    
    def get_recent_conversations(self, n: int = 10) -> list[dict]:
        """Get last N conversation turns."""
        return self._conversations[-n:]
    
    def get_summary_context(self) -> str:
        """Build context string for auto-summarization."""
        parts = []
        if self._data:
            parts.append("## Stored Memories")
            for k, v in self._data.items():
                parts.append(f"- {k}: {str(v['value'])[:200]}")
        if self._conversations:
            parts.append(f"\n## Recent Conversations ({len(self._conversations)} turns)")
            for c in self._conversations[-5:]:
                parts.append(f"User: {c['user'][:150]}")
                parts.append(f"Assistant: {c['assistant'][:150]}")
        return "\n".join(parts)


class GlobalMemory:
    """Singleton global memory shared across ALL sessions.
    
    Stores principles, configs, shared rules that apply to every session.
    Populated by explicit user command or agent learning.
    
    Usage:
        gm = GlobalMemory()
        gm.set("coding_style", "PEP 8, type hints required")
        gm.set("preferred_language", "Chinese")
    """
    
    def __init__(self):
        self._data: dict = {}
        self._lock = Lock()
        self._load()
    
    def _load(self):
        if os.path.exists(GLOBAL_MEMORY_FILE):
            try:
                with open(GLOBAL_MEMORY_FILE) as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._data = {}
    
    def _save(self):
        os.makedirs(os.path.dirname(GLOBAL_MEMORY_FILE), exist_ok=True)
        with open(GLOBAL_MEMORY_FILE, "w") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
    
    def set(self, key: str, value: Any):
        with self._lock:
            self._data[key] = {
                "value": value,
                "updated_at": datetime.now().isoformat(),
            }
        self._save()
    
    def get(self, key: str) -> Optional[Any]:
        item = self._data.get(key)
        return item["value"] if item else None
    
    def all(self) -> dict:
        return {k: v["value"] for k, v in self._data.items()}
    
    def delete(self, key: str):
        with self._lock:
            self._data.pop(key, None)
        self._save()
    
    def get_context_for_prompt(self) -> str:
        """Build a compact context string for LLM prompts."""
        if not self._data:
            return ""
        lines = ["## Global Rules (shared across all sessions)"]
        for k, v in self._data.items():
            lines.append(f"- {k}: {str(v['value'])[:200]}")
        return "\n".join(lines)


# Singletons
_global_memory: Optional[GlobalMemory] = None
_session_memories: dict[str, SessionMemory] = {}

def get_global_memory() -> GlobalMemory:
    global _global_memory
    if _global_memory is None:
        _global_memory = GlobalMemory()
    return _global_memory

def get_session_memory(session_id: str) -> SessionMemory:
    global _session_memories
    if session_id not in _session_memories:
        _session_memories[session_id] = SessionMemory(session_id)
    return _session_memories[session_id]
