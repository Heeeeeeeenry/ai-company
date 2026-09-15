"""Multi-user, multi-conversation session store (SQLite).

Solves the "embed ai-company into the admin system" problem:

  * A single **login credential** (cookie / Django session / JWT) is only used
    by the host system to identify WHO the user is — it should NEVER be used as
    the conversation id (it expires, and one credential can't express many
    conversations).
  * Instead we keep three independent layers:
        credential  ->  user_id   (identity, stable)
        user_id     ->  [conversation_id...]  (a user can have many chats)
        conversation_id -> history (permanent, survives login expiry)

This store holds the user -> conversations mapping in SQLite.  The per-chat
history itself reuses the existing SessionMemory file storage (memory.json),
so conversation content is durable and independent of the login credential.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Optional

import os as _os

_STORE_DIR = _os.path.expanduser("~/.ai-company")
_DB_PATH = _os.path.join(_STORE_DIR, "multiuser.db")

_LOCK = threading.Lock()


def _connect() -> sqlite3.Connection:
    _os.makedirs(_STORE_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id    TEXT PRIMARY KEY,
                username   TEXT,
                display_name TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY,
                user_id      TEXT NOT NULL,
                title        TEXT,
                created_at   TEXT,
                last_active  TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_conv_user
                ON conversations(user_id);
            """
        )


# ─── User API ─────────────────────────────────────────

def ensure_user(user_id: str, username: str = "", display_name: str = "") -> None:
    """Register a user if they don't exist yet (idempotent)."""
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            return
        conn.execute(
            "INSERT INTO users(user_id, username, display_name, created_at) "
            "VALUES(?,?,?,?)",
            (user_id, username or user_id, display_name or username or user_id,
             datetime.now().isoformat()),
        )


def get_user(user_id: str) -> Optional[dict]:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    return dict(row) if row else None


# ─── Conversation API ─────────────────────────────────

def create_conversation(user_id: str, title: str = "新对话") -> dict:
    """Create a new conversation owned by user_id; return its record."""
    ensure_user(user_id)
    cid = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO conversations(conversation_id, user_id, title, "
            "created_at, last_active) VALUES(?,?,?,?,?)",
            (cid, user_id, title, now, now),
        )
    return {
        "conversation_id": cid,
        "user_id": user_id,
        "title": title,
        "created_at": now,
        "last_active": now,
    }


def list_conversations(user_id: str) -> list[dict]:
    """All conversations belonging to user_id, most recently active first."""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE user_id=? "
            "ORDER BY last_active DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conversation_id: str) -> Optional[dict]:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
    return dict(row) if row else None


def touch_conversation(conversation_id: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "UPDATE conversations SET last_active=? WHERE conversation_id=?",
            (datetime.now().isoformat(), conversation_id),
        )


def delete_conversation(user_id: str, conversation_id: str) -> bool:
    """Delete a conversation, but only if it belongs to user_id (ownership check)."""
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "DELETE FROM conversations WHERE conversation_id=? AND user_id=?",
            (conversation_id, user_id),
        )
        return cur.rowcount > 0


def _init():
    _init_db()


_init()
