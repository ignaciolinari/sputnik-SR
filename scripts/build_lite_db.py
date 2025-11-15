#!/usr/bin/env python3
"""Script para crear una base de datos lite desde la base de datos completa.

Este script crea una versión reducida de la base de datos manteniendo:
- Toda la estructura de tablas
- Todos los releases y artists (catálogo completo)
- Géneros y relaciones completas
- release_pairs y release_recommendations (necesarios para recomendaciones contextuales)
- Embeddings de releases (necesarios para NMF y Two Towers)
- Interacciones reducidas (solo las más valiosas)
- Elimina tablas menos críticas (crawl_*, lists, staff_reviews, etc.)

Objetivo: Base de datos de 80-90 MB manteniendo funcionalidad completa.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


# Añadir el directorio raíz al path para importar módulos
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.recommender import _DATA_DIR


def get_db_size_mb(db_path: Path) -> float:
    """Obtener el tamaño de la base de datos en MB."""
    if not db_path.exists():
        return 0.0
    return db_path.stat().st_size / (1024 * 1024)


def copy_schema(source: sqlite3.Connection, target: sqlite3.Connection, schema_path: Path) -> None:
    """Copiar el esquema completo desde el archivo schema.sql."""
    print("Creando estructura de tablas...")
    schema_sql = schema_path.read_text(encoding="utf-8")
    # Reemplazar PRAGMA foreign_keys = ON para mantenerlas desactivadas durante la copia
    schema_sql = schema_sql.replace(
        "PRAGMA foreign_keys = ON;", "-- PRAGMA foreign_keys = ON; -- Desactivado durante copia"
    )
    # Desactivar triggers temporalmente para evitar errores con tablas crawl_* que no copiamos
    # Los triggers intentarían insertar en tablas que no copiamos en la versión lite
    # Nota: Comentamos los triggers completos, pero las tablas crawl_* se crearán vacías (sin datos)
    import re

    # Comentar bloques completos de triggers (desde CREATE TRIGGER hasta END;)
    # Usar DOTALL para que . coincida con saltos de línea
    schema_sql = re.sub(
        r"(CREATE TRIGGER.*?END;)",
        lambda m: "\n".join(
            "-- " + line if line.strip() else line for line in m.group(1).split("\n")
        ),
        schema_sql,
        flags=re.DOTALL,
    )
    # Las vistas pueden causar problemas si las tablas no están completas, pero las mantenemos
    # ya que solo son consultas y no afectan la copia de datos
    target.executescript(schema_sql)
    # Asegurar que las foreign keys estén desactivadas
    target.execute("PRAGMA foreign_keys = OFF")
    target.commit()
    print("✓ Estructura creada")


def copy_table_data(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    table_name: str,
    where_clause: str | None = None,
    limit: int | None = None,
    batch_size: int = 10000,
) -> int:
    """Copiar datos de una tabla de source a target, procesando por lotes."""
    # Obtener estructura de la tabla
    cursor = source.execute(
        f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table_name}'"
    )
    create_sql = cursor.fetchone()
    if not create_sql or not create_sql[0]:
        return 0

    # Contar registros
    count_query = f"SELECT COUNT(*) FROM {table_name}"
    if where_clause:
        count_query += f" WHERE {where_clause}"
    cursor = source.execute(count_query)
    total = cursor.fetchone()[0]

    if total == 0:
        return 0

    # Obtener nombres de columnas
    cursor = source.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cursor.fetchall()]
    placeholders = ",".join("?" * len(columns))
    columns_str = ",".join(columns)

    insert_query = f"INSERT OR IGNORE INTO {table_name} ({columns_str}) VALUES ({placeholders})"

    # Copiar datos procesando por lotes para evitar cargar todo en memoria
    select_query = f"SELECT * FROM {table_name}"
    if where_clause:
        select_query += f" WHERE {where_clause}"
    if limit:
        select_query += f" LIMIT {limit}"

    cursor = source.execute(select_query)
    total_copied = 0

    while True:
        batch = cursor.fetchmany(batch_size)
        if not batch:
            break

        target.executemany(insert_query, batch)
        total_copied += len(batch)

        # Commit periódico para evitar transacciones muy grandes
        if total_copied % (batch_size * 10) == 0:
            target.commit()

    target.commit()
    return total_copied


def copy_essential_tables(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
    """Copiar tablas esenciales completas."""
    essential_tables = [
        "artists",
        "releases",
        "genres",
        "artist_genres",
        "release_genres",
        "artist_similars",
        "release_recommendations",
    ]

    print("\nCopiando tablas esenciales...")
    for table in essential_tables:
        try:
            count = copy_table_data(source, target, table)
            print(f"  ✓ {table}: {count:,} registros")
        except Exception as e:
            print(f"  ✗ {table}: Error - {e}")


def copy_release_embeddings_reduced(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    max_releases: int = 10000,
) -> None:
    """Copiar embeddings de releases solo para los releases más populares."""
    print("\nCopiando embeddings de releases reducidos...")

    for table in ["release_embeddings", "release_embeddings_dl"]:
        try:
            # Verificar que la tabla existe
            cursor = source.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
            )
            if not cursor.fetchone():
                print(f"  ⚠ {table}: Tabla no existe en fuente, omitiendo")
                continue

            # Obtener estructura de la tabla
            cursor = source.execute(f"PRAGMA table_info({table})")
            columns = [row[1] for row in cursor.fetchall()]
            placeholders = ",".join("?" * len(columns))
            columns_str = ",".join(columns)

            insert_query = f"INSERT OR IGNORE INTO {table} ({columns_str}) VALUES ({placeholders})"

            # Copiar solo embeddings de releases populares
            cursor = source.execute(
                f"""
                SELECT e.* FROM {table} e
                INNER JOIN (
                    SELECT id_release
                    FROM releases
                    WHERE ratings_count > 0
                    ORDER BY ratings_count DESC, avg_rating DESC
                    LIMIT ?
                ) r ON e.id_release = r.id_release
            """,
                [max_releases],
            )

            total_copied = 0
            batch_size = 10000

            while True:
                batch = cursor.fetchmany(batch_size)
                if not batch:
                    break
                target.executemany(insert_query, batch)
                total_copied += len(batch)
                if total_copied % (batch_size * 5) == 0:
                    target.commit()
                    print(f"  Procesados {total_copied:,} embeddings de {table}...")

            target.commit()
            print(f"  ✓ {table}: {total_copied:,} registros")
        except Exception as e:
            print(f"  ✗ {table}: Error - {e}")


def copy_release_pairs_reduced(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    max_pairs: int = 5000000,  # Mantener más pares pero filtrados por releases populares
) -> None:
    """Copiar pares de releases relacionados con releases populares.

    Mantiene funcionalidad completa.
    """
    print("\nCopiando release_pairs reducidos (solo relacionados con releases populares)...")

    # Verificar que la tabla existe
    cursor = source.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='release_pairs'"
    )
    if not cursor.fetchone():
        print("  ⚠ release_pairs: Tabla no existe en fuente, omitiendo")
        return

    # Obtener estructura de la tabla
    cursor = source.execute("PRAGMA table_info(release_pairs)")
    columns = [row[1] for row in cursor.fetchall()]
    placeholders = ",".join("?" * len(columns))
    columns_str = ",".join(columns)

    insert_query = f"INSERT OR IGNORE INTO release_pairs ({columns_str}) VALUES ({placeholders})"

    # Filtrar pares que involucren releases populares (los que tienen ratings)
    # Esto mantiene la funcionalidad completa de co-ocurrencia pero solo para releases relevantes
    cursor = source.execute(
        """
        SELECT DISTINCT rp.* FROM release_pairs rp
        INNER JOIN releases r1 ON rp.id_release_1 = r1.id_release
        INNER JOIN releases r2 ON rp.id_release_2 = r2.id_release
        WHERE r1.ratings_count > 0 AND r2.ratings_count > 0
        ORDER BY rp.pair_count DESC, rp.jaccard DESC NULLS LAST, rp.lift DESC NULLS LAST
        LIMIT ?
    """,
        [max_pairs],
    )

    total_copied = 0
    batch_size = 10000

    while True:
        batch = cursor.fetchmany(batch_size)
        if not batch:
            break
        target.executemany(insert_query, batch)
        total_copied += len(batch)
        if total_copied % (batch_size * 10) == 0:
            target.commit()
            print(f"  Procesados {total_copied:,} pares...")

    target.commit()
    print(f"  ✓ release_pairs: {total_copied:,} registros (filtrados por releases populares)")


def copy_reduced_interactions(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    max_interactions: int = 500000,
) -> None:
    """Copiar interacciones reducidas, priorizando las más valiosas."""
    print("\nCopiando interacciones reducidas...")

    # Estrategia optimizada: usar una tabla temporal para ordenar en SQL
    # en lugar de cargar todo en memoria

    # Primero obtenemos los releases más populares (reducido para base lite)
    cursor = source.execute("""
        SELECT id_release
        FROM releases
        WHERE ratings_count > 0
        ORDER BY ratings_count DESC, avg_rating DESC
        LIMIT 8000
    """)

    popular_release_ids = [row[0] for row in cursor.fetchall()]

    # Obtener estructura de la tabla
    cursor = source.execute("PRAGMA table_info(interactions)")
    columns = [row[1] for row in cursor.fetchall()]
    placeholders = ",".join("?" * len(columns))
    columns_str = ",".join(columns)

    insert_query = f"INSERT OR IGNORE INTO interactions ({columns_str}) VALUES ({placeholders})"

    if not popular_release_ids:
        # Fallback: copiar interacciones más recientes usando SQL directamente
        print("  Usando fallback: interacciones más recientes")
        cursor = source.execute(
            """
            SELECT * FROM interactions
            ORDER BY rating DESC, rating_date DESC NULLS LAST
            LIMIT ?
        """,
            [max_interactions],
        )

        total_copied = 0
        batch_size = 10000
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                break
            target.executemany(insert_query, batch)
            total_copied += len(batch)
            if total_copied % (batch_size * 5) == 0:
                target.commit()
                print(f"  Procesadas {total_copied:,} interacciones...")

        target.commit()
        print(f"  ✓ interactions: {total_copied:,} registros")
    else:
        # Usar releases populares - procesar en lotes sin cargar todo en memoria
        print(f"  Procesando {len(popular_release_ids)} releases populares...")

        # Crear una tabla temporal para almacenar IDs de releases populares
        source.execute(
            "CREATE TEMP TABLE IF NOT EXISTS temp_popular_releases (id_release INTEGER PRIMARY KEY)"
        )
        source.commit()

        # Insertar releases populares en lotes usando executemany
        batch_size = 800
        insert_temp_query = "INSERT OR IGNORE INTO temp_popular_releases (id_release) VALUES (?)"
        for i in range(0, len(popular_release_ids), batch_size):
            batch = [(release_id,) for release_id in popular_release_ids[i : i + batch_size]]
            source.executemany(insert_temp_query, batch)
        source.commit()

        # Usar JOIN para obtener interacciones ordenadas directamente desde SQL
        # Nota: Todos los releases populares ya fueron copiados en copy_essential_tables
        # La validación final eliminará cualquier interacción huérfana
        # Esto evita cargar todo en memoria
        cursor = source.execute(
            """
            SELECT i.* FROM interactions i
            INNER JOIN temp_popular_releases t ON i.id_release = t.id_release
            ORDER BY i.rating DESC, i.rating_date DESC NULLS LAST
            LIMIT ?
        """,
            [max_interactions],
        )

        total_copied = 0
        batch_size = 10000
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                break
            target.executemany(insert_query, batch)
            total_copied += len(batch)
            if total_copied % (batch_size * 5) == 0:
                target.commit()
                print(f"  Procesadas {total_copied:,} interacciones...")

        target.commit()

        # Limpiar tabla temporal
        source.execute("DROP TABLE IF EXISTS temp_popular_releases")
        source.commit()

        print(f"  ✓ interactions: {total_copied:,} registros")


def copy_users_from_interactions(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    target_path: Path | None = None,
    min_ratings: int = 5,  # Filtrar usuarios con al menos N calificaciones
) -> None:
    """Copiar usuarios importantes que tienen interacciones en la base lite.

    Prioriza usuarios con más actividad (ratings_count) y roles especiales.
    """
    print("\nCopiando usuarios importantes...")

    # Obtener estructura de la tabla
    cursor = source.execute("PRAGMA table_info(users)")
    columns = [row[1] for row in cursor.fetchall()]
    placeholders = ",".join("?" * len(columns))
    columns_str = ",".join(columns)

    insert_query = f"INSERT OR IGNORE INTO users ({columns_str}) VALUES ({placeholders})"

    # Filtrar usuarios importantes: con muchas calificaciones o roles especiales
    # Esto mantiene usuarios valiosos para los sistemas de recomendación
    important_users_query = """
        SELECT DISTINCT u.* FROM users u
        WHERE u.ratings_count >= ?
           OR u.role IN ('staff', 'admin', 'moderator', 'contributor')
        ORDER BY
            CASE WHEN u.role IN ('staff', 'admin', 'moderator', 'contributor') THEN 0 ELSE 1 END,
            u.ratings_count DESC
    """

    # Intentar usar ATTACH DATABASE para hacer JOIN directo (más eficiente)
    if target_path:
        try:
            # Attach la base de datos destino a la fuente para poder hacer JOIN
            # Usar path absoluto y escapar comillas simples para evitar problemas
            target_path_str = str(target_path.resolve()).replace("'", "''")
            source.execute(f"ATTACH DATABASE '{target_path_str}' AS target_db")

            # Combinar: usuarios importantes Y usuarios que tienen interacciones en la base lite
            # Construir query con parámetro correctamente
            combined_query = """
                SELECT DISTINCT u.* FROM (
                    SELECT DISTINCT u.* FROM users u
                    WHERE u.ratings_count >= ?
                       OR u.role IN ('staff', 'admin', 'moderator', 'contributor')
                    ORDER BY
                        CASE
                            WHEN u.role IN ('staff', 'admin', 'moderator', 'contributor')
                            THEN 0
                            ELSE 1
                        END,
                        u.ratings_count DESC
                ) u
                WHERE EXISTS (
                    SELECT 1 FROM target_db.interactions i WHERE i.id_user = u.id_user
                )
            """
            cursor = source.execute(combined_query, [min_ratings])

            # Procesar usuarios por lotes
            total_copied = 0
            batch_size = 10000
            while True:
                batch = cursor.fetchmany(batch_size)
                if not batch:
                    break
                target.executemany(insert_query, batch)
                total_copied += len(batch)
                if total_copied % (batch_size * 5) == 0:
                    target.commit()
                    print(f"  Procesados {total_copied:,} usuarios...")

            target.commit()
            source.execute("DETACH DATABASE target_db")
            print(f"  ✓ users: {total_copied:,} registros")
            return
        except Exception:
            # Si falla, continuar con el método por lotes
            try:
                source.execute("DETACH DATABASE IF EXISTS target_db")
            except Exception:
                pass

    # Método por lotes: obtener usuarios importantes de la fuente
    # Primero obtener usuarios importantes directamente de la fuente
    cursor = source.execute(important_users_query, [min_ratings])
    important_user_ids = []
    batch_size = 1000
    while True:
        batch = cursor.fetchmany(batch_size)
        if not batch:
            break
        important_user_ids.extend([row[0] for row in batch])

    # También obtener usuarios que tienen interacciones en la base lite
    cursor = target.execute("SELECT DISTINCT id_user FROM interactions")
    interaction_user_ids = set()
    while True:
        batch = cursor.fetchmany(batch_size)
        if not batch:
            break
        interaction_user_ids.update([row[0] for row in batch])

    # Combinar: usuarios importantes + usuarios con interacciones
    all_user_ids = list(set(important_user_ids) | interaction_user_ids)

    if not all_user_ids:
        print("  ⚠ No hay usuarios para copiar")
        return

    # Procesar en lotes para evitar problemas con muchos parámetros
    param_batch_size = 800
    total_copied = 0

    for i in range(0, len(all_user_ids), param_batch_size):
        batch = all_user_ids[i : i + param_batch_size]
        placeholders_batch = ",".join("?" * len(batch))
        cursor = source.execute(
            f"""
            SELECT * FROM users
            WHERE id_user IN ({placeholders_batch})
        """,
            batch,
        )

        batch_users = cursor.fetchall()
        if batch_users:
            target.executemany(insert_query, batch_users)
            total_copied += len(batch_users)

        if (i + param_batch_size) % (param_batch_size * 10) == 0:
            target.commit()
            print(f"  Procesados {total_copied:,} usuarios...")

    target.commit()
    print(f"  ✓ users: {total_copied:,} registros")


def copy_user_embeddings(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
) -> None:
    """Copiar embeddings de usuarios que existen en la base lite."""
    print("\nCopiando embeddings de usuarios...")

    # Obtener usuarios que existen en la base lite (procesando por lotes)
    cursor = target.execute("SELECT id_user FROM users")
    user_ids = []
    batch_size = 1000
    while True:
        batch = cursor.fetchmany(batch_size)
        if not batch:
            break
        user_ids.extend([row[0] for row in batch])

    if not user_ids:
        print("  ⚠ No hay usuarios para copiar embeddings")
        return

    for table in ["user_embeddings", "user_embeddings_dl"]:
        try:
            # Verificar que la tabla existe en la fuente
            cursor = source.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
            )
            if not cursor.fetchone():
                print(f"  ⚠ {table}: Tabla no existe en fuente, omitiendo")
                continue

            # Obtener estructura de la tabla
            cursor = source.execute(f"PRAGMA table_info({table})")
            columns = [row[1] for row in cursor.fetchall()]
            placeholders = ",".join("?" * len(columns))
            columns_str = ",".join(columns)

            insert_query = f"INSERT OR IGNORE INTO {table} ({columns_str}) VALUES ({placeholders})"

            # Procesar usuarios en lotes para evitar problemas con muchos parámetros
            total_copied = 0
            param_batch_size = 800  # SQLite tiene límite de ~999 parámetros

            for i in range(0, len(user_ids), param_batch_size):
                batch = user_ids[i : i + param_batch_size]
                placeholders_batch = ",".join("?" * len(batch))
                cursor = source.execute(
                    f"""
                    SELECT * FROM {table}
                    WHERE id_user IN ({placeholders_batch})
                """,
                    batch,
                )

                # Procesar embeddings en lotes más pequeños para insertar
                insert_batch_size = 5000
                while True:
                    embeddings_batch = cursor.fetchmany(insert_batch_size)
                    if not embeddings_batch:
                        break
                    target.executemany(insert_query, embeddings_batch)
                    total_copied += len(embeddings_batch)
                    if total_copied % (insert_batch_size * 5) == 0:
                        target.commit()

            target.commit()

            if total_copied > 0:
                print(f"  ✓ {table}: {total_copied:,} registros")
            else:
                print(f"  ⚠ {table}: No se encontraron embeddings")
        except Exception as e:
            print(f"  ✗ {table}: Error - {e}")


def copy_release_tracks_reduced(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    max_releases: int = 3000,
) -> None:
    """Copiar tracklists solo de releases más populares."""
    print("\nCopiando tracklists reducidos...")

    # Verificar que la tabla existe en la fuente
    cursor = source.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='release_tracks'"
    )
    if not cursor.fetchone():
        print("  ⚠ release_tracks: Tabla no existe en fuente, omitiendo")
        return

    # Obtener estructura de la tabla
    cursor = source.execute("PRAGMA table_info(release_tracks)")
    columns = [row[1] for row in cursor.fetchall()]
    placeholders = ",".join("?" * len(columns))
    columns_str = ",".join(columns)

    insert_query = f"INSERT OR IGNORE INTO release_tracks ({columns_str}) VALUES ({placeholders})"

    # Usar JOIN directo en SQL en lugar de cargar IDs en memoria
    # Esto es más eficiente y usa menos memoria
    cursor = source.execute(
        """
        SELECT rt.* FROM release_tracks rt
        INNER JOIN (
            SELECT id_release
            FROM releases
            WHERE ratings_count > 0
            ORDER BY ratings_count DESC, avg_rating DESC
            LIMIT ?
        ) r ON rt.id_release = r.id_release
    """,
        [max_releases],
    )

    total_copied = 0
    batch_size = 10000

    while True:
        batch = cursor.fetchmany(batch_size)
        if not batch:
            break
        target.executemany(insert_query, batch)
        total_copied += len(batch)
        if total_copied % (batch_size * 5) == 0:
            target.commit()
            print(f"  Procesados {total_copied:,} tracks...")

    target.commit()

    if total_copied > 0:
        print(f"  ✓ release_tracks: {total_copied:,} registros")
    else:
        print("  ⚠ No se encontraron tracklists")


def validate_integrity(connection: sqlite3.Connection) -> bool:
    """Validar integridad referencial de la base de datos."""
    print("\nValidando integridad referencial...")
    try:
        # Activar foreign keys temporalmente para validar
        connection.execute("PRAGMA foreign_keys = ON")

        # Verificar integridad de interacciones
        cursor = connection.execute("""
            SELECT COUNT(*) FROM interactions i
            WHERE NOT EXISTS (SELECT 1 FROM releases r WHERE r.id_release = i.id_release)
               OR NOT EXISTS (SELECT 1 FROM users u WHERE u.id_user = i.id_user)
        """)
        orphaned = cursor.fetchone()[0]

        if orphaned > 0:
            print(f"  ⚠ Advertencia: {orphaned:,} interacciones huérfanas encontradas")
            # Eliminar interacciones huérfanas
            connection.execute("""
                DELETE FROM interactions
                WHERE NOT EXISTS (
                    SELECT 1 FROM releases r
                    WHERE r.id_release = interactions.id_release
                )
                   OR NOT EXISTS (
                    SELECT 1 FROM users u
                    WHERE u.id_user = interactions.id_user
                )
            """)
            connection.commit()
            print("  ✓ Interacciones huérfanas eliminadas")

        # Desactivar foreign keys nuevamente para optimización
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.commit()
        print("  ✓ Integridad validada")
        return True
    except Exception as e:
        print(f"  ✗ Error en validación: {e}")
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.commit()
        return False


def optimize_database(connection: sqlite3.Connection) -> None:
    """Optimizar la base de datos."""
    print("\nOptimizando base de datos...")
    connection.execute("VACUUM")
    connection.execute("ANALYZE")
    # Reactivar foreign keys al final
    connection.execute("PRAGMA foreign_keys = ON")
    connection.commit()
    print("  ✓ Optimización completada")


def main() -> int:
    parser = argparse.ArgumentParser(description="Crear base de datos lite desde la base completa")
    parser.add_argument(
        "--source",
        type=Path,
        default=_DATA_DIR / "sputnik.db",
        help="Ruta a la base de datos fuente (default: data/sputnik.db)",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=_DATA_DIR / "sputnik_lite.db",
        help="Ruta a la base de datos destino (default: data/sputnik_lite.db)",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=_DATA_DIR / "schema.sql",
        help="Ruta al archivo schema.sql (default: data/schema.sql)",
    )
    parser.add_argument(
        "--max-interactions",
        type=int,
        default=6000,
        help="Máximo número de interacciones a copiar (default: 6000)",
    )
    parser.add_argument(
        "--max-pairs",
        type=int,
        default=28000,
        help=(
            "Máximo número de release_pairs a copiar "
            "(default: 28000, filtrados por releases populares)"
        ),
    )
    parser.add_argument(
        "--min-user-ratings",
        type=int,
        default=60,
        help="Mínimo número de calificaciones para considerar un usuario importante (default: 60)",
    )
    parser.add_argument(
        "--target-size-mb",
        type=float,
        default=85.0,
        help="Tamaño objetivo en MB (default: 85.0)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Sobrescribir base de datos destino si existe",
    )

    args = parser.parse_args()

    # Validar archivos de entrada
    if not args.source.exists():
        print(f"Error: Base de datos fuente no encontrada: {args.source}")
        return 1

    if not args.schema.exists():
        print(f"Error: Archivo schema no encontrado: {args.schema}")
        return 1

    # Verificar si el destino existe
    if args.target.exists() and not args.force:
        print(f"Error: Base de datos destino ya existe: {args.target}")
        print("Usa --force para sobrescribir")
        return 1

    # Mostrar información inicial
    source_size = get_db_size_mb(args.source)
    print(f"Base de datos fuente: {args.source}")
    print(f"Tamaño fuente: {source_size:.2f} MB")
    print(f"Base de datos destino: {args.target}")
    print(f"Tamaño objetivo: {args.target_size_mb:.2f} MB")
    print(f"Máximo de interacciones: {args.max_interactions:,}")
    print(f"Máximo de release_pairs: {args.max_pairs:,}")
    print(f"Mínimo de calificaciones por usuario: {args.min_user_ratings}")
    print()

    # Eliminar destino si existe
    if args.target.exists():
        args.target.unlink()
        # También eliminar archivos WAL y SHM si existen
        for suffix in ["-wal", "-shm"]:
            wal_path = args.target.with_suffix(args.target.suffix + suffix)
            if wal_path.exists():
                wal_path.unlink()

    # Conectar a las bases de datos
    print("Conectando a bases de datos...")
    source_conn = sqlite3.connect(args.source)
    source_conn.row_factory = sqlite3.Row

    target_conn = sqlite3.connect(args.target)
    target_conn.row_factory = sqlite3.Row

    try:
        # Configurar conexiones para mejor rendimiento
        source_conn.execute("PRAGMA foreign_keys = OFF")
        source_conn.execute("PRAGMA cache_size = -64000")  # 64MB cache
        target_conn.execute("PRAGMA foreign_keys = OFF")
        target_conn.execute("PRAGMA journal_mode = WAL")
        target_conn.execute("PRAGMA synchronous = NORMAL")
        target_conn.execute("PRAGMA cache_size = -64000")  # 64MB cache
        target_conn.execute("PRAGMA temp_store = MEMORY")  # Usar memoria para temp

        # Copiar esquema
        copy_schema(source_conn, target_conn, args.schema)

        # Copiar tablas esenciales (sin release_pairs ni embeddings,
        # se copian después de forma reducida)
        copy_essential_tables(source_conn, target_conn)

        # Copiar embeddings de releases reducidos (solo los más populares)
        copy_release_embeddings_reduced(source_conn, target_conn)

        # Copiar release_pairs reducidos (solo los más importantes)
        copy_release_pairs_reduced(source_conn, target_conn, args.max_pairs)

        # Copiar interacciones reducidas
        copy_reduced_interactions(source_conn, target_conn, args.max_interactions)

        # Copiar usuarios importantes (con muchas calificaciones o roles especiales)
        copy_users_from_interactions(source_conn, target_conn, args.target, args.min_user_ratings)

        # Copiar embeddings de usuarios
        copy_user_embeddings(source_conn, target_conn)

        # Copiar tracklists reducidos
        copy_release_tracks_reduced(source_conn, target_conn)

        # Validar integridad referencial antes de optimizar
        validate_integrity(target_conn)

        # Optimizar base de datos (reactiva foreign keys al final)
        optimize_database(target_conn)

        # Mostrar resultado final
        final_size = get_db_size_mb(args.target)
        print(f"\n{'='*60}")
        print("Base de datos lite creada exitosamente")
        print(f"Tamaño final: {final_size:.2f} MB")
        reduction_pct = 100 * (1 - final_size / source_size)
        print(f"Reducción: {source_size - final_size:.2f} MB ({reduction_pct:.1f}%)")
        print(f"{'='*60}")

        if final_size > args.target_size_mb * 1.1:
            print(
                f"\n⚠ Advertencia: El tamaño ({final_size:.2f} MB) "
                f"excede el objetivo ({args.target_size_mb:.2f} MB)"
            )
            print("  Considera reducir --max-interactions o --max-pairs")

        return 0

    except Exception as e:
        print(f"\nError durante la creación: {e}")
        import traceback

        traceback.print_exc()
        return 1

    finally:
        source_conn.close()
        target_conn.close()


if __name__ == "__main__":
    sys.exit(main())
