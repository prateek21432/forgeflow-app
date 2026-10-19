@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" scripts\manage.py stop
if errorlevel 1 pause
