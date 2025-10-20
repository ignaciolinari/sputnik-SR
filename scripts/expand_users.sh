#!/usr/bin/env bash
set -euo pipefail

# Expansión de usuarios (fase 3): consume crawl_users y trae ratings públicos (uservote.php, sin fechas).
# Uso básico: scripts/expand_users.sh [flags extra]
# Flags por defecto:
#   --batch-size 25
#   --min-interval 0.30
#   --burst-size 4
#   --max-rating-pages none (for all pages)
#   --log-level DEBUG
# Ejemplo rápido: scripts/expand_users.sh --batch-size 40 --max-rating-pages 5 --log-level DEBUG
# Variables opcionales: DB_PATH, SCHEMA_PATH

DB_PATH=${DB_PATH:-data/sputnik.db}
SCHEMA_PATH=${SCHEMA_PATH:-data/schema.sql}
BATCH_SIZE=${BATCH_SIZE:-25}
MIN_INTERVAL=${MIN_INTERVAL:-0.30}
BURST_SIZE=${BURST_SIZE:-4}
MAX_RATING_PAGES=${MAX_RATING_PAGES:-none}
LOG_LEVEL=${LOG_LEVEL:-DEBUG}
TIMEOUT=${TIMEOUT:-20}
MAX_RETRIES=${MAX_RETRIES:-3}
SKIP_PROFILES=${SKIP_PROFILES:-0}

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DEFAULT_PYTHON="${SCRIPT_DIR}/../.venv/bin/python"
PYTHON_BIN=${PYTHON_BIN:-${DEFAULT_PYTHON}}

if [[ ! -x "${PYTHON_BIN}" ]]; then
    PYTHON_BIN=python
fi

if [[ -z "${SPUTNIK_ALLOW_INSECURE_SSL+x}" ]]; then
    # Probe TLS once to see if the remote certificate validates; fall back to insecure mode on errors.
    if "${PYTHON_BIN}" - <<'PY'
import sys
import requests

try:
    response = requests.get("https://www.sputnikmusic.com", timeout=5)
    response.raise_for_status()
except requests.exceptions.SSLError:
    sys.exit(1)
except Exception:
    sys.exit(2)
sys.exit(0)
PY
    then
        ALLOW_INSECURE=0
    else
        probe_status=$?
        if [[ ${probe_status} -ne 1 ]]; then
            echo "warning: TLS probe failed (status ${probe_status}); defaulting to insecure HTTPS" >&2
        fi
        ALLOW_INSECURE=1
    fi
else
    ALLOW_INSECURE=${SPUTNIK_ALLOW_INSECURE_SSL}
fi

if [[ "${ALLOW_INSECURE}" =~ ^(0|false|FALSE)$ ]]; then
    CA_BUNDLE="$(${PYTHON_BIN} - <<'PY'
import certifi
print(certifi.where(), end="")
PY
)"
    export SSL_CERT_FILE="${SSL_CERT_FILE:-${CA_BUNDLE}}"
    export REQUESTS_CA_BUNDLE="${REQUESTS_CA_BUNDLE:-${CA_BUNDLE}}"
else
    unset SSL_CERT_FILE
    unset REQUESTS_CA_BUNDLE
fi
export SPUTNIK_ALLOW_INSECURE_SSL="${ALLOW_INSECURE}"

ARGS=(
    --db "${DB_PATH}"
    --schema "${SCHEMA_PATH}"
    --batch-size "${BATCH_SIZE}"
    --timeout "${TIMEOUT}"
    --max-retries "${MAX_RETRIES}"
    --min-interval "${MIN_INTERVAL}"
    --burst-size "${BURST_SIZE}"
    --log-level "${LOG_LEVEL}"
)

if [[ -n "${MAX_RATING_PAGES}" ]]; then
    ARGS+=(--max-rating-pages "${MAX_RATING_PAGES}")
fi

if [[ "${SKIP_PROFILES}" =~ ^(1|true|TRUE)$ ]]; then
    ARGS+=(--skip-profiles)
fi

"${PYTHON_BIN}" -m crawler.user_expander "${ARGS[@]}" "$@"
