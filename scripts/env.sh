#!/usr/bin/env bash
# ─── ai-company shared environment resolver ────────────────────────────
# Source this file from launcher scripts (goudan, goudan-api).
#
# Provides, after sourcing:
#   AI_COMPANY_DIR   — project root
#   PYTHON           — resolved absolute path to the interpreter to use
#   resolve_python() — idempotent helper
#
# Python resolution order:
#   1. $AI_COMPANY_PYTHON (explicit override, absolute or on PATH)
#   2. <project>/.venv/bin/python
#   3. `python3` on PATH
#   4. `python` on PATH
#
# IMPORTANT (why we resolve to an ABSOLUTE path):
#   When the resolved interpreter is a conda shim (e.g. `python3` ->
#   ~/conda/bin/python3), running it from a NON-interactive / background shell
#   often fails because conda's `activate` hook crashes on some setups
#   (`conda shell.posix activate base` → TypeError on macOS arm64), so the
#   active environment's site-packages (fastapi, uvicorn, ...) are NOT on
#   sys.path and you get "ModuleNotFoundError".  Resolving the shim to its real
#   absolute interpreter path (via `-c 'import sys;print(sys.executable)'`)
#   sidesteps the conda activation machinery entirely and always uses the
#   correct env.

set -e

# ─── 1. Locate the project ─────────────────────────────────────────────
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"   # parent of scripts/
AI_COMPANY_DIR=""

if [ -n "$AI_COMPANY_HOME" ] && [ -f "$AI_COMPANY_HOME/src/main.py" ]; then
    AI_COMPANY_DIR="$AI_COMPANY_HOME"
elif [ -f "$_SCRIPT_DIR/src/main.py" ]; then
    AI_COMPANY_DIR="$_SCRIPT_DIR"
else
    for candidate in \
        "$HOME/.openclaw/workspace/ai-company" \
        "$HOME/ai-company" \
        "$HOME/openclaw/workspace/ai-company" \
    ; do
        if [ -f "$candidate/src/main.py" ]; then
            AI_COMPANY_DIR="$candidate"
            break
        fi
    done
fi

if [ -z "$AI_COMPANY_DIR" ]; then
    echo "❌ Cannot find ai-company project. Set AI_COMPANY_HOME."
    exit 1
fi
export AI_COMPANY_HOME="$AI_COMPANY_DIR"

# ─── 2. Resolve Python to an absolute path ─────────────────────────────
_resolve_abs() {
    # $1 = interpreter name/path. Prints absolute path (or empty on failure).
    if [ -z "$1" ]; then
        echo ""
        return 1
    fi
    local interp="$1"
    if command -v "$interp" &>/dev/null; then
        # Resolve shim to real executable; fall back to command -v path.
        local real
        real="$("$interp" -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
        if [ -n "$real" ] && [ -x "$real" ]; then
            echo "$real"
            return 0
        fi
        command -v "$interp"
        return 0
    fi
    echo ""
    return 1
}

resolve_python() {
    PYTHON=""
    # 1. Explicit override
    if [ -n "$AI_COMPANY_PYTHON" ]; then
        PYTHON="$(_resolve_abs "$AI_COMPANY_PYTHON")"
    fi
    # 2. Project venv
    if [ -z "$PYTHON" ] && [ -x "$AI_COMPANY_DIR/.venv/bin/python" ]; then
        PYTHON="$AI_COMPANY_DIR/.venv/bin/python"
    fi
    # 3. Conda base python (project deps live here; reliable in non-interactive
    #    shells where conda activation may fail).  Located via $CONDA_PYTHON_EXE
    #    or by finding conda on PATH and using its real prefix python.
    if [ -z "$PYTHON" ]; then
        if [ -n "$CONDA_PYTHON_EXE" ] && [ -x "$CONDA_PYTHON_EXE" ]; then
            PYTHON="$CONDA_PYTHON_EXE"
        elif command -v conda &>/dev/null; then
            local cbase
            cbase="$(command -v conda)"
            cbase="$(cd "$(dirname "$cbase")/.." 2>/dev/null && pwd -P)"
            if [ -x "$cbase/bin/python3" ]; then
                PYTHON="$cbase/bin/python3"
            fi
        fi
    fi
    # 4. `python3` on PATH (fallback)
    if [ -z "$PYTHON" ]; then
        PYTHON="$(_resolve_abs "python3")"
    fi
    # 5. `python` on PATH
    if [ -z "$PYTHON" ]; then
        PYTHON="$(_resolve_abs "python")"
    fi

    if [ -z "$PYTHON" ]; then
        echo "❌ Python not found. Set AI_COMPANY_PYTHON or install Python 3.10+"
        exit 1
    fi
    export PYTHON
}
