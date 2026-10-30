#!/usr/bin/env bash
# Restart `virtual-self archive flow` when it has exited but Takeout/archive work remains.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
LOG="${VSM_ARCHIVE_FLOW_LOG:-/tmp/archive-flow.log}"
POLL_SEC="${VSM_ARCHIVE_FLOW_WATCHDOG_POLL_SECONDS:-300}"
mkdir -p "$(dirname "$LOG")"
touch "$LOG" 2>/dev/null || true

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export VSM_DATABASE_URL="${VSM_DATABASE_URL:-}"

log() {
  printf '[%s] %s\n' "$(date -Is)" "$*" >>"$LOG"
}

PENDING_STATES="'discovered','stable','downloaded','unpacked','parsed','analyzed','promoted'"

_psql() {
  local sql="$1"
  if docker exec virtual_self_postgres pg_isready -U virtual_self -d virtual_self >/dev/null 2>&1; then
    docker exec virtual_self_postgres psql -U virtual_self -d virtual_self -t -A -c "$sql"
  elif [[ -n "$VSM_DATABASE_URL" ]] && command -v psql >/dev/null 2>&1; then
    psql "$VSM_DATABASE_URL" -t -A -c "$sql"
  else
    echo "0"
    return 1
  fi
}

pending_ledger_count() {
  _psql "SELECT COUNT(*)::int FROM archive_files WHERE state IN (${PENDING_STATES});" 2>/dev/null | tr -d '[:space:]' || echo "0"
}

inbox_zip_pending() {
  local root="${VSM_ARCHIVE_DATA_ROOT:-/data/archive}"
  local inbox="${root}/inbox/google"
  [[ -d "$inbox" ]] || return 1
  local name
  for zip in "$inbox"/*.zip; do
    [[ -f "$zip" ]] || continue
    name="$(basename "$zip")"
    local st
    st="$(_psql "SELECT state FROM archive_files WHERE file_name = '${name//\'/''}' LIMIT 1;" 2>/dev/null | tr -d '[:space:]')"
    if [[ -z "$st" || "$st" != "done" ]]; then
      return 0
    fi
  done
  return 1
}

remote_takeout_pending() {
  local cfg="${VSM_ARCHIVE_RCLONE_CONFIG:-}"
  local remote="${VSM_ARCHIVE_RCLONE_REMOTE:-}"
  local path="${VSM_ARCHIVE_TAKEOUT_PATH:-Takeout}"
  [[ -n "$cfg" && -f "$cfg" && -n "$remote" ]] || return 1
  command -v rclone >/dev/null 2>&1 || return 1
  command -v jq >/dev/null 2>&1 || return 1

  local names
  names="$(rclone lsjson "${remote}:${path}" --config "$cfg" 2>/dev/null | jq -r '.[] | select(.IsDir != true) | select(.Name | endswith(".zip")) | .Name' || true)"
  [[ -n "$names" ]] || return 1

  local name
  while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    local st
    st="$(_psql "SELECT COALESCE((SELECT state FROM archive_files WHERE file_name = '${name//\'/''}' LIMIT 1), 'missing');" 2>/dev/null | tr -d '[:space:]')"
    if [[ "$st" != "done" ]]; then
      return 0
    fi
  done <<<"$names"
  return 1
}

work_remains() {
  local n
  n="$(pending_ledger_count)"
  if [[ "$n" =~ ^[0-9]+$ ]] && (( n > 0 )); then
    return 0
  fi
  inbox_zip_pending && return 0
  remote_takeout_pending && return 0
  return 1
}

flow_running() {
  if pgrep -f '[v]irtual-self archive flow' >/dev/null 2>&1; then
    return 0
  fi
  if docker ps --format '{{.Command}}' 2>/dev/null | grep -qE 'archive flow'; then
    return 0
  fi
  return 1
}

run_archive_flow() {
  if [[ -f /.dockerenv || "${VSM_ARCHIVE_FLOW_IN_CONTAINER:-}" == "1" ]]; then
    virtual-self archive flow
    return $?
  fi
  if [[ -x "${ROOT}/.venv/bin/virtual-self" ]]; then
    "${ROOT}/.venv/bin/virtual-self" archive flow
    return $?
  fi
  if docker compose ps --status running postgres >/dev/null 2>&1; then
    docker compose --profile archive run --rm --no-deps archive-worker virtual-self archive flow
    return $?
  fi
  log "ERROR: no virtual-self archive flow runner (venv or docker compose)"
  return 1
}

log "archive-flow watchdog started (poll=${POLL_SEC}s, log=${LOG})"

while true; do
  if flow_running; then
    log "archive flow already running; sleeping ${POLL_SEC}s"
    sleep "$POLL_SEC"
    continue
  fi

  if work_remains; then
    log "pending archive work; starting archive flow"
    if run_archive_flow >>"$LOG" 2>&1; then
      log "archive flow finished ok"
    else
      log "archive flow exited with error $?"
    fi
  else
    log "idle (no pending ledger, inbox, or Drive takeout zips)"
  fi

  sleep "$POLL_SEC"
done
