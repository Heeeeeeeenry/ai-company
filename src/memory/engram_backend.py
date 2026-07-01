"""EngramBackend — engram-router 作为 AI-Company 记忆后端。

将 HermesMemory 的 declarative-fact API 映射到 engram-router 的
evidence-based SQLite 存储。支持 namespace 隔离（memory/user）。

配置:
    AI_COMPANY_MEMORY_BACKEND=engram  # 默认
    AI_COMPANY_MEMORY_BACKEND=json    # 切回旧版

存储路径: ~/.ai-company/engram_memory.db (SQLite)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Optional

logger = logging.getLogger("ai_company.engram_backend")

DATA_DIR = os.path.expanduser("~/.ai-company")
DB_PATH = os.path.join(DATA_DIR, "engram_memory.db")


class EngramBackend:
    """EngramRouter-backed memory with HermesMemory-compatible API.

    Uses two namespaces:
      - "memory" → declarative facts, environment, tools, lessons
      - "user"   → user profile, preferences, habits
    """

    # ── Class-level entry registry (in-memory index for list/stats) ──
    # We maintain a lightweight index because engram-router's recall()
    # is search-based and doesn't support "list all".
    _instance: EngramBackend | None = None

    def __init__(self):
        self._store: Any = None  # MemoryStore — init lazily
        self._lock = Lock()
        self._entries: dict[str, list[dict]] = {"memory": [], "user": []}
        self._init_store()

    # ═══ Store Init ═══

    def _init_store(self):
        """Lazy-init the engram-router MemoryStore."""
        try:
            from engram_router.store import MemoryStore
            os.makedirs(DATA_DIR, exist_ok=True)
            self._store = MemoryStore(path=Path(DB_PATH))
            self._seed_defaults()
            self._load_entries()
            logger.info("EngramBackend initialized: %s", DB_PATH)
        except ImportError:
            logger.error(
                "engram-router not installed. Install with: "
                "pip install -e /path/to/engram-router"
            )
            raise
        except Exception as exc:
            logger.error("EngramBackend init failed: %s", exc)
            raise

    def _seed_defaults(self):
        """Seed default memory entries on first creation (empty DB)."""
        # Check if DB already has entries
        stats = self.store.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE namespace = 'memory'"
        ).fetchone()
        if stats and stats[0] > 0:
            return  # Already has entries

        defaults_memory = [
            ("禁止主动删数据。需删除时中文二次确认。DROP/DELETE/TRUNCATE/git push -f 等破坏性操作先询问。", "safety"),
            ("修改代码后必须端到端测试验证才能说'完成'。禁止未测试就声称完成。", "quality"),
            ("3+轮未解→激活stubborn-problem-workflow；不要保守不等提示。", "process"),
            ("ai-company Python 3.12 (conda)，通过 goudan 启动。DeepSeek API (deepseek-chat)，LangGraph 工作流。", "environment"),
            ("wechat paste: pbcopy+Cmd/V 发中文; 文件传输助手 sidebar。Kimi vision moonshot-v1-8k-vision-preview。", "tool"),
            ("WeChat发送是canary功能——如果坏了用户立刻发现并愤怒。任何代码改动后必须验证WeChat发送仍可用才声称完成。", "critical"),
            ("偏好快速响应：综合查询时并行发起操作，不串行等待。偏好自主决策：'自行决断'=直接执行不逐项确认。", "preference"),
        ]
        defaults_user = [
            ("语言偏好: 简体中文", ""),
            ("OS: macOS (iTerm2透明85%, retina 2880x1800)", ""),
            ("交互习惯: 偏好自主决策，'自行决断'=直接执行不逐项确认。复杂任务用 delegate_task 多智能体并行。", ""),
            ("项目: ai-company (狗蛋儿) 独立进程个人助手; VoiceDirect (民意直通车) Go+Gin+Vue3 警用信访系统。", ""),
            ("push规则: 晚上7点(19:00)左右的push直接执行不用询问；其他时间段需先询问。", ""),
            ("微信联系人: 小号/文件传输助手(默认)。不再使用小媛儿宝儿。", ""),
        ]

        for text, cat in defaults_memory:
            self._add_internal(text, "memory", cat)
        for text, cat in defaults_user:
            self._add_internal(text, "user", cat)
        logger.info("Seeded %d default entries", len(defaults_memory) + len(defaults_user))

    def _load_entries(self):
        """Hydrate in-memory entry registry from SQLite DB."""
        from engram_router.store import MemoryRecord
        try:
            for target in ("memory", "user"):
                rows = self.store.conn.execute(
                    "SELECT id, raw_text, metadata FROM memories WHERE namespace = ?",
                    (target,),
                ).fetchall()
                entries = []
                for row in rows:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    entries.append({
                        "text": row["raw_text"],
                        "memory_id": row["id"],
                        "category": meta.get("category", ""),
                        "created_at": meta.get("created_at", ""),
                        "access_count": meta.get("access_count", 0),
                    })
                self._entries[target] = entries
        except Exception:
            pass  # DB may be empty from _seed_defaults

    @property
    def store(self):
        if self._store is None:
            self._init_store()
        return self._store

    # ═══ Migration ───

    def migrate_from_json(self, json_path: str) -> int:
        """Import entries from existing hermes_memory.json.

        Reads the JSON file, imports each entry into engram-router.
        Returns count of imported entries.
        """
        if not os.path.exists(json_path):
            return 0

        try:
            with open(json_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            return 0

        imported = 0
        for target_key in ("memory", "user"):
            entries = data.get(target_key, [])
            for entry in entries:
                text = entry.get("text", "").strip()
                if not text:
                    continue
                try:
                    self._add_internal(text, target_key, entry.get("category", ""))
                    imported += 1
                except Exception as exc:
                    logger.warning("Migration skip [%s]: %s", target_key, exc)

        if imported:
            logger.info("Migrated %d entries from %s", imported, json_path)
        return imported

    # ═══ Internal helpers ───

    def _add_internal(self, text: str, target: str, category: str = "") -> dict:
        """Core add logic — saves to store + updates entry registry."""
        now = datetime.now().isoformat()
        entry = {
            "text": text,
            "created_at": now,
            "access_count": 0,
        }
        if category:
            entry["category"] = category

        # Save to engram-router
        try:
            memory_id = self.store.save(
                text,
                source="hermes",
                metadata={"target": target, "category": category},
                namespace=target,
            )
            entry["memory_id"] = memory_id
        except Exception as exc:
            logger.error("Engram save failed: %s", exc)
            raise

        # Update registry
        with self._lock:
            self._entries[target].append(entry)

        return entry

    # ═══ Core Operations (HermesMemory-compatible) ═══

    def add(self, text: str, target: str = "memory", category: str = "") -> dict:
        """Add a declarative memory entry.

        Deduplication: if identical text exists in target, bumps access_count.
        """
        text = text.strip()
        if not text:
            raise ValueError("text must not be empty")

        with self._lock:
            # Check for duplicates
            for e in self._entries[target]:
                if e.get("text", "").strip() == text:
                    e["access_count"] = e.get("access_count", 0) + 1
                    e["updated_at"] = datetime.now().isoformat()
                    return e

        return self._add_internal(text, target, category)

    def replace(self, old_text: str, new_text: str, target: str = "memory") -> bool:
        """Replace a memory entry matching old_text substring.

        Requires exactly one match for safety.
        """
        old_text = old_text.strip()
        new_text = new_text.strip()

        with self._lock:
            matches = [e for e in self._entries[target] if old_text in e.get("text", "")]
            if len(matches) != 1:
                return False

            old_entry = matches[0]
            old_id = old_entry.get("memory_id")

            # Remove old
            if old_id:
                try:
                    self.store.delete(old_id)
                except Exception:
                    pass

            # Add new
            now = datetime.now().isoformat()
            try:
                new_id = self.store.save(
                    new_text,
                    source="hermes",
                    metadata={"target": target, "category": old_entry.get("category", "")},
                    namespace=target,
                )
            except Exception as exc:
                logger.error("Engram replace-save failed: %s", exc)
                return False

            old_entry["text"] = new_text
            old_entry["memory_id"] = new_id
            old_entry["updated_at"] = now

        return True

    def remove(self, text: str, target: str = "memory") -> int:
        """Remove memory entries matching text substring."""
        text = text.strip()
        removed = 0

        with self._lock:
            new_entries = []
            for e in self._entries[target]:
                if text in e.get("text", ""):
                    mid = e.get("memory_id")
                    if mid:
                        try:
                            self.store.delete(mid)
                        except Exception:
                            pass
                    removed += 1
                else:
                    new_entries.append(e)
            self._entries[target] = new_entries

        return removed

    def list(self, target: str = "memory") -> list[dict]:
        """List all entries for a target."""
        with self._lock:
            return list(self._entries[target])

    def search(self, query: str, target: str = "memory") -> list[dict]:
        """Keyword-based recall via engram-router FTS5 trigram search."""
        try:
            records = self.store.recall(query, top_k=10, namespace=target)
        except Exception:
            return []

        results = []
        for r in records:
            # Find matching entry in registry by text
            for e in self._entries[target]:
                if e.get("memory_id") == r.id:
                    results.append({**e, "score": r.score,
                                    "match_reason": r.match_reason,
                                    "summary": r.summary})
                    break
            else:
                # New entry from migration or direct save
                results.append({
                    "text": r.raw_text,
                    "memory_id": r.id,
                    "score": r.score,
                    "match_reason": r.match_reason,
                    "summary": r.summary,
                })

        results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return results

    def semantic_search(self, query: str, target: str = "memory", top_k: int = 5) -> list[dict]:
        """Semantic search using engram-router's entity-enhanced recall pipeline.

        Uses the full recall pipeline (FTS5 + LIKE + entity name fallback +
        entity hop + edge expansion) which provides much better semantic
        matching than simple keyword search.

        Falls back to broader search if top results score too low.
        """
        try:
            # Broader recall first — get more candidates for better ranking
            records = self.store.recall(query, top_k=max(top_k * 3, 15), namespace=target)
        except Exception:
            return self.search(query, target=target)[:top_k]

        results = []
        for r in records:
            entry = None
            for e in self._entries.get(target, []):
                if e.get("memory_id") == r.id:
                    entry = {**e, "score": r.score, "match_reason": r.match_reason,
                             "summary": r.summary}
                    break
            if entry is None:
                entry = {"text": r.raw_text, "memory_id": r.id, "score": r.score,
                         "match_reason": r.match_reason, "summary": r.summary}
            results.append(entry)

        results.sort(key=lambda x: x.get("score", 0), reverse=True)

        # Filter out very low confidence results (noise)
        filtered = [r for r in results if r.get("score", 0) >= 0.3]
        if not filtered:
            filtered = results[:top_k]

        return filtered[:top_k]

    # ═══ Prompt Injection ═══

    def get_user_context(self) -> str:
        """Build compact user profile for LLM prompt injection."""
        with self._lock:
            entries = list(self._entries["user"])
        if not entries:
            return ""

        lines = ["## USER PROFILE"]
        for e in entries:
            cat = f"[{e.get('category', '')}] " if e.get("category") else ""
            lines.append(f"- {cat}{e['text']}")
            e["access_count"] = e.get("access_count", 0) + 1
        return "\n".join(lines)

    def get_memory_context(self) -> str:
        """Build compact memory facts for LLM prompt injection."""
        with self._lock:
            entries = list(self._entries["memory"])
        if not entries:
            return ""

        # Sort: critical first, then by access_count desc
        cat_priority = {
            "critical": 0, "safety": 1, "quality": 2, "process": 3,
            "environment": 4, "tool": 5, "preference": 6,
        }
        entries.sort(key=lambda e: (
            cat_priority.get(e.get("category", ""), 99),
            -(e.get("access_count", 0)),
        ))

        total_chars = sum(len(e["text"]) for e in entries)
        pct = min(100, int(total_chars / 2200 * 100))

        lines = [f"## MEMORY [{pct}% — {total_chars}/2,200 chars]"]
        for e in entries:
            lines.append(f"- {e['text']}")
            e["access_count"] = e.get("access_count", 0) + 1

        return "\n".join(lines)

    def get_full_context(self) -> str:
        """Full context: user profile + memory facts."""
        parts = []
        user = self.get_user_context()
        mem = self.get_memory_context()
        if user:
            parts.append(user)
        if mem:
            parts.append(mem)
        return "\n\n".join(parts)

    # ═══ Stats ═══

    def stats(self) -> dict:
        """Memory usage statistics."""
        with self._lock:
            mem_chars = sum(len(e.get("text", "")) for e in self._entries["memory"])
            user_chars = sum(len(e.get("text", "")) for e in self._entries["user"])
        return {
            "memory_entries": len(self._entries["memory"]),
            "memory_chars": mem_chars,
            "user_entries": len(self._entries["user"]),
            "user_chars": user_chars,
            "total_chars": mem_chars + user_chars,
            "memory_pct": min(100, int(mem_chars / 2200 * 100)),
            "user_pct": min(100, int(user_chars / 1375 * 100)),
            "backend": "engram",
        }
