#!/usr/bin/env bash
# start.sh — Start the AI Agent Observability demo
# Run from the project root:  bash start.sh

set -e
if [[ "${1:-}" == "--port" && "${2:-}" =~ ^[0-9]+$ ]]; then
  export PORT="$2"
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/web_agent/.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "❌  Missing $ENV_FILE"
  echo "   Copy web_agent/.env.example → web_agent/.env and fill in keys"
  exit 1
fi

# server.py loads the private environment and prints the configured URLs.
# Prefer the Command Line Tools Python on Macs with an unaccepted Xcode license.
DEMO_PYTHON="${DEMO_PYTHON:-python3}"
if [[ "$DEMO_PYTHON" == "python3" && -x /Library/Developer/CommandLineTools/usr/bin/python3 ]]; then
  DEMO_PYTHON=/Library/Developer/CommandLineTools/usr/bin/python3
fi
exec "$DEMO_PYTHON" "$SCRIPT_DIR/web_agent/server.py"
