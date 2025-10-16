#!/usr/bin/env bash
set -euo pipefail

# Semilla inicial (fase 1): charts + soundoffs visibles para poblar rápido.
# Uso básico: scripts/seed_charts.sh [ANIO_INICIO] [ANIO_FIN] [flags extra]
# Flags de ejemplo:
#   --skip-tracklists
#   --skip-user-profiles
#   --user-queue-priority 5
#   --max-soundoffs 50 (en caso de querer limitar la busqueda, quitar para explorar todos los soundoffs)
#   --log-level INFO
# Ejemplo rápido: scripts/seed_charts.sh 2010 2024 --max-soundoffs 75 --log-level DEBUG
# Variables opcionales: START_YEAR_OVERRIDE, END_YEAR_OVERRIDE, DB_PATH, SCHEMA_PATH

START_YEAR=${START_YEAR_OVERRIDE:-${1:-1960}}
END_YEAR=${END_YEAR_OVERRIDE:-${2:-2024}}

if [[ $# -ge 2 ]]; then
    shift 2
else
    shift $(( $# ))
fi

DB_PATH=${DB_PATH:-data/sputnik.db}
SCHEMA_PATH=${SCHEMA_PATH:-data/schema.sql}

python -m crawler \
    --start-year "${START_YEAR}" \
    --end-year "${END_YEAR}" \
    --db "${DB_PATH}" \
    --schema "${SCHEMA_PATH}" \
    --skip-tracklists \
    --skip-user-profiles \
    --user-queue-priority 5 \
    --log-level DEBUG \
    "$@"
