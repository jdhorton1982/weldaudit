@echo off
REM Builds WeldAudit — no installer, no admin rights, two shapes.
REM
REM   dist\WeldAudit.exe      one file    easiest to hand over: copy and run
REM   dist\WeldAudit\         one folder  starts in ~3s instead of ~30s, and
REM                                       does not look like a packer to
REM                                       antivirus
REM
REM Both contain the same program; both read their contents from
REM build_config.py so they cannot drift apart.
REM
REM   Build.bat            builds both
REM   Build.bat file       just the one-file exe
REM   Build.bat folder     just the folder
REM
REM For whoever maintains WeldAudit, not for the auditors who run it.
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

set WHAT=%1
if "%WHAT%"=="" set WHAT=both

echo Checking the build tool is present.
"%PY%" -m pip install --quiet --upgrade pyinstaller || goto :fail

REM A stale build directory is the usual reason a fix does not appear, so it
REM goes rather than being reused.
if exist build rmdir /s /q build

if "%WHAT%"=="folder" goto :folder

echo.
echo Building the one-file exe. This takes a few minutes.
"%PY%" -m PyInstaller WeldAudit.spec --noconfirm --clean || goto :fail
if not exist "dist\WeldAudit.exe" (
  echo Build reported success but produced no exe. Copy the messages above.
  pause
  exit /b 1
)
for %%F in ("dist\WeldAudit.exe") do set SIZE=%%~zF
set /a MB=%SIZE% / 1048576
echo Built dist\WeldAudit.exe  (%MB% MB)

if "%WHAT%"=="file" goto :done

:folder
echo.
echo Building the folder version. This takes a few minutes.
"%PY%" -m PyInstaller WeldAudit-folder.spec --noconfirm --clean || goto :fail
if not exist "dist\WeldAudit\WeldAudit.exe" (
  echo Build reported success but produced no folder. Copy the messages above.
  pause
  exit /b 1
)
echo Built dist\WeldAudit\  (folder)

:done
echo.
echo Which to hand over:
echo   dist\WeldAudit.exe   one file to copy. Slow to start — it unpacks
echo                        itself into %%TEMP%% on every launch — and that
echo                        self-extracting behaviour is what antivirus
echo                        heuristics read as a packer.
echo   dist\WeldAudit\      copy the whole folder. Starts in a couple of
echo                        seconds and extracts nothing. Prefer this where
echo                        antivirus has objected, or where the wait grates.
echo.
echo Either way: no installer, no administrator rights, and nothing written
echo outside the user's own profile.
pause
exit /b 0

:fail
echo.
echo Build failed. Copy the messages above when asking for help.
pause
exit /b 1
