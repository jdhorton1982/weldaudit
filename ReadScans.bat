@echo off
REM Reads the scanned pages of an already-audited job — the ones no text layer
REM covers — and folds what they say back into the audit.
REM
REM   ReadScans.bat "Kestrel 8"
REM   ReadScans.bat "Kestrel 8" claude-haiku-4-5
setlocal enabledelayedexpansion

set VENV=%USERPROFILE%\.weldaudit\venv
set PY=%VENV%\Scripts\python.exe
set PROJECT=%~1
set MODEL=%~2

if not exist "%PY%" (
  echo WeldAudit is not set up on this machine yet.
  echo Run Install.bat first.
  pause
  exit /b 1
)

cd /d "%~dp0"

if "%PROJECT%"=="" (
  echo Jobs already audited on this machine:
  echo.
  "%PY%" -m weldaudit projects
  echo.
  set /p PROJECT="Which job? (type the name exactly) "
)
if "%PROJECT%"=="" (
  echo No job given, nothing to do.
  pause
  exit /b 1
)

REM The audit command takes the job's folder, so fetch the one already stored.
REM Via a file rather than `for /f`: with both the exe path and the job name
REM quoted, cmd splits the command at the first space in the job name and the
REM failure reads as a missing project rather than a quoting bug.
set ROOT=
set ROOTFILE=%TEMP%\weldaudit-root.txt
"%PY%" -m weldaudit projects --root "%PROJECT%" > "%ROOTFILE%" 2>nul
if exist "%ROOTFILE%" set /p ROOT=<"%ROOTFILE%"
del "%ROOTFILE%" >nul 2>&1
if "%ROOT%"=="" (
  echo No audited job called "%PROJECT%".
  echo Audit it first, then run this to read its scans.
  pause
  exit /b 1
)

if "%MODEL%"=="" set MODEL=local:qwen2.5vl:7b

REM Reading on this machine costs nothing, so it needs no per-kind approval.
REM A paid model does: the CLI asks before each one, and that is deliberate.
set CONFIRM=
echo %MODEL% | findstr /b /c:"local:" >nul
if not errorlevel 1 (
  set CONFIRM=--yes
  REM Ask the daemon, not the PATH: Ollama runs as a service, and a prompt
  REM opened before it was installed will not see the exe regardless.
  "%PY%" -c "import sys;from weldaudit.vision import local_available;ok,why=local_available();print('' if ok else why);sys.exit(0 if ok else 1)"
  if errorlevel 1 (
    echo.
    echo Install Ollama with:  winget install Ollama.Ollama
    echo Then in a NEW command prompt:  ollama pull qwen2.5vl:7b
    pause
    exit /b 1
  )
)

REM Weld reports and maps first: they create the welds the other rules hang
REM off, so reading them first means one pass rather than two.
set KINDS=daily_weld_report weld_map reader_sheet welder_cert mtr hydrotest coating backfill

echo.
echo === What this would read, before anything is sent ===
echo.
for %%K in (%KINDS%) do (
  "%PY%" -m weldaudit vision "%PROJECT%" --kind %%K --model %MODEL% --dry-run
)

echo.
set /p GO="Read these now? (y/N) "
if /i not "%GO%"=="y" (
  echo Nothing was sent.
  pause
  exit /b 0
)

for %%K in (%KINDS%) do (
  echo.
  echo === %%K ===
  "%PY%" -m weldaudit vision "%PROJECT%" --kind %%K --model %MODEL% %CONFIRM%
)

echo.
echo Re-auditing so the findings pick up what was read.
"%PY%" -m weldaudit audit "%ROOT%" --name "%PROJECT%" --top 20

echo.
pause
