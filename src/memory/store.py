"""Memory Layer - Graphiti Knowledge Graph + Letta Agent Memory + Chroma Vector Store

Three-tier memory architecture:
  1. EpisodeMemory — Episodic memory (what happened), Graphiti-backed with local fallback
  2. AgentState — Letta-style working memory (what's happening now)
  3. ChromaStore — Semantic vector search (what's similar), Chroma-backed
"""

import os
import json
import re
import logging
from typing import Optional
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("ai_company.memory")


# ─── Helpers ──────────────────────────────────────

def _ensure_data_dir() -> Path:
    """Ensure the data directory exists and return its path."""
    base = Path(__file__).resolve().parents[2] / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


# ═══════════════════════════════════════════════════
# EpisodeMemory — Episodic Memory with Persistence
# ═══════════════════════════════════════════════════

class EpisodeMemory:
    """Graphiti-style episodic + semantic memory backed by knowledge graph.

    Features:
    - Graphiti server integration (primary backend)
    - Local file-backed fallback with persistence
    - Improved search (scored token overlap, not just substring)
    - Near-duplicate detection
    - Configurable retention (max episodes, auto-prune)

    Fallback gracefully if Graphiti server is not running.
    """

    def __init__(
        self,
        graphiti_url: str = "http://localhost:8000",
        storage_path: str = None,
        max_episodes: int = None,
        dedup_threshold: float = 0.85,
    ):
        self.graphiti_url = graphiti_url
        self._client = None
        self._memory: list[dict] = []  # local fallback
        self._dirty = False

        # File persistence
        if storage_path is None:
            storage_path = str(_ensure_data_dir() / "episodes.json")
        self.storage_path = storage_path

        # Config
        self.max_episodes = max_episodes or int(
            os.getenv("MEMORY_MAX_EPISODES", "1000")
        )
        self.dedup_threshold = dedup_threshold  # Jaccard similarity threshold

        # ── Metrics tracking ──
        self._metrics = {
            "total_adds": 0,
            "dedup_skips": 0,
            "saves": 0,
            "loads": 0,
            "searches": 0,
            "prunes": 0,
            "compactions": 0,
            "total_bytes_stored": 0,
            "last_metrics_reset": datetime.now().isoformat(),
        }
        self._metrics_path = str(_ensure_data_dir() / "memory_metrics.json")
        self._load_metrics()

        self._load()

    # ── Persistence ───────────────────────────

    def _load(self):
        """Load episodes from disk."""
        try:
            if os.path.exists(self.storage_path):
                with open(self.storage_path, "r") as f:
                    data = json.load(f)
                    self._memory = data.get("episodes", [])
                    self._metrics["loads"] += 1
                    logger.info(
                        "EpisodeMemory loaded %d episodes from %s",
                        len(self._memory), self.storage_path,
                    )
        except Exception:
            logger.debug("EpisodeMemory load failed, starting fresh", exc_info=True)
            self._memory = []

    def _save(self):
        """Persist episodes to disk."""
        try:
            os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
            self._metrics["total_bytes_stored"] = len(
                json.dumps(self._memory, ensure_ascii=False)
            )
            with open(self.storage_path, "w") as f:
                json.dump(
                    {
                        "episodes": self._memory,
                        "count": len(self._memory),
                        "last_saved": datetime.now().isoformat(),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            self._dirty = False
            self._metrics["saves"] += 1
            self._save_metrics()
        except Exception:
            logger.debug("EpisodeMemory save failed", exc_info=True)

    def _save_metrics(self):
        """Persist metrics to disk."""
        try:
            with open(self._metrics_path, "w") as f:
                json.dump(self._metrics, f, ensure_ascii=False, indent=2)
        except Exception:
            _ = None  # Non-critical: metrics save failure is silent

    def _load_metrics(self):
        """Load metrics from disk."""
        try:
            if os.path.exists(self._metrics_path):
                with open(self._metrics_path, "r") as f:
                    stored = json.load(f)
                    self._metrics.update(stored)
        except Exception:
            self._metrics = {k: (0 if isinstance(v, int) else v) for k, v in self._metrics.items()}
            self._metrics["last_metrics_reset"] = datetime.now().isoformat()

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        try:
            from graphiti_core import Graphiti
            self._client = Graphiti(self.graphiti_url)
            logger.info("Graphiti client connected to %s", self.graphiti_url)
        except Exception:
            self._client = False  # sentinel for fallback mode
            logger.debug("Graphiti unavailable, using local fallback")
        return self._client

    # ── Add episode ────────────────────────────

    async def add_episode(
        self, content: str, role: str, metadata: dict = None
    ):
        """Record an episode (decision, event, outcome).

        If the content is a near-duplicate of an existing episode, skip.
        """
        # ── Dedup check ──
        if self._is_duplicate(content):
            logger.debug("EpisodeMemory: skipping duplicate episode for role=%s", role)
            return

        episode = {
            "timestamp": datetime.now().isoformat(),
            "role": role,
            "content": content,
            "metadata": metadata or {},
        }

        client = await self._ensure_client()
        if client and client is not False:
            try:
                await client.add_episode(
                    name=f"{role}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                    episode_body=content,
                    source="ai_company",
                    source_description=f"Action by {role}",
                )
            except Exception:
                logger.debug(
                    "Graphiti add_episode failed, falling back to local", exc_info=True
                )

        # Local fallback — always store for reliability
        self._memory.append(episode)
        self._dirty = True

        # Auto-prune if over limit
        if len(self._memory) > self.max_episodes:
            excess = len(self._memory) - self.max_episodes
            kept = max(100, self.max_episodes // 2)
            self._memory = self._memory[-(self.max_episodes - kept):]
            logger.info(
                "EpisodeMemory pruned %d entries, kept %d",
                excess, len(self._memory),
            )

        # Save periodically (every 5 new episodes)
        if len(self._memory) % 5 == 0:
            self._save()

    # ── Dedup logic ────────────────────────────

    def _is_duplicate(self, content: str) -> bool:
        """Check if content is a near-duplicate of an existing episode."""
        if not self._memory or len(content) < 20:
            return False

        # Compare against last N episodes
        recent = self._memory[-50:]
        tokens_a = self._tokenize(content)

        for ep in recent:
            tokens_b = self._tokenize(ep.get("content", ""))
            sim = self._jaccard_similarity(tokens_a, tokens_b)
            if sim >= self.dedup_threshold:
                return True
        return False

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """Tokenize Chinese + English text using jieba for Chinese.

        Falls back to character-level if jieba is not installed.
        """
        cleaned = re.sub(r"[^\w\u4e00-\u9fff]", " ", text.lower())

        # Separate CJK from non-CJK segments
        cjk_parts: list[str] = []
        non_cjk_parts: list[str] = []

        for word in cleaned.split():
            if re.search(r"[\u4e00-\u9fff]", word):
                cjk_parts.append(word)
            elif len(word) > 1:
                non_cjk_parts.append(word)

        tokens = set(non_cjk_parts)

        # Try jieba for Chinese segmentation
        if cjk_parts:
            try:
                import jieba
                for part in cjk_parts:
                    cut = jieba.cut(part, cut_all=False)
                    tokens.update(w for w in cut if len(w) > 1)
            except ImportError:
                # jieba not installed — fall back to character-level
                for part in cjk_parts:
                    tokens.update(part)

        return tokens

    @staticmethod
    def _jaccard_similarity(a: set, b: set) -> float:
        """Jaccard similarity between two token sets."""
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    # ── Search ─────────────────────────────────

    async def search(self, query: str, limit: int = 5) -> list[dict]:
        """Hybrid search across episodes.

        Primary: Graphiti semantic search
        Fallback: Scored token-overlap search (TF-IDF style)
        """
        self._metrics["searches"] += 1
        client = await self._ensure_client()

        if client and client is not False:
            try:
                results = await client.search(query, max_facts=limit)
                return [
                    {"content": r.fact, "score": getattr(r, "score", 0.0)}
                    for r in results[:limit]
                ]
            except Exception:
                logger.debug(
                    "Graphiti search failed, falling back to local", exc_info=True
                )

        # ── Local scored search ──
        return self._local_search(query, limit)

    def _local_search(self, query: str, limit: int = 5) -> list[dict]:
        """Scored local search using token overlap (TF-IDF style).

        Better than simple substring matching:
        - Scores by token overlap ratio
        - Boosts exact phrase matches
        - Weights by recency (newer = higher)
        """
        if not self._memory:
            return []

        query_tokens = self._tokenize(query)

        scored = []
        for i, ep in enumerate(self._memory):
            content = ep.get("content", "")
            content_tokens = self._tokenize(content)

            # Token overlap score (0.0 - 1.0)
            overlap_score = self._jaccard_similarity(query_tokens, content_tokens)

            # Boost exact substring match (bonus up to 0.3)
            substring_bonus = 0.0
            query_lower = query.lower()
            content_lower = content.lower()
            if query_lower in content_lower:
                substring_bonus = 0.3
            elif any(q in content_lower for q in query_lower.split() if len(q) > 2):
                substring_bonus = 0.15

            # Recency boost: newer episodes get slight bonus (max 0.1)
            recency_bonus = 0.1 * (i / max(len(self._memory), 1))

            score = overlap_score + substring_bonus + recency_bonus

            if score > 0.05:  # Minimum threshold
                scored.append({
                    "content": content,
                    "score": round(score, 4),
                    "role": ep.get("role", "unknown"),
                    "timestamp": ep.get("timestamp", ""),
                })

        # Sort by score descending
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    # ── Context builder ────────────────────────

    async def get_context(self, query: str, limit: int = 5) -> str:
        """Get formatted context string for LLM prompt."""
        results = await self.search(query, limit=limit)
        if not results:
            return "No relevant memory found."

        lines = []
        for r in results:
            role = r.get("role", r.get("source", "unknown"))
            content = r.get("content", str(r))
            score = r.get("score", 0)
            lines.append(f"[{role}] (relevance: {score:.2f}) {content[:300]}")
        return "\n".join(lines)

    # ── Stats & maintenance ────────────────────

    def stats(self) -> dict:
        """Return memory statistics with performance metrics."""
        roles = {}
        for ep in self._memory:
            r = ep.get("role", "unknown")
            roles[r] = roles.get(r, 0) + 1

        total_adds = self._metrics["total_adds"]
        dedup_rate = (
            round(self._metrics["dedup_skips"] / total_adds * 100, 1)
            if total_adds > 0 else 0
        )

        # Size distribution by role (top 5)
        top_roles = sorted(roles.items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            # Storage
            "total_episodes": len(self._memory),
            "storage_path": self.storage_path,
            "storage_size_bytes": self._metrics["total_bytes_stored"],
            "dirty": self._dirty,
            # Activity
            "total_adds": total_adds,
            "dedup_skips": self._metrics["dedup_skips"],
            "dedup_rate_pct": dedup_rate,
            "saves": self._metrics["saves"],
            "loads": self._metrics["loads"],
            "searches": self._metrics["searches"],
            "prunes": self._metrics["prunes"],
            "compactions": self._metrics["compactions"],
            # Breakdown
            "by_role": dict(top_roles),
            "role_count": len(roles),
            # Timing
            "last_episode_time": (
                self._memory[-1]["timestamp"] if self._memory else None
            ),
            "first_episode_time": (
                self._memory[0]["timestamp"] if self._memory else None
            ),
            "metrics_last_reset": self._metrics["last_metrics_reset"],
        }

    async def force_save(self):
        """Explicitly force save to disk."""
        self._save()

    async def clear(self):
        """Clear all episodes (memory + disk)."""
        self._memory = []
        self._dirty = True
        self._save()
        logger.info("EpisodeMemory cleared")

    async def compact(self, keep_recent: int = 200):
        """Compact memory: summarize old episodes, keep recent ones raw.

        Returns a summary of what was compacted.
        """
        if len(self._memory) <= keep_recent:
            return {"compacted": 0, "message": "Not enough episodes to compact"}

        old = self._memory[:-keep_recent]
        self._memory = self._memory[-keep_recent:]

        # Build summary
        roles = {}
        for ep in old:
            r = ep.get("role", "unknown")
            roles[r] = roles.get(r, 0) + 1

        summary = {
            "timestamp": datetime.now().isoformat(),
            "role": "system",
            "content": f"[COMPACTED] {len(old)} historical episodes: {roles}",
            "metadata": {"compacted_count": len(old), "roles": roles},
        }
        self._memory.insert(0, summary)
        self._dirty = True
        self._save()

        return {
            "compacted": len(old),
            "kept": len(self._memory),
            "message": f"Compacted {len(old)} old episodes, kept {keep_recent} recent",
        }


# ═══════════════════════════════════════════════════
# AgentState — Letta-style Working Memory
# ═══════════════════════════════════════════════════

class AgentState:
    """Letta-style agent state: keeps persona + context across interactions.

    Features:
    - File-backed persistence (survives restarts)
    - Auto-save on every mutation
    - Working memory with smart retention
    - Decision logs

    File-backed implementation; swap in Letta client for production.
    """

    def __init__(
        self,
        agent_id: str,
        letta_url: str = "http://localhost:8283",
        storage_dir: str = None,
    ):
        self.agent_id = agent_id
        self.letta_url = letta_url

        # File persistence
        if storage_dir is None:
            storage_dir = str(_ensure_data_dir() / "agent_states")
        self.storage_dir = storage_dir
        self.storage_path = os.path.join(storage_dir, f"{agent_id}.json")

        self._state: dict = {
            "current_task": None,
            "context": {},
            "working_memory": [],
            "decisions_log": [],
        }

        self._load()

    # ── Persistence ───────────────────────────

    def _load(self):
        """Load agent state from disk."""
        try:
            if os.path.exists(self.storage_path):
                with open(self.storage_path, "r") as f:
                    data = json.load(f)
                    # Merge with defaults to handle schema changes
                    defaults = dict(self._state)
                    defaults.update(data)
                    self._state = defaults
                    logger.debug(
                        "AgentState loaded for %s (%d decisions, %d wm items)",
                        self.agent_id,
                        len(self._state.get("decisions_log", [])),
                        len(self._state.get("working_memory", [])),
                    )
        except Exception:
            logger.debug(
                "AgentState load failed for %s, starting fresh", self.agent_id,
                exc_info=True,
            )

    def _save(self):
        """Persist agent state to disk."""
        try:
            os.makedirs(self.storage_dir, exist_ok=True)
            with open(self.storage_path, "w") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.debug(
                "AgentState save failed for %s", self.agent_id, exc_info=True,
            )

    # ── Mutations ─────────────────────────────

    def set_task(self, task: str, context: dict = None):
        self._state["current_task"] = task
        if context:
            self._state["context"].update(context)
        self._save()

    def add_to_working_memory(self, item: str):
        self._state["working_memory"].append(item)
        # Smart retention: keep unique items, trim oldest when over limit
        max_items = 100
        if len(self._state["working_memory"]) > max_items:
            # Keep last 60, prefer unique
            recent = self._state["working_memory"][-60:]
            older = self._state["working_memory"][:-60]
            # Add back any unique older items (up to 20)
            seen = set(recent)
            unique_old = []
            for item_text in reversed(older):
                if item_text not in seen and len(unique_old) < 20:
                    unique_old.append(item_text)
                    seen.add(item_text)
            self._state["working_memory"] = unique_old[::-1] + recent
        self._save()

    def log_decision(self, decision: str, reason: str):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "decision": decision,
            "reason": reason,
        }
        self._state["decisions_log"].append(entry)
        # Keep last 200 decisions
        if len(self._state["decisions_log"]) > 200:
            self._state["decisions_log"] = self._state["decisions_log"][-100:]
        self._save()

    # ── Accessors ─────────────────────────────

    def get_task(self) -> Optional[str]:
        return self._state["current_task"]

    def get_context(self) -> dict:
        return self._state["context"]

    def get_summary(self) -> str:
        """Human-readable state summary for the CEO."""
        lines = [
            f"Agent: {self.agent_id}",
            f"Current Task: {self._state['current_task'] or 'Idle'}",
            f"Context: {self._state['context'] or 'None'}",
            f"Working Memory ({len(self._state['working_memory'])} items):",
        ]
        for item in self._state["working_memory"][-5:]:
            lines.append(f"  - {item[:100]}")

        if self._state["decisions_log"]:
            lines.append(
                f"Recent Decisions ({len(self._state['decisions_log'])} total):"
            )
            for d in self._state["decisions_log"][-3:]:
                lines.append(
                    f"  [{d['timestamp']}] {d['decision']}: {d['reason'][:80]}"
                )

        return "\n".join(lines)

    # ── Lifecycle ─────────────────────────────

    def clear_task(self):
        self._state["current_task"] = None
        self._state["working_memory"] = []
        self._save()

    def reset(self):
        """Full reset of agent state."""
        self._state = {
            "current_task": None,
            "context": {},
            "working_memory": [],
            "decisions_log": [],
        }
        self._save()


# ═══════════════════════════════════════════════════
# ChromaVectorStore — Semantic Vector Search
# ═══════════════════════════════════════════════════

class ChromaVectorStore:
    """Chroma-backed vector store for semantic memory search.

    Uses sentence-transformers for embeddings by default.
    Falls back gracefully if Chroma is not installed or server is down.
    """

    def __init__(
        self,
        chroma_url: str = None,
        collection_name: str = "ai_company_memory",
        embedding_model: str = None,
    ):
        self.chroma_url = chroma_url or os.getenv(
            "CHROMA_URL", "http://localhost:8001"
        )
        self.collection_name = collection_name
        self.embedding_model = embedding_model or os.getenv(
            "EMBEDDING_MODEL", "all-MiniLM-L6-v2"
        )
        self._collection = None
        self._embed_fn = None
        self._available = None  # None = not checked yet

    def _check_available(self) -> bool:
        """Check if Chroma and embedding model are available."""
        if self._available is not None:
            return self._available

        try:
            import chromadb
            from chromadb.config import Settings

            self._chroma_client = chromadb.Client(Settings(
                chroma_server_host=self.chroma_url.replace("http://", "").split(":")[0],
                chroma_server_http_port=int(
                    self.chroma_url.replace("http://", "").split(":")[1]
                ) if ":" in self.chroma_url.replace("http://", "") else 8001,
            ))

            # Get or create collection
            try:
                self._collection = self._chroma_client.get_collection(
                    self.collection_name
                )
            except Exception:
                self._collection = self._chroma_client.create_collection(
                    self.collection_name
                )

            # Try to load embedding function
            try:
                from chromadb.utils import embedding_functions
                self._embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name=self.embedding_model
                )
            except ImportError:
                # Fall back to default Chroma embedding
                self._embed_fn = embedding_functions.DefaultEmbeddingFunction()

            self._available = True
            logger.info("ChromaVectorStore connected to %s", self.chroma_url)
            return True

        except Exception:
            self._available = False
            logger.debug("Chroma unavailable, vector search disabled", exc_info=True)
            return False

    async def add(
        self, text: str, metadata: dict = None, doc_id: str = None
    ) -> Optional[str]:
        """Add a document to the vector store."""
        if not self._check_available():
            return None

        doc_id = doc_id or f"doc_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        try:
            self._collection.add(
                documents=[text],
                metadatas=[metadata or {}],
                ids=[doc_id],
            )
            return doc_id
        except Exception:
            logger.debug("Chroma add failed", exc_info=True)
            return None

    async def search(
        self, query: str, limit: int = 5, filter_meta: dict = None
    ) -> list[dict]:
        """Semantic search in the vector store."""
        if not self._check_available():
            return []

        try:
            results = self._collection.query(
                query_texts=[query],
                n_results=limit,
                where=filter_meta,
            )
            if not results or not results.get("documents") or not results["documents"][0]:
                return []

            return [
                {
                    "content": doc,
                    "score": (
                        1.0 - dist if (dist := results.get("distances", [[]])[0][i]) else 0.0
                    ),
                    "metadata": (
                        results.get("metadatas", [[]])[0][i]
                        if results.get("metadatas") else {}
                    ),
                }
                for i, doc in enumerate(results["documents"][0])
            ]
        except Exception:
            logger.debug("Chroma search failed", exc_info=True)
            return []

    def is_available(self) -> bool:
        """Check if vector search is available."""
        return self._check_available()


# ═══════════════════════════════════════════════════
# Pending Proposals
# ═══════════════════════════════════════════════════

class PendingProposal:
    """A role proposal awaiting user confirmation or action."""

    def __init__(self, proposal_type: str, data: dict):
        self.proposal_type = proposal_type  # "new_role", "role_promotion"
        self.data = data

    def to_message(self) -> str:
        """Format for Telegram notification."""
        if self.proposal_type == "new_role":
            d = self.data
            audit = d.get("audit_result", {})
            score = audit.get("overall_score", "?")
            dims = ", ".join(
                f"{dim['name']}:{dim['score']}"
                for dim in audit.get("dimensions", [])[:3]
            )
            return (
                f"🆕 **建议创建新角色**\n\n"
                f"**{d.get('display_name', '?')}** (`{d.get('name', '?')}`)\n"
                f"描述: {d.get('description', '?')}\n"
                f"用于处理: _{d.get('original_task', '')[:100]}_\n\n"
                f"📊 角色审查: {score}/100 ({audit.get('verdict', '?')})\n"
                f"评分: {dims}\n"
                f"{audit.get('summary', '')}"
            )
        return str(self.data)


# Store pending proposals (keyed by user_id)
_pending_proposals: dict[str, PendingProposal] = {}


def set_pending(user_id: str, proposal: PendingProposal):
    _pending_proposals[user_id] = proposal


def get_pending(user_id: str) -> PendingProposal | None:
    return _pending_proposals.get(user_id)


def clear_pending(user_id: str):
    _pending_proposals.pop(user_id, None)


# ═══════════════════════════════════════════════════
# Global Instances
# ═══════════════════════════════════════════════════

episode_memory = EpisodeMemory()
chroma_store = ChromaVectorStore()
agent_states: dict[str, AgentState] = {}


def get_agent_state(agent_id: str) -> AgentState:
    if agent_id not in agent_states:
        agent_states[agent_id] = AgentState(agent_id)
    return agent_states[agent_id]


# ── Memory Integration Helper ─────────────────

async def sync_episode_to_chroma(episode: dict):
    """Sync a new episode to Chroma vector store (if available)."""
    content = episode.get("content", "")
    if len(content) < 10:
        return
    await chroma_store.add(
        text=content,
        metadata={
            "role": episode.get("role", "unknown"),
            "timestamp": episode.get("timestamp", ""),
        },
    )


def get_memory_health() -> dict:
    """Unified memory health check across all stores."""
    return {
        "episode_memory": episode_memory.stats(),
        "agent_states": {
            aid: {
                "has_task": s.get_task() is not None,
                "wm_items": len(s._state.get("working_memory", [])),
                "decisions": len(s._state.get("decisions_log", [])),
            }
            for aid, s in agent_states.items()
        },
        "chroma_available": chroma_store.is_available(),
    }
