#!/usr/bin/env bash
# One-command setup + test + run (macOS / Linux).
#   ./start.sh          -> setup, run tests, start server
#   ./start.sh demo     -> setup, start server in background, run the demo, stop server
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v tesseract >/dev/null 2>&1; then
  echo ">> Tesseract not found (needed only for scanned images)."
  echo "   macOS: brew install tesseract    Ubuntu/Debian: sudo apt-get install -y tesseract-ocr"
fi

if [ ! -d .venv ]; then
  echo ">> Creating virtualenv"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
echo ">> Installing dependencies"
pip install -q --upgrade pip
pip install -q -r requirements-dev.txt

[ -f .env ] || cp .env.example .env

echo ">> Running tests"
pytest

if [ "${1:-}" = "demo" ]; then
  echo ">> Starting server in background"
  uvicorn app.main:app --port 8000 > server.log 2>&1 &
  SERVER_PID=$!
  trap 'kill $SERVER_PID 2>/dev/null' EXIT
  for _ in $(seq 1 30); do curl -sf localhost:8000/health >/dev/null && break; sleep 0.5; done
  python scripts/demo.py --url http://localhost:8000 --key "$(grep -E '^MEDX_API_KEY=' .env | cut -d= -f2)"
else
  echo ">> Starting server on http://localhost:8000  (docs: http://localhost:8000/docs)"
  uvicorn app.main:app --reload --port 8000
fi
