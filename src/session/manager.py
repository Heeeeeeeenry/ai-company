"""Session Manager — multi-session support for AI Company CLI.

Stores sessions in ~/.ai-company/sessions/<session_id>/
  metadata.json: {id, name, created_at, last_active, message_count, is_active}
  memory.json: per-session memories (managed by SessionMemory)

Global: ~/.ai-company/global_memory.json
"""

import json
import os
import uuid
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field, asdict

SESSION_DIR = os.path.expanduser("~/.ai-company/sessions")
GLOBAL_MEMORY_FILE = os.path.expanduser("~/.ai-company/global_memory.json")

@dataclass
class Session:
    id: str
    name: str
    created_at: str
    last_active: str
    message_count: int = 0
    is_active: bool = False
    
    def to_dict(self):
        return asdict(self)
    
    @classmethod
    def from_dict(cls, d):
        return cls(**d)

class SessionManager:
    """Singleton manager for multiple conversation sessions."""
    
    def __init__(self):
        os.makedirs(SESSION_DIR, exist_ok=True)
        self._current: Optional[Session] = None
        self._sessions: dict[str, Session] = {}
        self._load_all()
        # Auto-resume last active session, or create default
        if not self._current:
            # Try to find last active
            active = [s for s in self._sessions.values() if s.is_active]
            if active:
                self._current = active[0]
            else:
                # Create default session
                self._current = self.create("default")
    
    @property
    def current(self) -> Session:
        return self._current
    
    def _session_dir(self, session_id: str) -> str:
        return os.path.join(SESSION_DIR, session_id)
    
    def _metadata_path(self, session_id: str) -> str:
        return os.path.join(self._session_dir(session_id), "metadata.json")
    
    def _load_all(self):
        """Load all sessions from disk."""
        if not os.path.exists(SESSION_DIR):
            return
        for sid in os.listdir(SESSION_DIR):
            path = self._metadata_path(sid)
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        data = json.load(f)
                    session = Session.from_dict(data)
                    self._sessions[session.id] = session
                    if session.is_active:
                        self._current = session
                except (json.JSONDecodeError, KeyError):
                    pass
    
    def create(self, name: str) -> Session:
        """Create a new session."""
        sid = uuid.uuid4().hex[:8]
        now = datetime.now().isoformat()
        session = Session(
            id=sid,
            name=name,
            created_at=now,
            last_active=now,
            is_active=True,
        )
        # Deactivate others
        for s in self._sessions.values():
            s.is_active = False
            self._save_metadata(s)
        self._sessions[sid] = session
        self._current = session
        os.makedirs(self._session_dir(sid), exist_ok=True)
        self._save_metadata(session)
        return session
    
    def switch(self, session_id_or_name: str) -> Optional[Session]:
        """Switch to a session by ID or name prefix."""
        target = None
        # Try exact ID match
        if session_id_or_name in self._sessions:
            target = self._sessions[session_id_or_name]
        else:
            # Try name match (prefix)
            matches = [s for s in self._sessions.values()
                      if s.name.startswith(session_id_or_name)]
            if len(matches) == 1:
                target = matches[0]
        if target is None:
            return None
        # Deactivate current, activate target
        if self._current:
            self._current.is_active = False
            self._save_metadata(self._current)
        target.is_active = True
        target.last_active = datetime.now().isoformat()
        self._current = target
        self._save_metadata(target)
        return target
    
    def delete(self, session_id_or_name: str) -> bool:
        """Delete a session (cannot delete current)."""
        target = self._find_session(session_id_or_name)
        if target is None or target == self._current:
            return False
        import shutil
        shutil.rmtree(self._session_dir(target.id), ignore_errors=True)
        del self._sessions[target.id]
        return True
    
    def list_all(self) -> list[Session]:
        """List all sessions sorted by last_active."""
        return sorted(self._sessions.values(),
                     key=lambda s: s.last_active, reverse=True)
    
    def record_message(self):
        """Increment message count for current session."""
        if self._current:
            self._current.message_count += 1
            self._current.last_active = datetime.now().isoformat()
            self._save_metadata(self._current)
    
    def _find_session(self, session_id_or_name: str) -> Optional[Session]:
        if session_id_or_name in self._sessions:
            return self._sessions[session_id_or_name]
        matches = [s for s in self._sessions.values()
                  if s.name.startswith(session_id_or_name)]
        return matches[0] if len(matches) == 1 else None
    
    def _save_metadata(self, session: Session):
        path = self._metadata_path(session.id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(session.to_dict(), f, ensure_ascii=False, indent=2)
    
    def save_all(self):
        """Save all session metadata (called on shutdown)."""
        for session in self._sessions.values():
            self._save_metadata(session)

# Singleton
_session_manager: Optional[SessionManager] = None

def get_session_manager() -> SessionManager:
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager
