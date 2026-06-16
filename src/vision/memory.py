"""Visual Memory — structured storage of screen observations (no raw images)."""

import json, os, time
from datetime import datetime
from typing import Optional
from threading import Lock
from dataclasses import dataclass, asdict

@dataclass
class VisualObservation:
    timestamp: float
    active_app: str
    activity: str
    topic: str
    summary: str
    apps: list  # list of detected app names
    method: str = "vision"
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, d: dict):
        return cls(**d)

class VisualMemory:
    """Persistent memory for visual context observations."""
    
    MAX_OBSERVATIONS = 500
    
    def __init__(self, session_id: str = ""):
        self.session_id = session_id
        self._observations: list[VisualObservation] = []
        self._lock = Lock()
        self._storage_path = self._get_storage_path()
        self._load()
    
    def _get_storage_path(self) -> str:
        base = os.path.expanduser("~/.ai-company")
        if self.session_id:
            return os.path.join(base, "sessions", self.session_id, "visual_memory.json")
        return os.path.join(base, "visual_memory.json")
    
    def _load(self):
        if os.path.exists(self._storage_path):
            try:
                with open(self._storage_path) as f:
                    data = json.load(f)
                self._observations = [VisualObservation.from_dict(o) for o in data.get("observations", [])]
            except (json.JSONDecodeError, KeyError):
                self._observations = []
    
    def _save(self):
        os.makedirs(os.path.dirname(self._storage_path), exist_ok=True)
        with self._lock:
            data = {
                "session_id": self.session_id,
                "updated_at": datetime.now().isoformat(),
                "observations": [o.to_dict() for o in self._observations[-self.MAX_OBSERVATIONS:]],
            }
        with open(self._storage_path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def record(self, analysis: dict, app_info: dict = None):
        """Record a visual observation."""
        obs = VisualObservation(
            timestamp=time.time(),
            active_app=analysis.get("active_app", "unknown"),
            activity=analysis.get("activity", "unknown"),
            topic=analysis.get("topic", ""),
            summary=analysis.get("summary", ""),
            apps=analysis.get("apps", []),
            method=analysis.get("method", "vision"),
        )
        with self._lock:
            self._observations.append(obs)
        self._save()
    
    def get_recent(self, n: int = 10) -> list[VisualObservation]:
        return self._observations[-n:]
    
    def get_current_activity(self) -> Optional[dict]:
        if not self._observations:
            return None
        latest = self._observations[-1]
        return {
            "active_app": latest.active_app,
            "activity": latest.activity,
            "topic": latest.topic,
            "ago_seconds": time.time() - latest.timestamp,
        }
    
    def get_timeline(self, minutes: int = 30) -> list[dict]:
        """Get recent timeline of activities."""
        cutoff = time.time() - minutes * 60
        recent = [o for o in self._observations if o.timestamp >= cutoff]
        
        # Deduplicate consecutive same-activity observations
        timeline = []
        for o in recent:
            if not timeline or o.active_app != timeline[-1]["active_app"]:
                timeline.append({
                    "time": datetime.fromtimestamp(o.timestamp).strftime("%H:%M:%S"),
                    "active_app": o.active_app,
                    "activity": o.activity,
                    "topic": o.topic,
                })
        return timeline
    
    def get_context_for_prompt(self) -> str:
        """Build context string for LLM injection."""
        current = self.get_current_activity()
        if not current:
            return ""
        
        ago = int(current["ago_seconds"])
        ago_str = f"{ago}s ago" if ago < 60 else f"{ago//60}m ago"
        
        lines = [
            "## Visual Context (screen observation)",
            f"- Active app: {current['active_app']} ({ago_str})",
            f"- Activity: {current['activity']}",
        ]
        if current["topic"]:
            lines.append(f"- Topic: {current['topic']}")
        
        timeline = self.get_timeline(30)
        if len(timeline) > 1:
            lines.append("- Recent timeline:")
            for t in timeline[-5:]:
                lines.append(f"  {t['time']} {t['active_app']}: {t['activity']}")
        
        return "\n".join(lines)


# Global singleton per session
_visual_memories: dict[str, VisualMemory] = {}

def get_visual_memory(session_id: str = "") -> VisualMemory:
    global _visual_memories
    key = session_id or "default"
    if key not in _visual_memories:
        _visual_memories[key] = VisualMemory(session_id)
    return _visual_memories[key]
