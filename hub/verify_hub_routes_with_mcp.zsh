#!/bin/zsh
# Smoke-test each mcp-proxy hub route over Streamable HTTP .../servers/<key>/mcp (same as Cursor
# "http" transport). Runs verify_hub_routes_with_mcp.py (initialize + Mcp-Session-Id + tools/list).
#
# Homebrew mcptools `mcp tools <url>/mcp` often returns 400 against mcp-proxy; use `mcp tools
# <url>/sse` for quick manual checks, or rely on this script for /mcp truth.
#
# Requires: python3 (no brew `mcp` required).
set -euo pipefail

readonly HUB_JSON="${MCP_PROXY_HUB_JSON:-${HOME}/.mcp/mcp-proxied-servers.json}"
readonly BASE="${MCP_HUB_BASE_URL:-http://127.0.0.1:8096}"
readonly PY="${0:A:h}/verify_hub_routes_with_mcp.py"

if [[ ! -r "$HUB_JSON" ]]; then
  print -r -- "[verify-mcp] hub JSON not readable: $HUB_JSON" >&2
  exit 1
fi
if [[ ! -r "$PY" ]]; then
  print -r -- "[verify-mcp] missing probe script: $PY" >&2
  exit 1
fi

export MCP_PROXY_HUB_JSON="$HUB_JSON"
export MCP_HUB_BASE_URL="$BASE"
exec python3 "$PY"
