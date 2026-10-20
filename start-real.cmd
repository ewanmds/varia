@echo off
cd /d "%~dp0"
call tools\ensure_windows_env.cmd
if errorlevel 1 (
  pause
  exit /b 1
)
echo MODE REEL : les ordres seront envoyes apres le bouton Demarrer.
echo Interface : http://127.0.0.1:8792
echo Garder l'onglet Omni ouvert dans votre Chrome habituel.
echo Charger browser_extension une seule fois dans chrome://extensions.
echo Ne pas demarrer la simulation en parallele.
".venv\Scripts\python.exe" tools\launch_local.py
pause
