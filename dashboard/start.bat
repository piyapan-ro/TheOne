@echo off
chcp 65001 >nul
cd /d "%~dp0backend"
if not exist "..\.venv\Scripts\python.exe" (
  echo Please run install.bat first.
  pause
  exit /b
)
start "" /min cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8000"
"..\.venv\Scripts\python.exe" main.py
pause
