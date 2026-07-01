@echo off
:: ============================================================================
:: Camins Rals v7 — Windows Build Script
:: Run this file on a Windows machine with Python 3.11 installed.
:: ============================================================================

setlocal

set "ROOT=%~dp0.."
cd /d "%ROOT%"

echo.
echo ============================================================
echo  Camins Rals v7 - Windows Build
echo ============================================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.11 from python.org
    pause & exit /b 1
)

:: Install dependencies
echo [1/5] Installing Python packages...
python -m pip install --upgrade pip
pip install -r build\requirements_windows.txt
if errorlevel 1 ( echo FAILED & pause & exit /b 1 )

:: Install Playwright Chromium
echo.
echo [2/5] Downloading Playwright Chromium (~150 MB, only once)...
playwright install chromium
if errorlevel 1 ( echo FAILED & pause & exit /b 1 )

:: Copy Chromium to repo root for bundling
echo.
echo [3/5] Staging Chromium for bundling...
if exist ms-playwright rmdir /s /q ms-playwright
xcopy "%LOCALAPPDATA%\ms-playwright" "ms-playwright\" /E /I /Q
if errorlevel 1 ( echo FAILED - could not copy Chromium & pause & exit /b 1 )

:: Build
echo.
echo [4/5] Running PyInstaller...
pyinstaller build\app_7.spec --clean --noconfirm
if errorlevel 1 ( echo BUILD FAILED & pause & exit /b 1 )

:: Package
echo.
echo [5/5] Creating zip archive...
powershell -Command "Compress-Archive -Path 'dist\CaminsRals\*' -DestinationPath 'CaminsRals-Windows.zip' -Force"
if errorlevel 1 ( echo FAILED & pause & exit /b 1 )

echo.
echo ============================================================
echo  BUILD COMPLETE
echo  Output: %ROOT%\CaminsRals-Windows.zip
echo ============================================================
echo.
echo Send CaminsRals-Windows.zip to your father.
echo Instructions: unzip anywhere, run CaminsRals.exe
echo.
pause
