#!/usr/bin/env bash
# Launch the AI CAD desktop app (builds the UI first if it changed).
set -e
cd "$(dirname "$0")"
if [ ! -d web/dist ] || [ -n "$(find web/src web/index.html -newer web/dist -print -quit)" ]; then
  (cd web && npm run build)
fi
[ -d desktop/node_modules ] || (cd desktop && npm install)
# VS Code and other Electron-based tools export this, which would make Electron run as plain Node.
unset ELECTRON_RUN_AS_NODE
exec desktop/node_modules/.bin/electron desktop "$@"
