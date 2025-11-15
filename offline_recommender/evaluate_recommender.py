"""Offline evaluation of recommender strategies using NDCG@k."""

from __future__ import annotations

import argparse
import csv
import logging
import random
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable
from typing import List

from app import metrics
from app import recommender


LOGGER = logging.getLogger("evaluate_recommender")


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
) -> dict:
    train, holdout = split_interactions(connection, user_id, holdout_ratio)
    if not holdout:
        return {}

    with _withheld_interactions(connection, user_id, holdout):
        build_candidate_pool(connection, user_id, holdout, pool_size)

        recommended_hybrid = recommender.recommend(user_id, limit=k)
        recommended_advanced = recommender.recommend_advanced(user_id, limit=k)
        recommended_nmf = recommender.recommend_nmf(user_id, limit=k)
        recommended_two_towers = recommender.recommend_two_towers(user_id, limit=k)
        recommended_pairs = recommender.recommend_from_pairs(user_id, limit=k)
        recommended_content = recommender.recommend_content_based(user_id, limit=k)
        recommended_random = recommender.recommend_random(user_id, limit=k)
        recommended_popular = recommender._popular_unseen_releases(user_id, k)

    # Cargar metadata para todas las recomendaciones
    all_recommended_ids = (
        recommended_hybrid
        + recommended_advanced
        + recommended_nmf
        + recommended_two_towers
        + recommended_pairs
        + recommended_content
        + recommended_random
        + recommended_popular
    )
    unique_release_ids = list(set(all_recommended_ids))
    release_to_genres, release_to_artist, release_to_ratings_count = load_release_metadata(
        connection, unique_release_ids
    )

    # Obtener max_ratings_count para novelty
    max_ratings_count_row = connection.execute(
        "SELECT MAX(ratings_count) as max_count FROM releases"
    ).fetchone()
    max_ratings_count = int(max_ratings_count_row["max_count"] or 1)

    relevance_set = set(holdout)

    def calculate_metrics(recommended: List[int], prefix: str) -> dict:
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

        scores = [1.0 if release_id in relevance_set else 0.0 for release_id in recommended]
        return {
            f"{prefix}_ndcg": metrics.normalized_discounted_cumulative_gain(scores),
            f"{prefix}_precision": metrics.precision_at_k(recommended, relevance_set, k),
            f"{prefix}_recall": metrics.recall_at_k(recommended, relevance_set, k),
            f"{prefix}_f1": metrics.f1_at_k(recommended, relevance_set, k),
            f"{prefix}_mrr": metrics.mean_reciprocal_rank(recommended, relevance_set),
            f"{prefix}_genre_diversity": metrics.genre_diversity(recommended, release_to_genres),
            f"{prefix}_artist_diversity": metrics.artist_diversity(recommended, release_to_artist),
            f"{prefix}_novelty": metrics.novelty(
                recommended, release_to_ratings_count, max_ratings_count
            ),
        }

    result = {
        "user_id": user_id,
        "holdout_size": len(holdout),
    }

    # Calcular métricas para cada estrategia
    result.update(calculate_metrics(recommended_hybrid, "hybrid"))
    result.update(calculate_metrics(recommended_advanced, "advanced"))
    result.update(calculate_metrics(recommended_nmf, "nmf"))
    result.update(calculate_metrics(recommended_two_towers, "two_towers"))
    result.update(calculate_metrics(recommended_pairs, "pairs"))
    result.update(calculate_metrics(recommended_content, "content"))
    result.update(calculate_metrics(recommended_random, "random"))
    result.update(calculate_metrics(recommended_popular, "popular"))

    return result


def evaluate(
    database_path: Path,
    min_ratings: int,
    sample_size: int,
    holdout_ratio: float,
    k: int,
    pool_size: int,
    output: Path | None,
    verbose: bool,
) -> None:
    configure_logging(verbose)
    LOGGER.info("Evaluating recommenders (k=%d)", k)

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        users = pick_users(connection, min_ratings, sample_size)
        LOGGER.info("Selected %d users", len(users))

        results = []
        for user_id in users:
            result = evaluate_user(connection, user_id, holdout_ratio, k, pool_size)
            if not result:
                continue
            results.append(result)
            if verbose:
                LOGGER.debug(
                    "%s -> NDCG hybrid=%.4f advanced=%.4f nmf=%.4f two_towers=%.4f "
                    "pairs=%.4f content=%.4f | Precision hybrid=%.4f advanced=%.4f",
                    user_id,
                    result["hybrid_ndcg"],
                    result["advanced_ndcg"],
                    result["nmf_ndcg"],
                    result["two_towers_ndcg"],
                    result["pairs_ndcg"],
                    result["content_ndcg"],
                    result["hybrid_precision"],
                    result["advanced_precision"],
                )

    if not results:
        LOGGER.warning("No results collected; check filters")
        return

    # Calcular promedios para todas las métricas
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
            if key in results[0]:
                averages[key] = sum(item[key] for item in results) / len(results)

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

    if output:
        fieldnames = list(results[0].keys())
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)
        LOGGER.info("Saved detailed results to %s", output)


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
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_arguments(argv)
    database_path = resolve_database_path(args.database)
    output_path = Path(args.output).expanduser().resolve() if args.output else None

    evaluate(
        database_path,
        args.min_ratings,
        args.sample_size,
        args.holdout_ratio,
        args.k,
        args.pool_size,
        output_path,
        args.verbose,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
