@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/2] Creating virtual environment...
if not exist .venv python -m venv .venv
echo [2/2] Installing packages...
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
echo.
echo Done. Run start.bat to open the dashboard.
pause
