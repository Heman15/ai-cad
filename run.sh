#!/usr/bin/env bash
# Build the UI if needed and start the app at http://127.0.0.1:8000
set -e
cd "$(dirname "$0")"
if [ ! -d web/dist ] || [ -n "$(find web/src web/index.html -newer web/dist -print -quit)" ]; then
  (cd web && npm run build)
fi
export AI_CAD_PORT="${PORT:-8000}"
exec .venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port "${PORT:-8000}"
