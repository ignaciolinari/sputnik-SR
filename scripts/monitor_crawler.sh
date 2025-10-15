#!/usr/bin/env bash
set -euo pipefail

DB_PATH=${1:-data/sputnik.db}
LOG_PATH=${2:-logs/crawler-full.log}
REFRESH_INTERVAL=${REFRESH_INTERVAL:-5}

if [[ ! -f "${LOG_PATH}" ]]; then
  echo "Log file not found at ${LOG_PATH}" >&2
  exit 1
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "sqlite3 is required on PATH" >&2
  exit 1
fi

trap 'echo; echo "Stopping monitor"; exit 0' INT TERM

while true; do
  clear
  printf "=== Sputnik crawler monitor ===\n"
  printf "Timestamp: %s\n\n" "$(date)"

  echo "Active crawler processes (PID command):"
  if ! pgrep -fl "python -m crawler" >/dev/null 2>&1; then
    echo "  (no crawler process detected)"
  else
    pgrep -fl "python -m crawler" | sed 's/^/  /'
  fi
  echo

  echo "Log tail (${LOG_PATH}):"
  tail -n 20 "${LOG_PATH}" || true
  echo

  if [[ -f "${DB_PATH}" ]]; then
    echo "Database counters (${DB_PATH}):"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers off
.mode list
SELECT '  releases=' || COUNT(*) FROM releases;
SELECT '  users=' || COUNT(*) FROM users;
SELECT '  interactions=' || COUNT(*) FROM interactions;
SELECT '  release_tracks=' || COUNT(*) FROM release_tracks;
SQL
  else
    echo "Database file not found at ${DB_PATH}"
  fi

  echo
  printf "Refreshing again in %s seconds (Ctrl+C to stop)\n" "${REFRESH_INTERVAL}"
  sleep "${REFRESH_INTERVAL}"
done
