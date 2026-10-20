@echo off
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m jarvis.cli.hud --open
if errorlevel 1 pause
