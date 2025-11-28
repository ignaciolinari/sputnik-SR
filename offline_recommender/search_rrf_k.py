"""Grid-search helper to pick the best RRF k value keeping holdouts fixed (with resume)."""

from __future__ import annotations

import argparse
import csv
import logging
import multiprocessing
import os
import random
import sqlite3
from pathlib import Path
from typing import Iterable
from typing import List

from app import metrics
from app import recommender
from offline_recommender import evaluate_recommender


LOGGER = logging.getLogger("search_rrf_k")


SUMMARY_FIELDS = ["k", "users", "ndcg", "precision", "recall", "mrr", "hit_rate"]
USER_FIELDS = ["k", "user_id", "ndcg", "precision", "recall", "mrr", "hit_rate"]


def parse_arguments(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database", type=str, help="Ruta a la base SQLite (default: data/sputnik.db)"
    )
    parser.add_argument("--min-ratings", type=int, default=30, help="Ratings mínimos por usuario")
    parser.add_argument("--sample-size", type=int, default=100, help="Usuarios a evaluar")
    parser.add_argument(
        "--holdout-ratio", type=float, default=0.2, help="Fracción de interacciones para holdout"
    )
    parser.add_argument(
        "--pool-size", type=int, default=100, help="Pool adicional para negative sampling"
    )
    parser.add_argument(
        "--cutoff", type=int, default=9, help="Top-K evaluado (NDCG/Recall/Precision)"
    )
    parser.add_argument(
        "--k-values",
        type=int,
        nargs="+",
        default=[5, 10, 15, 20, 30, 40, 50, 60, 100],
        help="Valores de k a evaluar (lista de enteros)",
    )
    parser.add_argument(
        "--metric",
        choices=["ndcg", "hit_rate", "recall", "precision", "mrr"],
        default="ndcg",
        help="Métrica utilizada para elegir el mejor k",
    )
    parser.add_argument("--seed", type=int, default=42, help="Semilla para el split holdout")
    parser.add_argument("--output", type=str, help="CSV opcional con resultados agregados por k")
    parser.add_argument(
        "--user-metrics",
        type=str,
        help="CSV opcional con métricas por usuario y valor de k",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Número de procesos en paralelo (default: CPU count - 1)",
    )
    parser.add_argument("--verbose", action="store_true", help="Logs detallados")
    return parser.parse_args(argv)


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s - %(message)s")


def _prepare_holdouts(
    connection: sqlite3.Connection,
    user_ids: List[str],
    holdout_ratio: float,
    seed: int,
) -> dict[str, List[int]]:
    """Split train/holdout once so every k usa el mismo conjunto."""
    saved_state = random.getstate()
    random.seed(seed)
    holdouts: dict[str, List[int]] = {}
    try:
        for user_id in user_ids:
            _, holdout = evaluate_recommender.split_interactions(connection, user_id, holdout_ratio)
            if holdout:
                holdouts[user_id] = holdout
    finally:
        random.setstate(saved_state)
    return holdouts


def _calculate_metrics(recommended: List[int], relevant: set[int], cutoff: int) -> dict:
    scores = [1.0 if release_id in relevant else 0.0 for release_id in recommended]
    return {
        "ndcg": metrics.normalized_discounted_cumulative_gain(scores),
        "precision": metrics.precision_at_k(recommended, relevant, cutoff),
        "recall": metrics.recall_at_k(recommended, relevant, cutoff),
        "mrr": metrics.mean_reciprocal_rank(recommended, relevant),
        "hit_rate": 1.0 if any(release_id in relevant for release_id in recommended) else 0.0,
    }


def _evaluate_user_for_k_values(
    connection: sqlite3.Connection,
    user_id: str,
    holdout: List[int],
    cutoff: int,
    pool_size: int,
    k_values: List[int],
) -> List[dict]:
    results: List[dict] = []
    if not holdout:
        return results

    holdout_set = set(holdout)

    with evaluate_recommender._withheld_interactions(connection, user_id, holdout):
        evaluate_recommender.build_candidate_pool(connection, user_id, holdout, pool_size)
        strategy_candidates, strategies_used = recommender.get_rrf_strategy_rankings(
            user_id, limit=cutoff
        )

        for rrf_k in k_values:
            try:
                recommendations, _, _ = recommender.build_rrf_recommendations_from_rankings(
                    user_id,
                    strategy_candidates,
                    strategies_used,
                    limit=cutoff,
                    k=rrf_k,
                )
            except Exception as exc:  # pragma: no cover - defensivo
                LOGGER.warning("k=%d - usuario %s falló: %s", rrf_k, user_id, exc)
                continue

            metrics_row = _calculate_metrics(recommendations, holdout_set, cutoff)
            metrics_row["user_id"] = user_id
            metrics_row["k"] = int(rrf_k)
            results.append(metrics_row)

    return results


def _evaluate_users_chunk(args: tuple) -> List[dict]:
    (
        database_path,
        user_entries,
        cutoff,
        pool_size,
        chunk_id,
    ) = args

    results: List[dict] = []

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA cache_size=-16000")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA mmap_size=33554432")

        for idx, (user_id, holdout, user_k_values) in enumerate(user_entries, 1):
            if not user_k_values:
                continue
            try:
                user_results = _evaluate_user_for_k_values(
                    connection,
                    user_id,
                    holdout,
                    cutoff,
                    pool_size,
                    user_k_values,
                )
                results.extend(user_results)
            except Exception as exc:  # pragma: no cover
                LOGGER.warning(
                    "Chunk %d - usuario %s (pos %d/%d) falló: %s",
                    chunk_id,
                    user_id,
                    idx,
                    len(user_entries),
                    exc,
                )

    LOGGER.info("Chunk %d completado (%d usuarios)", chunk_id, len(user_entries))
    return results


def _summarize(k_value: int, per_user_metrics: List[dict]) -> dict:
    summary = {
        "k": k_value,
        "users": len(per_user_metrics),
        "ndcg": 0.0,
        "precision": 0.0,
        "recall": 0.0,
        "mrr": 0.0,
        "hit_rate": 0.0,
    }
    if not per_user_metrics:
        return summary

    for key in ("ndcg", "precision", "recall", "mrr", "hit_rate"):
        summary[key] = sum(item[key] for item in per_user_metrics) / len(per_user_metrics)
    return summary


def _write_csv(
    path_str: str | None,
    rows: List[dict],
    fieldnames: List[str],
    append: bool = False,
) -> None:
    if not path_str or not rows:
        return
    path = Path(path_str).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    append_mode = append and path.exists()
    mode = "a" if append_mode else "w"
    write_header = not append_mode
    with path.open(mode, newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)
    LOGGER.info("Guardado %d registros en %s", len(rows), path)


def _load_existing_summaries(path_str: str | None) -> tuple[List[dict], set[int]]:
    if not path_str:
        return ([], set())
    path = Path(path_str).expanduser().resolve()
    if not path.exists():
        return ([], set())

    summaries: List[dict] = []
    completed: set[int] = set()
    try:
        with path.open("r", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if not row:
                    continue
                try:
                    k_value = int(float(row.get("k", 0)))
                except (TypeError, ValueError):
                    continue
                summary = {
                    "k": k_value,
                    "users": int(float(row.get("users", 0) or 0)),
                    "ndcg": float(row.get("ndcg", 0.0) or 0.0),
                    "precision": float(row.get("precision", 0.0) or 0.0),
                    "recall": float(row.get("recall", 0.0) or 0.0),
                    "mrr": float(row.get("mrr", 0.0) or 0.0),
                    "hit_rate": float(row.get("hit_rate", 0.0) or 0.0),
                }
                summaries.append(summary)
                completed.add(k_value)
    except Exception as exc:  # pragma: no cover - lectura defensiva
        LOGGER.warning("No se pudo leer %s: %s", path, exc)

    return summaries, completed


def _load_existing_user_metrics(
    path_str: str | None,
) -> tuple[List[str], set[tuple[str, int]], dict[int, dict]]:
    """
    Leer (si existe) el CSV de métricas por usuario y devolver:
      - Lista ordenada de user_ids (para reutilizar el mismo sample).
      - Conjunto de pares (user_id, k) ya procesados.
      - Stats agregadas por k (count y sumas de métricas) para reconstruir summaries faltantes.
    """
    user_ids: List[str] = []
    seen_users: set[str] = set()
    processed_pairs: set[tuple[str, int]] = set()
    stats_by_k: dict[int, dict] = {}

    if not path_str:
        return user_ids, processed_pairs, stats_by_k

    path = Path(path_str).expanduser().resolve()
    if not path.exists():
        return user_ids, processed_pairs, stats_by_k

    try:
        with path.open("r", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if not row:
                    continue
                user_id = row.get("user_id")
                k_raw = row.get("k")
                if not user_id or not k_raw:
                    continue
                try:
                    k_value = int(float(k_raw))
                except (TypeError, ValueError):
                    continue

                if user_id not in seen_users:
                    seen_users.add(user_id)
                    user_ids.append(user_id)

                pair = (user_id, k_value)
                if pair in processed_pairs:
                    continue
                processed_pairs.add(pair)

                stats = stats_by_k.setdefault(
                    k_value,
                    {
                        "count": 0,
                        "ndcg": 0.0,
                        "precision": 0.0,
                        "recall": 0.0,
                        "mrr": 0.0,
                        "hit_rate": 0.0,
                    },
                )
                stats["count"] += 1
                stats["ndcg"] += float(row.get("ndcg", 0.0) or 0.0)
                stats["precision"] += float(row.get("precision", 0.0) or 0.0)
                stats["recall"] += float(row.get("recall", 0.0) or 0.0)
                stats["mrr"] += float(row.get("mrr", 0.0) or 0.0)
                stats["hit_rate"] += float(row.get("hit_rate", 0.0) or 0.0)
    except Exception as exc:  # pragma: no cover - lectura defensiva
        LOGGER.warning("No se pudo leer usuarios desde %s: %s", path, exc)

    return user_ids, processed_pairs, stats_by_k


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_arguments(argv)
    _configure_logging(args.verbose)

    database_path = evaluate_recommender.resolve_database_path(args.database)
    if os.getenv("SPUTNIK_DB") != str(database_path):
        os.environ["SPUTNIK_DB"] = str(database_path)
        LOGGER.debug("SPUTNIK_DB -> %s", database_path)

    (
        existing_user_ids,
        processed_pairs,
        stats_by_k,
    ) = _load_existing_user_metrics(args.user_metrics)

    existing_summaries, completed_from_output = _load_existing_summaries(args.output)
    requested_k_values = sorted({int(k) for k in args.k_values})
    requested_k_set = set(requested_k_values)

    # Reconstruir resúmenes faltantes usando métricas previas por usuario
    auto_summaries: List[dict] = []
    for k_value, stats in stats_by_k.items():
        if requested_k_values and k_value not in requested_k_set:
            continue
        if k_value in completed_from_output:
            continue
        count = stats.get("count", 0)
        if count <= 0:
            continue
        auto_summaries.append(
            {
                "k": k_value,
                "users": count,
                "ndcg": stats["ndcg"] / count,
                "precision": stats["precision"] / count,
                "recall": stats["recall"] / count,
                "mrr": stats["mrr"] / count,
                "hit_rate": stats["hit_rate"] / count,
            }
        )
        completed_from_output.add(k_value)

    if auto_summaries:
        auto_summaries = sorted(auto_summaries, key=lambda item: item["k"])
        if args.output:
            _write_csv(args.output, auto_summaries, SUMMARY_FIELDS, append=True)
        existing_summaries.extend(auto_summaries)

    completed_k = completed_from_output
    pending_k_values = [k for k in requested_k_values if k not in completed_k]
    all_summaries: List[dict] = list(existing_summaries)

    if not pending_k_values:
        if not all_summaries:
            LOGGER.error(
                "No hay resultados previos y no quedan valores de k pendientes. Nada para hacer."
            )
            return 1
        LOGGER.info(
            "Todos los valores de k solicitados ya fueron procesados previamente (%s).",
            ", ".join(str(k) for k in requested_k_values) or "N/A",
        )
    else:
        if completed_k:
            LOGGER.info(
                "Omitiendo %d valores de k ya procesados previamente: %s",
                len(completed_k),
                ", ".join(str(k) for k in sorted(completed_k)),
            )

        with sqlite3.connect(database_path) as connection:
            connection.row_factory = sqlite3.Row

            if existing_user_ids:
                users = existing_user_ids
                LOGGER.info("Reutilizando %d usuarios desde %s", len(users), args.user_metrics)
            else:
                users = evaluate_recommender.pick_users(
                    connection, args.min_ratings, args.sample_size
                )
                if not users:
                    LOGGER.error("No se encontraron usuarios con >=%d ratings", args.min_ratings)
                    return 1
                LOGGER.info("Usuarios seleccionados: %d", len(users))

            user_holdouts = _prepare_holdouts(connection, users, args.holdout_ratio, args.seed)
            if not user_holdouts:
                LOGGER.error(
                    "Ningún usuario quedó con holdout después del split (revisá los filtros)"
                )
                return 1
            LOGGER.info("Usuarios con holdout válido: %d", len(user_holdouts))

        user_entries: List[tuple[str, List[int], List[int]]] = []
        needed_k_values: set[int] = set()
        for user_id in users:
            holdout = user_holdouts.get(user_id, [])
            if not holdout:
                continue
            user_pending_k = [k for k in pending_k_values if (user_id, k) not in processed_pairs]
            if not user_pending_k:
                continue
            user_entries.append((user_id, holdout, user_pending_k))
            needed_k_values.update(user_pending_k)

        if not user_entries:
            LOGGER.info(
                "No quedan combinaciones usuario/k pendientes; "
                "los CSV existentes ya cubren los valores solicitados."
            )
            pending_k_values = []
        else:
            pending_k_values = sorted(needed_k_values)

        if not pending_k_values:
            if not all_summaries:
                LOGGER.warning("No hay resultados para calcular resúmenes.")
            else:
                LOGGER.info(
                    "Nada para procesar, sólo faltaba completar resúmenes (ya actualizado)."
                )
        else:
            num_workers = args.workers or max(1, multiprocessing.cpu_count() - 1)
            num_workers = max(1, min(num_workers, len(user_entries)))

            base_size = len(user_entries) // num_workers
            remainder = len(user_entries) % num_workers
            chunk_args: List[tuple] = []
            start = 0
            chunk_id = 1
            for worker_idx in range(num_workers):
                size = base_size + (1 if worker_idx < remainder else 0)
                if size <= 0:
                    continue
                chunk_entries = user_entries[start : start + size]
                start += size
                if not chunk_entries:
                    continue
                chunk_args.append(
                    (
                        str(database_path),
                        chunk_entries,
                        args.cutoff,
                        args.pool_size,
                        chunk_id,
                    )
                )
                chunk_id += 1

            LOGGER.info(
                "Procesando %d usuarios × %d valores de k con %d worker(s)",
                len(user_entries),
                len(pending_k_values),
                len(chunk_args),
            )

            per_k_stats = {
                k_value: {
                    "count": 0,
                    "ndcg": 0.0,
                    "precision": 0.0,
                    "recall": 0.0,
                    "mrr": 0.0,
                    "hit_rate": 0.0,
                }
                for k_value in pending_k_values
            }

            def _ingest_chunk_results(chunk_results: List[dict]) -> None:
                if not chunk_results:
                    return
                deduped: List[dict] = []
                for row in chunk_results:
                    key = (row["user_id"], int(row["k"]))
                    if key in processed_pairs:
                        continue
                    processed_pairs.add(key)
                    deduped.append(row)
                    stats = per_k_stats.get(key[1])
                    if stats is None:
                        continue
                    stats["count"] += 1
                    stats["ndcg"] += row["ndcg"]
                    stats["precision"] += row["precision"]
                    stats["recall"] += row["recall"]
                    stats["mrr"] += row["mrr"]
                    stats["hit_rate"] += row["hit_rate"]
                if deduped and args.user_metrics:
                    _write_csv(args.user_metrics, deduped, USER_FIELDS, append=True)

            if len(chunk_args) == 1:
                chunk_results = _evaluate_users_chunk(chunk_args[0])
                _ingest_chunk_results(chunk_results)
            else:
                with multiprocessing.Pool(processes=len(chunk_args)) as pool:
                    for chunk_results in pool.imap_unordered(_evaluate_users_chunk, chunk_args):
                        _ingest_chunk_results(chunk_results)

            new_summaries: List[dict] = []
            for k_value in pending_k_values:
                stats = per_k_stats.get(k_value)
                if not stats or stats["count"] == 0:
                    LOGGER.warning("k=%d no generó métricas (sin usuarios evaluados).", k_value)
                    continue
                users_evaluated = stats["count"]
                summary = {
                    "k": k_value,
                    "users": users_evaluated,
                    "ndcg": stats["ndcg"] / users_evaluated,
                    "precision": stats["precision"] / users_evaluated,
                    "recall": stats["recall"] / users_evaluated,
                    "mrr": stats["mrr"] / users_evaluated,
                    "hit_rate": stats["hit_rate"] / users_evaluated,
                }
                new_summaries.append(summary)
                LOGGER.info(
                    "k=%d → users=%d NDCG=%.4f HitRate=%.4f Recall=%.4f Precision=%.4f",
                    k_value,
                    summary["users"],
                    summary["ndcg"],
                    summary["hit_rate"],
                    summary["recall"],
                    summary["precision"],
                )

            if args.output:
                _write_csv(args.output, new_summaries, SUMMARY_FIELDS, append=True)
            all_summaries.extend(new_summaries)

    if not all_summaries:
        LOGGER.error("No se generaron métricas; verificá que las estrategias devuelvan candidatos.")
        return 1

    metric_key = args.metric
    best = max(all_summaries, key=lambda item: item.get(metric_key, 0.0))
    LOGGER.info(
        "Mejor k según %s: %d (valor=%.4f)",
        metric_key,
        best["k"],
        best[metric_key],
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
