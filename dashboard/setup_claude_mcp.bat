@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run install.bat first.
  pause
  exit /b
)
".venv\Scripts\python.exe" setup_claude_mcp.py
pause
