@echo off
echo ========================================
echo   Polymarket Bots - Quick Start
echo ========================================
echo.

REM Check if .env exists
if not exist "config\.env" (
    echo [ERROR] config\.env not found!
    echo.
    echo Please:
    echo 1. Copy config\.env.example to config\.env
    echo 2. Fill in your API keys
    echo.
    pause
    exit /b 1
)

echo [1/3] Installing requirements...
pip install -r requirements.txt

echo.
echo [2/3] Testing connection...
python test_connection.py

echo.
echo [3/3] Ready to go!
echo.
echo To run a strategy:
echo   - Extreme Price: python -m strategies.extreme_price.strategy
echo   - Arbitrage:     python -m strategies.arbitrage.strategy
echo.
pause
