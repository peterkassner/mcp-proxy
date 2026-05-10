#!/bin/zsh
# Interactive CLI for mcp-proxy-hub
# Usage: hub/cli.zsh  (or via the `mcp-proxy-hub` shell alias)

set -uo pipefail

HUB_DIR="${0:A:h}"
LOG_FILE="$HOME/Library/Logs/com.peterkjackson.mcp-proxy-hub/mcp-proxy-hub.log"
STATUS_URL="http://localhost:8096/status"
GUI_URL="http://localhost:8096/gui/api/status"
STATUS_TIMEOUT=15

# ── colour helpers ───────────────────────────────────────────────────────────
_green()  { printf '\033[0;32m%s\033[0m' "$*" }
_yellow() { printf '\033[0;33m%s\033[0m' "$*" }
_red()    { printf '\033[0;31m%s\033[0m' "$*" }
_bold()   { printf '\033[1m%s\033[0m'    "$*" }
_dim()    { printf '\033[2m%s\033[0m'    "$*" }

# ── status probe ─────────────────────────────────────────────────────────────
# Returns 0 if hub responds within timeout, 1 otherwise.
_check_status() {
  local raw
  raw=$(curl -fsSL --max-time "$STATUS_TIMEOUT" "$STATUS_URL" 2>/dev/null) || return 1
  echo "$raw"
  return 0
}

# Pretty-print server status from the GUI API (best-effort; falls back to /status).
_show_status() {
  printf '\n'
  printf "$(_bold 'Checking hub…')  $(_dim "(timeout ${STATUS_TIMEOUT}s)")\n"

  local gui_raw
  gui_raw=$(curl -fsSL --max-time "$STATUS_TIMEOUT" "$GUI_URL" 2>/dev/null)
  local gui_ok=$?

  if [[ $gui_ok -ne 0 ]]; then
    # Try plain /status as fallback
    local plain
    plain=$(curl -fsSL --max-time "$STATUS_TIMEOUT" "$STATUS_URL" 2>/dev/null)
    if [[ $? -ne 0 || -z "$plain" ]]; then
      printf "$(_red '✖  Hub is DOWN') — no response after ${STATUS_TIMEOUT}s\n\n"
      return 1
    fi
    printf "$(_green '✔  Hub is UP')  $(_dim '(GUI API unavailable; /status OK)')\n\n"
    printf '%s\n\n' "$plain"
    return 0
  fi

  # Parse server list from GUI JSON (requires python3, always present on macOS).
  local summary
  summary=$(python3 - <<'PYEOF'
import json, sys

raw = sys.stdin.read()
try:
    data = json.loads(raw)
except Exception as e:
    print(f"  (could not parse JSON: {e})")
    sys.exit(0)

servers = data.get("servers") or data.get("server_instances") or {}
if isinstance(servers, dict):
    items = servers.items()
elif isinstance(servers, list):
    items = [(s.get("name", "?"), s.get("status", "?")) for s in servers]
else:
    items = []

total = len(list(items))
# re-iterate
if isinstance(servers, dict):
    items = servers.items()
elif isinstance(servers, list):
    items = [(s.get("name", "?"), s.get("status", "?")) for s in servers]

up = down = 0
rows = []
for name, status in items:
    st = str(status).lower()
    if "ok" in st or "running" in st or "up" in st:
        icon = "✔"; up += 1
    elif "fail" in st or "down" in st or "error" in st:
        icon = "✖"; down += 1
    else:
        icon = "–"; down += 1
    rows.append((icon, name, str(status)))

print(f"  {up}/{up+down} servers running\n")
w = max((len(r[1]) for r in rows), default=10)
for icon, name, status in rows:
    print(f"  {icon}  {name:<{w}}  {status}")
PYEOF
) <<< "$gui_raw"

  printf "$(_green '✔  Hub is UP')\n"
  printf '%s\n\n' "$summary"
  return 0
}

# ── menu loop ────────────────────────────────────────────────────────────────
_menu() {
  while true; do
    printf "\n$(_bold 'mcp-proxy-hub')  — what would you like to do?\n"
    printf "  $(_bold '1')  Check status\n"
    printf "  $(_bold '2')  Restart hub\n"
    printf "  $(_bold '3')  Tail logs  $(_dim '(last 40 lines, live)')\n"
    printf "  $(_bold '4')  Open GUI in browser\n"
    printf "  $(_bold 'q')  Quit\n\n"
    printf "Choice: "

    local choice
    read -r choice

    case "$choice" in
      1)
        _show_status || true
        ;;
      2)
        printf '\n'
        "$HUB_DIR/restart.zsh"
        ;;
      3)
        printf "\n$(_dim "Press Ctrl-C to stop tailing…")\n\n"
        tail -n 40 -f "$LOG_FILE" 2>/dev/null || {
          printf "$(_red 'Log file not found:')\n  %s\n" "$LOG_FILE"
        }
        ;;
      4)
        open "http://localhost:8096/gui/" 2>/dev/null \
          || printf "$(_yellow 'open failed — visit:')  http://localhost:8096/gui/\n"
        ;;
      q|Q|quit|exit)
        printf "bye.\n"
        exit 0
        ;;
      *)
        printf "$(_yellow 'Unknown choice — enter 1-4 or q')\n"
        ;;
    esac
  done
}

# ── entry point ──────────────────────────────────────────────────────────────
# If a subcommand is passed (e.g. `mcp-proxy-hub status`), run non-interactively.
case "${1:-}" in
  status)   _show_status; exit $? ;;
  restart)  exec "$HUB_DIR/restart.zsh" ;;
  logs)     exec tail -n 40 -f "$LOG_FILE" ;;
  "")       _menu ;;
  *)
    printf "Usage: %s [status|restart|logs]\n" "${0:t}" >&2
    exit 1
    ;;
esac
