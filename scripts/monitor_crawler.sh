#!/usr/bin/env bash
set -euo pipefail

DB_PATH=${1:-data/sputnik.db}
LOG_PATH=${2:-logs/crawler-full.log}
LOG_DIR=${LOG_DIR:-logs}
SEED_LOG=${SEED_LOG:-${LOG_DIR}/seed_charts_latest.log}
DISC_LOG=${DISC_LOG:-${LOG_DIR}/expand_discographies_latest.log}
USERS_LOG=${USERS_LOG:-${LOG_DIR}/expand_users_latest.log}
REFRESH_INTERVAL=${REFRESH_INTERVAL:-30}

# Crear directorio de logs si no existe
mkdir -p "$(dirname "${LOG_PATH}")"

# Verificar si sqlite3 está disponible
if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "sqlite3 is required on PATH" >&2
  exit 1
fi

trap 'echo; echo "Stopping monitor"; exit 0' INT TERM

# Helper to summarize log files
print_log_summary() {
  local label=$1
  local path=$2

  echo "${label}:"
  if [[ -f "${path}" ]]; then
    echo "  Archivo: ${path}"
    echo "  Últimas 5 líneas:"
    tail -n 5 "${path}" | sed 's/^/    /'
  else
    echo "  Log no encontrado."
  fi
  echo
}

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
  echo "2) Ver progreso de fase 1 (seed most popular charts)"
  echo "3) Ver estadísticas de la base de datos"
  echo "4) Ver estado de colas de expansión"
  echo "5) Ver resumen del pipeline"
  echo "6) Ver todo (actualización automática)"
  echo "7) Ver errores recientes"
  echo "8) Salir"
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

# Función para mostrar errores recientes en las colas de trabajo
show_recent_errors() {
  echo "=== Errores recientes en colas ==="
  echo

  if [[ -f "${DB_PATH}" ]]; then
    echo "Artistas con error (últimos 10):"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers on
.mode table
.width 8 26 10 20 50
SELECT
  ca.id_artist AS "ID",
  COALESCE(a.name, '-') AS "Artista",
  ca.attempts AS "Intentos",
  COALESCE(ca.updated_at, '-') AS "Actualizado",
  substr(COALESCE(ca.last_error, 'Sin detalle'), 1, 80) AS "Último error"
FROM crawl_artists ca
LEFT JOIN artists a ON a.id_artist = ca.id_artist
WHERE ca.status = 'error'
ORDER BY ca.updated_at DESC
LIMIT 10;
SQL

    echo
    echo "Usuarios con error (últimos 10):"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers on
.mode table
.width 18 10 20 50
SELECT
  id_user AS "Usuario",
  attempts AS "Intentos",
  COALESCE(updated_at, '-') AS "Actualizado",
  substr(COALESCE(last_error, 'Sin detalle'), 1, 80) AS "Último error"
FROM crawl_users
WHERE status = 'error'
ORDER BY updated_at DESC
LIMIT 10;
SQL

    echo
    echo "Releases con error (últimos 10):"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers on
.mode table
.width 8 32 20 50
SELECT
  cr.id_release AS "ID",
  COALESCE(r.title, '-') AS "Título",
  COALESCE(cr.updated_at, '-') AS "Actualizado",
  substr(COALESCE(cr.last_error, 'Sin detalle'), 1, 80) AS "Último error"
FROM crawl_releases cr
LEFT JOIN releases r ON r.id_release = cr.id_release
WHERE cr.status = 'error'
ORDER BY cr.updated_at DESC
LIMIT 10;
SQL
  else
    echo "Base de datos no encontrada en ${DB_PATH}"
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

    echo
    echo "Resumen general de años:"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers off
.mode list
SELECT '  Años totales: ' || COUNT(*) FROM crawl_state;
SELECT '  Años completados: ' || SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) FROM crawl_state;
SELECT '  Años en progreso: ' || SUM(CASE WHEN status IN ('pending','processing','seeded') THEN 1 ELSE 0 END) FROM crawl_state;
SELECT '  Años con error: ' || SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) FROM crawl_state;
SELECT '  Porcentaje completado: ' || printf('%.1f%%', CASE WHEN COUNT(*) = 0 THEN 0 ELSE (SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) * 100.0) / COUNT(*) END) FROM crawl_state;
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
SELECT '  Géneros: ' || COUNT(*) FROM genres;
SELECT '  Artist-Géneros (asignaciones): ' || COUNT(*) FROM artist_genres;
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
    echo "Estadísticas de géneros:"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers off
.mode list
SELECT '  Géneros únicos: ' || COUNT(*) FROM genres;
SELECT '  Artistas con géneros: ' || COUNT(DISTINCT id_artist) FROM artist_genres;
SELECT '  Artistas SIN géneros (terminados): ' || COUNT(DISTINCT a.id_artist)
  FROM artists a
  LEFT JOIN artist_genres ag ON ag.id_artist = a.id_artist
  WHERE ag.id_genre IS NULL;
SELECT '  Géneros por artista (promedio): ' || printf('%.2f', CAST(COUNT(*) AS FLOAT) / COUNT(DISTINCT id_artist))
  FROM artist_genres;
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

    echo
    echo "Top 5 releases por cantidad de ratings:"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers on
.mode table
.width 30 25 10 10
SELECT
  r.title AS "Release",
  COALESCE(a.name, '-') AS "Artista",
  r.ratings_count AS "Ratings",
  printf('%.2f', r.avg_rating) AS "Promedio"
FROM releases r
LEFT JOIN artists a ON a.id_artist = r.artist_id
WHERE r.ratings_count IS NOT NULL
ORDER BY r.ratings_count DESC
LIMIT 5;
SQL

    echo
    echo "Últimos 5 releases insertados:"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers on
.mode table
.width 6 30 25 6
SELECT
  r.id_release AS "ID",
  r.title AS "Título",
  COALESCE(a.name, '-') AS "Artista",
  COALESCE(r.release_year, '-') AS "Año"
FROM releases r
LEFT JOIN artists a ON a.id_artist = r.artist_id
ORDER BY r.id_release DESC
LIMIT 5;
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

    echo
    echo "Artistas pendientes destacados (hasta 10):"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers on
.mode table
.width 8 26 10 10 40
SELECT
  ca.id_artist AS "ID",
  COALESCE(a.name, '-') AS "Artista",
  ca.status AS "Estado",
  ca.attempts AS "Intentos",
  substr(COALESCE(ca.last_error, 'Sin errores'), 1, 60) AS "Último error"
FROM crawl_artists ca
LEFT JOIN artists a ON a.id_artist = ca.id_artist
WHERE ca.status IN ('pending','processing','error')
ORDER BY CASE ca.status WHEN 'error' THEN 0 WHEN 'processing' THEN 1 ELSE 2 END, ca.updated_at ASC
LIMIT 10;
SQL

    echo
    echo "Usuarios pendientes destacados (hasta 10):"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers on
.mode table
.width 18 10 10 40
SELECT
  id_user AS "Usuario",
  status AS "Estado",
  attempts AS "Intentos",
  substr(COALESCE(last_error, 'Sin errores'), 1, 60) AS "Último error"
FROM crawl_users
WHERE status IN ('pending','processing','error')
ORDER BY CASE status WHEN 'error' THEN 0 WHEN 'processing' THEN 1 ELSE 2 END, updated_at ASC
LIMIT 10;
SQL
  else
    echo "Base de datos no encontrada en ${DB_PATH}"
  fi

  echo
  read -p "Presiona Enter para continuar..."
}

show_pipeline_overview() {
  echo "=== Resumen del Pipeline ==="
  echo
  show_processes

  if [[ -f "${DB_PATH}" ]]; then
    echo "Fase 1 – Seed charts:"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers off
.mode list
SELECT '  Años totales: ' || COUNT(*) FROM crawl_state;
SELECT '  Años completados: ' || SUM(CASE WHEN status IN ('DONE','done') THEN 1 ELSE 0 END) FROM crawl_state;
SELECT '  Años con trabajo pendiente: ' || SUM(CASE WHEN status IN ('PENDING','IN_PROGRESS','pending','seeded','processing') THEN 1 ELSE 0 END) FROM crawl_state;
SELECT '  Años con error: ' || SUM(CASE WHEN status IN ('ERROR','error') THEN 1 ELSE 0 END) FROM crawl_state;
SQL
    echo
    echo "Fase 2 – Discografías:"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers off
.mode list
SELECT '  Artistas pendientes: ' || COUNT(*) FROM crawl_artists WHERE status = 'pending';
SELECT '  Artistas en proceso: ' || COUNT(*) FROM crawl_artists WHERE status = 'processing';
SELECT '  Artistas con error: ' || COUNT(*) FROM crawl_artists WHERE status = 'error';
SELECT '  Artistas completados: ' || COUNT(*) FROM crawl_artists WHERE status = 'done';
SELECT '  Artistas con géneros: ' || COUNT(DISTINCT id_artist) FROM artist_genres;
SELECT '  Artistas sin géneros (done): ' || COUNT(DISTINCT a.id_artist)
  FROM artists a
  LEFT JOIN crawl_artists ca ON ca.id_artist = a.id_artist
  LEFT JOIN artist_genres ag ON ag.id_artist = a.id_artist
  WHERE COALESCE(ca.status, 'pending') = 'done' AND ag.id_genre IS NULL;
SELECT '  Releases pendientes: ' || COUNT(*) FROM crawl_releases WHERE status IN ('pending','seeded');
SELECT '  Releases en proceso: ' || COUNT(*) FROM crawl_releases WHERE status = 'processing';
SELECT '  Releases con error: ' || COUNT(*) FROM crawl_releases WHERE status = 'error';
SQL
    echo
    echo "Fase 3 – Expansión de usuarios:"
    sqlite3 "${DB_PATH}" <<'SQL'
.headers off
.mode list
SELECT '  Usuarios pendientes: ' || COUNT(*) FROM crawl_users WHERE status = 'pending';
SELECT '  Usuarios en proceso: ' || COUNT(*) FROM crawl_users WHERE status = 'processing';
SELECT '  Usuarios completados: ' || COUNT(*) FROM crawl_users WHERE status = 'done';
SELECT '  Usuarios con error: ' || COUNT(*) FROM crawl_users WHERE status = 'error';
SELECT '  Ratings totales: ' || COUNT(*) FROM interactions;
SQL
  else
    echo "Base de datos no encontrada en ${DB_PATH}"
  fi

  echo
  echo "Logs recientes:"
  print_log_summary "  Fase 1 (seed_charts)" "${SEED_LOG}"
  print_log_summary "  Fase 2 (expand_discographies)" "${DISC_LOG}"
  print_log_summary "  Fase 3 (expand_users)" "${USERS_LOG}"
  print_log_summary "  Crawler general" "${LOG_PATH}"

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
SELECT '  artists=' || COUNT(*) FROM artists;
SELECT '  genres=' || COUNT(*) FROM genres;
SELECT '  artist_genres=' || COUNT(*) FROM artist_genres;
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
  read -p "Elige una opción (1-7): " choice
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
      show_pipeline_overview
      ;;
    6)
      auto_mode
      ;;
    7)
      show_recent_errors
      ;;
    8)
      echo "¡Hasta luego!"
      exit 0
      ;;
    *)
      echo "Opción inválida. Presiona Enter para continuar..."
      read
      ;;
  esac
done
