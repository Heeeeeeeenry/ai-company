"""Token Usage Tracker - LangChain callback for per-role token accounting.

Hooks into LLM calls to capture prompt/completion token counts per role,
persists to JSON for cost analysis and optimization guidance.
"""

import json
import os
import threading
from datetime import datetime
from typing import Optional
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult


# Per-call token record
class TokenRecord:
    __slots__ = ("role", "model", "prompt_tokens", "completion_tokens",
                 "total_tokens", "timestamp", "task_id")
    
    def __init__(self, role: str, model: str, prompt_tokens: int,
                 completion_tokens: int, timestamp: str = "",
                 task_id: str = ""):
        self.role = role
        self.model = model
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens
        self.timestamp = timestamp or datetime.now().isoformat()
        self.task_id = task_id
    
    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "timestamp": self.timestamp,
            "task_id": self.task_id,
        }


class TokenTracker(BaseCallbackHandler):
    """LangChain callback that records token usage per LLM call.
    
    Usage:
        tracker = TokenTracker()
        # Pass tracker to LLM: ChatOpenAI(..., callbacks=[tracker])
        # Or set current task context: tracker.set_context(role="ceo", task_id="t1")
    """
    
    STORAGE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")
    STORAGE_FILE = os.path.join(STORAGE_DIR, "token_usage.json")
    MAX_RECORDS = 5000  # Keep last N records
    
    def __init__(self):
        self._records: list[TokenRecord] = []
        self._lock = threading.Lock()
        self._current_role: str = "unknown"
        self._current_task_id: str = ""
        # Session starts fresh — token counts reset on restart.
        # Historical data is still persisted to disk for analysis.
        self._session_start = datetime.now()
    
    def set_context(self, role: str = "", task_id: str = ""):
        """Set role/task context for upcoming LLM calls."""
        if role:
            self._current_role = role
        if task_id:
            self._current_task_id = task_id
    
    def on_llm_start(self, serialized: dict, prompts: list[str], **kwargs):
        """Called when LLM starts generating."""
        # Could record prompt length estimate here
        pass
    
    def on_llm_end(self, response: LLMResult, **kwargs):
        """Capture token usage from LLM response."""
        try:
            usage = None
            if response.llm_output and "token_usage" in response.llm_output:
                usage = response.llm_output["token_usage"]
            elif hasattr(response, "generations") and response.generations:
                gen = response.generations[0][0]
                if hasattr(gen, "generation_info"):
                    usage = gen.generation_info.get("token_usage") or {}
                if not usage and hasattr(gen, "message") and hasattr(gen.message, "response_metadata"):
                    usage = gen.message.response_metadata.get("token_usage") or {}
            
            if usage and (usage.get("prompt_tokens") or usage.get("completion_tokens")):
                record = TokenRecord(
                    role=self._current_role,
                    model=response.llm_output.get("model_name", "unknown") if response.llm_output else "unknown",
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    task_id=self._current_task_id,
                )
                with self._lock:
                    self._records.append(record)
                    if len(self._records) > self.MAX_RECORDS:
                        self._records = self._records[-self.MAX_RECORDS:]
        except (json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError):
            return  # Tracking failure is non-critical
    
    def _load(self):
        try:
            if os.path.exists(self.STORAGE_FILE):
                with open(self.STORAGE_FILE) as f:
                    data = json.load(f)
                    records = []
                    for r in data.get("records", []):
                        # total_tokens is computed in __init__, not a constructor param
                        r.pop("total_tokens", None)
                        records.append(TokenRecord(**r))
                    self._records = records
        except (json.JSONDecodeError, IOError, OSError, TypeError):
            self._records = []  # Reset on corrupt data
    
    def save(self):
        """Persist records to disk."""
        try:
            os.makedirs(self.STORAGE_DIR, exist_ok=True)
            with open(self.STORAGE_FILE, "w") as f:
                json.dump({
                    "records": [r.to_dict() for r in self._records],
                    "last_saved": datetime.now().isoformat(),
                }, f, ensure_ascii=False, indent=2)
        except (IOError, OSError, TypeError):
            return  # Save failure is non-critical
    
    def get_stats(self) -> dict:
        """Return aggregate token usage statistics."""
        with self._lock:
            records = list(self._records)
        
        if not records:
            return {"total_calls": 0, "total_tokens": 0, "message": "No data yet"}
        
        total_prompt = sum(r.prompt_tokens for r in records)
        total_completion = sum(r.completion_tokens for r in records)
        
        # By role
        by_role = {}
        for r in records:
            if r.role not in by_role:
                by_role[r.role] = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
            by_role[r.role]["calls"] += 1
            by_role[r.role]["prompt_tokens"] += r.prompt_tokens
            by_role[r.role]["completion_tokens"] += r.completion_tokens
        
        for role in by_role:
            by_role[role]["total"] = by_role[role]["prompt_tokens"] + by_role[role]["completion_tokens"]
            by_role[role]["avg_per_call"] = round(by_role[role]["total"] / by_role[role]["calls"])
        
        # By model
        by_model = {}
        for r in records:
            if r.model not in by_model:
                by_model[r.model] = {"calls": 0, "total": 0}
            by_model[r.model]["calls"] += 1
            by_model[r.model]["total"] += r.total_tokens
        
        # Cost estimate (DeepSeek pricing: ¥1/M input, ¥2/M output ≈ $0.14/$0.28 per 1M)
        cost_input = total_prompt * 0.14 / 1_000_000
        cost_output = total_completion * 0.28 / 1_000_000
        
        return {
            "total_calls": len(records),
            "total_tokens": total_prompt + total_completion,
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "estimated_cost_usd": round(cost_input + cost_output, 4),
            "by_role": by_role,
            "by_model": by_model,
        }
    
    def get_summary_text(self) -> str:
        """Human-readable summary (session only — resets on restart)."""
        s = self.get_stats()
        if s.get("total_calls", 0) == 0:
            return "📊 当前会话尚无 API 调用数据"
        
        elapsed = (datetime.now() - self._session_start).total_seconds()
        elapsed_str = f"{int(elapsed//3600)}h{int((elapsed%3600)//60)}m" if elapsed >= 3600 else f"{int(elapsed//60)}m{int(elapsed%60)}s"
        
        lines = [
            f"📊 Session Token  ({elapsed_str}, {s['total_calls']} calls)",
            f"   Total: {s['total_tokens']:,} tokens  (~${s.get('estimated_cost_usd', 0):.2f})",
            f"   Prompt: {s['total_prompt_tokens']:,} | Completion: {s['total_completion_tokens']:,}",
            "   By role:",
        ]
        for role, data in sorted(s.get("by_role", {}).items(),
                                 key=lambda x: x[1]["total"], reverse=True):
            lines.append(f"     {role}: {data['total']:,} tokens ({data['calls']} calls, avg {data['avg_per_call']}/call)")
        
        return "\n".join(lines)


# Global singleton
_token_tracker: Optional[TokenTracker] = None


def get_token_tracker() -> TokenTracker:
    global _token_tracker
    if _token_tracker is None:
        _token_tracker = TokenTracker()
    return _token_tracker
