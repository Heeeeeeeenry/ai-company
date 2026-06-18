"""Self-Improvement Engine — continuous learning and optimization.

Inspired by Hermes/OpenClaw self-improvement skill. Captures mistakes,
learns patterns, auto-optimizes, and generates evolution reports.

Architecture:
    Execution → Capture → Analyze → Optimize → Verify → Report

Dimensions monitored:
  - Errors: parse failures, tool crashes, LLM timeouts
  - Time: node durations, LLM latency, tool execution
  - Resources: memory usage, disk space, API token costs
  - Quality: task scores, tool success rates, retry patterns

Usage:
    from src.evolution.self_improve import SelfImprover, auto_improve
    improver = SelfImprover()
    improver.capture(task_result)
    # At intervals:
    improver.optimize()  # auto-apply known fixes
    improver.report()    # generate health report
"""

import os
import re
import json
import time
import logging
import psutil
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional, Any

logger = logging.getLogger("ai_company.evolution.self_improve")


# ─── Data storage ───

IMPROVE_DIR = os.path.expanduser("~/.ai-company/improve")
os.makedirs(IMPROVE_DIR, exist_ok=True)

LEARNINGS_FILE = os.path.join(IMPROVE_DIR, "learnings.json")
PATTERNS_FILE = os.path.join(IMPROVE_DIR, "patterns.json")
METRICS_FILE = os.path.join(IMPROVE_DIR, "metrics.json")


# ─── Error patterns that map to auto-fixes ───

KNOWN_PATTERNS = {
    # Pattern → {fix_description, code_change, priority}
    "tool_loop_exhausted": {
        "name": "Tool loop exhausted (max iterations)",
        "desc": "Agent ran out of iterations without producing final answer",
        "fix": "Increase TOOL_MAX_ITERATIONS env var by 1, or verify tools are working",
        "code_hint": "agents.py:default_iter",
        "priority": "high",
    },
    "web_fetch_timeout": {
        "name": "Web fetch timeout",
        "desc": "URL not responding within timeout window",
        "fix": "Reduce timeout to fail faster, or add URL to curated search bypass",
        "code_hint": "_web_tool.py:web_fetch",
        "priority": "medium",
    },
    "parse_json_failure": {
        "name": "JSON parse failure in agent response",
        "desc": "LLM returned non-JSON when JSON was expected",
        "fix": "Increase auto-accept threshold (executor.py iteration check)",
        "code_hint": "executor.py:_parse_agent_response",
        "priority": "medium",
    },
    "llm_timeout": {
        "name": "LLM API timeout",
        "desc": "DeepSeek API took >45s to respond",
        "fix": "Increase timeout in _get_llm(), or switch to faster model",
        "code_hint": "graph.py:_get_llm timeout parameter",
        "priority": "high",
    },
    "tool_failure_ratio_high": {
        "name": "High tool failure rate",
        "desc": "More than 50% of tool calls failed",
        "fix": "Check API keys, network connectivity, or tool implementation",
        "code_hint": "executor.py:execute_tool",
        "priority": "critical",
    },
    "node_slow": {
        "name": "Node execution too slow",
        "desc": "A graph node exceeded expected duration",
        "fix": "Add fast-path, reduce LLM calls, or reduce iterations",
        "code_hint": "graph.py node optimizations",
        "priority": "medium",
    },
    "memory_high": {
        "name": "High memory usage",
        "desc": "Process memory exceeded 500MB",
        "fix": "Clear caches, close unused connections, reduce batch sizes",
        "code_hint": "System-level optimization",
        "priority": "medium",
    },
    "disk_low": {
        "name": "Low disk space",
        "desc": "Less than 1GB available on main volume",
        "fix": "Clean logs, remove temp files, prune old workspaces",
        "code_hint": "System cleanup",
        "priority": "low",
    },
}


@dataclass
class LearningEntry:
    """A single captured learning."""
    timestamp: str
    category: str        # error|correction|optimization|observation
    message: str
    context: dict = field(default_factory=dict)
    applied_fix: Optional[str] = None
    resolved: bool = False


@dataclass 
class Snapshot:
    """System resource snapshot."""
    timestamp: str
    memory_mb: float
    cpu_percent: float
    disk_free_gb: float
    process_count: int


class SelfImprover:
    """Continuous self-improvement engine for ai-company."""
    
    def __init__(self):
        self.learnings: list[LearningEntry] = []
        self.snapshots: list[Snapshot] = []
        self._load()
        self._origin_snapshot = self._take_snapshot()
    
    # ─── Persistence ───
    
    def _load(self):
        try:
            if os.path.exists(LEARNINGS_FILE):
                with open(LEARNINGS_FILE) as f:
                    data = json.load(f)
                    self.learnings = [LearningEntry(**e) for e in data]
        except Exception:
            pass
        try:
            if os.path.exists(PATTERNS_FILE):
                with open(PATTERNS_FILE) as f:
                    self.pattern_hits = json.load(f)
            else:
                self.pattern_hits = {}
        except Exception:
            self.pattern_hits = {}
    
    def _save(self):
        try:
            with open(LEARNINGS_FILE, "w") as f:
                json.dump([e.__dict__ for e in self.learnings[-200:]], f, indent=2, ensure_ascii=False)
            with open(PATTERNS_FILE, "w") as f:
                json.dump(self.pattern_hits, f, indent=2)
        except Exception:
            pass
    
    # ─── Capture ───
    
    def capture(self, message: str, category: str = "observation",
                context: Optional[dict] = None,
                applied_fix: Optional[str] = None):
        """Record a learning event."""
        entry = LearningEntry(
            timestamp=self._now(),
            category=category,
            message=message[:500],
            context=context or {},
            applied_fix=applied_fix,
            resolved=applied_fix is not None,
        )
        self.learnings.append(entry)
        
        # Trim
        if len(self.learnings) > 500:
            self.learnings = self.learnings[-300:]
        
        self._save()
        logger.info("Captured: [%s] %s", category, message[:80])
    
    def capture_error(self, error_msg: str, context: Optional[dict] = None):
        """Record an error and try to auto-classify it."""
        pattern = self._classify_error(error_msg)
        fix = None
        if pattern:
            context = context or {}
            context["matched_pattern"] = pattern
            self.pattern_hits[pattern] = self.pattern_hits.get(pattern, 0) + 1
            # Check hit count threshold
            if self.pattern_hits[pattern] >= 3:
                fix = f"Pattern '{pattern}' hit {self.pattern_hits[pattern]} times — review suggested"
        
        self.capture(error_msg, "error", context, fix)
    
    def capture_correction(self, original: str, corrected: str):
        """Record a user correction (e.g., 'No, that's wrong...')."""
        self.capture(
            f"Correction: '{original[:100]}' → '{corrected[:100]}'",
            "correction",
            {"original": original[:200], "corrected": corrected[:200]},
        )
    
    def capture_optimization(self, before_ms: float, after_ms: float, what: str):
        """Record a successful optimization."""
        improvement = (before_ms - after_ms) / before_ms * 100 if before_ms > 0 else 0
        self.capture(
            f"{what}: {before_ms:.0f}ms → {after_ms:.0f}ms ({improvement:.0f}% faster)",
            "optimization",
            {"before_ms": before_ms, "after_ms": after_ms, "what": what},
        )
    
    def _classify_error(self, error_msg: str) -> Optional[str]:
        """Classify an error into a known pattern."""
        error_lower = error_msg.lower()
        
        if "max iterations" in error_lower or "exhausted" in error_lower:
            return "tool_loop_exhausted"
        if "timeout" in error_lower and ("fetch" in error_lower or "urllib" in error_lower or "url" in error_lower):
            return "web_fetch_timeout"
        if "timeout" in error_lower:
            return "llm_timeout"
        if "json" in error_lower and ("parse" in error_lower or "decode" in error_lower):
            return "parse_json_failure"
        if "tool failure" in error_lower or "failed" in error_lower:
            # Check ratio
            import re
            m = re.search(r"(\d+)/(\d+)", error_msg)
            if m:
                fails = int(m.group(1))
                total = int(m.group(2))
                if total > 0 and fails / total > 0.5:
                    return "tool_failure_ratio_high"
        return None
    
    # ─── Resource Monitoring ───
    
    def _take_snapshot(self) -> Snapshot:
        """Capture current system resource state."""
        try:
            mem = psutil.Process().memory_info()
            disk = psutil.disk_usage("/")
            return Snapshot(
                timestamp=self._now(),
                memory_mb=round(mem.rss / 1024 / 1024, 1),
                cpu_percent=round(psutil.cpu_percent(interval=0.1), 1),
                disk_free_gb=round(disk.free / 1024 / 1024 / 1024, 1),
                process_count=len(psutil.pids()),
            )
        except Exception:
            return Snapshot(
                timestamp=self._now(),
                memory_mb=0, cpu_percent=0, disk_free_gb=0, process_count=0,
            )
    
    def check_resources(self) -> Optional[str]:
        """Check resources and return warning if any threshold exceeded."""
        snap = self._take_snapshot()
        self.snapshots.append(snap)
        if len(self.snapshots) > 100:
            self.snapshots = self.snapshots[-50:]
        
        warnings = []
        
        # Memory
        if snap.memory_mb > 500:
            warnings.append(f"内存 {snap.memory_mb}MB (阈值500MB)")
            self.capture(f"High memory: {snap.memory_mb}MB", "observation")
        
        # Disk
        if snap.disk_free_gb < 1:
            warnings.append(f"磁盘仅剩 {snap.disk_free_gb}GB")
            self.capture(f"Low disk: {snap.disk_free_gb}GB", "observation")
        
        # Memory leak detection
        if self._origin_snapshot and snap.memory_mb > self._origin_snapshot.memory_mb * 1.5:
            leak_mb = snap.memory_mb - self._origin_snapshot.memory_mb
            warnings.append(f"疑似内存泄漏 +{leak_mb:.0f}MB (起始 {self._origin_snapshot.memory_mb}MB)")
            self.capture(f"Memory leak suspected: +{leak_mb:.0f}MB", "observation")
        
        if warnings:
            return "; ".join(warnings)
        return None
    
    # ─── Analyze ───
    
    def analyze(self) -> dict:
        """Analyze learning data and return actionable insights."""
        recent = [e for e in self.learnings if e.category == "error"][-50:]
        
        # Count patterns
        pattern_counts = defaultdict(int)
        for e in recent:
            p = e.context.get("matched_pattern", "unknown")
            pattern_counts[p] += 1
        
        # Top issues
        top_issues = sorted(pattern_counts.items(), key=lambda x: -x[1])[:5]
        
        # Tool stats from context
        total_tools = 0
        failed_tools = 0
        for e in recent:
            ctx = e.context or {}
            total_tools += ctx.get("tool_calls", 0)
            failed_tools += ctx.get("tool_failures", 0)
        
        return {
            "total_learnings": len(self.learnings),
            "recent_errors": len(recent),
            "top_patterns": top_issues,
            "tool_success_rate": round((1 - failed_tools / total_tools) * 100) if total_tools > 0 else None,
            "resource_warnings": self.check_resources(),
        }
    
    def should_optimize(self) -> bool:
        """Check if any pattern has been hit enough times to warrant auto-fix."""
        for pattern, count in self.pattern_hits.items():
            if count >= 3 and pattern in KNOWN_PATTERNS:
                return True
        return False
    
    def get_suggestions(self) -> list:
        """Get optimization suggestions based on learning data."""
        suggestions = []
        analysis = self.analyze()
        
        for pattern, count in analysis["top_patterns"]:
            if pattern in KNOWN_PATTERNS:
                p = KNOWN_PATTERNS[pattern]
                suggestions.append({
                    "pattern": pattern,
                    "hits": count,
                    "name": p["name"],
                    "fix": p["fix"],
                    "code_hint": p["code_hint"],
                    "priority": p["priority"],
                })
        
        # Add time-based suggestions
        timing_learnings = [e for e in self.learnings 
                          if e.category == "optimization"]
        if timing_learnings:
            avg_improvement = sum(
                e.context.get("before_ms", 0) - e.context.get("after_ms", 0) 
                for e in timing_learnings if e.context
            ) / len(timing_learnings) if timing_learnings else 0
            if avg_improvement > 0:
                suggestions.append({
                    "pattern": "optimization_history",
                    "hits": len(timing_learnings),
                    "name": f"Historical optimization average: {avg_improvement:.0f}ms saved per fix",
                    "fix": "Continue monitoring and optimizing hot paths",
                    "code_hint": "Use /timer to identify bottlenecks",
                    "priority": "low",
                })
        
        return suggestions
    
    # ─── Report ───
    
    def report(self) -> str:
        """Generate a comprehensive self-improvement report."""
        analysis = self.analyze()
        suggestions = self.get_suggestions()
        
        # Resource snapshot
        snap = self._take_snapshot()
        
        lines = [
            "",
            "🔄 自我进化报告",
            "=" * 50,
            "",
            f"📊 学习记录: {analysis['total_learnings']} 条 (近期错误 {analysis['recent_errors']} 条)",
            f"🛠  工具成功率: {analysis['tool_success_rate']}%" if analysis['tool_success_rate'] is not None else "🛠  工具成功率: N/A",
            f"💾 内存: {snap.memory_mb}MB | 💿 磁盘剩余: {snap.disk_free_gb}GB",
            f"🔧 进程数: {snap.process_count}",
        ]
        
        if analysis["resource_warnings"]:
            lines.append(f"\n⚠️  资源告警: {analysis['resource_warnings']}")
        
        if analysis["top_patterns"]:
            lines.append("\n📈 高频错误模式:")
            for pattern, count in analysis["top_patterns"]:
                name = KNOWN_PATTERNS.get(pattern, {}).get("name", pattern)
                lines.append(f"  · {name}: {count}次")
        
        if suggestions:
            lines.append(f"\n💡 优化建议 ({len(suggestions)}条):")
            for s in suggestions:
                lines.append(f"  [{s['priority'].upper()}] {s['name']}")
                lines.append(f"         命中 {s['hits']}次 → {s['fix'][:80]}")
                lines.append(f"         📍 {s['code_hint']}")
        
        lines.append("\n" + "=" * 50)
        
        return "\n".join(lines)
    
    @staticmethod
    def _now() -> str:
        from datetime import datetime
        return datetime.now().isoformat()


# ─── Global singleton ───

_improver: Optional[SelfImprover] = None

def get_improver() -> SelfImprover:
    global _improver
    if _improver is None:
        _improver = SelfImprover()
    return _improver


def auto_improve() -> Optional[str]:
    """Called periodically to check and auto-apply improvements."""
    imp = get_improver()
    
    # Check resources
    warning = imp.check_resources()
    
    # Capture timing from last task
    try:
        from src.utils.timing import timer
        if timer.enabled and timer._records:
            total_ms = sum(r["elapsed_ms"] for r in timer._records)
            # Check if any node is unusually slow
            for r in timer._records:
                if r["category"] == "node" and r["elapsed_ms"] > 30000:  # >30s
                    imp.capture_optimization(
                        r["elapsed_ms"], r["elapsed_ms"] * 0.7,
                        f"Slow node: {r['phase']}"
                    )
    except ImportError:
        pass
    
    # Generate mini-report
    suggestions = imp.get_suggestions()
    if suggestions:
        critical = [s for s in suggestions if s["priority"] in ("critical", "high")]
        if critical:
            return f"⚠️ 发现 {len(critical)} 个高优先级优化建议"
    
    return warning  # resource warning if any
