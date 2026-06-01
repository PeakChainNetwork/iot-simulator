@echo off
REM One-command bootstrap + run for the IoT Simulator (Windows).
REM Creates a venv, installs deps, copies .env if missing, then starts publishing.
cd /d "%~dp0"

if not exist .venv (
  echo Creating virtual environment...
  python -m venv .venv || goto :error
)

echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :error

if not exist .env (
  echo Creating .env from .env.example...
  copy /y .env.example .env >nul
)

echo Starting simulator on http://localhost:8001 ...
".venv\Scripts\python.exe" -m uvicorn app.main:app --port 8001
goto :eof

:error
echo Setup failed. Make sure Python 3.10+ is installed and on PATH.
exit /b 1
