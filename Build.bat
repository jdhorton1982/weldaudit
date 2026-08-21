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
REM   Build.bat installer  the folder, then dist\WeldAudit-Setup.exe
REM
REM The installer is per-user: it lands in %LOCALAPPDATA%\Programs\WeldAudit,
REM raises no UAC prompt, and appears in Settings > Apps like any other
REM program. It needs Inno Setup 6 (winget install JRSoftware.InnoSetup).
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

REM The version, asked of the program rather than typed here, so the exe, the
REM installer and any release published from them cannot disagree.
for /f "delims=" %%V in ('"%PY%" -c "import weldaudit;print(weldaudit.__version__)"') do set VER=%%V
echo Building WeldAudit %VER%.

if "%WHAT%"=="installer" goto :folder
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
REM The release's own identity, travelling with the build. The updater reads
REM this back, so a copy always knows which release it is even if the number
REM compiled into it says otherwise.
echo %VER%> "dist\WeldAudit\weldaudit-version.txt"
echo Built dist\WeldAudit\  (folder, stamped %VER%)

if not "%WHAT%"=="installer" goto :done

set ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" set ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" (
  echo.
  echo Inno Setup 6 is not installed, so the installer cannot be built.
  echo   winget install --id JRSoftware.InnoSetup --scope user
  goto :fail
)
echo.
echo Building the installer.
"%ISCC%" /DAppVersion=%VER% "installer\WeldAudit.iss" || goto :fail
echo Built dist\WeldAudit-Setup.exe

:done
echo.
echo Which to hand over:
echo   dist\WeldAudit-Setup.exe  the ordinary way to give it to somebody. Per
echo                        user, so no administrator rights and no UAC
echo                        prompt; it makes the shortcuts and appears in
echo                        Settings ^> Apps. Removing it never touches the
echo                        audits and page readings in %%USERPROFILE%%\.weldaudit.
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
