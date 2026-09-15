"""Conversation scope — the single source of truth for "who is asking".

Why this exists
---------------
ai-company was originally a single-user CLI: everything that persists outside a
per-conversation store was therefore *global*.  Two such globals are shared by
every agent:

  * ``src/memory/artifacts.py``  — workspace/artifacts/*.json
  * ``src/memory/hermes.py``     — engram memory namespaces

That is harmless for one local user, but the moment ai-company is embedded as a
multi-user service it becomes a data-leak: request for user B reads the artifact
(or memory) written during user A's earlier request.

``scope`` is a ``contextvar`` holding the *current conversation id*.  The HTTP
layer binds it per request (see ``src/api_server.py``) and every shared store
resolves its namespace from it.  When it is unset the legacy global behaviour is
preserved, so the single-user CLI is unaffected.

``contextvars`` (not a module global) is deliberate: it is task-local, so two
concurrent requests in the same event loop each see their own conversation id.
``asyncio.to_thread`` / ``run_in_executor`` copy the context into the worker
thread, so the scope survives offloading to a thread pool as well.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Optional

_scope: ContextVar[Optional[str]] = ContextVar("ai_company_conversation_scope", default=None)


def current_scope() -> Optional[str]:
    """Return the conversation id bound to the current task, or None."""
    return _scope.get()


def set_scope(conversation_id: Optional[str]) -> Token:
    """Bind the scope. Returns a token to pass to :func:`reset_scope`."""
    return _scope.set(conversation_id or None)


def reset_scope(token: Token) -> None:
    _scope.reset(token)


@contextmanager
def scope(conversation_id: Optional[str]) -> Iterator[None]:
    """Bind ``conversation_id`` for the duration of the block.

    Usage::

        with scope(conversation_id):
            await run_ceo(message)
    """
    token = _scope.set(conversation_id or None)
    try:
        yield
    finally:
        _scope.reset(token)
