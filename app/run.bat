@echo off
REM Launch Church Translate using the local Windows Python environment.
REM If you want to use a virtual environment, create it at .venv in the project root.
setlocal enabledelayedexpansion

pushd "%~dp0\.."

set "PYTHON="
if exist ".venv\Scripts\python.exe" (
  set "PYTHON=%CD%\.venv\Scripts\python.exe"
  "%PYTHON%" --version >nul 2>&1 || set "PYTHON="
)
if not defined PYTHON (
  for %%C in (python3 python py) do if not defined PYTHON (
    for /f "usebackq delims=" %%Q in (`where %%C 2^>nul`) do if exist "%%Q" (
      "%%Q" --version >nul 2>&1
      if !ERRORLEVEL! equ 0 (
        set "PYTHON=%%Q"
        goto :found_python
      )
    )
  )
)

:found_python
if not defined PYTHON (
  echo.
  echo ERROR: Python not found. Install Python and/or create a venv at "%CD%\.venv".
  echo If `python` points to a Windows Store alias, install Python from python.org or disable the App Execution Alias for `python` and `python3`.
  echo.
  pause
  exit /b 1
)

echo.
echo Starting Church Translate using %PYTHON%
echo Project: %CD%
echo.
"%PYTHON%" app\server.py
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Server stopped with exit code %EXIT_CODE%.
pause
popd
exit /b %EXIT_CODE%