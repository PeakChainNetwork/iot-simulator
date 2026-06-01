#!/usr/bin/env bash
# One-command bootstrap + run for the IoT Simulator (macOS/Linux).
# Creates a venv, installs deps, copies .env if missing, then starts publishing.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

echo "Installing dependencies..."
./.venv/bin/python -m pip install -q -r requirements.txt

if [ ! -f .env ]; then
  echo "Creating .env from .env.example..."
  cp .env.example .env
fi

echo "Starting simulator on http://localhost:8001 ..."
exec ./.venv/bin/python -m uvicorn app.main:app --port 8001
