@echo off
rem One-time setup: creates .venv, installs dependencies, creates .env.
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    python --version >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo Python 3.9+ is not installed.
    echo Install it with:  winget install -e --id Python.Python.3.12
    echo or from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), then run setup.bat again.
    pause
    exit /b 1
)

if not exist .venv (
    echo Creating virtual environment...
    %PY% -m venv .venv || goto :fail
)

echo Installing dependencies...
.venv\Scripts\python.exe -m pip install --upgrade pip >nul
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt || goto :fail

if not exist .env (
    copy .env.example .env >nul
    echo.
    echo Created .env - opening it now. Fill in the 4 required values, save, then run check.bat.
    start notepad .env
) else (
    echo.
    echo Setup done. .env already exists - run check.bat to verify it.
)
pause
exit /b 0

:fail
echo.
echo Setup failed - see the error above.
pause
exit /b 1
