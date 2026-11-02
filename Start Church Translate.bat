@echo off
setlocal EnableDelayedExpansion
title Church Translate (cloud)
cd /d "%~dp0"
echo === Aron - live captions and translation ===

REM Find a modern Python (>=3.9). Probe python.org install locations and PATH.
REM We avoid the `py` launcher because an Anaconda shim can hijack it.
set "PYEXE="
for %%P in ("%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" "C:\Program Files\Python313\python.exe" "C:\Program Files\Python312\python.exe" "C:\Program Files\Python311\python.exe") do @if not defined PYEXE if exist "%%~P" set "PYEXE=%%~P"
if not defined PYEXE (python -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1 && set "PYEXE=python")
if not defined PYEXE ( echo. & echo ERROR: No Python 3.9+ found on Windows. Install Python 3.12 from https://www.python.org/downloads/windows/ -- tick "Add python.exe to PATH" -- then re-run this file. & echo. & pause & exit /b 1 )
echo Using Python: %PYEXE%

REM First-run setup: with no .env, ask for the API keys and save them. The
REM Soniox key alone gives captions, translation and a voice; every other key is
REM optional -- press Enter to skip it and add it to .env later.
if not exist ".env" (
  echo.
  echo   First-time setup -- see QUICKSTART.pdf. Paste your API keys; they're saved to .env
  echo   ^(git-ignored, stays on this PC^). Re-running skips straight to start.
  echo.
  echo     Soniox key ^(required^):  https://console.soniox.com
  echo       -- on its own it gives captions, translation and a voice.
  echo     Optional, for better translation ^(press Enter to skip^):
  echo       Anthropic:  https://console.anthropic.com  ^(NOT Claude.ai Pro^)
  echo       OpenAI:     https://platform.openai.com    ^(also the Router and an extra ear^)
  echo       Google:     https://aistudio.google.com    ^(Gemini^)
  echo     Optional voice: ElevenLabs  https://elevenlabs.io
  echo.
  set /p "SKEY=  Soniox API key: "
  set /p "AKEY=  Anthropic API key (Enter to skip): "
  set /p "OKEY=  OpenAI API key (Enter to skip): "
  set /p "GKEY=  Gemini API key (Enter to skip): "
  set /p "EKEY=  ElevenLabs API key (Enter to skip): "
  set "VOICE="
  if not "!EKEY!"=="" set /p "VOICE=  ElevenLabs voice id (from your Voice Library): "
  (
    echo SONIOX_API_KEY=!SKEY!
    echo ANTHROPIC_API_KEY=!AKEY!
    echo OPENAI_API_KEY=!OKEY!
    echo GEMINI_API_KEY=!GKEY!
    echo ELEVENLABS_API_KEY=!EKEY!
    echo CT_EL_VOICE=!VOICE!
  )> ".env"
  echo.
  echo   Saved .env.
  echo.
)

REM (Re)build the env if missing or too old (e.g. a stale 3.6 venv from a prior run).
set "VPY=.venv-win\Scripts\python.exe"
set "NEEDBUILD="
if not exist "%VPY%" set NEEDBUILD=1
if exist "%VPY%" ("%VPY%" -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1 || set NEEDBUILD=1)
if defined NEEDBUILD if exist ".venv-win" rmdir /s /q ".venv-win"
if defined NEEDBUILD echo Building Windows Python env (one-time, ~1 min)...
if defined NEEDBUILD "%PYEXE%" -m venv ".venv-win"
if defined NEEDBUILD "%VPY%" -m pip install --upgrade pip
if defined NEEDBUILD "%VPY%" -m pip install -r requirements.txt

REM Make Python use UTF-8 so Romanian diacritics in logs don't crash on the
REM Windows console's legacy code page (server.py also forces this defensively).
set PYTHONUTF8=1
echo Starting server... a browser tab will open at http://localhost:5000
"%VPY%" "app\server.py"
echo.
echo Server stopped. Press a key to close.
pause >nul
