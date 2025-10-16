#!/usr/bin/env bash
set -euo pipefail

# Expansión de discografías (fase 3): procesa crawl_artists y suma releases/tracklists/soundoffs.
# Uso básico: scripts/expand_discographies.sh [flags extra]
# Flags por defecto:
#   --batch-size 25
#   --max-soundoffs 100
#   --log-level INFO
# Ejemplo rápido: scripts/expand_discographies.sh --batch-size 15 --max-soundoffs 60 --log-level DEBUG
# Variables opcionales: DB_PATH, SCHEMA_PATH

DB_PATH=${DB_PATH:-data/sputnik.db}
SCHEMA_PATH=${SCHEMA_PATH:-data/schema.sql}

python -m crawler.discography \
    --db "${DB_PATH}" \
    --schema "${SCHEMA_PATH}" \
    --batch-size 25 \
    --max-soundoffs 100 \
    --log-level INFO \
    "$@"
