#!/usr/bin/env bash
# Run every check: unit/API tests, the five browser UI suites (Chrome), and the desktop self-test.
set -euo pipefail
cd "$(dirname "$0")/.."
TMP=$(mktemp -d)
trap 'kill $SERVER 2>/dev/null || true; rm -rf "$TMP"' EXIT
export AI_CAD_CONFIG="$TMP/config.json" AI_CAD_CACHE="$TMP/cache"   # never touch the real key or cache

echo "== unit / API tests"
.venv/bin/python -m pytest -q tests

(cd web && npm run build >/dev/null)
PORT=$(.venv/bin/python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
.venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port "$PORT" --log-level warning &
SERVER=$!
for _ in $(seq 60); do curl -s -o /dev/null "http://127.0.0.1:$PORT/api/info" && break; sleep 0.5; done
for t in ui_smoke ui_sketch ui_drawing ui_analysis ui_assembly; do
  echo "== $t"
  .venv/bin/python "tests/$t.py" "http://127.0.0.1:$PORT" "$TMP"
done

echo "== desktop self-test"
AI_CAD_SELFTEST="$TMP/desktop.png" timeout -k 5 120 ./desktop.sh 2>/dev/null | grep SELFTEST
echo "ALL CHECKS PASSED"
