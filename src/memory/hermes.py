"""AI Company memory system — session-aware engram-router backend.

Each session gets an isolated namespace in the shared SQLite database:
  - Global (no session)  → "memory" / "user"
  - Session "abc123"     → "session:abc123:memory" / "session:abc123:user"

The module-level ``hermes_memory`` proxy auto-detects the active session
from SessionManager and delegates to the correct EngramBackend instance.

Usage (unchanged):
    from src.memory.hermes import hermes_memory
    hermes_memory.add("fact text", "memory")
    hermes_memory.search("query")
    ctx = hermes_memory.get_full_context()
"""

from __future__ import annotations

import logging
from threading import Lock
from typing import Any

logger = logging.getLogger("ai_company.memory")


# ═══ Session-aware proxy ═══

class _StoreProxy:
    """Namespace-aware wrapper around engram-router MemoryStore.

    Automatically injects the session-scoped namespace into recall()
    calls so that direct store access stays isolated per session.
    """

    def __init__(self, store: Any, ns_resolver):
        # Use object.__setattr__ so __getattr__ never intercepts these
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_ns", ns_resolver)

    def __getattr__(self, name: str):
        """Delegate everything except recall to the real store.

        Safe-guarded: uses self.__dict__ to access _store/_ns without
        triggering __getattr__ recursion even if _store was deleted.
        """
        store = self.__dict__.get("_store")
        if store is None:
            raise RuntimeError("_StoreProxy._store is None — proxy in bad state")

        if name == "recall":
            return self._recall
        if name == "should_inject":
            return getattr(store, "should_inject")
        return getattr(store, name)

    def _recall(self, query: str, top_k: int = 10, namespace: str = "memory", **kwargs):
        """Recall with session-scoped namespace auto-injected."""
        return self.__dict__["_store"].recall(
            query,
            top_k=top_k,
            namespace=self.__dict__["_ns"](namespace),
            **kwargs,
        )


class SessionAwareMemory:
    """Proxy that routes memory operations to the correct EngramBackend.

    Auto-detects the active session from SessionManager and maintains
    a cache of per-session EngramBackend instances.  Global memory
    (session_id=None) holds shared rules and principles; per-session
    backends hold session-private facts.

    Works as a drop-in replacement for the old EngramBackend singleton.
    """

    def __init__(self):
        self._lock = Lock()
        self._backends: dict[str, Any] = {}  # session_id → EngramBackend
        self._global: Any | None = None      # Global (session_id=None)

    # ── Internal helpers ──

    def _get_global(self):
        """Lazy-init the global backend (shared rules/principles)."""
        if self._global is None:
            with self._lock:
                if self._global is None:
                    from src.memory.engram_backend import EngramBackend
                    self._global = EngramBackend(session_id=None)
        return self._global

    def _resolve_backend(self) -> Any:
        """Return the backend for the currently active session.

        If no session is active, falls back to the global backend.
        Captures session object atomically to avoid TOCTOU between
        existence check and id read.
        """
        try:
            from src.session.manager import get_session_manager
            sm = get_session_manager()
            current = sm.current  # Atomic capture — P1-1 fix
            if current is not None:
                sid = current.id
                if sid not in self._backends:
                    with self._lock:
                        if sid not in self._backends:
                            from src.memory.engram_backend import EngramBackend
                            logger.info("Creating per-session backend: %s", sid)
                            self._backends[sid] = EngramBackend(session_id=sid)
                return self._backends[sid]
        except Exception:
            pass
        return self._get_global()

    @property
    def store(self):
        """Expose namespace-aware MemoryStore proxy for direct recall calls."""
        backend = self._resolve_backend()
        return _StoreProxy(backend._store, backend._ns)

    # ── Lifecycle / cleanup ──

    def on_session_deleted(self, session_id: str):
        """Clean up backend cache when a session is deleted.

        Called by SessionManager.delete() to prevent zombie backend
        objects from leaking memory and holding the lock.
        """
        with self._lock:
            backend = self._backends.pop(session_id, None)
            if backend is not None:
                backend.close()
                logger.info("Cleaned up backend for deleted session: %s", session_id)

    # ── Core Operations (delegated) ──

    def add(self, text: str, target: str = "memory", category: str = "") -> dict:
        return self._resolve_backend().add(text, target=target, category=category)

    def replace(self, old_text: str, new_text: str, target: str = "memory") -> bool:
        return self._resolve_backend().replace(old_text, new_text, target=target)

    def remove(self, text: str, target: str = "memory") -> int:
        return self._resolve_backend().remove(text, target=target)

    def list(self, target: str = "memory") -> list[dict]:
        return self._resolve_backend().list(target=target)

    def search(self, query: str, target: str = "memory") -> list[dict]:
        return self._resolve_backend().search(query, target=target)

    def semantic_search(self, query: str, target: str = "memory", top_k: int = 5) -> list[dict]:
        return self._resolve_backend().semantic_search(query, target=target, top_k=top_k)

    # ── Prompt Injection ──

    def get_user_context(self) -> str:
        return self._resolve_backend().get_user_context()

    def get_memory_context(self) -> str:
        return self._resolve_backend().get_memory_context()

    def get_full_context(self) -> str:
        """Build full context: global rules + session-private memories."""
        backend = self._resolve_backend()
        # For per-session backends, also include global context
        if backend.session_id is not None:
            global_part = self._get_global().get_full_context()
            session_part = backend.get_full_context()
            parts = []
            if global_part:
                parts.append(global_part)
            if session_part:
                parts.append(f"\n## SESSION CONTEXT\n{session_part}")
            return "\n\n".join(parts)
        return backend.get_full_context()

    def migrate_from_json(self, json_path: str) -> int:
        return self._resolve_backend().migrate_from_json(json_path)

    # ── Stats ──

    def stats(self) -> dict:
        backend = self._resolve_backend()
        st = backend.stats()
        st["session_id"] = backend.session_id
        return st


# ═══ Module-level singleton ═══

hermes_memory = SessionAwareMemory()
"""Session-aware memory proxy — always delegates to the correct backend."""
