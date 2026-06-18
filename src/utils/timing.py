"""Task Timing — track per-node and per-LLM-call duration.

Toggle with: from src.utils.timing import timer; timer.enabled = True/False
CLI command: /timer on | off | status

Usage:
    from src.utils.timing import timer, timed
    
    with timed("triage_node"):
        ...  # code to time
    
    # Or manually:
    timer.start("my_task")
    ...
    timer.stop("my_task")

Output at deliver_node shows a breakdown table of all timed segments.
"""

import time
import contextlib
from collections import defaultdict
from typing import Optional, Callable
import functools

# ─── Timing record ───

class Timer:
    """Global timing tracker. Singleton pattern."""
    
    def __init__(self):
        self.enabled = True  # enabled by default
        self._records: list = []  # [{phase, name, elapsed_ms, category}]
        self._started: dict = {}  # {name: start_time}
        self._task_start: Optional[float] = None
        self._llm_calls: list = []  # [{model, role, elapsed_ms, tokens?}]
    
    # ─── Public API ───
    
    def start_task(self):
        """Mark the beginning of a user task."""
        self._task_start = time.time()
        self._records.clear()
        self._llm_calls.clear()
    
    def start(self, name: str, category: str = "node"):
        if not self.enabled:
            return
        self._started[name] = (time.time(), category)
    
    def stop(self, name: str):
        if not self.enabled:
            return
        start_data = self._started.pop(name, None)
        if start_data:
            start_t, category = start_data
            elapsed = (time.time() - start_t) * 1000  # ms
            self._records.append({
                "phase": name,
                "category": category,
                "elapsed_ms": round(elapsed),
            })
    
    def record_llm(self, model: str, role: str, elapsed_ms: float, tokens: int = 0):
        """Record an LLM API call."""
        if not self.enabled:
            return
        self._llm_calls.append({
            "model": model,
            "role": role,
            "elapsed_ms": round(elapsed_ms),
            "tokens": tokens,
        })
    
    def get_summary(self) -> str:
        """Generate a human-readable timing summary."""
        if not self.enabled or not self._records:
            return ""
        
        total_ms = sum(r["elapsed_ms"] for r in self._records)
        if self._task_start:
            wall_ms = (time.time() - self._task_start) * 1000
        else:
            wall_ms = total_ms
        
        lines = [
            "\n⏱ 耗时统计",
            f"  总耗时: {wall_ms/1000:.1f}s",
        ]
        
        # Per-node breakdown
        by_node = defaultdict(int)
        for r in self._records:
            if r["category"] == "node":
                by_node[r["phase"]] += r["elapsed_ms"]
        
        if by_node:
            lines.append("")
            lines.append("  节点耗时:")
            for name, ms in sorted(by_node.items(), key=lambda x: -x[1]):
                pct = ms / total_ms * 100
                lines.append(f"    {name:<30} {ms/1000:>5.1f}s ({pct:>4.0f}%)")
        
        # Per-role LLM calls
        if self._llm_calls:
            by_role = defaultdict(lambda: {"ms": 0, "calls": 0, "tokens": 0})
            for call in self._llm_calls:
                role = call["role"]
                by_role[role]["ms"] += call["elapsed_ms"]
                by_role[role]["calls"] += 1
                by_role[role]["tokens"] += call["tokens"]
            
            lines.append("")
            lines.append("  LLM 调用:")
            for role, stats in sorted(by_role.items(), key=lambda x: -x[1]["ms"]):
                lines.append(
                    f"    {role:<20} {stats['ms']/1000:>5.1f}s "
                    f"({stats['calls']}次, {stats['tokens']}tokens)"
                )
        
        # Gap analysis
        accounted = sum(r["elapsed_ms"] for r in self._records)
        gap = wall_ms - accounted
        if gap > 1000:  # >1s gap worth reporting
            lines.append(f"\n  未计入: {gap/1000:.1f}s (tool执行/sleep等)")
        
        return "\n".join(lines)


# ─── Context manager ───

@contextlib.contextmanager
def timed(name: str, category: str = "node"):
    """Context manager for timing a code block."""
    timer.start(name, category)
    try:
        yield
    finally:
        timer.stop(name)


def timed_llm(model: str, role: str, tokens: int = 0):
    """Decorator/context for timing LLM calls."""
    start = time.time()
    
    def _record():
        if timer.enabled:
            elapsed = (time.time() - start) * 1000
            timer.record_llm(model, role, elapsed, tokens)
    
    return _record


# Global singleton
timer = Timer()


# ─── LangChain callback for LLM timing ───

from langchain_core.callbacks.base import BaseCallbackHandler


class TimingCallback(BaseCallbackHandler):
    """LangChain callback that records LLM call duration."""
    
    def __init__(self, role: str = "unknown", model: str = "unknown"):
        super().__init__()
        self.role = role
        self.model = model
        self._start: Optional[float] = None
    
    # Duck-typing: LangChain checks for these methods by name, not inheritance
    def on_llm_start(self, serialized=None, prompts=None, **kwargs):
        if timer.enabled:
            self._start = time.time()
    
    def on_llm_end(self, response=None, **kwargs):
        if self._start and timer.enabled:
            elapsed = (time.time() - self._start) * 1000
            tokens = 0
            try:
                if hasattr(response, 'llm_output') and response.llm_output:
                    usage = response.llm_output.get('token_usage', {})
                    tokens = usage.get('total_tokens', 0)
            except Exception:
                pass
            timer.record_llm(self.model, self.role, elapsed, tokens)
            self._start = None
    
    def on_llm_error(self, error=None, **kwargs):
        self._start = None
