"""HermesMemory — Hermes-style declarative memory system for AI Company.

Two targets:
  - memory: Durable facts, environment details, tool quirks, lessons learned
  - user:   User profile — preferences, name, timezone, habits

Operations: add, replace (by old_text match), remove (by text match), list.

Storage: ~/.ai-company/hermes_memory.json
Format: compact declarative facts — NOT instructions to self.
  ✓ "User prefers concise responses"
  ✗ "Always respond concisely"

Injection priority: user preferences > environment facts > procedural knowledge.
Each entry tracks created_at and access_count for optimal token usage.
"""

from __future__ import annotations

import json
import os
import logging
from datetime import datetime
from threading import Lock
from typing import Optional

logger = logging.getLogger("ai_company.hermes_memory")

DATA_DIR = os.path.expanduser("~/.ai-company")
MEMORY_FILE = os.path.join(DATA_DIR, "hermes_memory.json")


# ═══ Default entries ═══

DEFAULT_MEMORY = [
    {
        "text": "禁止主动删数据。需删除时中文二次确认。DROP/DELETE/TRUNCATE/git push -f 等破坏性操作先询问。",
        "category": "safety",
    },
    {
        "text": "修改代码后必须端到端测试验证才能说'完成'。禁止未测试就声称完成。",
        "category": "quality",
    },
    {
        "text": "3+轮未解→激活stubborn-problem-workflow；不要保守不等提示。",
        "category": "process",
    },
    {
        "text": "ai-company Python 3.12 (conda: /Users/v_liheng02/work/conda/anaconda3/bin/python3.12)，通过 goudan 启动。DeepSeek API (deepseek-chat)，LangGraph 工作流。",
        "category": "environment",
    },
    {
        "text": "wechat paste: pbcopy+Cmd/V 发中文; 文件传输助手 sidebar Cmd+1→Down→Enter。Kimi vision moonshot-v1-8k-vision-preview。Qwen-VL 精确匹配 is_exact_match。",
        "category": "tool",
    },
    {
        "text": "WeChat发送是canary功能——如果坏了用户立刻发现并愤怒。任何代码改动后必须验证WeChat发送仍可用才声称完成。",
        "category": "critical",
    },
    {
        "text": "偏好快速响应：综合查询时并行发起操作，不串行等待。偏好自主决策：'自行决断'=直接执行不逐项确认。",
        "category": "preference",
    },
]

DEFAULT_USER = [
    {
        "text": "语言偏好: 简体中文",
    },
    {
        "text": "OS: macOS (iTerm2透明85%, retina 2880x1800)",
    },
    {
        "text": "交互习惯: 偏好自主决策，'自行决断'=直接执行不逐项确认。复杂任务用 delegate_task 多智能体并行。",
    },
    {
        "text": "项目: ai-company (狗蛋儿) 独立进程个人助手; VoiceDirect (民意直通车) Go+Gin+Vue3 警用信访系统; 五个相关代码库。",
    },
    {
        "text": "push规则: 晚上7点(19:00)左右的push直接执行不用询问；其他时间段需先询问。涉及5个代码库。",
    },
    {
        "text": "微信联系人: 小号/文件传输助手(默认)。不再使用小媛儿宝儿。",
    },
]


class HermesMemory:
    """Hermes-style memory with add/replace/remove operations.

    Two stores: memory (facts/knowledge) and user (preferences/profile).
    Each entry is a declarative fact with metadata.
    """

    def __init__(self):
        self._memory: list[dict] = []
        self._user: list[dict] = []
        self._lock = Lock()
        self._vector_store = None  # Lazy init
        self._load()

    # ═══ Load / Save ═══

    def _load(self):
        """Load from disk, merge with defaults."""
        loaded_memory = []
        loaded_user = []

        if os.path.exists(MEMORY_FILE):
            try:
                with open(MEMORY_FILE) as f:
                    data = json.load(f)
                loaded_memory = data.get("memory", [])
                loaded_user = data.get("user", [])
            except (json.JSONDecodeError, IOError):
                pass

        # Merge defaults: keep loaded entries, add defaults if not already present
        existing_texts_mem = {e.get("text", "") for e in loaded_memory}
        for default in DEFAULT_MEMORY:
            if default["text"] not in existing_texts_mem:
                loaded_memory.append({**default, "created_at": datetime.now().isoformat(), "access_count": 0})

        existing_texts_user = {e.get("text", "") for e in loaded_user}
        for default in DEFAULT_USER:
            if default["text"] not in existing_texts_user:
                loaded_user.append({**default, "created_at": datetime.now().isoformat(), "access_count": 0})

        with self._lock:
            self._memory = loaded_memory
            self._user = loaded_user

    def _save(self):
        """Persist to disk."""
        os.makedirs(DATA_DIR, exist_ok=True)
        with self._lock:
            data = {
                "memory": self._memory,
                "user": self._user,
                "updated_at": datetime.now().isoformat(),
            }
        with open(MEMORY_FILE, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _get_vector_store(self):
        """Lazy-init vector store for semantic search."""
        if self._vector_store is None:
            try:
                from src.memory.vector_store import VectorMemoryStore
                self._vector_store = VectorMemoryStore()
                # Sync existing entries into vector store
                self._sync_to_vector_store()
            except ImportError as e:
                logger.warning("Vector store unavailable: %s", e)
            except Exception as e:
                logger.warning("Vector store init failed: %s", e)
        return self._vector_store

    def _sync_to_vector_store(self):
        """Rebuild vector index from current memory entries."""
        vs = self._vector_store
        if vs is None:
            return
        with self._lock:
            all_texts = [e["text"] for e in self._memory]
            all_metas = [{k: v for k, v in e.items() if k != "text"} for e in self._memory]
        if all_texts:
            vs.build_index(all_texts, all_metas)

    # ═══ Core Operations ═══

    def add(self, text: str, target: str = "memory", category: str = "") -> dict:
        """Add a declarative memory entry.

        Args:
            text: Declarative fact (NOT an instruction to self)
            target: 'memory' or 'user'
            category: Optional tag for grouping (safety, quality, tool, etc.)

        Returns:
            The added entry dict.
        """
        now = datetime.now().isoformat()
        entry = {
            "text": text.strip(),
            "created_at": now,
            "access_count": 0,
        }
        if category:
            entry["category"] = category

        store = self._memory if target == "memory" else self._user
        is_new = True
        with self._lock:
            # Check for duplicates
            for existing in store:
                if existing.get("text", "").strip() == text.strip():
                    existing["access_count"] = existing.get("access_count", 0) + 1
                    existing["updated_at"] = now
                    self._save()
                    return existing
            store.append(entry)
        self._save()

        # Sync to vector store (memory target only)
        if target == "memory" and is_new:
            try:
                vs = self._get_vector_store()
                if vs:
                    vs.add(text.strip(), {k: v for k, v in entry.items() if k != "text"})
            except Exception as e:
                logger.warning("Vector store add failed: %s", e)

        return entry

    def replace(self, old_text: str, new_text: str, target: str = "memory") -> bool:
        """Replace a memory entry matching old_text substring.

        Args:
            old_text: Unique substring to identify the entry to replace
            new_text: Replacement text
            target: 'memory' or 'user'

        Returns:
            True if a match was found and replaced, False otherwise.
        """
        store = self._memory if target == "memory" else self._user
        replaced = False
        with self._lock:
            matches = [e for e in store if old_text in e.get("text", "")]
            if len(matches) != 1:
                return False  # Need exactly one match for safety
            matches[0]["text"] = new_text.strip()
            matches[0]["updated_at"] = datetime.now().isoformat()
            replaced = True
        if replaced:
            self._save()
            # Sync to vector store: remove old, add new
            if target == "memory":
                try:
                    vs = self._get_vector_store()
                    if vs:
                        vs.remove_by_text(old_text)
                        vs.add(new_text.strip(), {
                            k: v for k, v in matches[0].items() if k != "text"
                        })
                except Exception as e:
                    logger.warning("Vector store replace failed: %s", e)
        return replaced

    def remove(self, text: str, target: str = "memory") -> int:
        """Remove memory entries matching text substring. Requires confirmation.

        Args:
            text: Substring to match
            target: 'memory' or 'user'

        Returns:
            Number of entries removed.
        """
        store = self._memory if target == "memory" else self._user
        removed = 0
        with self._lock:
            new_store = []
            for e in store:
                if text in e.get("text", ""):
                    removed += 1
                else:
                    new_store.append(e)
            if target == "memory":
                self._memory = new_store
            else:
                self._user = new_store
        if removed:
            self._save()
            # Sync to vector store
            if target == "memory":
                try:
                    vs = self._get_vector_store()
                    if vs:
                        vs.remove_by_text(text)
                except Exception as e:
                    logger.warning("Vector store remove failed: %s", e)
        return removed

    def list(self, target: str = "memory") -> list[dict]:
        """List all entries for a target."""
        store = self._memory if target == "memory" else self._user
        with self._lock:
            return list(store)

    def search(self, query: str, target: str = "memory"):
        # Returns list of dict
        """Hybrid search: vector semantic (0.6) + keyword (0.4).

        Returns entries sorted by combined score.
        Falls back to keyword-only if vector store unavailable.
        """
        store = self._memory if target == "memory" else self._user

        # ── Vector semantic search ──
        vector_results: dict[str, float] = {}  # text → score
        try:
            vs = self._get_vector_store()
            if vs and not vs.is_empty:
                vec_hits = vs.search(query, top_k=min(len(store), 10))
                for hit in vec_hits:
                    vector_results[hit["text"]] = hit["score"]
        except Exception as e:
            logger.warning("Vector search failed, falling back to keyword: %s", e)

        # ── Keyword search (substring) ──
        query_lower = query.lower()
        keyword_results: dict[str, float] = {}
        for e in store:
            text = e.get("text", "")
            if query_lower in text.lower():
                score = 1.0 if query_lower == text.lower() else 0.8 if f" {query_lower}" in text.lower() else 0.5
                keyword_results[text] = score

        # ── Merge scores ──
        all_texts = set(list(vector_results.keys()) + list(keyword_results.keys()))
        results = []
        for e in store:
            text = e["text"]
            if text not in all_texts and not vector_results:
                continue  # No vector available and no keyword match — skip
            vec_score = vector_results.get(text, 0.0)
            kw_score = keyword_results.get(text, 0.0)

            if vec_score > 0 and kw_score > 0:
                # Both matched — weighted hybrid
                combined = vec_score * 0.6 + kw_score * 0.4
            elif vec_score > 0:
                combined = vec_score * 1.0  # Only vector
            elif kw_score > 0:
                combined = kw_score * 1.0  # Only keyword
            else:
                continue

            results.append({**e, "score": round(combined, 4),
                           "vec_score": round(vec_score, 4),
                           "kw_score": round(kw_score, 4)})

        results.sort(key=lambda x: x["score"], reverse=True)
        return results

    def semantic_search(self, query: str, target: str = "memory", top_k: int = 5) -> list[dict]:
        """纯向量语义搜索 — 优先向量, 回退关键词.

        Args:
            query: 搜索查询
            target: 'memory' or 'user'
            top_k: 返回结果数

        Returns:
            [{text, metadata, score}] 按相关度降序
        """
        store = self._memory if target == "memory" else self._user
        if not store:
            return []

        # ── Try vector search first ──
        try:
            vs = self._get_vector_store()
            if vs and not vs.is_empty:
                results = vs.search(query, top_k=top_k)
                if results:
                    # Merge with full entry metadata
                    enriched = []
                    for r in results:
                        for e in store:
                            if e.get("text", "") == r["text"]:
                                enriched.append({**e, "score": r["score"]})
                                break
                        else:
                            enriched.append(r)
                    return enriched[:top_k]
        except Exception as e:
            logger.warning("Vector semantic search failed, falling back: %s", e)

        # ── Fallback to keyword search ──
        return self.search(query, target=target)[:top_k]

    # ═══ Prompt Injection ═══

    def get_user_context(self) -> str:
        """Build compact user profile for LLM prompt injection.

        Hermes-style: one line per fact, no markdown formatting.
        """
        with self._lock:
            entries = list(self._user)
        if not entries:
            return ""

        lines = ["## USER PROFILE"]
        for e in entries:
            cat = f"[{e.get('category', '')}] " if e.get("category") else ""
            lines.append(f"- {cat}{e['text']}")
            # Bump access count
            e["access_count"] = e.get("access_count", 0) + 1
        return "\n".join(lines)

    def get_memory_context(self) -> str:
        """Build compact memory facts for LLM prompt injection.

        Sorted by: category=critical > safety > quality > access_count.
        Hermes-style: compact declarative facts.
        """
        with self._lock:
            entries = list(self._memory)
        if not entries:
            return ""

        # Sort: critical first, then by access_count desc
        cat_priority = {"critical": 0, "safety": 1, "quality": 2, "process": 3, "environment": 4, "tool": 5, "preference": 6}
        entries.sort(key=lambda e: (cat_priority.get(e.get("category", ""), 99), -(e.get("access_count", 0))))

        total_chars = sum(len(e["text"]) for e in entries)
        pct = min(100, int(total_chars / 2200 * 100))  # ~2200 char budget

        lines = [f"## MEMORY [{pct}% — {total_chars}/2,200 chars]"]
        for e in entries:
            lines.append(f"- {e['text']}")
            # Bump access count
            e["access_count"] = e.get("access_count", 0) + 1

        return "\n".join(lines)

    def get_full_context(self) -> str:
        """Full context for LLM prompt: user profile + memory facts."""
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
            mem_chars = sum(len(e.get("text", "")) for e in self._memory)
            user_chars = sum(len(e.get("text", "")) for e in self._user)
        return {
            "memory_entries": len(self._memory),
            "memory_chars": mem_chars,
            "user_entries": len(self._user),
            "user_chars": user_chars,
            "total_chars": mem_chars + user_chars,
            "memory_pct": min(100, int(mem_chars / 2200 * 100)),
            "user_pct": min(100, int(user_chars / 1375 * 100)),
        }


# ═══ Module Singleton ═══

hermes_memory = HermesMemory()
