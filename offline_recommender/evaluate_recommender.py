"""Offline evaluation of recommender strategies using NDCG@k."""

from __future__ import annotations

import argparse
import csv
import gc
import logging
import multiprocessing
import os
import random
import sqlite3
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable
from typing import List
from typing import Set

from app import metrics
from app import recommender


LOGGER = logging.getLogger("evaluate_recommender")


@contextmanager
def _process_lock(lock_file_path: Path):
    """Context manager para prevenir ejecuciones simultáneas del mismo script.

    Crea un archivo de lock con el PID del proceso. Si el archivo existe y el proceso
    sigue activo, lanza una excepción. El lock se elimina automáticamente al salir.
    """
    lock_file = lock_file_path

    # Verificar si ya existe un lock file
    if lock_file.exists():
        try:
            # Leer el PID del archivo
            with lock_file.open("r") as f:
                stored_pid = int(f.read().strip())

            # Verificar si el proceso sigue activo
            try:
                # En Unix, enviar señal 0 no hace nada pero verifica si el proceso existe
                os.kill(stored_pid, 0)
                # Si llegamos aquí, el proceso existe
                raise RuntimeError(
                    f"Another instance of evaluate_recommender is already running "
                    f"(PID: {stored_pid}). Lock file: {lock_file}. "
                    "Please wait for it to finish or remove the lock file if the "
                    "process crashed."
                )
            except ProcessLookupError:
                # El proceso no existe, el lock file es huérfano
                LOGGER.warning(
                    "Found orphaned lock file (PID %d no longer exists). Removing it.",
                    stored_pid,
                )
                lock_file.unlink()
            except PermissionError:
                # No tenemos permisos para verificar, pero el proceso podría existir
                # Asumir que está corriendo para ser conservador
                raise RuntimeError(
                    f"Lock file exists and cannot verify if process {stored_pid} "
                    f"is running. Lock file: {lock_file}. "
                    "Please check manually or remove the lock file if safe."
                ) from None
        except (ValueError, OSError) as e:
            # Error leyendo el lock file, asumir que está corrupto y eliminarlo
            LOGGER.warning("Lock file appears corrupted. Removing it: %s", e)
            try:
                lock_file.unlink()
            except OSError:
                pass

    # Crear el lock file con el PID actual
    try:
        with lock_file.open("w") as f:
            f.write(str(os.getpid()))
        LOGGER.debug("Created lock file: %s (PID: %d)", lock_file, os.getpid())
    except OSError as e:
        raise RuntimeError(f"Failed to create lock file {lock_file}: {e}") from e

    try:
        yield
    finally:
        # Eliminar el lock file al salir
        try:
            if lock_file.exists():
                lock_file.unlink()
                LOGGER.debug("Removed lock file: %s", lock_file)
        except OSError as e:
            LOGGER.warning("Failed to remove lock file %s: %s", lock_file, e)


@contextmanager
def _withheld_interactions(
    connection: sqlite3.Connection,
    user_id: str,
    release_ids: List[int],
):
    unique_ids = sorted({int(release_id) for release_id in release_ids})
    if not unique_ids:
        yield
        return

    placeholders = ",".join("?" for _ in unique_ids)
    query_params = [user_id, *unique_ids]

    original_rows = connection.execute(
        f"""
        SELECT id_release, rating, rating_date, soundoff_text, source_url
        FROM interactions
        WHERE id_user = ? AND id_release IN ({placeholders});
        """,
        query_params,
    ).fetchall()

    try:
        if original_rows:
            connection.execute(
                f"DELETE FROM interactions WHERE id_user = ? AND id_release IN ({placeholders});",
                query_params,
            )
            connection.commit()
        yield
    finally:
        if original_rows:
            connection.executemany(
                """
                INSERT INTO interactions (
                    id_release,
                    id_user,
                    rating,
                    rating_date,
                    soundoff_text,
                    source_url
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id_release, id_user) DO UPDATE SET
                    rating = excluded.rating,
                    rating_date = excluded.rating_date,
                    soundoff_text = excluded.soundoff_text,
                    source_url = excluded.source_url;
                """,
                [
                    (
                        int(row["id_release"]),
                        user_id,
                        row["rating"],
                        row["rating_date"],
                        row["soundoff_text"],
                        row["source_url"],
                    )
                    for row in original_rows
                ],
            )
            connection.commit()


def resolve_database_path(database: str | None) -> Path:
    if database:
        return Path(database).expanduser().resolve()
    return Path(__file__).resolve().parents[1] / "data" / "sputnik.db"


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s - %(message)s")


def load_processed_users(output_path: Path | None) -> Set[str]:
    """Cargar usuarios ya procesados desde el archivo CSV de salida."""
    if not output_path or not output_path.exists():
        return set()

    processed = set()
    try:
        with output_path.open("r", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                user_id = row.get("user_id")
                if user_id:
                    processed.add(user_id)
        LOGGER.info("Found %d already processed users in %s", len(processed), output_path)
    except Exception as e:
        LOGGER.warning("Error reading existing results file: %s", e)

    return processed


def write_results_incremental(
    output_path: Path,
    results: List[dict],
    fieldnames: List[str],
    append: bool = False,
) -> None:
    """Escribir resultados incrementalmente al CSV."""
    file_exists = output_path.exists() and append

    with output_path.open("a" if file_exists else "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerows(results)

    LOGGER.info("Wrote %d results to %s (append=%s)", len(results), output_path, append)


def write_results_with_lock(
    output_path: Path,
    results: List[dict],
    fieldnames: List[str] | None,
) -> None:
    """Escribir resultados al CSV con lock file para sincronización entre procesos."""
    if not results:
        return

    # Si fieldnames es None, determinarlos del primer resultado
    if fieldnames is None:
        if results:
            fieldnames = list(results[0].keys())
        else:
            return

    lock_file = output_path.parent / f".{output_path.name}.lock"
    max_wait = 30  # segundos máximo de espera
    wait_interval = 0.1  # segundos entre intentos

    file_exists = output_path.exists()
    start_time = time.time()

    # Intentar adquirir lock
    while time.time() - start_time < max_wait:
        try:
            # Intentar crear lock file exclusivamente
            if not lock_file.exists():
                with lock_file.open("x") as f:
                    f.write(str(os.getpid()))
                break
        except FileExistsError:
            # Otro proceso tiene el lock, esperar
            time.sleep(wait_interval)
            continue
    else:
        # Timeout esperando lock
        LOGGER.warning("Timeout waiting for lock file, writing without lock (may cause conflicts)")
        lock_file = None

    try:
        # Escribir resultados
        with output_path.open("a" if file_exists else "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerows(results)

        LOGGER.debug("Wrote %d results to %s (with lock)", len(results), output_path)
    finally:
        # Liberar lock
        if lock_file and lock_file.exists():
            try:
                lock_file.unlink()
            except OSError:
                pass


def pick_users(connection: sqlite3.Connection, min_ratings: int, sample_size: int) -> List[str]:
    rows = connection.execute(
        """
        SELECT id_user
        FROM interactions
        GROUP BY id_user
        HAVING SUM(CASE WHEN rating > 0 THEN 1 ELSE 0 END) >= ?
        ORDER BY RANDOM()
        LIMIT ?;
        """,
        (min_ratings, sample_size),
    ).fetchall()
    return [row["id_user"] for row in rows]


def split_interactions(connection: sqlite3.Connection, user_id: str, holdout_ratio: float):
    rows = connection.execute(
        """
        SELECT id_release, rating
        FROM interactions
        WHERE id_user = ? AND rating > 0
        ORDER BY rating_date DESC;
        """,
        (user_id,),
    ).fetchall()
    release_ids = [int(row["id_release"]) for row in rows]
    random.shuffle(release_ids)
    cutoff = max(1, int(len(release_ids) * (1 - holdout_ratio)))
    return release_ids[:cutoff], release_ids[cutoff:]


def build_candidate_pool(
    connection: sqlite3.Connection,
    user_id: str,
    holdout: List[int],
    max_pool_size: int,
):
    seen = set(recommender.rated_release_ids(user_id) + recommender.seen_release_ids(user_id))
    pool = set(holdout)
    rows = connection.execute(
        """
        SELECT id_release
        FROM releases
        WHERE id_release NOT IN (SELECT id_release FROM interactions WHERE id_user = ?)
        ORDER BY ratings_count DESC, avg_rating DESC
        LIMIT ?;
        """,
        (user_id, max_pool_size),
    ).fetchall()
    pool.update(int(row["id_release"]) for row in rows if int(row["id_release"]) not in seen)
    return list(pool)


def load_release_metadata(
    connection: sqlite3.Connection,
    release_ids: List[int],
) -> tuple[dict[int, list[int]], dict[int, int], dict[int, int]]:
    """Cargar metadata de releases: géneros (desde releases y artistas), artistas, ratings_count."""
    if not release_ids:
        return {}, {}, {}

    placeholders = ",".join("?" for _ in release_ids)

    # Cargar artistas y ratings_count primero (necesarios para obtener géneros de artistas)
    release_rows = connection.execute(
        f"""
        SELECT id_release, artist_id, ratings_count
        FROM releases
        WHERE id_release IN ({placeholders});
        """,
        release_ids,
    ).fetchall()
    release_to_artist: dict[int, int] = {}
    release_to_ratings_count: dict[int, int] = {}
    artist_ids_set: set[int] = set()

    for row in release_rows:
        release_id = int(row["id_release"])
        artist_id = int(row["artist_id"] or 0)
        ratings_count = int(row["ratings_count"] or 0)
        if artist_id > 0:
            release_to_artist[release_id] = artist_id
            artist_ids_set.add(artist_id)
        release_to_ratings_count[release_id] = ratings_count

    # Cargar géneros directamente de releases
    genre_rows = connection.execute(
        f"""
        SELECT id_release, id_genre
        FROM release_genres
        WHERE id_release IN ({placeholders});
        """,
        release_ids,
    ).fetchall()
    release_to_genres: dict[int, list[int]] = defaultdict(list)
    for row in genre_rows:
        release_id = int(row["id_release"])
        genre_id = int(row["id_genre"])
        release_to_genres[release_id].append(genre_id)

    # Cargar géneros desde artistas para releases que no tienen géneros directos
    if artist_ids_set:
        artist_placeholders = ",".join("?" for _ in artist_ids_set)
        artist_genre_rows = connection.execute(
            f"""
            SELECT id_artist, id_genre
            FROM artist_genres
            WHERE id_artist IN ({artist_placeholders});
            """,
            list(artist_ids_set),
        ).fetchall()

        # Crear mapeo artista -> géneros
        artist_to_genres: dict[int, list[int]] = defaultdict(list)
        for row in artist_genre_rows:
            artist_id = int(row["id_artist"])
            genre_id = int(row["id_genre"])
            artist_to_genres[artist_id].append(genre_id)

        # Agregar géneros de artistas a releases que no tienen géneros directos
        for release_id, artist_id in release_to_artist.items():
            if release_id not in release_to_genres or not release_to_genres[release_id]:
                # Si el release no tiene géneros directos, usar los del artista
                artist_genres = artist_to_genres.get(artist_id, [])
                if artist_genres:
                    release_to_genres[release_id] = artist_genres

    return release_to_genres, release_to_artist, release_to_ratings_count


def evaluate_user(
    connection: sqlite3.Connection,
    user_id: str,
    holdout_ratio: float,
    k: int,
    pool_size: int,
    max_ratings_count: int,
) -> dict:
    train, holdout = split_interactions(connection, user_id, holdout_ratio)
    if not holdout:
        return {}

    with _withheld_interactions(connection, user_id, holdout):
        build_candidate_pool(connection, user_id, holdout, pool_size)

        # Paralelizar sistemas de recomendación usando ThreadPoolExecutor
        # Los sistemas son I/O bound (queries a DB), así que threads son apropiados
        recommendations = {}

        def get_recommendation(system_name: str, func):
            """Wrapper para capturar excepciones y retornar lista vacía si falla."""
            try:
                return system_name, func()
            except Exception as e:
                LOGGER.debug("Error in %s for user %s: %s", system_name, user_id, e)
                return system_name, []

        # Definir sistemas a ejecutar en paralelo
        # Algunas funciones usan keyword-only arguments para limit
        systems = [
            ("hybrid", lambda: recommender.recommend(user_id, limit=k)),
            ("advanced", lambda: recommender.recommend_advanced(user_id, limit=k)),
            ("nmf", lambda: recommender.recommend_nmf(user_id, limit=k)),
            ("two_towers", lambda: recommender.recommend_two_towers(user_id, limit=k)),
            ("pairs", lambda: recommender.recommend_from_pairs(user_id, limit=k)),
            ("content", lambda: recommender.recommend_content_based(user_id, limit=k)),
            ("random", lambda: recommender.recommend_random(user_id, limit=k)),
            ("popular", lambda: recommender._popular_unseen_releases(user_id, k)),
        ]

        # Ejecutar sistemas en paralelo (máximo 2 threads para reducir memoria)
        # Con 6 workers × 2 threads = 12 threads totales (vs 6 × 8 = 48 antes)
        # Esto reduce memoria significativamente mientras mantiene paralelización de I/O
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                executor.submit(get_recommendation, name, func): name for name, func in systems
            }

            for future in as_completed(futures):
                system_name, result = future.result()
                recommendations[system_name] = result
                # Limpiar future inmediatamente
                del future

            # Limpiar futures dict después de completar
            del futures
            gc.collect()

        # Extraer resultados
        recommended_hybrid = recommendations.get("hybrid", [])
        recommended_advanced = recommendations.get("advanced", [])
        recommended_nmf = recommendations.get("nmf", [])
        recommended_two_towers = recommendations.get("two_towers", [])
        recommended_pairs = recommendations.get("pairs", [])
        recommended_content = recommendations.get("content", [])
        recommended_random = recommendations.get("random", [])
        recommended_popular = recommendations.get("popular", [])

    # Cargar metadata para todas las recomendaciones
    # Usar set para deduplicar eficientemente
    all_recommended_ids = set()
    all_recommended_ids.update(recommended_hybrid)
    all_recommended_ids.update(recommended_advanced)
    all_recommended_ids.update(recommended_nmf)
    all_recommended_ids.update(recommended_two_towers)
    all_recommended_ids.update(recommended_pairs)
    all_recommended_ids.update(recommended_content)
    all_recommended_ids.update(recommended_random)
    all_recommended_ids.update(recommended_popular)

    unique_release_ids = list(all_recommended_ids)
    # Limpiar el set grande inmediatamente
    del all_recommended_ids
    gc.collect()

    release_to_genres, release_to_artist, release_to_ratings_count = load_release_metadata(
        connection, unique_release_ids
    )

    # max_ratings_count ahora se pasa como parámetro (calculado una sola vez)
    relevance_set = set(holdout)

    def calculate_metrics(
        recommended: List[int],
        prefix: str,
        rel_set: set[int],
        rel_to_genres: dict[int, list[int]],
        rel_to_artist: dict[int, int],
        rel_to_ratings_count: dict[int, int],
        max_ratings: int,
    ) -> dict:
        """Calcular todas las métricas para una lista de recomendaciones."""
        if not recommended:
            return {
                f"{prefix}_ndcg": 0.0,
                f"{prefix}_precision": 0.0,
                f"{prefix}_recall": 0.0,
                f"{prefix}_f1": 0.0,
                f"{prefix}_mrr": 0.0,
                f"{prefix}_genre_diversity": 0.0,
                f"{prefix}_artist_diversity": 0.0,
                f"{prefix}_novelty": 0.0,
            }

        scores = [1.0 if release_id in rel_set else 0.0 for release_id in recommended]
        return {
            f"{prefix}_ndcg": metrics.normalized_discounted_cumulative_gain(scores),
            f"{prefix}_precision": metrics.precision_at_k(recommended, rel_set, k),
            f"{prefix}_recall": metrics.recall_at_k(recommended, rel_set, k),
            f"{prefix}_f1": metrics.f1_at_k(recommended, rel_set, k),
            f"{prefix}_mrr": metrics.mean_reciprocal_rank(recommended, rel_set),
            f"{prefix}_genre_diversity": metrics.genre_diversity(recommended, rel_to_genres),
            f"{prefix}_artist_diversity": metrics.artist_diversity(recommended, rel_to_artist),
            f"{prefix}_novelty": metrics.novelty(recommended, rel_to_ratings_count, max_ratings),
        }

    result = {
        "user_id": user_id,
        "holdout_size": len(holdout),
    }

    # Calcular métricas para cada estrategia
    result.update(
        calculate_metrics(
            recommended_hybrid,
            "hybrid",
            relevance_set,
            release_to_genres,
            release_to_artist,
            release_to_ratings_count,
            max_ratings_count,
        )
    )
    result.update(
        calculate_metrics(
            recommended_advanced,
            "advanced",
            relevance_set,
            release_to_genres,
            release_to_artist,
            release_to_ratings_count,
            max_ratings_count,
        )
    )
    result.update(
        calculate_metrics(
            recommended_nmf,
            "nmf",
            relevance_set,
            release_to_genres,
            release_to_artist,
            release_to_ratings_count,
            max_ratings_count,
        )
    )
    result.update(
        calculate_metrics(
            recommended_two_towers,
            "two_towers",
            relevance_set,
            release_to_genres,
            release_to_artist,
            release_to_ratings_count,
            max_ratings_count,
        )
    )
    result.update(
        calculate_metrics(
            recommended_pairs,
            "pairs",
            relevance_set,
            release_to_genres,
            release_to_artist,
            release_to_ratings_count,
            max_ratings_count,
        )
    )
    result.update(
        calculate_metrics(
            recommended_content,
            "content",
            relevance_set,
            release_to_genres,
            release_to_artist,
            release_to_ratings_count,
            max_ratings_count,
        )
    )
    result.update(
        calculate_metrics(
            recommended_random,
            "random",
            relevance_set,
            release_to_genres,
            release_to_artist,
            release_to_ratings_count,
            max_ratings_count,
        )
    )
    result.update(
        calculate_metrics(
            recommended_popular,
            "popular",
            relevance_set,
            release_to_genres,
            release_to_artist,
            release_to_ratings_count,
            max_ratings_count,
        )
    )

    # Limpiar memoria explícitamente antes de retornar
    del recommended_hybrid, recommended_advanced, recommended_nmf, recommended_two_towers
    del recommended_pairs, recommended_content, recommended_random, recommended_popular
    del unique_release_ids, release_to_genres, release_to_artist, release_to_ratings_count
    del recommendations, relevance_set
    gc.collect()

    return result


def _evaluate_user_chunk(args: tuple) -> List[dict]:
    """Evaluar un chunk de usuarios en un proceso separado."""
    (
        database_path_str,
        user_ids,
        holdout_ratio,
        k,
        pool_size,
        max_ratings_count,
        chunk_id,
        verbose,
        sqlite_threads,
        output_path_str,
        fieldnames,
    ) = args

    # Configurar logging en el proceso hijo (necesario para multiprocessing)
    configure_logging(verbose)

    database_path = Path(database_path_str)
    output_path = Path(output_path_str) if output_path_str else None
    results = []
    save_interval = 50  # Guardar cada 50 usuarios
    saved_count = 0  # Contador de resultados ya guardados

    # Limpiar memoria al inicio del proceso hijo (reduce memoria heredada del fork)
    gc.collect()

    # Cada proceso tiene su propia conexión
    with sqlite3.connect(database_path) as connection:
        # Optimización: Habilitar WAL mode para mejor rendimiento
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        # Cache muy reducido para procesos hijos (32MB por proceso)
        connection.execute(
            "PRAGMA cache_size=-32000"
        )  # 32MB cache (reducido para procesos paralelos)
        connection.execute("PRAGMA temp_store=MEMORY")  # Usar RAM para temp tables
        # mmap_size muy reducido para procesos hijos (64MB por proceso)
        connection.execute(
            "PRAGMA mmap_size=67108864"
        )  # 64MB memory-mapped I/O (reducido para procesos paralelos)
        connection.execute(f"PRAGMA threads={sqlite_threads}")  # Threads ajustados según workers
        connection.row_factory = sqlite3.Row

        total_users = len(user_ids)
        LOGGER.info("Chunk %d: Starting evaluation of %d users", chunk_id, total_users)

        for idx, user_id in enumerate(user_ids, 1):
            # Retry logic para errores de "database is locked"
            max_retries = 3
            retry_delay = 0.5  # segundos

            for attempt in range(max_retries):
                try:
                    result = evaluate_user(
                        connection, user_id, holdout_ratio, k, pool_size, max_ratings_count
                    )
                    if result:
                        results.append(result)

                        # Limpiar memoria después de cada usuario procesado
                        if idx % 10 == 0:  # Cada 10 usuarios
                            gc.collect()

                    # Guardar resultados cada N usuarios si hay output path
                    if output_path and len(results) - saved_count >= save_interval:
                        # Determinar fieldnames si no están disponibles
                        current_fieldnames = fieldnames
                        if current_fieldnames is None and results:
                            current_fieldnames = list(results[0].keys())

                        if current_fieldnames:
                            # Guardar solo los nuevos resultados desde el último guardado
                            batch_to_save = results[saved_count : saved_count + save_interval]
                            write_results_with_lock(output_path, batch_to_save, current_fieldnames)
                            saved_count += len(batch_to_save)
                            # Limpiar batch guardado de la lista para liberar memoria
                            # (mantenemos solo los índices, no los datos)
                            LOGGER.debug(
                                "Chunk %d: Saved %d results (total processed: %d, total saved: %d)",
                                chunk_id,
                                len(batch_to_save),
                                idx,
                                saved_count,
                            )
                            gc.collect()

                    # Logging progreso cada 100 usuarios o si verbose
                    if verbose or idx % 100 == 0:
                        LOGGER.info(
                            "Chunk %d: [%d/%d] %s -> NDCG hybrid=%.4f advanced=%.4f "
                            "nmf=%.4f two_towers=%.4f pairs=%.4f content=%.4f "
                            "random=%.4f popular=%.4f",
                            chunk_id,
                            idx,
                            total_users,
                            user_id,
                            result.get("hybrid_ndcg", 0.0) if result else 0.0,
                            result.get("advanced_ndcg", 0.0) if result else 0.0,
                            result.get("nmf_ndcg", 0.0) if result else 0.0,
                            result.get("two_towers_ndcg", 0.0) if result else 0.0,
                            result.get("pairs_ndcg", 0.0) if result else 0.0,
                            result.get("content_ndcg", 0.0) if result else 0.0,
                            result.get("random_ndcg", 0.0) if result else 0.0,
                            result.get("popular_ndcg", 0.0) if result else 0.0,
                        )
                    break  # Éxito, salir del loop de retry
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                        wait_time = retry_delay * (2**attempt)  # Exponential backoff
                        LOGGER.debug(
                            "Database locked for user %s (attempt %d/%d), retrying in %.2fs",
                            user_id,
                            attempt + 1,
                            max_retries,
                            wait_time,
                        )
                        time.sleep(wait_time)
                        continue
                    else:
                        LOGGER.warning(
                            "Error evaluating user %s in chunk %d: %s", user_id, chunk_id, e
                        )
                        break
                except Exception as e:
                    LOGGER.warning("Error evaluating user %s in chunk %d: %s", user_id, chunk_id, e)
                    break

        # Guardar resultados restantes al final del chunk (los que no se guardaron en batches)
        if output_path and results and saved_count < len(results):
            # Determinar fieldnames si no están disponibles
            current_fieldnames = fieldnames
            if current_fieldnames is None and results:
                current_fieldnames = list(results[0].keys())

            if current_fieldnames:
                # Guardar los resultados que quedan sin guardar
                remaining = results[saved_count:]
                if remaining:
                    write_results_with_lock(output_path, remaining, current_fieldnames)
                    saved_count += len(remaining)
                    LOGGER.debug(
                        "Chunk %d: Saved final %d results (total saved: %d)",
                        chunk_id,
                        len(remaining),
                        saved_count,
                    )

    LOGGER.info(
        "Chunk %d completed: %d/%d users processed successfully",
        chunk_id,
        len(results),
        len(user_ids),
    )
    return results


def evaluate(
    database_path: Path,
    min_ratings: int,
    sample_size: int,
    holdout_ratio: float,
    k: int,
    pool_size: int,
    output: Path | None,
    verbose: bool,
    num_workers: int | None = None,
) -> None:
    configure_logging(verbose)
    LOGGER.info("Evaluating recommenders (k=%d)", k)

    # Determinar número de workers
    if num_workers is None:
        num_workers = max(1, multiprocessing.cpu_count() - 1)  # Dejar 1 core libre
    LOGGER.info("Using %d worker processes", num_workers)

    # Calcular threads SQLite por worker: distribuir núcleos entre workers
    # Máximo 4 threads por worker, mínimo 1, balanceando según núcleos disponibles
    cpu_count = multiprocessing.cpu_count()
    sqlite_threads = max(1, min(4, cpu_count // num_workers))
    LOGGER.info(
        "SQLite threads per worker: %d (total capacity: %d threads)",
        sqlite_threads,
        num_workers * sqlite_threads,
    )

    with sqlite3.connect(database_path) as connection:
        # Optimización: Habilitar WAL mode para mejor rendimiento
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        # Cache reducido para proceso principal también (32MB)
        connection.execute("PRAGMA cache_size=-32000")  # 32MB cache (reducido para ahorrar memoria)
        connection.execute("PRAGMA temp_store=MEMORY")  # Usar RAM para temp tables
        # mmap_size reducido para proceso principal también (64MB)
        connection.execute(
            "PRAGMA mmap_size=67108864"
        )  # 64MB memory-mapped I/O (reducido para ahorrar memoria)
        connection.execute(f"PRAGMA threads={sqlite_threads}")  # Threads ajustados según workers
        connection.row_factory = sqlite3.Row

        # Optimización: Calcular max_ratings_count UNA SOLA VEZ antes del loop
        max_ratings_count_row = connection.execute(
            "SELECT MAX(ratings_count) as max_count FROM releases"
        ).fetchone()
        max_ratings_count = int(max_ratings_count_row["max_count"] or 1)
        LOGGER.info("Max ratings_count for novelty calculation: %d", max_ratings_count)

        users = pick_users(connection, min_ratings, sample_size)
        LOGGER.info("Selected %d users", len(users))

        if not users:
            LOGGER.warning("No users selected")
            return

        # Filtrar usuarios ya procesados si hay archivo de salida existente
        if output:
            processed_users = load_processed_users(output)
            if processed_users:
                original_count = len(users)
                users = [u for u in users if u not in processed_users]
                LOGGER.info(
                    "Filtered out %d already processed users, %d remaining",
                    original_count - len(users),
                    len(users),
                )

        if not users:
            LOGGER.warning("All users already processed")
            return

    # Dividir usuarios en chunks para procesamiento paralelo
    # Distribuir equitativamente: dividir el resto entre los primeros chunks
    base_size = len(users) // num_workers
    remainder = len(users) % num_workers
    user_chunks = []
    start_idx = 0
    for i in range(num_workers):
        chunk_size = base_size + (1 if i < remainder else 0)
        end_idx = start_idx + chunk_size
        user_chunks.append(users[start_idx:end_idx])
        start_idx = end_idx

    LOGGER.info(
        "Divided %d users into %d chunks (sizes: %s)",
        len(users),
        len(user_chunks),
        [len(chunk) for chunk in user_chunks],
    )

    # Determinar fieldnames para escritura incremental (necesario para guardado cada 50 usuarios)
    fieldnames = None
    if output and output.exists():
        # Leer fieldnames del archivo existente
        try:
            with output.open("r", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                fieldnames = reader.fieldnames
        except Exception:
            pass

    # Si no hay fieldnames, obtenerlos de una muestra (se determinarán después del primer resultado)
    # Por ahora usar None y se determinarán en el primer guardado

    # Preparar argumentos para cada chunk
    chunk_args = [
        (
            str(database_path),
            chunk,
            holdout_ratio,
            k,
            pool_size,
            max_ratings_count,
            chunk_id,
            verbose,
            sqlite_threads,
            str(output) if output else None,
            fieldnames,
        )
        for chunk_id, chunk in enumerate(user_chunks, 1)
    ]

    # Limpiar memoria antes de crear procesos hijos (reduce memoria heredada)
    gc.collect()

    # Procesar chunks en paralelo
    results = []
    if num_workers > 1 and len(user_chunks) > 1:
        LOGGER.info("Processing chunks in parallel with %d workers...", num_workers)
        LOGGER.info("Total chunks: %d, Users per chunk: ~%d", len(user_chunks), chunk_size)

        # Usar imap_unordered para ver progreso en tiempo real
        with multiprocessing.Pool(processes=num_workers) as pool:
            completed_chunks = 0
            chunk_results = []
            for chunk_result in pool.imap_unordered(_evaluate_user_chunk, chunk_args):
                completed_chunks += 1
                chunk_results.append(chunk_result)
                total_results = sum(len(cr) for cr in chunk_results)
                LOGGER.info(
                    "Progress: %d/%d chunks completed (%d total results so far)",
                    completed_chunks,
                    len(user_chunks),
                    total_results,
                )

                # Escribir resultados incrementalmente después de cada chunk
                if output and chunk_result:
                    if fieldnames is None:
                        fieldnames = list(chunk_result[0].keys())
                    write_results_incremental(
                        output,
                        chunk_result,
                        fieldnames,
                        append=(completed_chunks > 1 or output.exists()),
                    )

        # Aplanar resultados
        results = [result for chunk_result in chunk_results for result in chunk_result]
    else:
        # Procesamiento secuencial (fallback o si solo hay 1 worker)
        LOGGER.info("Processing chunks sequentially...")

        # Determinar fieldnames para escritura incremental
        fieldnames = None
        if output and output.exists():
            try:
                with output.open("r", encoding="utf-8") as handle:
                    reader = csv.DictReader(handle)
                    fieldnames = reader.fieldnames
            except Exception:
                pass

        for chunk_idx, args in enumerate(chunk_args, 1):
            chunk_results = _evaluate_user_chunk(args)
            results.extend(chunk_results)

            # Escribir resultados incrementalmente después de cada chunk
            if output and chunk_results:
                if fieldnames is None:
                    fieldnames = list(chunk_results[0].keys())
                write_results_incremental(
                    output,
                    chunk_results,
                    fieldnames,
                    append=(chunk_idx > 1 or output.exists()),
                )

            # Logging progreso
            LOGGER.info(
                "Processed chunk %d/%d: %d results so far",
                args[6],
                len(chunk_args),
                len(results),
            )

    # Si hay archivo de salida existente, cargar todos los resultados
    # para calcular promedios completos
    all_results = results.copy()
    if output and output.exists() and results:
        try:
            with output.open("r", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                existing_results = list(reader)
                # Convertir strings a números donde corresponda
                for row in existing_results:
                    for key, value in row.items():
                        if key != "user_id" and value:
                            try:
                                row[key] = float(value)
                            except (ValueError, TypeError):
                                pass
                all_results.extend(existing_results)
                LOGGER.info(
                    "Loaded %d existing results + %d new results = %d total for averages",
                    len(existing_results),
                    len(results),
                    len(all_results),
                )
        except Exception as e:
            LOGGER.warning("Error loading existing results for averages: %s", e)
            all_results = results

    if not all_results:
        LOGGER.warning("No results collected; check filters")
        return

    # Calcular promedios para todas las métricas usando todos los resultados
    strategies = [
        "hybrid",
        "advanced",
        "nmf",
        "two_towers",
        "pairs",
        "content",
        "random",
        "popular",
    ]
    metric_names = [
        "ndcg",
        "precision",
        "recall",
        "f1",
        "mrr",
        "genre_diversity",
        "artist_diversity",
        "novelty",
    ]

    averages = {}
    for strategy in strategies:
        for metric in metric_names:
            key = f"{strategy}_{metric}"
            if key in all_results[0]:
                values = [
                    item.get(key, 0.0)
                    for item in all_results
                    if isinstance(item.get(key), (int, float))
                ]
                if values:
                    averages[key] = sum(values) / len(values)
                else:
                    averages[key] = 0.0

    # Mostrar resumen de métricas principales
    LOGGER.info("=" * 80)
    LOGGER.info("Average Metrics@%d", k)
    LOGGER.info("=" * 80)

    for strategy in strategies:
        LOGGER.info(
            "%s: NDCG=%.4f Precision=%.4f Recall=%.4f F1=%.4f MRR=%.4f "
            "GenreDiv=%.4f ArtistDiv=%.4f Novelty=%.4f",
            strategy.upper(),
            averages.get(f"{strategy}_ndcg", 0.0),
            averages.get(f"{strategy}_precision", 0.0),
            averages.get(f"{strategy}_recall", 0.0),
            averages.get(f"{strategy}_f1", 0.0),
            averages.get(f"{strategy}_mrr", 0.0),
            averages.get(f"{strategy}_genre_diversity", 0.0),
            averages.get(f"{strategy}_artist_diversity", 0.0),
            averages.get(f"{strategy}_novelty", 0.0),
        )

    LOGGER.info("=" * 80)

    # Los resultados ya fueron escritos incrementalmente, solo loguear
    if output:
        if results:
            LOGGER.info(
                "Results already written incrementally to %s (%d total users)",
                output,
                len(results),
            )
        else:
            LOGGER.warning("No results to save")


def parse_arguments(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=str, help="Path to the Sputnik SQLite database")
    parser.add_argument(
        "--min-ratings", type=int, default=50, help="Minimum ratings per user to evaluate"
    )
    parser.add_argument("--sample-size", type=int, default=100, help="Number of users to sample")
    parser.add_argument(
        "--holdout-ratio", type=float, default=0.2, help="Holdout ratio for testing"
    )
    parser.add_argument("--k", type=int, default=9, help="Cutoff for NDCG")
    parser.add_argument("--pool-size", type=int, default=100, help="Size of candidate pool")
    parser.add_argument("--output", type=str, help="Output CSV file path")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Number of parallel workers (default: CPU count - 1)",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_arguments(argv)
    database_path = resolve_database_path(args.database)
    output_path = Path(args.output).expanduser().resolve() if args.output else None

    # Crear lock file en el directorio temporal o junto al script
    lock_file_path = Path(__file__).parent / ".evaluate_recommender.lock"

    try:
        with _process_lock(lock_file_path):
            evaluate(
                database_path,
                args.min_ratings,
                args.sample_size,
                args.holdout_ratio,
                args.k,
                args.pool_size,
                output_path,
                args.verbose,
                args.workers,
            )
    except RuntimeError as e:
        LOGGER.error(str(e))
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
