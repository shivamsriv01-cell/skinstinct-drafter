@echo off
rem Starts the bot. Keep this window open - closing it stops the bot.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Run setup.bat first.
    pause
    exit /b 1
)
.venv\Scripts\python.exe -m app.main
pause
