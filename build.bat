@echo off
REM Build script for Church Translate native Windows executable
REM Creates a standalone .exe that includes Python, all dependencies, and the app

setlocal EnableDelayedExpansion

echo.
echo ========================================
echo Church Translate - Build Native App
echo ========================================
echo.

REM Check Python version
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found in PATH
    echo Install Python 3.12 from https://www.python.org/downloads/windows/
    echo and make sure "Add python.exe to PATH" is checked
    pause
    exit /b 1
)

REM Check if PyInstaller is installed
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    pip install pyinstaller
)

REM Install project dependencies
echo.
echo Installing project dependencies...
pip install -r requirements.txt

REM Clean previous build
if exist "build" (
    echo Cleaning previous build...
    rmdir /s /q build >nul 2>&1
)
if exist "dist" (
    echo Cleaning previous dist...
    rmdir /s /q dist >nul 2>&1
)

REM Run PyInstaller
echo.
echo Building executable...
echo This may take 2-3 minutes...
echo.
pyinstaller church-translate.spec --distpath dist --noconfirm

if errorlevel 1 (
    echo.
    echo ERROR: Build failed
    pause
    exit /b 1
)

echo.
echo ========================================
echo Build Complete!
echo ========================================
echo.
echo Executable: dist\church-translate.exe
echo Size: ~80-100 MB (includes Python + all dependencies)
echo.
echo To run:
echo   1. Copy .env file (with your API keys) to same folder as .exe
echo   2. Double-click church-translate.exe
echo.
echo The app will:
echo   - Start the Flask server in the background
echo   - Automatically open your browser to http://localhost:5000
echo   - Show server logs in the console window
echo.
echo To distribute:
echo   - Zip up the entire dist\ folder OR
echo   - Create an NSIS installer (see BUILD.md)
echo.
pause
