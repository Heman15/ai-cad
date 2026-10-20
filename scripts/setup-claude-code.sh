#!/usr/bin/env bash
# Connect Claude Code (VS Code extension or CLI) to AI CAD on this machine.
# Writes .mcp.json for this project folder; with --global also registers it for every folder.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "Python environment not found at $PY - run the setup steps in README.md first"; exit 1; }
cat > .mcp.json <<JSON
{
  "mcpServers": {
    "ai-cad": { "command": "$PY", "args": ["$ROOT/server/mcp_server.py"] }
  }
}
JSON
echo "wrote $ROOT/.mcp.json - start Claude Code in this folder and approve the 'ai-cad' server (/mcp)."
if [ "${1:-}" = "--global" ]; then
  CLAUDE=$(command -v claude || ls -d ~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude 2>/dev/null | sort -V | tail -1)
  [ -n "$CLAUDE" ] || { echo "Claude Code not found"; exit 1; }
  "$CLAUDE" mcp add --scope user ai-cad -- "$PY" "$ROOT/server/mcp_server.py"
fi
