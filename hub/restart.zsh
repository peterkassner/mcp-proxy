#!/bin/zsh
# Restart the Mac MCP LAN bridge (mcp-proxy LaunchAgent) and verify via HTTP /status.
#
# Plist refresh: before bootstrap, copies MCP_PROXY_LAUNCHAGENT_PLIST_SOURCE (default:
# deploy/mcp/mcp-proxy-hub/com.peterkjackson.mcp-proxy-bridge.plist) onto
# MCP_PROXY_LAUNCHAGENT_PLIST (default: ~/Library/LaunchAgents/<label>.plist). Set
# MCP_PROXY_SKIP_PLIST_SYNC=1 to skip copying (e.g. you manage the LaunchAgents plist elsewhere).
#
# KeepAlive only means launchd respawns *this* job while it stays loaded; it does not
# prevent a *separate* mcp-proxy (manual run, other plist, old orphan) from holding :8096.
# We therefore clear the port after bootout so the new instance can bind.
set -euo pipefail

readonly LABEL='com.peterkjackson.mcp-proxy-bridge'
readonly PORT='8096'
readonly STATUS_URL="http://127.0.0.1:${PORT}/status"
readonly USER_ID="$(id -u)"
readonly DOMAIN="gui/${USER_ID}"
readonly USER_DOMAIN="user/${USER_ID}"
readonly SERVICE_TARGET="${DOMAIN}/${LABEL}"
readonly USER_SERVICE_TARGET="${USER_DOMAIN}/${LABEL}"
readonly MAX_WAIT_SEC="${MCP_PROXY_RESTART_MAX_WAIT_SEC:-120}"
readonly PLIST="${MCP_PROXY_LAUNCHAGENT_PLIST:-${HOME}/Library/LaunchAgents/${LABEL}.plist}"
# Repo-side plist (default: alongside this script). Copied onto PLIST before each reload so
# launchd always bootstraps the on-disk definition from the hub, not a stale LaunchAgents copy.
readonly PLIST_SOURCE="${MCP_PROXY_LAUNCHAGENT_PLIST_SOURCE:-${0:A:h}/com.peterkjackson.mcp-proxy-bridge.plist}"

log() { print -r -- "[bridge-restart] $*"; }

typeset -a PORT_LISTENER_PIDS
typeset -a MCP_PROXY_PIDS

refresh_port_listener_pids() {
  typeset out=''
  out=$(LC_ALL=C lsof -t -iTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null || true)
  PORT_LISTENER_PIDS=( ${(@f)out} )
}

refresh_mcp_proxy_pids() {
  typeset out=''
  out=$(pgrep -f 'mcp-proxy' 2>/dev/null || true)
  MCP_PROXY_PIDS=( ${(@f)out} )
}

emit_output() {
  typeset text="${1:-}"
  [[ -z "${text}" ]] && return 0
  while IFS= read -r line; do
    log "  ${line}"
  done <<< "${text}"
}

run_best_effort() {
  typeset label="$1"
  shift
  typeset out=''
  if out=$("$@" 2>&1); then
    log "${label}: ok"
    emit_output "${out}"
    return 0
  else
    typeset st=$?
    log "${label}: exit ${st} (continuing)"
    emit_output "${out}"
    return "${st}"
  fi
}

service_is_loaded() {
  launchctl print "${SERVICE_TARGET}" >/dev/null 2>&1
}

should_verify_hub_routes() {
  typeset mode="${MCP_HUB_VERIFY_MCPTOOLS:-1}"

  case "${mode}" in
    ask|1|true|on|yes|y)
      return 0
      ;;
    0|false|off|no|n)
      log "hub MCP route verification skipped (MCP_HUB_VERIFY_MCPTOOLS=0)"
      return 1
      ;;
    *)
      log "MCP_HUB_VERIFY_MCPTOOLS value '${mode}' treated as disabled for safety"
      return 1
      ;;
  esac
}

# After bootout, anything still bound to PORT is almost certainly a stray hub (or zombie
# listener). launchd's kickstart -k only kills the *registered* job's process tree.
describe_pids() {
  typeset -a pids
  pids=( "$@" )
  (( ${#pids} == 0 )) && return 0
  ps -o pid= -o ppid= -o command= -p "${pids[@]}" 2>/dev/null | while IFS= read -r line; do
    log "  process ${line}"
  done
}

kill_stale_port_listeners() {
  typeset -a pids
  refresh_port_listener_pids
  pids=( "${PORT_LISTENER_PIDS[@]}" )
  if (( ${#pids} == 0 )); then
    log "port ${PORT}: no stray listener after bootout (ok)"
    return 0
  fi
  log "port ${PORT}: still listening (PIDs: ${pids[*]}) — sending SIGTERM (stray mcp-proxy or stuck bind)"
  describe_pids "${pids[@]}"
  kill -TERM "${pids[@]}" 2>/dev/null || true
  typeset waited=0
  while (( waited < 8 )); do
    sleep 1
    refresh_port_listener_pids
    pids=( "${PORT_LISTENER_PIDS[@]}" )
    (( ${#pids} == 0 )) && return 0
    waited=$((waited + 1))
  done
  refresh_port_listener_pids
  pids=( "${PORT_LISTENER_PIDS[@]}" )
  if (( ${#pids} == 0 )); then
    return 0
  fi
  log "port ${PORT}: still listening (PIDs: ${pids[*]}) — sending SIGKILL"
  describe_pids "${pids[@]}"
  kill -KILL "${pids[@]}" 2>/dev/null || true
  sleep 1
  refresh_port_listener_pids
  pids=( "${PORT_LISTENER_PIDS[@]}" )
  if (( ${#pids} > 0 )); then
    log "WARNING: port ${PORT} still has listeners: ${pids[*]} — new hub may fail to bind"
  fi
}

# Emit launchd / process state before mutating anything (best-effort; launchctl text is not a stable API).
emit_launchd_snapshot() {
  typeset title="${1:-launchd snapshot}"
  log "--- ${title} ---"
  log "  domain: ${DOMAIN}"
  log "  label:  ${LABEL}"
  if [[ -f ${PLIST} ]]; then
    log "  plist:  ${PLIST} (exists)"
  else
    log "  plist:  ${PLIST} (MISSING)"
  fi

  typeset out
  if out=$(launchctl print "${SERVICE_TARGET}" 2>/dev/null); then
    log "  launchctl print ${SERVICE_TARGET}:"
    print -r -- "$out" | grep -Ei '(^|[[:space:]])(state|type|path|program|pid|active count|last exit|disabled|disabling|exits|bootstrap|substate|reason) =' | sed 's/^[[:space:]]*//' | while IFS= read -r line; do
      log "    ${line}"
    done
  else
    log "  launchctl print: job not loaded in ${DOMAIN} (or print failed)"
  fi

  typeset disabled_domain dline
  for disabled_domain in "${DOMAIN}" "${USER_DOMAIN}"; do
    dline=$(launchctl print-disabled "${disabled_domain}" 2>/dev/null | grep -F "\"${LABEL}\"" || true)
    if [[ -n ${dline} ]]; then
      log "  print-disabled ${disabled_domain}: $(print -r -- "$dline" | sed 's/^[[:space:]]*//')"
      log "    (here '=> disabled' means launchd will not start it; '=> enabled' means not blocked by disable-store)"
    else
      log "  print-disabled ${disabled_domain}: (no line containing \"${LABEL}\")"
    fi
  done

  if pgrep -fq 'mcp-proxy' 2>/dev/null; then
    log "  process: mcp-proxy matched by pgrep (see: pgrep -fl mcp-proxy)"
  else
    log "  process: no mcp-proxy process matched by pgrep"
  fi
  log "--- end ${title} ---"
}

emit_http_timeout_snapshot() {
  log "launchd restart completed, but HTTP did not bind before timeout"
  refresh_mcp_proxy_pids
  if (( ${#MCP_PROXY_PIDS} > 0 )); then
    log "mcp-proxy process still running (PIDs: ${MCP_PROXY_PIDS[*]})"
    describe_pids "${MCP_PROXY_PIDS[@]}"
  else
    log "mcp-proxy process not found after kickstart"
  fi

  refresh_port_listener_pids
  if (( ${#PORT_LISTENER_PIDS} > 0 )); then
    log "port ${PORT}: listener exists (PIDs: ${PORT_LISTENER_PIDS[*]})"
    describe_pids "${PORT_LISTENER_PIDS[@]}"
  else
    log "port ${PORT}: no listener after kickstart; hub likely hung before HTTP bind"
  fi
  emit_launchd_snapshot "launchd post-timeout snapshot"
}

# Print LaunchAgent ProgramArguments (one line per array entry) for a plist on disk.
log_plist_program_arguments() {
  typeset plist_path="${1:?}"
  typeset context_label="${2:-}"
  if [[ ! -f "${plist_path}" ]]; then
    log "  (no plist at path: ${plist_path})"
    return 0
  fi
  typeset abs="${plist_path:A}"
  if [[ -n ${context_label} ]]; then
    log "  ProgramArguments — ${context_label}"
  else
    log "  ProgramArguments"
  fi
  log "  plist file: ${abs}"
  typeset py_out py_stat
  set +e
  py_out=$(
    python3 -c "
import plistlib, pathlib, sys
p = pathlib.Path(sys.argv[1])
if not p.is_file():
    sys.exit(1)
with p.open('rb') as f:
    d = plistlib.load(f)
args = d.get('ProgramArguments')
if not isinstance(args, list):
    print('(missing or not an array: ProgramArguments)')
    sys.exit(0)
for i, s in enumerate(args):
    print(f'[{i}] {s}')
" "${abs}" 2>&1
  )
  py_stat=$?
  set -e
  if (( py_stat != 0 )); then
    log "  (failed to read ProgramArguments from plist: ${py_out})"
    return 0
  fi
  typeset -a lines
  lines=( ${(f)py_out} )
  typeset line
  for line in "${lines[@]}"; do
    log "    ${line}"
  done
}

sync_plist_from_source() {
  if [[ "${MCP_PROXY_SKIP_PLIST_SYNC:-0}" == "1" ]]; then
    log "not copying because MCP_PROXY_SKIP_PLIST_SYNC=1 (plist sync disabled)"
    log_plist_program_arguments "${PLIST}" "current install (not modified)"
    return 0
  fi
  if [[ ! -f "${PLIST_SOURCE}" ]]; then
    log "not copying because source plist is missing: ${PLIST_SOURCE:A}"
    if [[ -f "${PLIST}" ]]; then
      log_plist_program_arguments "${PLIST}" "current install (no source to copy from)"
    else
      log "  (install plist also missing: ${PLIST:A})"
    fi
    return 0
  fi
  typeset out=''
  if ! out=$(plutil -lint "${PLIST_SOURCE}" 2>&1); then
    log "ERROR: plist source failed lint (not copying): ${PLIST_SOURCE:A}"
    emit_output "${out}"
    return 1
  fi
  log "plist sync: source lint ok (${PLIST_SOURCE:A})"
  emit_output "${out}"
  log "=== ProgramArguments before copy (source plist) ==="
  log_plist_program_arguments "${PLIST_SOURCE}" "source: ${PLIST_SOURCE:A}"
  mkdir -p "${PLIST:h}"
  # macOS cp exits non-zero when source and dest are the same file; with set -e that aborts
  # the restart. Skip copy when source and install name the same target.
  # Use stat -L: plain stat lstat()s symlinks, so LaunchAgents → repo symlink never matched
  # the real file’s inode; cp still followed links and errored "identical (not copied)".
  typeset id_src id_dst
  id_src=$(stat -L -f '%d:%i' "${PLIST_SOURCE}" 2>/dev/null) || id_src=''
  id_dst=$(stat -L -f '%d:%i' "${PLIST}" 2>/dev/null) || id_dst=''
  if [[ -n ${id_src} && ${id_src} == "${id_dst}" ]]; then
    log "not copying because source and install path are the same file after resolving symlinks (device:inode ${id_src})"
    log "  source: ${PLIST_SOURCE:A}"
    log "  install: ${PLIST:A}"
    return 0
  fi
  log "plist sync: copying (full paths):"
  log "  source: ${PLIST_SOURCE:A}"
  log "  dest:   ${PLIST:A}"
  cp -f "${PLIST_SOURCE}" "${PLIST}"
  log "plist sync: copy completed"
  log "=== ProgramArguments after copy (install plist) ==="
  log_plist_program_arguments "${PLIST}" "install: ${PLIST:A}"
}

validate_plist() {
  if [[ ! -f ${PLIST} ]]; then
    log "ERROR: plist missing: ${PLIST}"
    return 1
  fi

  typeset out=''
  if out=$(plutil -lint "${PLIST}" 2>&1); then
    log "plist lint: ok (${PLIST})"
    emit_output "${out}"
    return 0
  fi

  log "ERROR: plist lint failed: ${PLIST}"
  emit_output "${out}"
  return 1
}

wait_until_unloaded() {
  typeset waited=0
  while (( waited < 10 )); do
    if ! service_is_loaded; then
      log "launchd: ${SERVICE_TARGET} is unloaded"
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  log "WARNING: ${SERVICE_TARGET} still appears loaded after bootout attempts"
  return 1
}

clear_disable_state() {
  # macOS can record disabled state under either gui/<uid> or user/<uid>.
  # Enabling both avoids a stale disabled-store entry blocking bootstrap/kickstart.
  run_best_effort "launchd enable ${SERVICE_TARGET}" launchctl enable "${SERVICE_TARGET}" || true
  run_best_effort "launchd enable ${USER_SERVICE_TARGET}" launchctl enable "${USER_SERVICE_TARGET}" || true
}

bootout_launchagent() {
  if service_is_loaded; then
    run_best_effort "launchd kill TERM ${SERVICE_TARGET}" launchctl kill TERM "${SERVICE_TARGET}" || true
    sleep 1
  else
    log "launchd: ${SERVICE_TARGET} not loaded before bootout"
  fi

  run_best_effort "launchd bootout ${SERVICE_TARGET}" launchctl bootout "${SERVICE_TARGET}" || true
  run_best_effort "launchd bootout ${DOMAIN} ${PLIST}" launchctl bootout "${DOMAIN}" "${PLIST}" || true
  wait_until_unloaded || true
  kill_stale_port_listeners
}

bootstrap_launchagent() {
  typeset out=''
  if out=$(launchctl bootstrap "${DOMAIN}" "${PLIST}" 2>&1); then
    log "launchd bootstrap ${DOMAIN} <- ${PLIST}: ok"
    emit_output "${out}"
    return 0
  else
    typeset st=$?
  fi

  if service_is_loaded; then
    log "launchd bootstrap ${DOMAIN} <- ${PLIST}: exit ${st}, but service is loaded; continuing"
    emit_output "${out}"
    return 0
  fi

  log "ERROR: launchd bootstrap ${DOMAIN} <- ${PLIST}: exit ${st}"
  emit_output "${out}"
  return "${st}"
}

kickstart_launchagent() {
  typeset out=''
  if out=$(launchctl kickstart -k "${SERVICE_TARGET}" 2>&1); then
    log "launchd kickstart -k ${SERVICE_TARGET}: ok"
    emit_output "${out}"
    return 0
  else
    typeset st=$?
  fi

  log "launchd kickstart -k ${SERVICE_TARGET}: exit ${st}; retrying one clean bootstrap"
  emit_output "${out}"
  run_best_effort "launchd bootout ${SERVICE_TARGET}" launchctl bootout "${SERVICE_TARGET}" || true
  wait_until_unloaded || true
  kill_stale_port_listeners
  clear_disable_state
  bootstrap_launchagent

  out=''
  if out=$(launchctl kickstart -k "${SERVICE_TARGET}" 2>&1); then
    log "launchd kickstart retry: ok"
    emit_output "${out}"
    return 0
  else
    st=$?
  fi

  log "ERROR: launchd kickstart retry failed: exit ${st}"
  emit_output "${out}"
  return "${st}"
}

# Hard reload: sync plist from hub copy → validate → bootout by service and plist path →
# clear stale port → clear disabled-state in both domains → bootstrap → kickstart -k.
reload_mcp_proxy_launchagent() {
  sync_plist_from_source
  validate_plist
  bootout_launchagent
  clear_disable_state
  bootstrap_launchagent
  kickstart_launchagent
}

log "starting restart at $(LC_ALL=C TZ=America/Los_Angeles date '+%Y-%m-%dT%H:%M:%S %Z') (Pacific)"
if [[ "${MCP_PROXY_SKIP_DIAG_PATCH:-0}" == "1" ]]; then
  log "hub diag overlay check skipped (MCP_PROXY_SKIP_DIAG_PATCH=1)"
elif [[ -r "${0:A:h}/apply_mcp_hub_diag_patch.py" ]]; then
  if ! python3 "${0:A:h}/apply_mcp_hub_diag_patch.py" --check 2>/dev/null; then
    log "hub diag overlays missing or version skew — applying vendor-patch-overlays…"
    python3 "${0:A:h}/apply_mcp_hub_diag_patch.py" || log "WARNING: apply_mcp_hub_diag_patch.py failed (continuing)"
  fi
fi
emit_launchd_snapshot "launchd pre-restart snapshot"
log "launchd: plist sync (if source present) → lint → bootout(service+plist) → enable(gui+user) → bootstrap → kickstart -k (${SERVICE_TARGET})"
reload_mcp_proxy_launchagent

body=''
start_seconds=${SECONDS}
next_notice=5
while (( SECONDS - start_seconds < MAX_WAIT_SEC )); do
  waited=$((SECONDS - start_seconds))
  if body=$(curl -fsS -m 3 --connect-timeout 2 "$STATUS_URL" 2>/dev/null); then
    log "HTTP ${STATUS_URL} responded after ${waited}s — bridge is up"
    print -r -- "$body" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin), indent=2))' 2>/dev/null || print -r -- "$body"

    if should_verify_hub_routes; then
      if [[ -f "${0:A:h}/snippets/restart_mcptools_verify.block.zsh" ]]; then
        log "auto: verify hub MCP routes (initialize + tools/list on /mcp)…"
        if ! zsh "${0:A:h}/snippets/restart_mcptools_verify.block.zsh"; then
          log "WARNING: verify_hub_routes_with_mcp reported one or more failures — /status is still ok; check child servers / hub JSON keys"
        fi
      else
        log "optional hub route verification skipped: verify script missing (mcp-proxy-hub/snippets/restart_mcptools_verify.block.zsh)"
      fi
    fi
    exit 0
  fi
  sleep 1
  waited=$((SECONDS - start_seconds))
  if (( waited >= next_notice )); then
    log "still waiting for ${STATUS_URL} (${waited}s / ${MAX_WAIT_SEC}s)…"
    next_notice=$((next_notice + 5))
  fi
done

log "ERROR: no response from ${STATUS_URL} within ${MAX_WAIT_SEC}s"
emit_http_timeout_snapshot
log "check: launchctl print ${SERVICE_TARGET}"
log "check: tail ~/Library/Logs/com.peterkjackson.mcp-proxy-bridge/mcp-proxy-hub.log"
exit 1
