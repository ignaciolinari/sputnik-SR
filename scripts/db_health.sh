#!/usr/bin/env bash
# Wrapper para ejecutar maintenance/db_health.py con feedback visual.

set -euo pipefail

show_help() {
  cat <<'EOF'
Uso: maintenance/db_health.sh [opciones]

Opciones soportadas (además de las propias del script Python):
  --db RUTA            Ruta a la base de datos SQLite (default: data/sputnik.db)
  --python BIN         Ejecutable de Python a utilizar (default: python)
  --no-spinner         Desactiva la animación de progreso
  --help               Muestra esta ayuda

Cualquier otra opción se pasa tal cual al script Python maintenance/db_health.py.
Ejemplos:
  maintenance/db_health.sh --db data/sputnik.db
  maintenance/db_health.sh --fix users.error.timeout --apply
  maintenance/db_health.sh --no-spinner --format json
EOF
}

DB_PATH="data/sputnik.db"
PYTHON_BIN="${PYTHON_BIN:-python}"
USE_SPINNER=1

declare -a EXTRA_ARGS=()

EXTRA_ARGS=()
set +u # Permitir variables no inicializadas temporalmente

while [[ $# -gt 0 ]]; do
  case "$1" in
    --db)
      [[ $# -ge 2 ]] || { echo "Error: --db requiere un valor" >&2; exit 1; }
      DB_PATH="$2"
      shift 2
      ;;
    --python)
      [[ $# -ge 2 ]] || { echo "Error: --python requiere un valor" >&2; exit 1; }
      PYTHON_BIN="$2"
      shift 2
      ;;
    --no-spinner)
      USE_SPINNER=0
      shift
      ;;
    --help)
      show_help
      exit 0
      ;;
    *)
      EXTRA_ARGS+=("$1")
      shift
      ;;
  esac
done


# Permitir correr desde scripts/ o raíz
if [[ -f "maintenance/db_health.py" ]]; then
  SCRIPT_PATH="maintenance/db_health.py"
elif [[ -f "../maintenance/db_health.py" ]]; then
  SCRIPT_PATH="../maintenance/db_health.py"
else
  echo "Error: no se encontró maintenance/db_health.py" >&2
  exit 1
fi

if [[ ! -f "$SCRIPT_PATH" ]]; then
  echo "Error: no se encontró $SCRIPT_PATH" >&2
  exit 1
fi

if [[ ! -f "$DB_PATH" ]]; then
  echo "Error: base de datos no encontrada en $DB_PATH" >&2
  exit 1
fi

export PYTHONUNBUFFERED=1

CMD=("${PYTHON_BIN}" "$SCRIPT_PATH" "--db" "$DB_PATH" "${EXTRA_ARGS[@]}")

start_time=$(date +%s)
start_human=$(date '+%Y-%m-%d %H:%M:%S')
echo ">> Iniciando chequeo de salud a las $start_human"
echo ">> Ejecutando: ${CMD[*]}"

spinner() {
  local pid=$1
  local frames='|/-\'
  local delay=0.1
  local i=0
  while kill -0 "$pid" 2>/dev/null; do
    printf '\r[%c] Analizando base de datos...' "${frames:i%${#frames}:1}" >&2
    sleep "$delay"
    ((i++))
  done
  printf '\r' >&2
}

if [[ "$USE_SPINNER" -eq 1 ]]; then
  "${CMD[@]}" &
  main_pid=$!
  spinner "$main_pid" &
  spinner_pid=$!
  trap 'kill "$spinner_pid" "$main_pid" 2>/dev/null || true' INT TERM
  wait "$main_pid"
  status=$?
  kill "$spinner_pid" 2>/dev/null || true
  wait "$spinner_pid" 2>/dev/null || true
else
  "${CMD[@]}"
  status=$?
fi

end_time=$(date +%s)
elapsed=$((end_time - start_time))
minutes=$((elapsed / 60))
seconds=$((elapsed % 60))

if [[ $status -eq 0 ]]; then
  echo ">> Chequeo finalizado correctamente en ${minutes}m ${seconds}s"
else
  echo ">> Chequeo finalizado con errores (codigo $status) en ${minutes}m ${seconds}s" >&2
fi

exit "$status"
