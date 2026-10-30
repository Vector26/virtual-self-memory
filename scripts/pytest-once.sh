#!/usr/bin/env bash
# Orchestrator: one pytest at a time for this repo. Agents must use this script
# (not raw `.venv/bin/pytest` or `flock -w` spin-waits); others fail fast on lock.
# Run pytest with an exclusive repo lock (fail fast if another run holds it).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCK="${ROOT}/.state/pytest.lock"
mkdir -p "${ROOT}/.state"
exec 9>"${LOCK}"
if ! flock -n 9; then
  echo "pytest already running for virtual-self-memory (lock: ${LOCK}). Do not parallelize; retry later." >&2
  exit 1
fi
cd "${ROOT}"
LAST="${ROOT}/.state/pytest-last.txt"
set +e
out="$("${ROOT}/.venv/bin/pytest" "$@" 2>&1)"
rc=$?
printf '%s\n' "${out}" | tee -a "${LAST}"
printf '\n--- exit %s at %s ---\n' "${rc}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "${LAST}"
exit "${rc}"
