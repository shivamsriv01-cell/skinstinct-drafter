@echo off
rem Verifies .env: Supabase connection + tables, Telegram token, Gemini key.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Run setup.bat first.
    pause
    exit /b 1
)
.venv\Scripts\python.exe -m app check
pause
