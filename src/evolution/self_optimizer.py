"""Self-Optimization Engine — background code optimization.

Toggle: /optimize on|off|status
Behavior: When ON, detects issues and spawns background sub-tasks to fix them.
          Rate-limited (max 1 active, queue up to 3 pending).
          Does NOT block the main task — optimizations run asynchronously.

Architecture:
    Main task → deliver_node
        ↓ (if enabled)
    detect_issues()
        ↓ (if issues found + below rate limit)
    enqueue_optimization()
        ↓ (asyncio background)
    run_optimizer(issue) → analyze → patch → verify → commit

State machine:
    IDLE → OPTIMIZING (1 active) → IDLE
    PENDING: issues queued, will run when active slot frees
    SHUTTING_DOWN: waiting for active to finish, rejecting new tasks

Usage:
    from src.evolution.self_optimizer import optimizer
    optimizer.enable()           # turn on
    optimizer.on_task_complete() # called from deliver_node
    optimizer.disable()          # graceful shutdown, returns when safe
"""

import os
import re
import json
import time
import asyncio
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Callable

logger = logging.getLogger("ai_company.evolution.self_optimizer")

# ─── Data storage ───
OPTIMIZE_DIR = os.path.expanduser("~/.ai-company/optimize")
os.makedirs(OPTIMIZE_DIR, exist_ok=True)
STATE_FILE = os.path.join(OPTIMIZE_DIR, "state.json")
QUEUE_FILE = os.path.join(OPTIMIZE_DIR, "queue.json")

# ─── Rate limits ───
MAX_ACTIVE = 1           # Only 1 optimization at a time
MAX_QUEUED = 3           # Queue up to 3 pending
COOLDOWN_SECONDS = 30    # Don't optimize more than once per 30s
MAX_PER_HOUR = 5         # Max optimizations per hour


# ═══════════════════════════════════════════════════
# Issue detection patterns
# ═══════════════════════════════════════════════════

ISSUE_DETECTORS = [
    {
        "name": "tool_loop_exhausted",
        "pattern": r"Max iterations exhausted|iteration.*exhausted",
        "priority": "high",
        "description": "Agent ran out of iterations",
        "fix_prompt": """The agent exhausted its max iterations. 
Analyze the execution logs below and:
1. Identify why iterations were wasted (failed tools? loops?)
2. If tools failed: fix the tool or add fallback
3. If too few iterations: increase TOOL_MAX_ITERATIONS in agents.py
4. If LLM was looping: add stop condition or reduce complexity
Only make changes if you're confident. If unsure, just log analysis.""",
    },
    {
        "name": "tool_failure_high",
        "pattern": r"Tool failures:\s*(\d+)/(\d+)",
        "priority": "critical",
        "description": "High tool failure rate",
        "fix_prompt": """High tool failure rate detected ({match}).
Analyze which tools are failing and why:
1. Check API keys (DEEPSEEK, TAVILY, QWEN) in .env
2. Check network connectivity
3. Check if tool implementations have bugs
4. Add fallback or retry logic""",
    },
    {
        "name": "node_slow",
        "pattern": None,  # detected via timing data
        "priority": "medium",
        "description": "Node execution too slow (>30s)",
        "fix_prompt": """A graph node took too long (>30s).
Analyze the timing data and:
1. Check if LLM calls can be reduced
2. Check if a fast-path can be added
3. Check if iterations can be reduced
4. Check if tool timeouts are too long""",
    },
    {
        "name": "parse_json_failure",
        "pattern": r"JSONDecodeError|parse.*json.*fail|json.*parse.*error",
        "priority": "medium",
        "description": "JSON parse failure",
        "fix_prompt": """JSON parsing is failing in agent responses.
Check:
1. Is the auto-accept threshold appropriate?
2. Are retry limits set correctly?
3. Is the LLM model not following format instructions?
4. Could the parser be more lenient?""",
    },
]


@dataclass
class OptimizationTask:
    """A pending or active optimization task."""
    id: str
    issue_name: str
    priority: str
    description: str
    fix_prompt: str
    context: dict = field(default_factory=dict)
    status: str = "pending"  # pending|active|done|failed
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    result: Optional[str] = None


class SelfOptimizer:
    """Background code optimization engine.
    
    Singleton. Use `get_optimizer()` to access.
    """
    
    def __init__(self):
        self._enabled = False
        self._active: Optional[OptimizationTask] = None
        self._queue: deque = deque(maxlen=MAX_QUEUED)
        self._history: list = []  # completed optimizations
        self._lock = threading.Lock()
        self._shutting_down = False
        self._hourly_count = 0
        self._last_optimize_time = 0.0
        self._hour_start = time.time()
        self._load()
    
    # ─── State management ───
    
    def _load(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    state = json.load(f)
                    self._enabled = state.get("enabled", False)
                    self._history = state.get("history", [])[-20:]
                    self._hourly_count = state.get("hourly_count", 0)
        except Exception:
            pass
        try:
            if os.path.exists(QUEUE_FILE):
                with open(QUEUE_FILE) as f:
                    data = json.load(f)
                    for item in data:
                        if len(self._queue) < MAX_QUEUED:
                            self._queue.append(OptimizationTask(**item))
        except Exception:
            pass
    
    def _save(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({
                    "enabled": self._enabled,
                    "history": [
                        {"id": t.id, "issue_name": t.issue_name, "priority": t.priority,
                         "status": t.status, "result": t.result}
                        for t in self._history[-20:]
                    ],
                    "hourly_count": self._hourly_count,
                }, f, indent=2)
            with open(QUEUE_FILE, "w") as f:
                json.dump([
                    {k: v for k, v in t.__dict__.items() if k != "_lock"}
                    for t in self._queue
                ], f, indent=2)
        except Exception:
            pass
    
    # ─── Public API ───
    
    def enable(self):
        """Turn on self-optimization."""
        with self._lock:
            self._enabled = True
            self._shutting_down = False
            self._save()
        logger.info("Self-optimization ENABLED")
    
    def disable(self, blocking: bool = True) -> bool:
        """Turn off. If blocking=True, waits for active optimization to finish.
        Returns True if shutdown was clean (no active tasks killed).
        """
        with self._lock:
            self._enabled = False
            self._shutting_down = True
        
        if blocking and self._active:
            logger.info("Waiting for active optimization to complete...")
            timeout = 120
            start = time.time()
            while self._active and (time.time() - start) < timeout:
                time.sleep(0.5)
            if self._active:
                logger.warning("Active optimization did not finish within %ds", timeout)
                return False
        
        with self._lock:
            self._shutting_down = False
            self._save()
        logger.info("Self-optimization DISABLED")
        return True
    
    @property
    def enabled(self) -> bool:
        return self._enabled
    
    def status(self) -> dict:
        """Return current status for display."""
        with self._lock:
            return {
                "enabled": self._enabled,
                "active": self._active.issue_name if self._active else None,
                "queued": len(self._queue),
                "completed": len([t for t in self._history if t.status == "done"]),
                "failed": len([t for t in self._history if t.status == "failed"]),
                "hourly_count": self._hourly_count,
                "max_per_hour": MAX_PER_HOUR,
            }
    
    # ─── Issue detection ───
    
    def detect_issues(self, task: str, execution_log: list, 
                      timing_data: Optional[list] = None) -> list:
        """Scan execution data for optimization opportunities.
        
        Returns list of detected OptimizationTask objects (not yet queued).
        """
        if not self._enabled or self._shutting_down:
            return []
        
        issues = []
        log_text = " ".join(str(e) for e in execution_log)
        
        for detector in ISSUE_DETECTORS:
            if detector["pattern"]:
                match = re.search(detector["pattern"], log_text, re.IGNORECASE)
                if match:
                    fix_prompt = detector["fix_prompt"]
                    if "{match}" in fix_prompt:
                        fix_prompt = fix_prompt.replace("{match}", match.group(0))
                    
                    task_obj = OptimizationTask(
                        id=f"opt_{int(time.time())}_{detector['name']}",
                        issue_name=detector["name"],
                        priority=detector["priority"],
                        description=f"{detector['description']}: {match.group(0)[:80]}",
                        fix_prompt=fix_prompt,
                        context={
                            "task": task[:200],
                            "log_snippet": match.group(0)[:300],
                        },
                    )
                    issues.append(task_obj)
        
        # Check timing data for slow nodes
        if timing_data:
            for record in timing_data:
                if record.get("category") == "node" and record.get("elapsed_ms", 0) > 30000:
                    task_obj = OptimizationTask(
                        id=f"opt_{int(time.time())}_node_slow",
                        issue_name="node_slow",
                        priority="medium",
                        description=f"Slow node: {record['phase']} ({record['elapsed_ms']}ms)",
                        fix_prompt=ISSUE_DETECTORS[2]["fix_prompt"],
                        context={"node": record["phase"], "elapsed_ms": record["elapsed_ms"]},
                    )
                    issues.append(task_obj)
        
        return issues
    
    # ─── Task management ───
    
    def enqueue(self, task: OptimizationTask) -> bool:
        """Add an optimization task to the queue.
        Returns True if queued, False if rejected (full or rate limited).
        """
        with self._lock:
            if not self._enabled or self._shutting_down:
                return False
            
            # Rate limit: cooldown
            if time.time() - self._last_optimize_time < COOLDOWN_SECONDS:
                logger.debug("Cooldown active, skipping optimization")
                return False
            
            # Hourly limit
            if time.time() - self._hour_start > 3600:
                self._hourly_count = 0
                self._hour_start = time.time()
            if self._hourly_count >= MAX_PER_HOUR:
                logger.debug("Hourly limit reached (%d/%d)", self._hourly_count, MAX_PER_HOUR)
                return False
            
            # Queue limit
            if len(self._queue) >= MAX_QUEUED:
                logger.debug("Queue full (%d/%d), dropping lowest priority", len(self._queue), MAX_QUEUED)
                # Drop lowest priority from queue
                self._queue.popleft()
            
            # Deduplicate by issue name
            for existing in self._queue:
                if existing.issue_name == task.issue_name:
                    logger.debug("Duplicate issue '%s' already queued", task.issue_name)
                    return False
            
            self._queue.append(task)
            self._save()
            logger.info("Queued optimization: %s (priority=%s)", task.issue_name, task.priority)
            return True
    
    def on_task_complete(self, task: str, execution_log: list, 
                         timing_data: Optional[list] = None):
        """Called from deliver_node after each task completes.
        Detects issues and spawns background optimizations if appropriate.
        """
        issues = self.detect_issues(task, execution_log, timing_data)
        for issue in issues:
            self.enqueue(issue)
        
        # Try to start processing
        self._process_queue_async()
    
    def _process_queue_async(self):
        """Start processing the queue in the background (non-blocking)."""
        with self._lock:
            if self._active is not None:
                return  # already processing
            if not self._queue:
                return  # nothing to do
            if not self._enabled or self._shutting_down:
                return
        
        # Pop from queue
        with self._lock:
            task = self._queue.popleft()
            task.status = "active"
            task.started_at = time.time()
            self._active = task
            self._last_optimize_time = time.time()
            self._hourly_count += 1
            self._save()
        
        # Run in background thread
        thread = threading.Thread(
            target=self._run_optimization,
            args=(task,),
            daemon=True,
            name=f"optimizer-{task.id}",
        )
        thread.start()
    
    def _run_optimization(self, task: OptimizationTask):
        """Execute a single optimization task (runs in background thread).
        
        The optimization agent:
        1. Reads relevant source files
        2. Analyzes the issue
        3. Makes targeted fixes
        4. Verifies with syntax check
        5. Records results
        """
        logger.info("Starting optimization: %s", task.issue_name)
        
        try:
            # ═══ Step 1: Analyze ═══
            import subprocess
            
            # Build analysis prompt for the LLM
            analysis_prompt = f"""You are an optimization agent for the AI Company project at ~/.openclaw/workspace/ai-company.

ISSUE DETECTED: {task.description}
CONTEXT: Task was "{task.context.get('task', 'unknown')[:200]}"
LOG: {task.context.get('log_snippet', 'no log')[:300]}

{task.fix_prompt}

IMPORTANT RULES:
1. Make MINIMAL changes — fix only what's needed
2. NEVER break existing functionality
3. Run AST check after changes: python3.12 -c "import ast; ast.parse(open('FILE').read())"
4. If you can't fix it confidently, just report the analysis — don't make changes
5. Focus on ONE file at a time

Output your analysis and any changes you recommend."""

            # Use DeepSeek to analyze
            from openai import OpenAI
            import os as _os
            client = OpenAI(
                api_key=_os.environ.get("DEEPSEEK_API_KEY", ""),
                base_url="https://api.deepseek.com/v1",
            )
            
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": analysis_prompt}],
                max_tokens=500,
                temperature=0.5,
            )
            analysis = resp.choices[0].message.content
            logger.info("Optimization analysis: %s", analysis[:200])
            
            # ═══ Step 2: Apply fixes ═══
            # Parse actionable fix from analysis
            fix_applied = False
            actions = self._parse_actions(analysis)
            
            for action in actions:
                if action.get("type") == "patch" and action.get("file"):
                    filepath = os.path.expanduser(
                        f"~/.openclaw/workspace/ai-company/{action['file']}"
                    )
                    if os.path.exists(filepath) and action.get("old") and action.get("new"):
                        try:
                            # Read file
                            with open(filepath) as f:
                                content = f.read()
                            
                            # Apply patch
                            if action["old"] in content:
                                new_content = content.replace(action["old"], action["new"])
                                # AST check
                                try:
                                    import ast
                                    ast.parse(new_content)
                                    with open(filepath, "w") as f:
                                        f.write(new_content)
                                    fix_applied = True
                                    logger.info("Applied fix to %s", action["file"])
                                    
                                    # Git commit
                                    subprocess.run(
                                        ["git", "-C", os.path.expanduser("~/.openclaw/workspace/ai-company"),
                                         "add", action["file"]],
                                        capture_output=True, timeout=10,
                                    )
                                    subprocess.run(
                                        ["git", "-C", os.path.expanduser("~/.openclaw/workspace/ai-company"),
                                         "commit", "-m",
                                         f"optimize: {task.issue_name} ({task.priority})"],
                                        capture_output=True, timeout=10,
                                    )
                                except SyntaxError as se:
                                    logger.warning("AST check failed for %s: %s", action["file"], se)
                            else:
                                logger.debug("Patch string not found in %s", action["file"])
                        except Exception as e:
                            logger.error("Failed to apply patch to %s: %s", action["file"], e)
            
            # ═══ Step 3: Record result ═══
            task.status = "done" if fix_applied else "failed"
            task.result = analysis[:500]
            task.finished_at = time.time()
            self._history.append(task)
            
        except Exception as e:
            logger.error("Optimization failed: %s", e)
            task.status = "failed"
            task.result = str(e)[:500]
            task.finished_at = time.time()
            self._history.append(task)
        
        finally:
            # Cleanup
            with self._lock:
                self._active = None
            self._save()
            
            # Process next in queue
            time.sleep(COOLDOWN_SECONDS)
            self._process_queue_async()
    
    def _parse_actions(self, analysis: str) -> list:
        """Parse actionable patches from LLM analysis."""
        actions = []
        # Look for sed-style or patch-style instructions
        # Pattern: FILE: filepath | OLD: old_text | NEW: new_text
        blocks = re.split(r'\n(?=FILE:)', analysis)
        for block in blocks:
            file_match = re.search(r'FILE:\s*(\S+)', block)
            old_match = re.search(r'OLD:\s*(.+?)(?:\n|$)', block, re.DOTALL)
            new_match = re.search(r'NEW:\s*(.+?)(?:\n|$)', block, re.DOTALL)
            if file_match:
                action = {"type": "patch", "file": file_match.group(1).strip()}
                if old_match:
                    action["old"] = old_match.group(1).strip().strip('"').strip("'")
                if new_match:
                    action["new"] = new_match.group(1).strip().strip('"').strip("'")
                if action.get("old") and action.get("new"):
                    actions.append(action)
        return actions


# ─── Global singleton ───

_optimizer: Optional[SelfOptimizer] = None

def get_optimizer() -> SelfOptimizer:
    global _optimizer
    if _optimizer is None:
        _optimizer = SelfOptimizer()
    return _optimizer
