@echo off
cd /d "%~dp0"
set "CXKITTY_RUN_PY=python"
if exist "%~dp0.venv-build\Scripts\python.exe" set "CXKITTY_RUN_PY=%~dp0.venv-build\Scripts\python.exe"
if exist "%~dp0.venv\Scripts\python.exe" set "CXKITTY_RUN_PY=%~dp0.venv\Scripts\python.exe"
"%CXKITTY_RUN_PY%" main.py
if errorlevel 1 pause
