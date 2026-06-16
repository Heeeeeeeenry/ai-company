#!/usr/bin/env python3
"""Security Audit Verification Test Suite

Runs automated checks against the src/ directory to verify
the security audit findings. Must be run from the ai-company root.
"""
import ast
import os
import re
import sys
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent / "src"
passed = 0
failed = 0
warnings = 0

def test(label, condition, severity="error"):
    global passed, failed, warnings
    if condition:
        passed += 1
        print(f"  ✅ {label}")
    elif severity == "warning":
        warnings += 1
        print(f"  ⚠️  {label}")
    else:
        failed += 1
        print(f"  ❌ {label}")

def check_file(path):
    """Check a single Python source file for issues."""
    with open(path) as f:
        content = f.read()
        lines = content.split('\n')
    return content, lines

def main():
    global passed, failed, warnings
    print("=" * 60)
    print("AI-Company Security Audit Verification")
    print("=" * 60)
    
    py_files = list(SRC_DIR.rglob("*.py"))
    print(f"\nAnalyzing {len(py_files)} Python source files...\n")
    
    # ── Test 1: No bare excepts ──
    print("── Test 1: Bare Except Detection ──")
    bare_excepts = []
    for f in py_files:
        content, lines = check_file(f)
        for i, line in enumerate(lines, 1):
            if re.match(r'^\s*except\s*:', line):
                bare_excepts.append(f"{f.relative_to(SRC_DIR.parent)}:{i}")
    test("No bare except clauses", len(bare_excepts) == 0)
    if bare_excepts:
        for be in bare_excepts:
            print(f"    Found: {be}")
    
    # ── Test 2: No hardcoded secrets ──
    print("\n── Test 2: Hardcoded Secrets ──")
    secret_patterns = [
        r'(?:api_key|secret|token|password)\s*=\s*["\'][A-Za-z0-9_\-=]{16,}["\']',
        r'Bearer\s+[A-Za-z0-9_\-\.]{20,}',
    ]
    secrets = []
    for f in py_files:
        content, lines = check_file(f)
        for i, line in enumerate(lines, 1):
            for pat in secret_patterns:
                if re.search(pat, line, re.IGNORECASE):
                    secrets.append(f"{f.relative_to(SRC_DIR.parent)}:{i}: {line.strip()[:80]}")
    test("No hardcoded API keys/secrets", len(secrets) == 0)
    if secrets:
        for s in secrets:
            print(f"    Found: {s}")
    
    # ── Test 3: API keys via environment variables ──
    print("\n── Test 3: API Key Management ──")
    config_file = SRC_DIR / "config.py"
    if config_file.exists():
        content = config_file.read_text()
        env_gets = len(re.findall(r'os\.getenv\(["\']?\w*API_KEY', content))
        test(f"API keys managed via os.getenv() ({env_gets} found)", env_gets >= 3)
    else:
        test("config.py exists", False)
    
    # ── Test 4: Command injection patterns ──
    print("\n── Test 4: Command Injection Risk ──")
    exec_file = SRC_DIR / "execution" / "executor.py"
    if exec_file.exists():
        content = exec_file.read_text()
        shell_execs = content.count("create_subprocess_shell")
        docker_fstring = "docker run --rm" in content
        test(f"create_subprocess_shell usage ({shell_execs} found)", 
             shell_execs <= 1, severity="warning")
        test("Docker command uses safe subprocess_exec", 
             not docker_fstring, severity="warning")
    
    # ── Test 5: Type annotation coverage ──
    print("\n── Test 5: Type Annotation Coverage ──")
    total_funcs = 0
    annotated_funcs = 0
    uncovered_files = []
    for f in py_files:
        try:
            tree = ast.parse(f.read_text())
            funcs = [n for n in ast.walk(tree) 
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            annotated = sum(1 for func in funcs 
                          if func.returns or any(a.annotation for a in func.args.args))
            total_funcs += len(funcs)
            annotated_funcs += annotated
            if len(funcs) > 0 and annotated == 0:
                uncovered_files.append(f.relative_to(SRC_DIR.parent))
        except SyntaxError:
            pass
    
    coverage = annotated_funcs / total_funcs * 100 if total_funcs else 0
    test(f"Type annotation coverage: {annotated_funcs}/{total_funcs} ({coverage:.0f}%)", 
         coverage >= 70)
    if uncovered_files:
        print(f"    Files with 0% coverage: {', '.join(str(f) for f in uncovered_files)}")
    
    # ── Test 6: ainvoke timeout ──
    print("\n── Test 6: LLM API Call Timeouts ──")
    ainvoke_total = 0
    ainvoke_with_timeout = 0
    for f in py_files:
        content = f.read_text()
        # Count ainvoke calls
        calls = re.findall(r'\.ainvoke\(', content)
        ainvoke_total += len(calls)
    test(f"ainvoke() calls with timeout ({ainvoke_total} total)", 
         ainvoke_total == 0 or ainvoke_with_timeout > 0, severity="warning")
    
    # ── Test 7: Dead code - operation dimension ──
    print("\n── Test 7: Dead Code Detection ──")
    auditor_file = SRC_DIR / "verification" / "auditor.py"
    graph_file = SRC_DIR / "ceo" / "graph.py"
    
    if auditor_file.exists() and graph_file.exists():
        auditor = auditor_file.read_text()
        graph = graph_file.read_text()
        
        has_operation_dim = '"operation"' in auditor
        op_in_triage = 'operation' in graph.lower()
        test("'operation' scoring dimension is reachable", 
             not has_operation_dim or op_in_triage, severity="warning")
        if has_operation_dim and not op_in_triage:
            print("    WARNING: 'operation' dimension defined but never routed to")
    
    # ── Test 8: Unused dependencies ──
    print("\n── Test 8: Unused Dependencies ──")
    req_file = SRC_DIR.parent / "requirements.txt"
    if req_file.exists():
        all_src = ""
        for f in py_files:
            all_src += f.read_text().lower()
        
        unused = []
        for dep in ["pydantic", "structlog", "tenacity"]:
            if dep not in all_src:
                unused.append(dep)
        test(f"Unused dependencies: {len(unused)} (pydantic/structlog/tenacity)", 
             len(unused) == 0, severity="warning")
        if unused:
            print(f"    Unused: {', '.join(unused)}")
    
    # ── Test 9: Error message leak ──
    print("\n── Test 9: Error Information Leakage ──")
    bot_file = SRC_DIR / "telegram_bot.py"
    if bot_file.exists():
        content = bot_file.read_text()
        # Check if str(e) is sent to user
        leak_patterns = re.findall(r'str\(e\)', content)
        test(f"Exception details exposed to user ({len(leak_patterns)} occurrences)", 
             len(leak_patterns) == 0, severity="warning")
        for lp in leak_patterns:
            print(f"    str(e) found in error response")
    
    # ── Test 10: Resource cleanup ──
    print("\n── Test 10: Resource Cleanup ──")
    
    # Check MCPClient shutdown is called
    all_shutdown = 0
    for f in py_files:
        content = f.read_text()
        if "shutdown" in content and "mcp" in content.lower():
            all_shutdown += 1
    test("MCPClient.shutdown() is called somewhere", all_shutdown > 1, severity="warning")
    
    # Check finally blocks
    finally_count = 0
    for f in py_files:
        content = f.read_text()
        finally_count += content.count("finally:")
    test(f"Resource cleanup (finally blocks): {finally_count}", finally_count >= 1)
    
    # ── Summary ──
    print("\n" + "=" * 60)
    total = passed + failed + warnings
    print(f"RESULTS: {passed} passed, {failed} failed, {warnings} warnings")
    print(f"SCORE: {passed}/{total} ({passed/total*100:.0f}%)")
    print("=" * 60)
    
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
