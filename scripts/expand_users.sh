#!/usr/bin/env bash
set -euo pipefail

# Expansión de usuarios (fase 3): consume crawl_users y trae ratings públicos (uservote.php, sin fechas).
# Uso básico: scripts/expand_users.sh [flags extra]
# Flags por defecto:
#   --batch-size 25
#   --max-rating-pages none (for all pages)
#   --log-level INFO
# Ejemplo rápido: scripts/expand_users.sh --batch-size 40 --max-rating-pages 5 --log-level DEBUG
# Variables opcionales: DB_PATH, SCHEMA_PATH

DB_PATH=${DB_PATH:-data/sputnik.db}
SCHEMA_PATH=${SCHEMA_PATH:-data/schema.sql}

python -m crawler.user_expander \
    --db "${DB_PATH}" \
    --schema "${SCHEMA_PATH}" \
    --batch-size 25 \
    --max-rating-pages none \
    --log-level INFO \
    "$@"
