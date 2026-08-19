@echo off
REM Builds dist\WeldAudit.exe — one file, no installer, no admin rights.
REM
REM For whoever maintains WeldAudit, not for the auditors who run it. They get
REM the exe on its own; this is what produces it.
setlocal

set VENV=%USERPROFILE%\.weldaudit\venv
set PY=%VENV%\Scripts\python.exe

if not exist "%PY%" (
  echo WeldAudit is not set up on this machine yet.
  echo Run Install.bat first.
  pause
  exit /b 1
)

cd /d "%~dp0"

echo Checking the build tool is present.
"%PY%" -m pip install --quiet --upgrade pyinstaller || goto :fail

REM A stale build directory is the usual reason a fix does not appear in the
REM exe, so it goes rather than being reused.
if exist build rmdir /s /q build

echo.
echo Building. This takes a few minutes.
"%PY%" -m PyInstaller WeldAudit.spec --noconfirm --clean || goto :fail

echo.
if not exist "dist\WeldAudit.exe" (
  echo Build reported success but produced no exe. Copy the messages above.
  pause
  exit /b 1
)
for %%F in ("dist\WeldAudit.exe") do set SIZE=%%~zF
set /a MB=%SIZE% / 1048576
echo Built dist\WeldAudit.exe  (%MB% MB)
echo.
echo Give that one file to an auditor. Nothing else needs installing:
echo   double-click it            opens the WeldAudit window
echo   WeldAudit.exe audit "D:\Jobs\Kestrel 8"    runs an audit from a script
pause
exit /b 0

:fail
echo.
echo Build failed. Copy the messages above when asking for help.
pause
exit /b 1
