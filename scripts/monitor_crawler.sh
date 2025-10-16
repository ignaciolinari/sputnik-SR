#!/usr/bin/env bash
set -euo pipefail

DB_PATH=${1:-data/sputnik.db}
LOG_PATH=${2:-logs/crawler-full.log}
REFRESH_INTERVAL=${REFRESH_INTERVAL:-5}

# Crear directorio de logs si no existe
mkdir -p "$(dirname "${LOG_PATH}")"

# Verificar si sqlite3 está disponible
if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "sqlite3 is required on PATH" >&2
  exit 1
fi

trap 'echo; echo "Stopping monitor"; exit 0' INT TERM

# Función para mostrar el menú principal
show_menu() {
  clear
  printf "=== Sputnik Crawler Monitor ===\n"
  printf "Database: %s" "${DB_PATH}"
  if [[ -f "${DB_PATH}" ]]; then
    printf " ✅\n"
  else
    printf " ❌\n"
  fi

  printf "Log file: %s" "${LOG_PATH}"
  if [[ -f "${LOG_PATH}" ]]; then
    printf " ✅\n"
  else
    printf " ❌\n"
  fi
  echo

  echo "Selecciona qué información quieres ver:"
  echo "1) Ver log del crawler"
  echo "2) Ver progreso de carga de datos"
  echo "3) Ver estadísticas de la base de datos"
  echo "4) Ver estado de colas de expansión"
  echo "5) Ver todo (modo automático)"
  echo "6) Salir"
  echo
}

# Función para mostrar el log
show_log() {
  echo "=== Log del Crawler (${LOG_PATH}) ==="

  if [[ ! -f "${LOG_PATH}" ]]; then
    echo "❌ Archivo de log no encontrado."
    echo "💡 El crawler no se ha ejecutado aún o no ha generado logs."
    echo "   Ejecuta: python -m crawler --start-year 2024 --end-year 2024"
  elif [[ ! -s "${LOG_PATH}" ]]; then
    echo "📄 Archivo de log existe pero está vacío."
    echo "   El crawler podría estar ejecutándose o no ha generado logs aún."
  else
    echo "Últimas 30 líneas:"
    echo
    tail -n 30 "${LOG_PATH}"
  fi

  echo
  read -p "Presiona Enter para continuar..."
}

# Función para mostrar progreso de carga
show_progress() {
  echo "=== Progreso de Carga de Datos ==="
  echo "Estado por año en la tabla crawl_state:"
  echo

  if [[ -f "${DB_PATH}" ]]; then
    sqlite3 "${DB_PATH}" <<'SQL'
.headers on
.mode table
.width 6 12 25 20
SELECT
  year as "Año",
  status as "Estado",
  last_album_title as "Último Álbum",
  last_note as "Última Nota"
FROM crawl_state
ORDER BY year DESC;
SQL
    echo
    echo "Resumen por estado:"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers off
.mode list
SELECT '  ' || status || ': ' || COUNT(*) || ' años'
FROM crawl_state
GROUP BY status
ORDER BY COUNT(*) DESC;
SQL
  else
    echo "Base de datos no encontrada en ${DB_PATH}"
  fi

  echo
  read -p "Presiona Enter para continuar..."
}

# Función para mostrar estadísticas de la DB
show_stats() {
  echo "=== Estadísticas de la Base de Datos (${DB_PATH}) ==="
  echo

  if [[ -f "${DB_PATH}" ]]; then
    echo "Contadores principales:"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers off
.mode list
SELECT '  Releases (álbums): ' || COUNT(*) FROM releases;
SELECT '  Users (usuarios): ' || COUNT(*) FROM users;
SELECT '  Interactions (ratings): ' || COUNT(*) FROM interactions;
SELECT '  Release tracks (canciones): ' || COUNT(*) FROM release_tracks;
SELECT '  Artists (artistas): ' || COUNT(*) FROM artists;
SQL

    echo
    echo "Estadísticas de ratings:"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers off
.mode list
SELECT '  Ratings totales: ' || COUNT(*) FROM interactions;
SELECT '  Rating promedio: ' || printf('%.2f', AVG(rating)) FROM interactions;
SELECT '  Ratings por usuario (promedio): ' || printf('%.1f', CAST(COUNT(*) AS FLOAT) / COUNT(DISTINCT id_user)) FROM interactions;
SELECT '  Ratings por release (promedio): ' || printf('%.1f', CAST(COUNT(*) AS FLOAT) / COUNT(DISTINCT id_release)) FROM interactions;
SQL

    echo
    echo "Usuarios por rol:"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers off
.mode list
SELECT '  ' || COALESCE(role, 'Sin rol') || ': ' || COUNT(*)
FROM users
GROUP BY role
ORDER BY COUNT(*) DESC;
SQL

    echo
    echo "Distribución de ratings:"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers off
.mode list
SELECT '  Rating ' || CAST(ROUND(rating) AS INTEGER) || ': ' || COUNT(*)
FROM interactions
GROUP BY ROUND(rating)
ORDER BY ROUND(rating) DESC;
SQL
  else
    echo "Base de datos no encontrada en ${DB_PATH}"
  fi

  echo
  read -p "Presiona Enter para continuar..."
}

# Función para mostrar procesos activos
show_processes() {
  echo "Procesos activos del crawler:"
  if ! pgrep -fl "python -m crawler" >/dev/null 2>&1 && \
     ! pgrep -fl "python -m crawler.user_expander" >/dev/null 2>&1 && \
     ! pgrep -fl "python -m crawler.discography" >/dev/null 2>&1; then
    echo "  (ningún proceso del crawler detectado)"
  else
    pgrep -fl "python -m crawler" | sed 's/^/  /' || true
    pgrep -fl "python -m crawler.user_expander" | sed 's/^/  /' || true
    pgrep -fl "python -m crawler.discography" | sed 's/^/  /' || true
  fi
  echo
}

# Función para mostrar estado de colas de expansión
show_queues() {
  echo "=== Estado de Colas de Expansión ==="
  echo "Estado de usuarios en cola:"
  echo

  if [[ -f "${DB_PATH}" ]]; then
    sqlite3 "${DB_PATH}" <<'SQL'
.headers on
.mode table
.width 10 12 10
SELECT status as "Estado", priority as "Prioridad", COUNT(*) as "Cantidad"
FROM crawl_users
GROUP BY status, priority
ORDER BY status, priority DESC;
SQL

    echo
    echo "Estado de artistas en cola:"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers on
.mode table
.width 12 10
SELECT status as "Estado", COUNT(*) as "Cantidad"
FROM crawl_artists
GROUP BY status
ORDER BY status;
SQL

    echo
    echo "Estado de releases en cola:"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers on
.mode table
.width 12 10
SELECT status as "Estado", COUNT(*) as "Cantidad"
FROM crawl_releases
GROUP BY status
ORDER BY status;
SQL

    echo
    echo "Resumen de colas:"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers off
.mode list
SELECT '  Usuarios totales: ' || COUNT(*) FROM crawl_users;
SELECT '  Artistas totales: ' || COUNT(*) FROM crawl_artists;
SELECT '  Releases totales: ' || COUNT(*) FROM crawl_releases;
SELECT '  Usuarios pendientes: ' || COUNT(*) FROM crawl_users WHERE status = 'pending';
SELECT '  Artistas pendientes: ' || COUNT(*) FROM crawl_artists WHERE status = 'pending';
SQL
  else
    echo "Base de datos no encontrada en ${DB_PATH}"
  fi

  echo
  read -p "Presiona Enter para continuar..."
}

# Función para el modo automático (como el original)
auto_mode() {
  while true; do
    clear
    printf "=== Sputnik crawler monitor (Modo Automático) ===\n"
    printf "Timestamp: %s\n\n" "$(date)"

    show_processes

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
SELECT '  crawl_users=' || COUNT(*) FROM crawl_users;
SELECT '  crawl_artists=' || COUNT(*) FROM crawl_artists;
SELECT '  crawl_releases=' || COUNT(*) FROM crawl_releases;
SQL
    else
      echo "Database file not found at ${DB_PATH}"
    fi

    echo
    printf "Refreshing again in %s seconds (Ctrl+C to stop)\n" "${REFRESH_INTERVAL}"
    sleep "${REFRESH_INTERVAL}"
  done
}

# Loop principal del menú
while true; do
  show_menu
  read -p "Elige una opción (1-6): " choice
  echo

  case $choice in
    1)
      show_log
      ;;
    2)
      show_progress
      ;;
    3)
      show_stats
      ;;
    4)
      show_queues
      ;;
    5)
      auto_mode
      ;;
    6)
      echo "¡Hasta luego!"
      exit 0
      ;;
    *)
      echo "Opción inválida. Presiona Enter para continuar..."
      read
      ;;
  esac
done
