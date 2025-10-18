#!/usr/bin/env bash
set -euo pipefail

# Expansión de discografías (fase 2): procesa crawl_artists y suma releases/tracklists/soundoffs.
# Uso básico: scripts/expand_discographies.sh [flags extra]
# Flags por defecto:
#   --batch-size 25
#   --max-soundoffs 100 (eliminar para busqueda completa)
#   --log-level INFO
# Ejemplo rápido: scripts/expand_discographies.sh --batch-size 15 --max-soundoffs 60 --log-level DEBUG
# Variables opcionales: DB_PATH, SCHEMA_PATH

DB_PATH=${DB_PATH:-data/sputnik.db}
SCHEMA_PATH=${SCHEMA_PATH:-data/schema.sql}
BATCH_SIZE=${BATCH_SIZE:-25}
MIN_INTERVAL=${MIN_INTERVAL:-0.30}
BURST_SIZE=${BURST_SIZE:-4}
MAX_SOUNDOFFS=${MAX_SOUNDOFFS:-100}
LOG_LEVEL=${LOG_LEVEL:-DEBUG}

ARGS=(
    --db "${DB_PATH}"
    --schema "${SCHEMA_PATH}"
    --batch-size "${BATCH_SIZE}"
    --min-interval "${MIN_INTERVAL}"
    --burst-size "${BURST_SIZE}"
    --log-level "${LOG_LEVEL}"
)

if [ -n "${MAX_SOUNDOFFS}" ]; then
    ARGS+=(--max-soundoffs "${MAX_SOUNDOFFS}")
fi

python -m crawler.discography "${ARGS[@]}" "$@"
