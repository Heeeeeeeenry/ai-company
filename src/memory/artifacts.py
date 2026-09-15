"""Artifact Store — shared file-based storage across all agents.

Agents save their work (search results, generated files, URLs) here.
Later agents (or the same agent on a follow-up task) can load them.
Prevents the "lost context → os.listdir()" problem.

Usage:
    from src.memory.artifacts import artifact_store

    # Save
    artifact_store.put("github_search", {"urls": [...], "results": [...]})

    # Load
    data = artifact_store.get("github_search")

    # List keys
    keys = artifact_store.keys()
"""

import json
import os
import logging
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("ai_company.artifacts")

# Shared workspace — all agents see the same directory
DEFAULT_WORKSPACE = os.environ.get(
    "AI_COMPANY_WORKSPACE",
    os.path.expanduser("~/.ai-company/workspace"),
)


class ArtifactStore:
    """Key-value store backed by JSON files in a shared workspace.

    Storage is scoped by the current conversation (``src.session.scope``):

    * scoped   → ``<workspace>/artifacts/<conversation_id>/``
    * unscoped → ``<workspace>/artifacts/``  (legacy single-user CLI layout)

    Scoping is what keeps an embedded multi-user deployment from reading another
    user's (or another chat's) artifacts: ``list_all()`` only ever walks the
    current scope's directory.
    """

    def __init__(self, workspace: str = DEFAULT_WORKSPACE):
        self.workspace = workspace
        self._base = os.path.join(workspace, "artifacts")
        os.makedirs(self._base, exist_ok=True)

    # ── Scope resolution ──────────────────────────────────────────────

    @property
    def _dir(self) -> str:
        """Directory for the currently active conversation scope."""
        try:
            from src.session.scope import current_scope
            cid = current_scope()
        except Exception:
            cid = None
        if not cid:
            return self._base
        safe = str(cid).replace("/", "_").replace("..", "_")
        path = os.path.join(self._base, safe)
        os.makedirs(path, exist_ok=True)
        return path

    def _path(self, key: str) -> str:
        safe = key.replace("/", "_").replace("..", "_")
        return os.path.join(self._dir, f"{safe}.json")

    def put(self, key: str, data: Any, metadata: Optional[dict] = None) -> str:
        """Save an artifact. Returns its file path."""
        record = {
            "key": key,
            "saved_at": datetime.now().isoformat(),
            "metadata": metadata or {},
            "data": data,
        }
        path = self._path(key)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        logger.debug("Artifact saved: %s (%d chars)", key, len(json.dumps(data)))
        return path

    def get(self, key: str) -> Optional[dict]:
        """Load an artifact. Returns the full record dict, or None."""
        path = self._path(key)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Failed to load artifact %s: %s", key, e)
            return None

    def get_data(self, key: str) -> Optional[Any]:
        """Load just the 'data' field of an artifact."""
        record = self.get(key)
        return record["data"] if record else None

    def keys(self) -> list[str]:
        """List artifact keys visible to the current scope."""
        d = self._dir
        if not os.path.exists(d):
            return []
        return sorted(
            f.replace(".json", "")
            for f in os.listdir(d)
            if f.endswith(".json")
        )

    def list_all(self) -> list[dict]:
        """List all artifacts with metadata (no data payload)."""
        result = []
        for key in self.keys():
            record = self.get(key)
            if record:
                result.append({
                    "key": key,
                    "saved_at": record.get("saved_at", "?"),
                    "metadata": record.get("metadata", {}),
                })
        result.sort(key=lambda r: r["saved_at"], reverse=True)
        return result

    def exists(self, key: str) -> bool:
        return os.path.exists(self._path(key))

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    def clear(self):
        """Remove all artifacts from this workspace."""
        for key in self.keys():
            self.delete(key)


# Singleton
artifact_store = ArtifactStore()
