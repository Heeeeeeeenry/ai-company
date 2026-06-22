"""
向量记忆存储 — 基于 sklearn Tfidf + cosine_similarity 的轻量语义搜索

零外部依赖 (scikit-learn 已安装), 模型极小 (~1MB), 对中文短文本 (50-200字) 效果足够.

用法:
    store = VectorMemoryStore()
    store.add("用户偏好简洁回复", {"category": "preference"})
    results = store.search("回复风格", top_k=5)
    # → [{text, metadata, score}]
"""

import json
import os
import pickle
import hashlib
import logging
from threading import RLock
from typing import Optional

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

logger = logging.getLogger("ai_company.vector_memory")

INDEX_PATH = os.path.expanduser("~/.ai-company/memory_index.pkl")


class VectorMemoryStore:
    """Tfidf向量记忆存储 — 语义搜索增强

    用 char_wb 分析器处理中文 (2-4 gram), 无需分词.
    模型持久化到 ~/.ai-company/memory_index.pkl.
    """

    def __init__(self, index_path: str = INDEX_PATH):
        self.vectorizer = TfidfVectorizer(
            max_features=500,
            analyzer='char_wb',
            ngram_range=(2, 4),
        )
        self.entries: list[dict] = []  # [{id, text, metadata, vector}]
        self._fitted = False
        self._matrix = None  # np.ndarray, shape (n_entries, n_features)
        self._lock = RLock()
        self.index_path = os.path.expanduser(index_path)
        self._load_index()

    # ═══ ID generation ═══

    @staticmethod
    def _make_id(text: str) -> str:
        """Generate a stable ID from text content (SHA256 first 12 chars)."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]

    # ═══ Persistence ═══

    def _save_index(self):
        """Persist entries and vectorizer state to disk."""
        os.makedirs(os.path.dirname(self.index_path), exist_ok=True)
        with self._lock:
            data = {
                "entries": [
                    {k: v for k, v in e.items() if k != "vector"}
                    for e in self.entries
                ],
                "vectorizer": self.vectorizer,
                "fitted": self._fitted,
            }
        try:
            with open(self.index_path, "wb") as f:
                pickle.dump(data, f)
        except (IOError, pickle.PickleError) as e:
            logger.warning("Failed to save vector index: %s", e)

    def _load_index(self):
        """Load entries and vectorizer from disk, then rebuild the TF-IDF matrix."""
        if not os.path.exists(self.index_path):
            return

        try:
            with open(self.index_path, "rb") as f:
                data = pickle.load(f)
        except (IOError, pickle.PickleError, EOFError) as e:
            logger.warning("Failed to load vector index: %s", e)
            return

        with self._lock:
            self.entries = data.get("entries", [])
            saved_vectorizer = data.get("vectorizer")
            self._fitted = data.get("fitted", False)

            if saved_vectorizer is not None and self._fitted and self.entries:
                self.vectorizer = saved_vectorizer
                # Rebuild TF-IDF matrix from loaded entries
                texts = [e["text"] for e in self.entries]
                try:
                    self._matrix = self.vectorizer.transform(texts)
                except Exception as e:
                    logger.warning("Failed to rebuild matrix: %s — reindexing", e)
                    self._fitted = False
                    self._matrix = None

    # ═══ Rebuild matrix helper ═══

    def _rebuild_matrix(self):
        """Re-fit the vectorizer on all current entries and rebuild the matrix."""
        if not self.entries:
            self._matrix = None
            self._fitted = False
            return
        texts = [e["text"] for e in self.entries]
        self._matrix = self.vectorizer.fit_transform(texts)
        self._fitted = True

    # ═══ Core Operations ═══

    def add(self, text: str, metadata: Optional[dict] = None) -> str:
        """Add a text entry to the index.

        Args:
            text: 记忆文本
            metadata: 可选的元数据 (category, created_at 等)

        Returns:
            entry_id: 条目唯一 ID
        """
        text = text.strip()
        if not text:
            raise ValueError("Cannot add empty text to vector store")

        entry_id = self._make_id(text)

        with self._lock:
            # Check for duplicate by text
            for e in self.entries:
                if e.get("text", "").strip() == text:
                    # Update metadata if provided
                    if metadata:
                        e["metadata"] = {**(e.get("metadata") or {}), **metadata}
                    self._save_index()
                    return e["id"]

            entry = {
                "id": entry_id,
                "text": text,
                "metadata": metadata or {},
            }
            self.entries.append(entry)

            # Rebuild the full matrix (simple approach for small N)
            self._rebuild_matrix()

        self._save_index()
        return entry_id

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """TF-IDF + cosine 语义搜索.

        Args:
            query: 搜索查询
            top_k: 返回结果数

        Returns:
            [{text, metadata, score}] 按相似度降序
        """
        query = query.strip()
        if not query or not self._fitted or self._matrix is None:
            return []

        with self._lock:
            try:
                query_vec = self.vectorizer.transform([query])
                similarities = cosine_similarity(query_vec, self._matrix)[0]
            except Exception as e:
                logger.warning("Vector search failed: %s", e)
                return []

        # Get top_k indices
        if len(similarities) == 0:
            return []

        # Sort and take top_k
        indices = np.argsort(similarities)[::-1]
        results = []
        for idx in indices[:top_k]:
            score = float(similarities[idx])
            if score < 0.01:  # Don't return near-zero matches
                continue
            e = self.entries[idx]
            results.append({
                "id": e["id"],
                "text": e["text"],
                "metadata": e.get("metadata", {}),
                "score": round(score, 4),
            })
        return results

    def build_index(self, texts: list[str], metadatas: Optional[list[dict]] = None):
        """一次性构建全部索引 (覆盖现有).

        Args:
            texts: 文本列表
            metadatas: 对应的元数据列表
        """
        if metadatas is None:
            metadatas = [{} for _ in texts]

        with self._lock:
            self.entries = []
            for text, meta in zip(texts, metadatas):
                text = text.strip()
                if not text:
                    continue
                self.entries.append({
                    "id": self._make_id(text),
                    "text": text,
                    "metadata": meta,
                })
            self._rebuild_matrix()

        self._save_index()
        logger.info("Vector index built: %d entries, %d features",
                     len(self.entries),
                     self._matrix.shape[1] if self._matrix is not None else 0)

    def remove(self, entry_id: str) -> bool:
        """从索引中移除条目.

        Args:
            entry_id: 要移除的条目 ID

        Returns:
            是否找到并移除
        """
        with self._lock:
            for i, e in enumerate(self.entries):
                if e["id"] == entry_id:
                    self.entries.pop(i)
                    self._rebuild_matrix()
                    self._save_index()
                    return True
        return False

    def remove_by_text(self, text: str) -> int:
        """按文本 (子串匹配) 移除条目.

        Args:
            text: 子串匹配

        Returns:
            移除的条目数
        """
        removed = 0
        with self._lock:
            new_entries = []
            for e in self.entries:
                if text in e.get("text", ""):
                    removed += 1
                else:
                    new_entries.append(e)
            if removed:
                self.entries = new_entries
                self._rebuild_matrix()
                self._save_index()
        return removed

    # ═══ Query helpers ═══

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def is_empty(self) -> bool:
        return len(self.entries) == 0


# ═══ Module Singleton ═══

vector_store = VectorMemoryStore()
