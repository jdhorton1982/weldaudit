@echo off
REM Starts the local WeldAudit server and opens it in the default browser.
setlocal

set VENV=%USERPROFILE%\.weldaudit\venv
set PORT=8765

if not exist "%VENV%\Scripts\python.exe" (
  echo WeldAudit is not set up on this machine yet.
  echo Run Install.bat first.
  pause
  exit /b 1
)

cd /d "%~dp0"
start "" http://127.0.0.1:%PORT%/
"%VENV%\Scripts\python.exe" -m weldaudit serve --port %PORT%
