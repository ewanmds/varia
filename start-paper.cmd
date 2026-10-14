@echo off
cd /d "%~dp0"
echo Simulation paire petrole : aucun ordre reel.
".venv\Scripts\python.exe" main.py --paper
pause
