@echo off
rem Called from the project root by both launchers.
if not exist ".venv\Scripts\python.exe" goto find_python
".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info[:2] != (3, 12))" >nul 2>&1
if errorlevel 1 goto find_python
".venv\Scripts\python.exe" tools\bootstrap_windows.py
exit /b %errorlevel%

:find_python
py -3.12 -c "import sys; sys.exit(sys.version_info[:2] != (3, 12))" >nul 2>&1
if errorlevel 1 goto try_python
py -3.12 tools\bootstrap_windows.py
exit /b %errorlevel%

:try_python
python -c "import sys; sys.exit(sys.version_info[:2] != (3, 12))" >nul 2>&1
if errorlevel 1 goto missing_python
python tools\bootstrap_windows.py
exit /b %errorlevel%

:missing_python
echo Python 3.12 introuvable. Installez Python 3.12 pour Windows :
echo https://www.python.org/downloads/windows/
echo Cochez Add python.exe to PATH, puis relancez ce fichier .cmd.
exit /b 1
