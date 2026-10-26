@echo off
chcp 65001 >nul
cd /d "%~dp0backend"
if not exist "..\.venv\Scripts\python.exe" (
  echo Please run install.bat first.
  pause
  exit /b
)
if not exist "..\data" mkdir "..\data"
start "" /min cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8000"
title AI Trader Dashboard
echo AI Trader Dashboard  ->  http://127.0.0.1:8000
echo Output goes to data\dashboard.log. Close this window to stop.
:loop
echo [%date% %time%] starting dashboard >> "..\data\dashboard.log"
"..\.venv\Scripts\python.exe" main.py >> "..\data\dashboard.log" 2>&1
echo [%date% %time%] dashboard stopped (exit %errorlevel%) - restarting in 5 s >> "..\data\dashboard.log"
echo [%date% %time%] dashboard stopped (exit %errorlevel%) - restarting in 5 seconds...
timeout /t 5 >nul
goto loop
