"""MemoryLayer — 统一三级记忆系统

三层记忆架构:
  L1: Session Memory — 当前会话上下文，内存存储（进程生命周期）
  L2: User Memory — 用户全局偏好，持久化到 ~/.ai-company/user_memory.json
  L3: Knowledge Memory — 能力知识/经验，持久化到 ~/.ai-company/knowledge_memory.json

与现有 session 系统兼容:
  - SessionManager / SessionMemory / GlobalMemory 保持不变
  - MemoryLayer 作为更简单的统一接口，无 session_id 概念
  - 可在 CLI 中同时使用两套系统

Usage:
    from src.memory.layer import memory_layer

    # Session (当前会话)
    memory_layer.session_set("task", "修复微信发送")
    memory_layer.session_get("task")

    # User (全局偏好)
    memory_layer.user_set("language", "中文")
    memory_layer.user_get("language")

    # Knowledge (能力知识)
    memory_layer.knowledge_set("wechat_fix", "粘贴节流问题")
    results = memory_layer.knowledge_search("微信")

    # LLM上下文注入
    ctx = memory_layer.get_context_for_prompt()
"""

import json
import os
import re
from datetime import datetime
from threading import Lock
from typing import Any, Optional

# ── Paths ─────────────────────────────────────────────────
DATA_DIR = os.path.expanduser("~/.ai-company")
USER_MEMORY_FILE = os.path.join(DATA_DIR, "user_memory.json")
KNOWLEDGE_MEMORY_FILE = os.path.join(DATA_DIR, "knowledge_memory.json")

# ── Default values ────────────────────────────────────────

DEFAULT_KNOWLEDGE = {
    "wechat_paste_method": "pbcopy + Cmd+V (Chinese text needs clipboard)",
    "wechat_search_toggle": "ESC before Cmd+F (toggle issue)",
    "wechat_vision_model": "moonshot-v1-8k-vision-preview",
    "python_path": "/Users/v_liheng02/work/conda/anaconda3/bin/python3.12",
    "macos_retina_mapping": "image_to_screen_coords divide by 2",
}

DEFAULT_USER = {
    "language": "中文",
    "os": "macOS",
    "response_style": "简洁",
    "auto_execute": True,
}


class MemoryLayer:
    """统一三级记忆系统。

    提供 session/user/knowledge 三层记忆的读写和搜索能力，
    以及 get_context_for_prompt() 用于自动注入 LLM prompt。
    """

    def __init__(self):
        # L1: Session — 纯内存，不持久化
        self._session: dict = {}
        self._session_lock = Lock()

        # L2: User — 文件持久化
        self._user: dict = {}
        self._user_lock = Lock()
        self._load_user()

        # L3: Knowledge — 文件持久化
        self._knowledge: dict = {}
        self._knowledge_lock = Lock()
        self._load_knowledge()

    # ══════════════════════════════════════════════════════
    # L1: Session Memory (当前会话)
    # ══════════════════════════════════════════════════════

    def session_set(self, key: str, value: Any) -> None:
        """设置会话级记忆项（仅当前进程生命周期）。"""
        with self._session_lock:
            self._session[key] = value

    def session_get(self, key: str) -> Optional[Any]:
        """获取会话级记忆项。"""
        return self._session.get(key)

    def session_all(self) -> dict:
        """获取所有会话记忆。"""
        with self._session_lock:
            return dict(self._session)

    def session_clear(self) -> None:
        """清空所有会话记忆。"""
        with self._session_lock:
            self._session.clear()

    # ══════════════════════════════════════════════════════
    # L2: User Memory (用户全局偏好)
    # ══════════════════════════════════════════════════════

    def _load_user(self) -> None:
        """从磁盘加载用户记忆。"""
        if os.path.exists(USER_MEMORY_FILE):
            try:
                with open(USER_MEMORY_FILE) as f:
                    loaded = json.load(f)
                self._user = loaded.get("preferences", {})
            except (json.JSONDecodeError, IOError):
                self._user = {}
        # 确保默认值存在（不覆盖已有值）
        for k, v in DEFAULT_USER.items():
            if k not in self._user:
                self._user[k] = v

    def _save_user(self) -> None:
        """持久化用户记忆到磁盘。"""
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(USER_MEMORY_FILE, "w") as f:
            json.dump(
                {
                    "preferences": self._user,
                    "updated_at": datetime.now().isoformat(),
                },
                f, ensure_ascii=False, indent=2,
            )

    def user_set(self, key: str, value: Any) -> None:
        """设置用户全局偏好。"""
        with self._user_lock:
            self._user[key] = value
        self._save_user()

    def user_get(self, key: str) -> Optional[Any]:
        """获取用户全局偏好。"""
        return self._user.get(key)

    def user_all(self) -> dict:
        """获取所有用户偏好。"""
        with self._user_lock:
            return dict(self._user)

    def user_delete(self, key: str) -> None:
        """删除用户偏好项。"""
        with self._user_lock:
            self._user.pop(key, None)
        self._save_user()

    # ══════════════════════════════════════════════════════
    # L3: Knowledge Memory (能力知识)
    # ══════════════════════════════════════════════════════

    def _load_knowledge(self) -> None:
        """从磁盘加载知识记忆。"""
        if os.path.exists(KNOWLEDGE_MEMORY_FILE):
            try:
                with open(KNOWLEDGE_MEMORY_FILE) as f:
                    loaded = json.load(f)
                self._knowledge = loaded.get("entries", {})
            except (json.JSONDecodeError, IOError):
                self._knowledge = {}
        # 确保默认知识存在（不覆盖已有值）
        for k, v in DEFAULT_KNOWLEDGE.items():
            if k not in self._knowledge:
                self._knowledge[k] = v

    def _save_knowledge(self) -> None:
        """持久化知识记忆到磁盘。"""
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(KNOWLEDGE_MEMORY_FILE, "w") as f:
            json.dump(
                {
                    "entries": self._knowledge,
                    "updated_at": datetime.now().isoformat(),
                },
                f, ensure_ascii=False, indent=2,
            )

    def knowledge_set(self, key: str, value: Any) -> None:
        """设置知识条目。"""
        with self._knowledge_lock:
            self._knowledge[key] = value
        self._save_knowledge()

    def knowledge_get(self, key: str) -> Optional[Any]:
        """获取知识条目。"""
        return self._knowledge.get(key)

    def knowledge_all(self) -> dict:
        """获取所有知识条目。"""
        with self._knowledge_lock:
            return dict(self._knowledge)

    def knowledge_delete(self, key: str) -> None:
        """删除知识条目。"""
        with self._knowledge_lock:
            self._knowledge.pop(key, None)
        self._save_knowledge()

    def knowledge_search(self, query: str) -> list[dict]:
        """关键词搜索知识库。

        对 query 分词后，在 knowledge 的 key 和 value 中进行匹配打分。
        返回按相关度降序排列的结果列表，每项包含 key, value, score。

        Args:
            query: 搜索关键词

        Returns:
            list of dicts with keys: key, value, score
        """
        if not query or not self._knowledge:
            return []

        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        scored: list[dict] = []
        for key, value in self._knowledge.items():
            # 对 key 和 value 分别计算匹配度
            key_text = str(key)
            val_text = str(value)

            key_tokens = self._tokenize(key_text)
            val_tokens = self._tokenize(val_text)

            # Token overlap score
            key_score = self._token_overlap(query_tokens, key_tokens)
            val_score = self._token_overlap(query_tokens, val_tokens)

            # 组合分数：key 匹配权重高（3:7）
            score = key_score * 0.6 + val_score * 0.4

            # 精确子串匹配加分
            query_lower = query.lower()
            if query_lower in key_text.lower():
                score += 0.3
            if query_lower in val_text.lower():
                score += 0.2

            if score > 0.05:  # 最低阈值
                scored.append({
                    "key": key,
                    "value": value,
                    "score": round(min(score, 1.0), 4),
                })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """中文+英文混合分词。

        使用 jieba 进行中文分词（如可用），否则回退到字符级。
        英文按空格分词，过滤短词和标点。
        """
        cleaned = re.sub(r"[^\w\u4e00-\u9fff]", " ", str(text).lower())
        words = [w for w in cleaned.split() if len(w) > 1]

        tokens: set[str] = set()

        for word in words:
            if re.search(r"[\u4e00-\u9fff]", word):
                # 中文字段
                try:
                    import jieba
                    cut = jieba.cut(word, cut_all=False)
                    tokens.update(w for w in cut if len(w) > 1)
                except ImportError:
                    # jieba 不可用，回退到字符级 bigram
                    for i in range(len(word) - 1):
                        tokens.add(word[i:i + 2])
                    tokens.add(word)
            else:
                tokens.add(word)

        return tokens

    @staticmethod
    def _token_overlap(query_tokens: set, target_tokens: set) -> float:
        """计算两词集合的 Jaccard 相似度。"""
        if not query_tokens or not target_tokens:
            return 0.0
        return len(query_tokens & target_tokens) / len(query_tokens | target_tokens)

    # ══════════════════════════════════════════════════════
    # LLM 上下文注入
    # ══════════════════════════════════════════════════════

    def get_context_for_prompt(self, knowledge_query: str = None) -> str:
        """生成注入 LLM prompt 的记忆上下文文本。

        注入顺序:
          1. User 偏好（始终注入，优先级高）
          2. Session 记忆（如有）
          3. Knowledge 知识（匹配 knowledge_query 的相关条目，或全部）

        Args:
            knowledge_query: 可选，用于过滤相关知识的搜索词。
                             为 None 时注入全部知识。

        Returns:
            格式化的记忆上下文字符串，可直接拼接到 system prompt。
            无内容时返回空字符串。
        """
        parts: list[str] = []

        # ── User 偏好（优先） ──
        user_prefs = self.user_all()
        if user_prefs:
            lines = ["## 用户偏好 (User Preferences)"]
            for k, v in user_prefs.items():
                lines.append(f"- {k}: {v}")
            parts.append("\n".join(lines))

        # ── Session 记忆 ──
        session = self.session_all()
        if session:
            lines = ["## 会话上下文 (Session Context)"]
            for k, v in session.items():
                lines.append(f"- {k}: {v}")
            parts.append("\n".join(lines))

        # ── Knowledge 知识 ──
        if knowledge_query:
            # 搜索相关知识
            results = self.knowledge_search(knowledge_query)
            if results:
                lines = [f"## 相关知识 (Relevant Knowledge for: {knowledge_query})"]
                for r in results:
                    lines.append(f"- [{r['key']}] (relevance: {r['score']:.2f}) {r['value']}")
                parts.append("\n".join(lines))
        else:
            # 注入全部知识
            all_knowledge = self.knowledge_all()
            if all_knowledge:
                lines = ["## 系统知识 (System Knowledge)"]
                for k, v in all_knowledge.items():
                    lines.append(f"- {k}: {v}")
                parts.append("\n".join(lines))

        return "\n\n".join(parts) if parts else ""

    # ══════════════════════════════════════════════════════
    # 管理方法
    # ══════════════════════════════════════════════════════

    def reset_user_to_defaults(self) -> None:
        """重置用户偏好为默认值。"""
        with self._user_lock:
            self._user = dict(DEFAULT_USER)
        self._save_user()

    def reset_knowledge_to_defaults(self) -> None:
        """重置知识库为默认值。"""
        with self._knowledge_lock:
            self._knowledge = dict(DEFAULT_KNOWLEDGE)
        self._save_knowledge()

    def reset_all(self) -> None:
        """重置所有记忆（会话清空，用户/知识恢复默认）。"""
        self.session_clear()
        self.reset_user_to_defaults()
        self.reset_knowledge_to_defaults()


# ── 模块级单例 ─────────────────────────────────────────────

memory_layer = MemoryLayer()
