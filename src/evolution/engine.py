"""AI-Company Auto-Evolution Engine

Human-like learning: records every task outcome, identifies patterns,
and automatically adapts roles, thresholds, and prompts.

Architecture:
  Experience Tracker → Pattern Analyzer → Adaptation Engine → Role Evolution
  
Like a human junior dev growing into a senior:
  1. Record every mistake and success
  2. Notice patterns ("I always mess up routing code reviews")
  3. Adjust behavior ("Next time, check keywords first")
  4. Share knowledge ("Update the team playbook")

⚠️  Evolution Guardrails:
  ALLOWED: gate thresholds, prompt refinement suggestions, role keyword tuning
  FORBIDDEN: auto-modify code, auto-change models, auto-delete roles, auto-route changes
"""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional
from pathlib import Path


# ─── Evolution Guardrails ───────────────────────

# ⚠️  These define what the evolution engine CAN and CANNOT touch.
# Like a parent setting boundaries for a child's growth.

EVOLUTION_ALLOWED = {
    "suggest_threshold": True,       # Suggest gate score adjustments
    "suggest_prompt": True,          # Suggest role prompt improvements
    "suggest_keywords": True,        # Suggest role keyword tuning
    "identify_weak_roles": True,     # Flag underperforming roles
    "track_trends": True,            # Monitor score/retry trends
    "auto_retry_strategy": False,    # DON'T auto-change retry count
    "auto_model_switch": False,      # DON'T auto-change model assignments
    "auto_code_modify": False,       # DON'T auto-modify source code
    "auto_role_delete": False,       # DON'T auto-delete roles
    "auto_route_change": False,      # DON'T auto-change routing logic
}

# Evolution focus dimensions — what we measure
EVOLUTION_DIMENSIONS = [
    "quality",       # Score trends
    "efficiency",    # Retry counts
    "reliability",   # Force-approve rate
    "coverage",      # Task type distribution
    "routing",       # Department routing accuracy
]

# Memory efficiency settings
MAX_TASK_TEXT_LENGTH = 100          # Truncate task text for storage
MAX_RECORDS_KEPT = 500              # Max raw records before compression
COMPRESSION_THRESHOLD = 300         # When to start compressing old records
COMPRESSION_KEEP_RECENT = 50        # Keep latest N records uncompressed


# ─── Data Structures ──────────────────────────────

@dataclass
class ExperienceRecord:
    """One complete task execution — the atomic unit of learning."""
    task: str
    department: str
    task_type: str          # DEVELOPMENT / CODE_REVIEW / RESEARCH / CREATIVE
    auditor_score: float
    pmo_score: float
    final_score: float
    retries: int
    verdict: str            # APPROVE / REVISE / REJECT / FORCE_APPROVE
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    task_id: str = ""       # Unique ID
    errors: list[str] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    peak_retry_score: float = 0.0  # Best score achieved during retries
    tool_failures: int = 0       # Number of tool calls that failed
    tools_used: list[str] = field(default_factory=list)  # Tool names tried


@dataclass
class EvolutionInsight:
    """A pattern discovered from experience — actionable."""
    insight_type: str       # "ROUTING_FIX" / "PROMPT_WEAKNESS" / "THRESHOLD_ADJUST"
    severity: str           # "P0" / "P1" / "P2"
    description: str
    evidence: str           # Data backing this insight
    suggested_action: str   # What to do about it
    affected_role: str      # Which role/department
    confidence: float       # 0.0-1.0 how certain


# ─── Experience Store ─────────────────────────────

class ExperienceStore:
    """Persistent store for task execution records.
    
    Auto-saves to disk. Supports querying by department, score range, etc.
    """
    
    def __init__(self, storage_path: str = None, save_interval: int = 10):
        self.storage_path = storage_path or os.path.join(
            os.path.dirname(__file__), "..", "..", "data", "experience.json"
        )
        self._records: list[ExperienceRecord] = []
        self._task_counter: int = 0
        self._save_interval = max(1, save_interval)  # Batch writes: save every N records
        self._unsaved_count: int = 0
        self._load()
    
    def _load(self):
        try:
            if os.path.exists(self.storage_path):
                with open(self.storage_path, "r") as f:
                    data = json.load(f)
                    self._records = [ExperienceRecord(**r) for r in data.get("records", [])]
                    self._task_counter = data.get("counter", 0)
        except Exception:
            import logging
            logging.getLogger("ai_company.evolution").warning(
                "Failed to load experience store", exc_info=True)
    
    def _save(self):
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        
        # ═══ Memory compression: avoid bloat ═══
        self._compact_if_needed()
        
        with open(self.storage_path, "w") as f:
            json.dump({
                "counter": self._task_counter,
                "records": [asdict(r) for r in self._records],
                "last_saved": datetime.now().isoformat(),
            }, f, ensure_ascii=False, indent=2)
        self._unsaved_count = 0
    
    def _compact_if_needed(self):
        """Smart compression: keep recent records raw, summarize old ones."""
        # Truncate long task texts in all records
        for r in self._records:
            if len(r.task) > MAX_TASK_TEXT_LENGTH:
                r.task = r.task[:MAX_TASK_TEXT_LENGTH - 3] + "..."
        
        # Hard cap: keep only last N records
        if len(self._records) > MAX_RECORDS_KEPT:
            # Compress old records into aggregated stats
            old = self._records[:-COMPRESSION_KEEP_RECENT]
            self._records = self._records[-COMPRESSION_KEEP_RECENT:]
            
            # Store compressed summary as a special record
            if old:
                scores = [r.final_score for r in old]
                depts = {}
                for r in old:
                    depts[r.department] = depts.get(r.department, 0) + 1
                compressed = ExperienceRecord(
                    task=f"[COMPRESSED] {len(old)} historical tasks",
                    department="system",
                    task_type="ARCHIVE",
                    auditor_score=round(sum(scores)/len(scores), 1),
                    pmo_score=round(sum(scores)/len(scores), 1),
                    final_score=round(sum(scores)/len(scores), 1),
                    retries=0,
                    verdict="ARCHIVED",
                    errors=[f"Compressed {len(old)} records: {dict(depts)}"],
                )
                self._records.insert(0, compressed)
                import logging
                logging.getLogger("ai_company.evolution").info(
                    "Compressed %d old records into summary", len(old))
    
    def record(self, record: ExperienceRecord) -> str:
        """Record a completed task execution. Returns task_id.

        Records are buffered in memory and flushed to disk every
        self._save_interval records to avoid I/O bottlenecks.
        Call flush() to force an immediate write.
        """
        self._task_counter += 1
        record.task_id = f"task_{self._task_counter:06d}"
        self._records.append(record)
        # Keep last 2000 records max
        if len(self._records) > 2000:
            self._records = self._records[-1000:]
        self._unsaved_count += 1
        if self._unsaved_count >= self._save_interval:
            self._save()
        return record.task_id

    def flush(self):
        """Force save pending records to disk immediately."""
        if self._unsaved_count > 0:
            self._save()
    
    def query(self, department: str = None, task_type: str = None,
              min_score: float = None, max_score: float = None,
              limit: int = 50) -> list[ExperienceRecord]:
        """Query experience records with filters."""
        results = self._records
        if department:
            results = [r for r in results if r.department == department]
        if task_type:
            results = [r for r in results if r.task_type == task_type]
        if min_score is not None:
            results = [r for r in results if r.final_score >= min_score]
        if max_score is not None:
            results = [r for r in results if r.final_score <= max_score]
        return results[-limit:]
    
    def get_stats(self) -> dict:
        """Get aggregate statistics."""
        if not self._records:
            return {"total_tasks": 0, "message": "No experience yet"}
        
        scores = [r.final_score for r in self._records]
        retries = [r.retries for r in self._records]
        
        by_dept = {}
        for r in self._records:
            if r.department not in by_dept:
                by_dept[r.department] = {"count": 0, "total_score": 0, "total_retries": 0}
            by_dept[r.department]["count"] += 1
            by_dept[r.department]["total_score"] += r.final_score
            by_dept[r.department]["total_retries"] += r.retries
        
        for dept in by_dept:
            c = by_dept[dept]["count"]
            by_dept[dept]["avg_score"] = round(by_dept[dept]["total_score"] / c, 1)
            by_dept[dept]["avg_retries"] = round(by_dept[dept]["total_retries"] / c, 1)
        
        return {
            "total_tasks": len(self._records),
            "avg_score": round(sum(scores) / len(scores), 1),
            "avg_retries": round(sum(retries) / len(retries), 1),
            "best_score": max(scores),
            "worst_score": min(scores),
            "force_approves": sum(1 for r in self._records if "FORCE" in (r.verdict or "")),
            "by_department": by_dept,
            "score_trend": [scores[i] for i in range(0, len(scores), max(1, len(scores)//10))],
        }
    
    def count(self) -> int:
        return len(self._records)


# ─── Pattern Analyzer ─────────────────────────────

class PatternAnalyzer:
    """Analyzes accumulated experience for patterns and generates insights.
    
    Like a senior engineer doing a retrospective: "We keep failing at X,
    here's why, here's the fix."
    """
    
    MIN_SAMPLES = 3  # Need at least this many to trust a pattern
    
    def __init__(self, store: ExperienceStore):
        self.store = store
    
    def analyze(self) -> list[EvolutionInsight]:
        """Run full analysis on accumulated experience. Returns actionable insights."""
        insights = []
        
        stats = self.store.get_stats()
        if stats.get("total_tasks", 0) < self.MIN_SAMPLES:
            return [EvolutionInsight(
                insight_type="NOT_ENOUGH_DATA",
                severity="P2",
                description=f"Only {stats['total_tasks']} tasks completed. Need {self.MIN_SAMPLES}+ for reliable patterns.",
                evidence=str(stats),
                suggested_action="Run more tasks through the system.",
                affected_role="system",
                confidence=0.3,
            )]
        
        # Pattern 1: Department score analysis
        insights.extend(self._analyze_department_performance(stats))
        
        # Pattern 2: Retry pattern analysis
        insights.extend(self._analyze_retry_patterns())
        
        # Pattern 3: Task type analysis
        insights.extend(self._analyze_task_types())
        
        # Pattern 4: Score degradation analysis
        insights.extend(self._analyze_score_degradation())
        
        # Pattern 5: Success rate trends
        insights.extend(self._analyze_success_trends())

        # Pattern 6: Tool reliability analysis
        insights.extend(self._analyze_tool_reliability())
        
        return insights
    
    def _analyze_department_performance(self, stats: dict) -> list[EvolutionInsight]:
        """Which departments consistently underperform?"""
        insights = []
        by_dept = stats.get("by_department", {})
        
        for dept, data in by_dept.items():
            if data["count"] < self.MIN_SAMPLES:
                continue
            
            avg_score = data["avg_score"]
            avg_retries = data["avg_retries"]
            
            if avg_score < 60:
                insights.append(EvolutionInsight(
                    insight_type="PROMPT_WEAKNESS",
                    severity="P0" if avg_score < 45 else "P1",
                    description=f"{dept} department averages {avg_score}/100 over {data['count']} tasks",
                    evidence=f"avg_score={avg_score}, avg_retries={avg_retries}, samples={data['count']}",
                    suggested_action=f"Review and improve {dept} role's system prompt. Consider stronger model.",
                    affected_role=dept,
                    confidence=min(0.9, data["count"] / 10),
                ))
            
            if avg_retries > 2.0:
                insights.append(EvolutionInsight(
                    insight_type="THRESHOLD_ADJUST",
                    severity="P2",
                    description=f"{dept} averages {avg_retries} retries per task — excessive",
                    evidence=f"avg_retries={avg_retries}, over {data['count']} tasks",
                    suggested_action=f"Review {dept}'s acceptance criteria strictness or gate thresholds.",
                    affected_role=dept,
                    confidence=0.7,
                ))
        
        return insights
    
    def _analyze_retry_patterns(self) -> list[EvolutionInsight]:
        """Do retries improve or degrade scores?"""
        records = self.store._records
        if len(records) < 5:
            return []
        
        # Check if retries actually help
        improved = []
        degraded = []
        for r in records:
            if r.retries > 0 and r.peak_retry_score > 0:
                if r.peak_retry_score > r.final_score:
                    degraded.append(r)
                else:
                    improved.append(r)
        
        insights = []
        if len(degraded) > len(improved) * 2:
            insights.append(EvolutionInsight(
                insight_type="RETRY_STRATEGY",
                severity="P1",
                description=f"Retries degrade scores: {len(degraded)} degraded vs {len(improved)} improved",
                evidence=f"degraded={len(degraded)}, improved={len(improved)}",
                suggested_action="Reduce max_retries from 3 to 1 or fix the retry feedback loop.",
                affected_role="ceo",
                confidence=0.8,
            ))
        
        return insights
    
    def _analyze_task_types(self) -> list[EvolutionInsight]:
        """Which task types need routing fixes?"""
        records = self.store._records
        if len(records) < 5:
            return []
        
        by_type = {}
        for r in records:
            tt = r.task_type or "unknown"
            if tt not in by_type:
                by_type[tt] = {"scores": [], "retries": [], "sample_tasks": []}
            by_type[tt]["scores"].append(r.final_score)
            by_type[tt]["retries"].append(r.retries)
            if len(by_type[tt]["sample_tasks"]) < 3:
                by_type[tt]["sample_tasks"].append(r.task[:100])
        
        insights = []
        for tt, data in by_type.items():
            if len(data["scores"]) < 3:
                continue
            avg = sum(data["scores"]) / len(data["scores"])
            if avg < 55:
                insights.append(EvolutionInsight(
                    insight_type="ROUTING_FIX",
                    severity="P1",
                    description=f"{tt} tasks average {avg:.1f}/100 — may need routing or template fix",
                    evidence=f"avg={avg:.1f}, samples={data['sample_tasks'][:2]}",
                    suggested_action=f"Check if {tt} tasks are correctly matched to department and task type.",
                    affected_role="ceo",
                    confidence=0.7,
                ))
        
        return insights
    
    def _analyze_score_degradation(self) -> list[EvolutionInsight]:
        """Are scores trending down over time?"""
        records = self.store._records
        if len(records) < 10:
            return []
        
        # Compare first half vs second half
        mid = len(records) // 2
        first_half = [r.final_score for r in records[:mid]]
        second_half = [r.final_score for r in records[mid:]]
        
        avg_first = sum(first_half) / len(first_half)
        avg_second = sum(second_half) / len(second_half)
        
        if avg_second < avg_first - 10:
            return [EvolutionInsight(
                insight_type="SCORE_DEGRADATION",
                severity="P1",
                description=f"Scores trending down: {avg_first:.1f} → {avg_second:.1f}",
                evidence=f"first_half_avg={avg_first:.1f}, second_half_avg={avg_second:.1f}",
                suggested_action="Investigate if recent changes introduced regressions.",
                affected_role="system",
                confidence=0.6,
            )]
        elif avg_second > avg_first + 5:
            return [EvolutionInsight(
                insight_type="SCORE_IMPROVEMENT",
                severity="P2",
                description=f"Scores trending up: {avg_first:.1f} → {avg_second:.1f} — evolution working! 📈",
                evidence=f"first_half_avg={avg_first:.1f}, second_half_avg={avg_second:.1f}",
                suggested_action="Continue current improvement trajectory.",
                affected_role="system",
                confidence=0.7,
            )]
        
        return []
    
    def _analyze_success_trends(self) -> list[EvolutionInsight]:
        """What's the overall pass rate?"""
        records = self.store._records
        if len(records) < 10:
            return []
        
        passed = sum(1 for r in records if r.verdict in ("APPROVE", "APPROVE_AFTER_RETRY"))
        pass_rate = passed / len(records)
        
        if pass_rate < 0.3:
            return [EvolutionInsight(
                insight_type="LOW_PASS_RATE",
                severity="P0",
                description=f"Only {pass_rate*100:.0f}% tasks pass first round",
                evidence=f"passed={passed}, total={len(records)}",
                suggested_action="Gate thresholds may be too high. Consider lowering or accepting REVISE as OK.",
                affected_role="ceo",
                confidence=0.9,
            )]
        
        return []

    def _analyze_tool_reliability(self) -> list[EvolutionInsight]:
        """Detect consistently failing tools and suggest action."""
        records = self.store._records
        if len(records) < 5:
            return []

        # Aggregate tool stats
        tool_fail_count: dict[str, int] = {}
        tool_total: dict[str, int] = {}
        for r in records:
            for t in (r.tools_used or []):
                tool_total[t] = tool_total.get(t, 0) + 1
            if r.tool_failures > 0:
                for t in (r.tools_used or []):
                    tool_fail_count[t] = tool_fail_count.get(t, 0) + 1

        insights = []
        for tool, total in tool_total.items():
            fails = tool_fail_count.get(tool, 0)
            if total >= 3:
                fail_rate = fails / total
                if fail_rate >= 0.5:
                    insights.append(EvolutionInsight(
                        insight_type="TOOL_RELIABILITY",
                        severity="P1" if fail_rate >= 0.7 else "P2",
                        description=(
                            f"Tool '{tool}' fails {fail_rate*100:.0f}% of the time "
                            f"({fails}/{total} calls)"
                        ),
                        evidence=f"fail_rate={fail_rate:.2f}, total={total}",
                        suggested_action=(
                            f"Consider disabling '{tool}' or providing a local fallback."
                        ),
                        affected_role="system",
                        confidence=min(0.9, total / 10),
                    ))

        return insights


# ─── Adaptation Engine ────────────────────────────

class AdaptationEngine:
    """Applies learned insights to improve the system.
    
    Handles:
    - Role keyword auto-tuning
    - Prompt refinement suggestions
    - Gate threshold recommendations
    """
    
    def __init__(self, store: ExperienceStore, analyzer: PatternAnalyzer):
        self.store = store
        self.analyzer = analyzer
        self._adaptation_log: list[dict] = []
        self._log_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "data", "adaptation_log.json"
        )
        self._load_log()

    def _load_log(self):
        """Load adaptation history from disk."""
        try:
            if os.path.exists(self._log_path):
                with open(self._log_path, "r") as f:
                    data = json.load(f)
                    self._adaptation_log = data.get("log", [])
        except Exception:
            # First load or corrupted file — start fresh
            self._adaptation_log = []

    def _save_log(self):
        """Persist adaptation history."""
        try:
            os.makedirs(os.path.dirname(self._log_path), exist_ok=True)
            with open(self._log_path, "w") as f:
                json.dump({
                    "log": self._adaptation_log,
                    "last_updated": datetime.now().isoformat(),
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            # Non-critical: log persistence failure doesn't block evolution
            _ = None

    def evolve(self) -> dict:
        """Run full evolution cycle: analyze → adapt → report."""
        insights = self.analyzer.analyze()

        applied = []
        pending = []

        for insight in insights:
            if insight.confidence < 0.5:
                pending.append(asdict(insight))
                continue

            action = self._apply_insight(insight)
            if action and action.get("applied"):
                applied.append(action)
            elif action:
                pending.append(asdict(insight))
            else:
                pending.append(asdict(insight))

        # Log this evolution cycle
        self._adaptation_log.append({
            "timestamp": datetime.now().isoformat(),
            "insights_found": len(insights),
            "applied": len(applied),
            "pending": len(pending),
            "actions": [a.get("action", "?") for a in applied],
        })
        self._save_log()

        return {
            "cycle": len(self._adaptation_log),
            "insights_total": len(insights),
            "adaptations_applied": len(applied),
            "adaptations_pending": len(pending),
            "applied": applied,
            "pending": [i if isinstance(i, dict) else asdict(i) for i in pending],
            "stats": self.store.get_stats(),
        }

    def _apply_insight(self, insight: EvolutionInsight) -> dict | None:
        """Apply an insight to the system.

        Now actually applies low-risk changes:
        - Keyword tuning: auto-adds keywords to role registry
        - Prompt improvement: auto-enhances role system prompts
        - Threshold adjustment: auto-adjusts gate within safe bounds (55-85)
        """

        if insight.insight_type == "ROUTING_FIX":
            return self._fix_routing(insight)

        elif insight.insight_type == "PROMPT_WEAKNESS":
            return self._improve_prompt(insight)

        elif insight.insight_type == "THRESHOLD_ADJUST":
            return self._adjust_threshold(insight)

        elif insight.insight_type == "RETRY_STRATEGY":
            return self._suggest_retry_strategy(insight)

        elif insight.insight_type == "SCORE_IMPROVEMENT":
            return {"action": "acknowledge", "detail": insight.description, "applied": False}

        elif insight.insight_type == "LOW_PASS_RATE":
            return self._adjust_threshold(insight)

        return None

    def _fix_routing(self, insight: EvolutionInsight) -> dict:
        """Auto-tune role keywords to improve routing."""
        from src.departments.roles import role_registry

        role = role_registry.get(insight.affected_role)
        if not role:
            return {"action": "routing_fix", "applied": False, "reason": "Role not found"}

        # Extract potential keywords from the evidence (sample tasks)
        import re
        evidence = insight.evidence or ""
        # Pull out task keywords from the evidence
        task_words = re.findall(r"'([^']+)'", str(evidence))

        new_keywords = []
        for word in task_words[:5]:
            word = word.strip().lower()[:30]
            if word and word not in role.keywords and len(word) > 1:
                new_keywords.append(word)

        if not new_keywords:
            return {
                "action": "routing_fix",
                "role": insight.affected_role,
                "applied": False,
                "reason": "No extractable keywords from evidence",
            }

        old_count = len(role.keywords)
        role.keywords.extend(new_keywords)
        # Deduplicate
        role.keywords = list(dict.fromkeys(role.keywords))

        # Persist if dynamic role
        if role.dynamic:
            role_registry._save_dynamic()

        return {
            "action": "keyword_tuning",
            "role": insight.affected_role,
            "applied": True,
            "added_keywords": new_keywords,
            "before": old_count,
            "after": len(role.keywords),
            "detail": f"Added {len(new_keywords)} keywords to {insight.affected_role}: {new_keywords}",
        }

    def _improve_prompt(self, insight: EvolutionInsight) -> dict:
        """Auto-enhance role system prompt when a role underperforms."""
        from src.departments.roles import role_registry

        role = role_registry.get(insight.affected_role)
        if not role:
            return {"action": "prompt_improve", "applied": False, "reason": "Role not found"}

        # Only enhance once per role per evolution cycle (avoid bloat)
        prompt_key = f"prompt_enhanced_{insight.affected_role}"
        if any(
            isinstance(a, dict)
            and a.get("action") == "prompt_improve"
            and a.get("role") == insight.affected_role
            for entry in self._adaptation_log[-3:]
            for a in entry.get("actions", [])
        ):
            return {
                "action": "prompt_improve",
                "role": insight.affected_role,
                "applied": False,
                "reason": "Already enhanced recently (within last 3 cycles)",
            }

        # Add a quality-focused suffix to the prompt
        enhancement = (
            f"\n\n[Auto-Evolution {datetime.now().strftime('%Y-%m-%d')}] "
            f"Quality Alert: Recent audits show {insight.description}. "
            f"Focus on: {insight.suggested_action}"
        )

        # Only append if not already present (ignore timestamp in comparison)
        # Strip the [Auto-Evolution ...] prefix for duplicate detection
        enhancement_body = (
            f"Quality Alert: Recent audits show {insight.description}. "
            f"Focus on: {insight.suggested_action}"
        )
        if enhancement_body.strip() not in role.system_prompt:
            role.system_prompt = role.system_prompt.rstrip() + enhancement

        if role.dynamic:
            role_registry._save_dynamic()

        return {
            "action": "prompt_improve",
            "role": insight.affected_role,
            "applied": True,
            "detail": f"Enhanced {insight.affected_role}'s prompt with quality focus: {insight.suggested_action[:100]}",
        }

    def _adjust_threshold(self, insight: EvolutionInsight) -> dict:
        """Auto-adjust gate thresholds within safe bounds.

        Safe range: 55-85. Never goes below 55 or above 85 automatically.
        Adjusts by ±5 based on patterns.
        """
        from src.config import config

        current = config.gate_final_score

        # Determine direction
        if insight.insight_type == "LOW_PASS_RATE":
            new_value = max(55, current - 5)
            reason = f"Low pass rate detected, lowering gate from {current} to {new_value}"
        elif insight.insight_type == "THRESHOLD_ADJUST" and "excessive" in insight.description.lower():
            new_value = max(55, current - 5)
            reason = f"Excessive retries, lowering gate from {current} to {new_value}"
        else:
            # Default: slight relaxation (score trending down)
            new_value = max(55, current - 5)
            reason = f"Performance pattern suggests relaxing gate from {current} to {new_value}"

        if new_value == current:
            return {
                "action": "threshold_adjust",
                "applied": False,
                "reason": f"Gate already at minimum safe value ({current})",
            }

        config.gate_final_score = new_value

        return {
            "action": "threshold_adjust",
            "applied": True,
            "before": current,
            "after": new_value,
            "detail": reason,
        }

    def _suggest_retry_strategy(self, insight: EvolutionInsight) -> dict:
        """Retry strategy is high-risk, suggestion only."""
        return {
            "action": "retry_strategy",
            "applied": False,
            "suggestion": insight.suggested_action,
            "reason": "Retry strategy change requires manual review (safety guardrail)",
        }
    
    def get_evolution_report(self) -> str:
        """Human-readable evolution status."""
        stats = self.store.get_stats()
        insights = self.analyzer.analyze()
        
        lines = [
            "🧬 AI-Company Evolution Status",
            f"   Tasks completed: {stats.get('total_tasks', 0)}",
            f"   Average score: {stats.get('avg_score', 'N/A')}",
            f"   Force approves: {stats.get('force_approves', 0)}",
            f"   Adaptation cycles: {len(self._adaptation_log)}",
            f"   Active insights: {len(insights)}",
            "",
            "📊 Patterns found:",
        ]
        
        for i, insight in enumerate(insights[:5], 1):
            lines.append(f"   [{insight.severity}] {insight.description[:80]}")
            if insight.suggested_action:
                lines.append(f"       → {insight.suggested_action[:100]}")
        
        by_dept = stats.get("by_department", {})
        if by_dept:
            lines.append("\n📈 By department:")
            for dept, data in sorted(by_dept.items(), key=lambda x: x[1].get("avg_score", 0)):
                lines.append(f"   {dept}: {data.get('avg_score', '?')}/100 ({data['count']} tasks)")
        
        return "\n".join(lines)


# Global instance
_experience_store: Optional[ExperienceStore] = None
_adaptation_engine: Optional[AdaptationEngine] = None


def get_experience_store() -> ExperienceStore:
    global _experience_store
    if _experience_store is None:
        _experience_store = ExperienceStore()
    return _experience_store


def get_adaptation_engine() -> AdaptationEngine:
    global _adaptation_engine, _experience_store
    if _adaptation_engine is None:
        store = get_experience_store()
        analyzer = PatternAnalyzer(store)
        _adaptation_engine = AdaptationEngine(store, analyzer)
    return _adaptation_engine


def record_completed_task(
    task: str,
    department: str,
    task_type: str,
    auditor_score: float,
    pmo_score: float,
    final_score: float,
    retries: int,
    verdict: str,
    errors: list[str] = None,
    peak_retry_score: float = 0.0,
    tool_failures: int = 0,
    tools_used: list[str] = None,
):
    """Convenience function: record a completed task for evolution.
    
    Args:
        tool_failures: Number of tool calls that failed during execution.
        tools_used: List of tool names that were called.
    """
    store = get_experience_store()
    record = ExperienceRecord(
        task=task[:MAX_TASK_TEXT_LENGTH],
        department=department,
        task_type=task_type,
        auditor_score=auditor_score,
        pmo_score=pmo_score,
        final_score=final_score,
        retries=retries,
        verdict=verdict,
        errors=errors or [],
        peak_retry_score=peak_retry_score,
        tool_failures=tool_failures,
        tools_used=tools_used or [],
    )
    return store.record(record)


# Ensure unsaved experience records are flushed on process exit
import atexit as _atexit


@_atexit.register
def _flush_experience_store():
    """Flush any buffered experience records to disk on exit."""
    if _experience_store is not None:
        try:
            _experience_store.flush()
        except Exception:
            import logging
            logging.getLogger("ai_company.evolution").debug("flush on exit failed", exc_info=True)
