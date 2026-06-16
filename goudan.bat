@echo off
REM ─── 狗蛋儿 · AI Company Launcher (Windows) ───
REM
REM Usage:  goudan              # Interactive CLI
REM         goudan "write fib"  # One-shot query

setlocal

REM Find ai-company root
set AI_COMPANY_DIR=%~dp0

if not exist "%AI_COMPANY_DIR%src\main.py" (
    REM Try common locations
    if exist "%USERPROFILE%\.openclaw\workspace\ai-company\src\main.py" (
        set AI_COMPANY_DIR=%USERPROFILE%\.openclaw\workspace\ai-company\
    ) else if exist "%USERPROFILE%\ai-company\src\main.py" (
        set AI_COMPANY_DIR=%USERPROFILE%\ai-company\
    )
)

if not exist "%AI_COMPANY_DIR%src\main.py" (
    echo Cannot find ai-company project.
    echo Expected: %AI_COMPANY_DIR%src\main.py
    exit /b 1
)

set AI_COMPANY_HOME=%AI_COMPANY_DIR%

REM Find Python
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    where python3 >nul 2>&1
    if %ERRORLEVEL% neq 0 (
        echo Python not found. Install Python 3.10+
        exit /b 1
    )
    set PYTHON=python3
) else (
    set PYTHON=python
)

cd /d "%AI_COMPANY_DIR%"

if "%~1"=="" (
    %PYTHON% -m src.main --mode cli
) else (
    %PYTHON% -m src.main %*
)

endlocal
