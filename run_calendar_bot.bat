@echo off
REM Calendar Arbitrage Bot Launcher for Windows
REM Usage: run_calendar_bot.bat [--live] [--profit 0.02] [--scan 10]

setlocal enabledelayedexpansion

REM Check if .venv exists
if not exist ".venv\Scripts\activate.bat" (
    echo ❌ Virtual environment not found. Please run: python -m venv .venv
    pause
    exit /b 1
)

REM Activate virtual environment
call .venv\Scripts\activate.bat

REM Run the bot
echo.
echo ========================================
echo 🤖 Calendar Arbitrage Bot Launcher
echo ========================================
echo.

python run_calendar_bot.py %*

pause
