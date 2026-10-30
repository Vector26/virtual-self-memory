#!/usr/bin/env bash
# Track 1: enrich People + Journal corpus areas, then sync memories (import-my-life).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-virtual-self-memory}"

if [[ ! -f .env ]]; then
  cp -n .env.example .env
fi
set -a
# shellcheck disable=SC1091
source .env
set +a
: "${VSM_MY_LIFE_ROOT:?set VSM_MY_LIFE_ROOT in $ROOT/.env}"
: "${VSM_BOOTSTRAP_TOKEN:?set VSM_BOOTSTRAP_TOKEN in $ROOT/.env}"

REBUILD=1
ENRICH_FULL=0
for arg in "$@"; do
  case "$arg" in
    --no-rebuild) REBUILD=0 ;;
    --full) ENRICH_FULL=1 ;;
    -h|--help)
      cat <<'EOF'
Usage: backfill-people-journal.sh [--no-rebuild] [--full]

  Rebuilds the api image (unless --no-rebuild), starts postgres+api+worker,
  runs enrich-my-life for People and Journal (--only-prefix), then
  import-my-life --apply for memory refresh + index.

  COMPOSE_PROJECT_NAME defaults to virtual-self-memory.
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 2
      ;;
  esac
done

COMPOSE=(docker compose)
ENRICH_FLAGS=()
if [[ "$ENRICH_FULL" -eq 1 ]]; then
  ENRICH_FLAGS+=(--full)
fi

_stack_running() {
  "${COMPOSE[@]}" ps --status running --services 2>/dev/null | grep -qx postgres
}

_ensure_stack() {
  if ! _stack_running; then
    "${COMPOSE[@]}" up -d postgres api worker
    for _ in $(seq 1 60); do
      _stack_running && break
      sleep 1
    done
    if ! _stack_running; then
      echo "postgres did not become ready" >&2
      exit 1
    fi
  fi
}

if [[ "$REBUILD" -eq 1 ]]; then
  echo "==> docker compose build api worker"
  "${COMPOSE[@]}" build api worker
fi

_ensure_stack

echo "==> migrate"
"${COMPOSE[@]}" --profile tools run --rm migrate

for area in People Journal; do
  echo "==> enrich-my-life --only-prefix ${area}"
  "${COMPOSE[@]}" exec -T api virtual-self enrich-my-life "${ENRICH_FLAGS[@]}" --only-prefix "$area"
done

echo "==> import-my-life --apply (memory refresh, personality bootstrap, reindex)"
"${COMPOSE[@]}" exec -T api virtual-self import-my-life --apply

echo "==> backfill complete"
