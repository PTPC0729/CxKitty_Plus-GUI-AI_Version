@echo off
cd /d "%~dp0"
if exist "%~dp0CxKitty-Packager.exe" (
    start "" "%~dp0CxKitty-Packager.exe"
    exit /b
)
set "CXKITTY_BUILD_PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "CXKITTY_BUILD_PY=%~dp0.venv\Scripts\python.exe"
if exist "%~dp0.venv-build\Scripts\python.exe" set "CXKITTY_BUILD_PY=%~dp0.venv-build\Scripts\python.exe"
"%CXKITTY_BUILD_PY%" -c "import PySide6" >nul 2>&1
if errorlevel 1 (
    echo Python / PySide6 is missing. Install Python 3.10-3.14, then run:
    echo python -m pip install -r requirements-build.txt
    pause
    exit /b 1
)
"%CXKITTY_BUILD_PY%" packager.py
if errorlevel 1 pause
