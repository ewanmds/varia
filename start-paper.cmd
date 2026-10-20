@echo off
cd /d "%~dp0"
call tools\ensure_windows_env.cmd
if errorlevel 1 (
  pause
  exit /b 1
)
echo Simulation paire petrole : aucun ordre reel.
".venv\Scripts\python.exe" main.py --paper
pause
