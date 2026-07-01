"""AI Company memory system — engram-router backend.

All memory operations go through EngramBackend (engram-router SQLite).
No JSON fallback, no config switching. One backend, one source of truth.

Usage:
    from src.memory.hermes import hermes_memory
    hermes_memory.add("fact text", "memory")
    hermes_memory.search("query")
    ctx = hermes_memory.get_full_context()
"""

from __future__ import annotations

import logging

logger = logging.getLogger("ai_company.memory")

# ═══ EngramBackend singleton ═══

def _init_engram():
    from src.memory.engram_backend import EngramBackend
    return EngramBackend()


hermes_memory: EngramBackend = _init_engram()  # type: ignore[name-defined]
"""Global memory singleton — always EngramBackend."""
