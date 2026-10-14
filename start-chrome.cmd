@echo off
cd /d "%~dp0"
set "BOT_CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%BOT_CHROME%" set "BOT_CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%BOT_CHROME%" (
  echo Google Chrome introuvable.
  pause
  exit /b 1
)
start "" "%BOT_CHROME%" https://omni.variational.io http://127.0.0.1:8792 chrome://extensions/
