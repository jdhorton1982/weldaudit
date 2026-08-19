@echo off
REM One-time setup. Creates a private Python environment outside OneDrive so
REM thousands of small library files are never synced.
setlocal

set VENV=%USERPROFILE%\.weldaudit\venv

where python >nul 2>&1
if errorlevel 1 (
  echo Python was not found on this machine.
  echo Install Python 3.11 or newer from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup, then run this again.
  pause
  exit /b 1
)

echo Creating the WeldAudit environment in %VENV%
python -m venv "%VENV%" || goto :fail
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip --quiet
"%VENV%\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt" || goto :fail

echo.
echo Setup complete. Start WeldAudit with WeldAudit.bat
pause
exit /b 0

:fail
echo.
echo Setup failed. Copy the messages above when asking for help.
pause
exit /b 1
