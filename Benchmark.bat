@echo off
REM Scores a model against the pages in this corpus that have been read by
REM hand, so you can see what it gets right before trusting it with an audit.
setlocal

set VENV=%USERPROFILE%\.weldaudit\venv
set MODEL=%1
set MAXEDGE=%2

if not exist "%VENV%\Scripts\python.exe" (
  echo WeldAudit is not set up on this machine yet.
  echo Run Install.bat first.
  pause
  exit /b 1
)

if "%MODEL%"=="" set MODEL=local:qwen2.5vl:7b

REM Local models turn the image into vision tokens at its native size, so a
REM full-resolution page can be four times the work and time out on a laptop
REM GPU. The hosted models are billed per image instead, and read better at
REM the larger size.
if "%MAXEDGE%"=="" (
  echo %MODEL% | findstr /b /c:"local:" >nul
  if errorlevel 1 (set MAXEDGE=2000) else (set MAXEDGE=1100)
)

echo Scoring %MODEL% at %MAXEDGE%px against the hand-checked pages.
echo.

cd /d "%~dp0"

REM Ask the daemon, not the PATH. Ollama runs as a background service, and a
REM command prompt opened before it was installed will not see the exe even
REM though the service is answering perfectly well.
echo %MODEL% | findstr /b /c:"local:" >nul
if not errorlevel 1 (
  "%VENV%\Scripts\python.exe" -c "import sys;from weldaudit.vision import local_available;ok,why=local_available();print('' if ok else why);sys.exit(0 if ok else 1)"
  if errorlevel 1 (
    echo.
    echo Install Ollama with:  winget install Ollama.Ollama
    echo Then in a NEW command prompt:  ollama pull qwen2.5vl:7b
    pause
    exit /b 1
  )
)
"%VENV%\Scripts\python.exe" -m eval.score --model %MODEL% --max-edge %MAXEDGE%

echo.
echo A blank critical field is a page to flag for a human reader.
echo A wrong one is a finding that quietly disappears.
pause
