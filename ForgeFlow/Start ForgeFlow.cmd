@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment is missing. Follow README.md to install.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\manage.py start
if errorlevel 1 pause
