"""AI Company - Main Entry Point

Start:  goudan
Or:     cd ai-company && python3 -m src.main --mode cli
"""

import os
import sys
import warnings

# ── Silence noisy libraries BEFORE any other imports ──
import logging
for _noisy in ("jieba", "jieba.cache", "jieba.cutter", "jieba.cpex"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
    logging.getLogger(_noisy).propagate = False
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="jieba")

import asyncio
import argparse
import select
import signal
import sys
import time

# Ensure project root is in Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.config import config
from src.telegram_bot import start_bot
from src.utils.platform import setup_readline, get_platform
from src.session import get_session_manager, get_session_memory, auto_summarize_conversation
from src.session.summarizer import detect_global_memory_intent, save_to_global_memory

# CJK multi-byte backspace support (gnureadline on macOS, GNU readline on Linux)
_readline_ok = setup_readline()

logging.basicConfig(
    level=getattr(logging, config.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("ai_company")


async def show_evolution(run_analysis: bool = False):
    """Display auto-evolution status."""
    from src.evolution.engine import get_experience_store, get_adaptation_engine
    from rich.console import Console
    from rich.panel import Panel
    
    console = Console()
    store = get_experience_store()
    engine = get_adaptation_engine()
    
    if run_analysis:
        console.print("\n🔬 [bold]Running evolution analysis...[/bold]\n")
        result = engine.evolve()
        console.print(Panel(
            f"Insights found: {result['insights_total']}\n"
            f"Applied: {result['adaptations_applied']}\n"
            f"Pending: {result['adaptations_pending']}",
            title="Evolution Cycle Complete"
        ))
        if result.get('applied'):
            for a in result['applied']:
                console.print(f"  ✅ {a.get('action', '?')}: {a.get('detail', str(a)[:100])}")
    
    report = engine.get_evolution_report()
    console.print(Panel(report, title="🧬 Auto-Evolution Status"))
    
    if store.count() == 0:
        console.print("\n[yellow]No experience records yet. Run some tasks first![/yellow]")


async def show_memory(do_compact: bool = False, do_clear: bool = False):
    """Display memory health across all stores."""
    from src.memory.store import get_memory_health, episode_memory
    from src.evolution.engine import get_experience_store
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()

    if do_clear:
        await episode_memory.clear()
        console.print("✅ [green]Episode memory cleared[/green]")
        return

    if do_compact:
        result = await episode_memory.compact()
        console.print(Panel(
            f"Compacted: {result['compacted']} episodes\n"
            f"Kept: {result['kept']} episodes",
            title="🧹 Memory Compaction"
        ))
        return

    # Memory health
    health = get_memory_health()

    # Episode stats
    ep = health["episode_memory"]
    dedup = ep.get("dedup_rate_pct", 0)
    dedup_color = "green" if dedup > 10 else ("yellow" if dedup > 0 else "red")

    console.print(Panel(
        f"📦 Total Episodes: {ep['total_episodes']}  "
        f"|  💾 Storage: {ep.get('storage_size_bytes', 0) // 1024} KB\n"
        f"{'🟢 Dirty' if ep['dirty'] else '✅ Clean'} (unsaved changes)\n"
        f"📂 Path: {ep['storage_path']}\n\n"
        f"📊 Activity Metrics:\n"
        f"  Adds: {ep.get('total_adds', 0)}  "
        f"|  Searches: {ep.get('searches', 0)}  "
        f"|  Saves: {ep.get('saves', 0)}\n"
        f"  [{dedup_color}]Dedup Rate: {dedup}%[/{dedup_color}]  "
        f"|  Prunes: {ep.get('prunes', 0)}  "
        f"|  Compactions: {ep.get('compactions', 0)}\n\n"
        f"👥 By Role ({ep.get('role_count', 0)} roles): {ep.get('by_role', {})}",
        title="📦 Episode Memory",
    ))

    # Agent states
    agents = health.get("agent_states", {})
    if agents:
        table = Table(title="🤖 Agent States")
        table.add_column("Agent", style="cyan")
        table.add_column("Has Task", style="yellow")
        table.add_column("WM Items", style="green")
        table.add_column("Decisions", style="magenta")
        for aid, info in agents.items():
            table.add_row(
                aid,
                str(info["has_task"]),
                str(info["wm_items"]),
                str(info["decisions"]),
            )
        console.print(table)

    # Chroma
    console.print(f"🔍 Chroma Available: {health['chroma_available']}")

    # Experience store
    try:
        store = get_experience_store()
        stats = store.get_stats()
        console.print(Panel(
            f"Tasks: {stats.get('total_tasks', 0)}\n"
            f"Avg Score: {stats.get('avg_score', 'N/A')}\n"
            f"Avg Retries: {stats.get('avg_retries', 'N/A')}\n"
            f"Best/Worst: {stats.get('best_score', '?')}/{stats.get('worst_score', '?')}\n"
            f"Force Approves: {stats.get('force_approves', 0)}",
            title="🧬 Evolution Experience"
        ))
    except Exception:
        console.print("[dim]Experience store not available[/dim]")


async def run_cli():
    """CLI mode: interactive chat with CEO.
    Clean startup banner like OpenClaw/Hermes style."""
    from src.ceo.graph import run_ceo
    from src.departments.roles import role_registry
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from src.evolution.engine import get_experience_store

    console = Console()

    # ── Signal handling for graceful shutdown ──
    _shutdown_requested = False

    def _signal_handler(signum, frame):
        nonlocal _shutdown_requested
        if _shutdown_requested:
            # Second signal → force exit
            console.print("\n  [red]Force quitting...[/red]")
            os._exit(1)
        _shutdown_requested = True
        console.print("\n  [yellow]Shutting down... (press Ctrl+C again to force)[/yellow]")

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    def _read_input(prompt: str = "▸ ") -> str:
        """Read input with multi-line support and paste detection.
        
        Multi-line: end a line with \\ to continue on the next line (like bash).
        Paste detection: when stdin is a TTY and multiple lines arrive at once
        (e.g. copy-paste), they are joined into a single input.
        Piped input is read line-by-line.
        """
        try:
            first = input(prompt)
        except (EOFError, KeyboardInterrupt):
            raise
        if not first or not first.strip():
            return first.strip()
        # Only use paste detection + continuation when interactive (TTY)
        if not sys.stdin.isatty():
            return first.strip()
        
        # ── Multi-line continuation: \\ at end of line → continue ──
        lines = []
        current = first
        cont_prompt = "… "  # Continuation prompt
        while current.rstrip().endswith("\\"):
            # Strip trailing \\ and whitespace, keep the line
            stripped = current.rstrip()[:-1].rstrip()
            if stripped:
                lines.append(stripped)
            current = input(cont_prompt)
        if current.strip():
            lines.append(current.rstrip())
        
        # If continuation triggered, return joined lines
        if lines:
            return "\n".join(lines)
        
        # ── Paste detection: check if more data is buffered ──
        paste_lines = [first]
        try:
            while select.select([sys.stdin], [], [], 0)[0]:
                try:
                    line = input()
                    paste_lines.append(line)
                except (EOFError, KeyboardInterrupt):
                    break
        except (ValueError, OSError):
            pass  # stdin not a tty, select not supported
        if len(paste_lines) == 1:
            return first.strip()
        return "\n".join(line.rstrip() for line in paste_lines)

    # ── Startup Banner (OpenClaw-style clean) ──
    plat = get_platform()
    store = get_experience_store()
    stats = store.get_stats()
    roles = role_registry.list_all()
    
    # Initialize session manager
    session_mgr = get_session_manager()
    current_session = session_mgr.current
    session_name = current_session.name if current_session else "default"
    exec_count = sum(1 for r in roles if r.category == "execution")
    ctrl_count = sum(1 for r in roles if r.category == "control")
    exp_tasks = stats.get("total_tasks", 0)
    avg_score = stats.get("avg_score", "N/A")

    # Status line
    status_parts = [
        f"{plat.system}/{plat.arch}",
        f"py{plat.python_version}",
        f"{ctrl_count} ctrl + {exec_count} exec",
    ]
    if exp_tasks > 0:
        status_parts.append(f"{exp_tasks} tasks (avg {avg_score}/100)")
    else:
        status_parts.append("fresh start")
    if not plat.has_ruff:
        status_parts.append("⚠ no ruff")
    if not _readline_ok and plat.system == "Darwin":
        status_parts.append("⚠ libedit CJK issue")
    status_line = " · ".join(status_parts)

    title = Text(f"🐶 狗蛋儿 · AI Company  [{session_name}]", style="bold cyan")
    console.print(title)
    console.print(f"  {status_line}", style="dim")
    console.print(f"  Session: [cyan]{session_name}[/cyan] | Type /help for commands, /quit to exit")
    console.print()

    while True:
        if _shutdown_requested:
            console.print("  👋 Bye!")
            break
        try:
            user_input = _read_input()
        except (EOFError, KeyboardInterrupt):
            console.print("\n  👋 Bye!")
            break

        if not user_input:
            continue

        # ── Built-in commands ──
        if user_input.lower() in ("/quit", "/exit", "/q"):
            # Auto-summarize before exit
            if current_session:
                console.print("  [yellow]💭 Saving session memory...[/yellow]")
                try:
                    from src.session import auto_summarize_conversation
                    summary = await auto_summarize_conversation(
                        current_session.id, llm=None, force=True
                    )
                    if summary:
                        console.print(f"  [dim]📝 {summary[:100]}[/dim]")
                except Exception:
                    pass
                session_mgr.save_all()
            console.print("  👋 Bye!")
            break
        if user_input.lower() in ("/help", "/?"):
            _show_help(console)
            continue
        # ── Session commands ──
        if user_input.lower().startswith("/session") or user_input.lower() == "/sessions":
            await _cmd_session(console, user_input, session_mgr, current_session)
            # Refresh current_session after possible switch
            current_session = session_mgr.current
            session_name = current_session.name if current_session else "default"
            title = Text(f"🐶 狗蛋儿 · AI Company  [{session_name}]", style="bold cyan")
            continue
        # ── Global memory commands ──
        if user_input.lower().startswith("/global"):
            _cmd_global(console, user_input)
            continue
        # ── Vision commands ──
        if user_input.lower().startswith("/vision"):
            _cmd_vision(console, user_input, current_session)
            continue
        if user_input.lower() == "/status":
            _show_status(console, plat, roles, store)
            continue
        if user_input.lower() == "/roles":
            _show_roles(console, roles)
            continue
        if user_input.lower() in ("/token", "/token_usage", "/usage"):
            _show_token_stats(console)
            continue
        if user_input.lower() == "/memory":
            await show_memory(do_compact=False, do_clear=False)
            continue
        if user_input.lower() in ("/clear", "/cls"):
            console.clear()
            console.print(title)
            console.print(f"  {status_line}", style="dim")
            continue
        if user_input.lower() in ("/fix", "/self-heal", "/heal"):
            await _cmd_self_heal(console)
            continue
        if user_input.lower() in ("/diag", "/diagnose"):
            await _cmd_diag(console)
            continue

        console.print()
        # Show cursor during thinking (Rich spinner hides it; ensure restore)
        console.show_cursor(True)
        t_start = time.time()
        with console.status("[yellow]CEO thinking…[/yellow]", spinner="dots"):
            t0 = time.time()
            try:
                result = await run_ceo(user_input)
            except (KeyboardInterrupt, asyncio.CancelledError):
                console.print("\n  [yellow]Task cancelled by user.[/yellow]")
                continue
            except Exception as e:
                logger.exception("CEO workflow crashed")
                console.print(f"  [red]✗ Error: {type(e).__name__}: {e}[/red]")
                import traceback
                tb = traceback.format_exc()
                console.print(f"  [dim]{tb.split(chr(10))[-4]}[/dim]")
                continue
            elapsed = time.time() - t0
        total_elapsed = time.time() - t_start
        # Ensure cursor is visible after spinner
        console.show_cursor(True)

        phase = result.get("phase", "?")
        score = result.get("score_card", {}).get("final_score",
                result.get("score_card", {}).get("score", "—"))
        verdict = result.get("score_card", {}).get("decision",
                  result.get("score_card", {}).get("verdict", ""))
        logs = result.get("execution_log", [])

        # Result header
        verdict_color = "green" if isinstance(score, (int, float)) and score >= 70 else \
                        "yellow" if isinstance(score, (int, float)) and score >= 50 else "red"
        console.print(
            f"  [{verdict_color}]●[/{verdict_color}] {phase} | score: {score} | {elapsed:.1f}s" +
            (f" (total {total_elapsed:.1f}s)" if total_elapsed > elapsed + 1 else "")
        )
        if verdict and verdict != phase:
            console.print(f"  verdict: {verdict}", style="dim")

        # Execution log (compact)
        if logs:
            for log in logs[-5:]:
                console.print(f"  [dim]{log}[/dim]")

        # Output
        final = result.get("final_output", "")
        if final:
            from src.ceo.graph import _clean_output
            cleaned = _clean_output(final)
            console.print()
            display = cleaned[:2000]
            if len(cleaned) > 2000:
                display += "\n…[truncated]"
            console.print(Panel(display, border_style="cyan", padding=(1, 2)))

        console.print()
        
        # ── Record conversation to session memory ──
        if current_session and final:
            try:
                session_mgr.record_message()
                mem = get_session_memory(current_session.id)
                mem.record_conversation(user_input, cleaned or final)
                # Check for global memory intent
                if detect_global_memory_intent(user_input):
                    console.print("  [dim]🌐 Detected global memory intent[/dim]")
            except Exception:
                pass


# ─── Built-in Commands ──────────────────────────

def _show_help(console):
    """Display available commands."""
    from rich.table import Table
    table = Table(title="Commands", show_header=False, padding=(0, 2))
    table.add_column(style="cyan")
    table.add_column(style="dim")
    table.add_row("/help, /?", "Show this help")
    table.add_row("/status", "System status & stats")
    table.add_row("/roles", "List all agents")
    table.add_row("/token, /usage", "Session token usage")
    table.add_row("/memory", "Memory health")
    table.add_row("/sessions", "List all sessions")
    table.add_row("/session new <name>", "Create new session")
    table.add_row("/session switch <name>", "Switch session")
    table.add_row("/session rename <name>", "Rename current session")
    table.add_row("/session delete <name>", "Delete a session")
    table.add_row("/global set <k> <v>", "Set global memory")
    table.add_row("/global get <k>", "Get global memory")
    table.add_row("/clear, /cls", "Clear screen")
    table.add_row("/vision scan", "Capture + analyze screen")
    table.add_row("/vision status", "Visual context status")
    table.add_row("/fix, /heal", "Auto-detect and fix code errors")
    table.add_row("/quit, /q", "Exit")
    console.print(table)
    console.print("\n[dim]💡 Multi-line: end a line with \\\\ to continue on the next line[/dim]")


async def _cmd_self_heal(console):
    """Manually trigger self-healing on recent execution errors."""
    from rich.panel import Panel
    from src.self_heal import parse_error_from_logs, attempt_repair, get_repair_history
    from src.evolution.engine import get_experience_store

    store = get_experience_store()
    stats = store.get_stats()

    # Try to find errors from recent experience records
    console.print("\n[yellow]🔧 Self-heal: scanning for errors...[/yellow]\n")

    # Check repair history first
    history = get_repair_history()
    if history:
        last = history[-1]
        console.print(Panel(
            f"Last repair: {last.get('data', {}).get('file', '?')}:{last.get('data', {}).get('line', '?')}\n"
            f"Error: {last.get('data', {}).get('error', '?')}\n"
            f"Result: {'✅ Fixed' if last.get('data', {}).get('result') else '❌ Failed'}",
            title="Previous Repair",
        ))
        return

    # Try to find error from experience records
    records = store._records[-5:] if hasattr(store, '_records') else []
    for record in reversed(records):
        logs = getattr(record, 'execution_log', []) or []
        error_info = parse_error_from_logs(logs)
        if error_info:
            console.print(f"Found error: {error_info['error_type']} in {error_info.get('file', '?')}:{error_info.get('line', '?')}")
            console.print("[yellow]Attempting repair...[/yellow]")
            result = await attempt_repair(error_info, "Manual /fix command")
            if result.get("fixed"):
                console.print(Panel(
                    f"✅ Fixed {error_info['error_type']}\n"
                    f"File: {error_info['file']}\n"
                    f"Changes: {result.get('changes', '')[:500]}",
                    title="Self-Heal Result",
                    border_style="green",
                ))
            else:
                console.print(f"[red]❌ Could not fix: {result.get('error', 'Unknown')}[/red]")
            return

    console.print("[dim]No errors found in recent records.[/dim]")


async def _cmd_diag(console):
    """Diagnostic: test web_search, web_fetch, and URL knowledge base."""
    from rich.panel import Panel
    from rich.table import Table

    console.print("\n[yellow]🔍 Running diagnostics...[/yellow]\n")

    # Test 1: web_search
    console.print("[bold]1. web_search test:[/bold]")
    from src.execution._web_tool import web_search
    result = web_search("test query", max_results=2)
    ok = "SEARCH" not in result and "NETWORK" not in result
    console.print(f"   {'✅ 可用' if ok else '❌ 不可用'}: {result[:100]}")

    # Test 2: web_fetch
    console.print("\n[bold]2. web_fetch test (Wikipedia):[/bold]")
    from src.execution._web_tool import web_fetch
    result2 = web_fetch("https://en.wikipedia.org/wiki/Test", max_chars=200)
    ok2 = "HTTP" not in result2 and "ERROR" not in result2 and "Network error" not in result2
    console.print(f"   {'✅ 可用' if ok2 else '❌ 不可用'}")

    # Test 3: URL KB
    console.print("\n[bold]3. URL Knowledge Base:[/bold]")
    from src.execution.url_kb import find_urls
    tests = ["苹果最近一个月股价", "最近金价", "美元人民币汇率"]
    for t in tests:
        urls = find_urls(t)
        status = f"✅ {len(urls)} URLs" if urls else "❌ 无匹配"
        console.print(f"   '{t}' → {status}")
        for u in urls:
            console.print(f"     {u}")

    console.print()


def _show_status(console, plat, roles, store):
    """Show system status summary."""
    from rich.panel import Panel
    exec_count = sum(1 for r in roles if r.category == "execution")
    ctrl_count = sum(1 for r in roles if r.category == "control")
    stats = store.get_stats()
    lines = [
        f"Platform: {plat.system} {plat.arch} · Python {plat.python_version}",
        f"Agents:   {ctrl_count} control + {exec_count} execution",
        f"Readline: {'GNU' if _readline_ok else 'libedit'}",
        f"Ruff:     {'✓' if plat.has_ruff else '✗ (code lint disabled)'}",
        f"Docker:   {'✓' if plat.has_docker else '✗ (sandbox disabled)'}",
        f"Git:      {'✓' if plat.has_git else '✗'}",
    ]
    if stats.get("total_tasks", 0) > 0:
        lines.extend([
            f"",
            f"Tasks:    {stats['total_tasks']} completed",
            f"Avg:      {stats.get('avg_score', '—')}/100",
            f"Best:     {stats.get('best_score', '—')}/100",
            f"Force OK: {stats.get('force_approves', 0)}",
        ])
    console.print(Panel("\n".join(lines), title="Status", border_style="green"))


def _show_roles(console, roles):
    """Show all registered roles."""
    from rich.table import Table
    table = Table(title="Agents")
    table.add_column("Role", style="cyan")
    table.add_column("Layer", style="dim")
    table.add_column("Description")
    table.add_column("Status")
    for r in roles:
        layer = "CTRL" if r.category == "control" else "EXEC"
        status = r.status if hasattr(r, 'status') else "core"
        table.add_row(r.display_name, layer, r.description[:60], status)
    console.print(table)


def _show_token_stats(console):
    """Show token usage statistics."""
    from src.utils.token_tracker import get_token_tracker
    tracker = get_token_tracker()
    summary = tracker.get_summary_text()
    from rich.panel import Panel
    console.print(Panel(summary, title="Token Usage", border_style="yellow"))


async def _cmd_session(console, user_input, session_mgr, current_session):
    """Handle session commands: /sessions, /session new|switch|rename|delete"""
    from rich.table import Table
    from src.session.memory import get_session_memory
    parts = user_input.strip().split(maxsplit=2)
    cmd = parts[0].lower()

    if cmd == "/sessions":
        sessions = session_mgr.list_all()
        if not sessions:
            console.print("[dim]No sessions.[/dim]")
            return
        table = Table(title="Sessions")
        table.add_column("Name", style="cyan")
        table.add_column("ID", style="dim")
        table.add_column("Msgs")
        table.add_column("Mems")
        table.add_column("Active")
        for s in sessions:
            mem = get_session_memory(s.id)
            mem_count = len(mem.all())
            active = "●" if s.is_active else "○"
            table.add_row(s.name, s.id, str(s.message_count), str(mem_count), active)
        console.print(table)
        return

    if len(parts) >= 2 and parts[1].lower() == "new":
        name = parts[2] if len(parts) > 2 else "untitled"
        new_session = session_mgr.create(name)
        console.print(f"[green]✅ Created: {new_session.name} ({new_session.id})[/green]")
        return

    if len(parts) >= 2 and parts[1].lower() == "switch":
        if len(parts) < 3:
            console.print("[yellow]Usage: /session switch <name|id>[/yellow]")
            return
        # Auto-summarize current
        if current_session:
            from src.session import auto_summarize_conversation
            try:
                await auto_summarize_conversation(current_session.id, llm=None, force=True)
            except Exception:
                pass
        switched = session_mgr.switch(parts[2])
        if switched:
            console.print(f"[green]✅ Switched to: {switched.name}[/green]")
        else:
            console.print(f"[red]Not found: {parts[2]}[/red]")
        return

    if len(parts) >= 2 and parts[1].lower() == "rename":
        if len(parts) < 3 or not current_session:
            console.print("[yellow]Usage: /session rename <new_name>[/yellow]")
            return
        current_session.name = parts[2]
        session_mgr._save_metadata(current_session)
        console.print(f"[green]✅ Renamed: {current_session.name}[/green]")
        return

    if len(parts) >= 2 and parts[1].lower() == "delete":
        if len(parts) < 3:
            console.print("[yellow]Usage: /session delete <name|id>[/yellow]")
            return
        deleted = session_mgr.delete(parts[2])
        if deleted:
            console.print(f"[green]✅ Deleted: {parts[2]}[/green]")
        else:
            console.print(f"[red]Cannot delete current: {parts[2]}[/red]")
        return

    console.print("[yellow]Try: /sessions, /session new|switch|rename|delete[/yellow]")


def _cmd_global(console, user_input):
    """Handle global memory: /global set|get|list"""
    from src.session.memory import get_global_memory
    from rich.table import Table
    gm = get_global_memory()
    parts = user_input.strip().split(maxsplit=3)

    if len(parts) == 1 or (len(parts) >= 2 and parts[1].lower() == "list"):
        all_items = gm.all()
        if not all_items:
            console.print("[dim]No global memories.[/dim]")
            return
        table = Table(title="Global Memories")
        table.add_column("Key", style="cyan")
        table.add_column("Value")
        for k, v in all_items.items():
            table.add_row(k, str(v)[:200])
        console.print(table)
        return

    if len(parts) >= 2 and parts[1].lower() == "set":
        if len(parts) < 4:
            console.print("[yellow]Usage: /global set <key> <value>[/yellow]")
            return
        gm.set(parts[2], parts[3])
        console.print(f"[green]✅ Global: {parts[2]} = {parts[3]}[/green]")
        return

    if len(parts) >= 2 and parts[1].lower() == "get":
        if len(parts) < 3:
            console.print("[yellow]Usage: /global get <key>[/yellow]")
            return
        value = gm.get(parts[2])
        console.print(f"[cyan]{parts[2]}[/cyan] = {value}" if value else f"[dim]Not found: {parts[2]}[/dim]")
        return

    console.print("[yellow]Usage: /global set|get|list[/yellow]")


def _cmd_vision(console, user_input, current_session):
    """Handle visual context commands: /vision scan|status|on|off"""
    from rich.panel import Panel
    from rich.table import Table
    parts = user_input.strip().split(maxsplit=2)
    sub = parts[1].lower() if len(parts) >= 2 else "status"
    sid = current_session.id if current_session else ""

    if sub in ("scan", "s"):
        console.print("[yellow]📸 Capturing screen...[/yellow]")
        try:
            from src.vision import get_visual_engine, ScreenCapture, EfficiencyRouter
            router = EfficiencyRouter()
            app_info = router.get_active_app()
            cap = ScreenCapture()
            ss = cap.capture()
            if ss:
                console.print(f"   Screen: {ss.width}x{ss.height} ({ss.platform})")
                console.print(f"   Active: [cyan]{app_info.get('app', '?')}[/cyan] — {app_info.get('title', '(no title)')}")

                from src.vision.analyzer import VisionAnalyzer
                analyzer = VisionAnalyzer(model_name="qwen-vl")
                if analyzer.is_available:
                    console.print("   [dim]Analyzing with vision model...[/dim]")
                    analysis = analyzer.analyze(ss.image)
                    if analysis:
                        console.print(f"   Activity: {analysis.get('activity', '?')}")
                        console.print(f"   Topic: {analysis.get('topic', '') or '—'}")
                        console.print(f"   Summary: {analysis.get('summary', '—')}")
                        apps = analysis.get("apps", [])
                        if apps:
                            console.print(f"   Apps: {', '.join(a['name'] if isinstance(a,dict) else str(a) for a in apps[:5])}")
                    else:
                        console.print("   [dim]Vision API not configured. Set OPENAI_API_KEY or ANTHROPIC_API_KEY[/dim]")
                else:
                    console.print("   [dim]No vision model available. Set API key for GPT-4o or Claude.[/dim]")
                    console.print("   [dim]Models supported: gpt-4o, claude-3.5, gemini, qwen-vl[/dim]")
                    console.print("   [dim]DeepSeek: vision not yet supported (placeholder ready)[/dim]")
            else:
                console.print("[red]Screen capture failed[/red]")
        except Exception as e:
            console.print(f"[red]Vision scan error: {e}[/red]")

    elif sub in ("status", "st"):
        try:
            from src.vision import get_visual_engine
            engine = get_visual_engine(sid)
            status = engine.get_status()

            lines = [
                f"Auto mode: {'🟢 ON' if status['auto_mode'] else '⚫ OFF'}",
                f"Scans: {status['scan_count']}",
                f"Interval: {status['interval']}s | Threshold: {status['threshold']}",
                f"Model: {status['model']} ({'✅' if status['model_available'] else '❌ not configured'})",
            ]
            if status["current_activity"]:
                a = status["current_activity"]
                lines.append(f"\nCurrent: [cyan]{a['active_app']}[/cyan] — {a['activity']}")
                if a.get("topic"):
                    lines.append(f"Topic: {a['topic']}")

            if status["timeline"]:
                lines.append("\nRecent timeline:")
                for t in status["timeline"][-5:]:
                    lines.append(f"  {t['time']} {t['active_app']}: {t['activity']}")

            console.print(Panel("\n".join(lines), title="Visual Context Engine", border_style="cyan"))
        except Exception as e:
            console.print(f"[red]Vision status error: {e}[/red]")

    elif sub in ("on", "start"):
        try:
            from src.vision import get_visual_engine
            engine = get_visual_engine(sid)
            engine.start_auto()
            console.print("[green]✅ VCE auto mode started[/green]")
        except Exception as e:
            console.print(f"[red]Failed: {e}[/red]")

    elif sub in ("off", "stop"):
        try:
            from src.vision import get_visual_engine
            engine = get_visual_engine(sid)
            engine.stop_auto()
            console.print("[yellow]⏹ VCE auto mode stopped[/yellow]")
        except Exception as e:
            console.print(f"[red]Failed: {e}[/red]")

    else:
        console.print("[yellow]/vision scan|status|on|off[/yellow]")


# ─── Entry Point ────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AI Company - Multi-Agent System")
    parser.add_argument(
        "--mode", choices=["telegram", "cli"], default="cli",
        help="Run mode (default: cli)"
    )
    parser.add_argument(
        "--query", "-q", type=str,
        help="One-shot query (cli mode only)"
    )
    parser.add_argument(
        "--evolve", action="store_true",
        help="Show auto-evolution status and insights"
    )
    parser.add_argument(
        "--evolve-run", action="store_true",
        help="Run auto-evolution analysis cycle"
    )
    parser.add_argument(
        "--memory", action="store_true",
        help="Show memory health status"
    )
    parser.add_argument(
        "--memory-compact", action="store_true",
        help="Compact episode memory (summarize old episodes)"
    )
    parser.add_argument(
        "--memory-clear", action="store_true",
        help="Clear all episode memory"
    )
    args = parser.parse_args()
    
    if args.memory or args.memory_compact or args.memory_clear:
        asyncio.run(show_memory(
            do_compact=args.memory_compact,
            do_clear=args.memory_clear,
        ))
        return

    if args.evolve or args.evolve_run:
        asyncio.run(show_evolution(run_analysis=args.evolve_run))
        return
    
    if args.mode == "cli":
        if args.query:
            async def one_shot():
                from src.ceo.graph import run_ceo
                result = await run_ceo(args.query)
                print(f"\n{'='*50}")
                print(f"Phase: {result.get('phase')}")
                print(f"Score: {result.get('score_card', {}).get('score', 'N/A')}/100")
                logs = result.get('execution_log', [])
                if logs:
                    print(f"\nExecution:")
                    for log in logs[-8:]:
                        print(f"  {log}")
                output = result.get('final_output', '')
                if output:
                    print(f"\n{'─'*50}")
                    print(output[:3000])
                print(f"{'='*50}")
            
            asyncio.run(one_shot())
        else:
            asyncio.run(run_cli())
    else:
        # Telegram mode
        if not config.telegram_bot_token:
            logger.error(
                "TELEGRAM_BOT_TOKEN not set! "
                "Create a bot at @BotFather and add the token to .env"
            )
            sys.exit(1)
        asyncio.run(start_bot())


if __name__ == "__main__":
    main()
