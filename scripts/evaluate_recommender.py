"""Offline evaluation of recommender strategies using NDCG@k."""

from __future__ import annotations

import argparse
import csv
import logging
import random
import sqlite3
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
        recommended_pairs = recommender.recommend_from_pairs(user_id, limit=k)
        recommended_content = recommender.recommend_content_based(user_id, limit=k)
        recommended_random = recommender.recommend_random(user_id, limit=k)
        recommended_popular = recommender._popular_unseen_releases(user_id, k)

    relevance_map = {release_id: 1.0 for release_id in holdout}

    def ndcg_for(recommended: List[int]) -> float:
        scores = [relevance_map.get(release_id, 0.0) for release_id in recommended]
        return metrics.normalized_discounted_cumulative_gain(scores)

    return {
        "user_id": user_id,
        "ndcg_hybrid": ndcg_for(recommended_hybrid),
        "ndcg_pairs": ndcg_for(recommended_pairs),
        "ndcg_content": ndcg_for(recommended_content),
        "ndcg_random": ndcg_for(recommended_random),
        "ndcg_popular": ndcg_for(recommended_popular),
        "holdout_size": len(holdout),
    }


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
            LOGGER.debug(
                "%s -> NDCG hybrid=%.4f pairs=%.4f content=%.4f",
                user_id,
                result["ndcg_hybrid"],
                result["ndcg_pairs"],
                result["ndcg_content"],
            )

    if not results:
        LOGGER.warning("No results collected; check filters")
        return

    avg_hybrid = sum(item["ndcg_hybrid"] for item in results) / len(results)
    avg_pairs = sum(item["ndcg_pairs"] for item in results) / len(results)
    avg_content = sum(item["ndcg_content"] for item in results) / len(results)
    avg_random = sum(item["ndcg_random"] for item in results) / len(results)
    avg_popular = sum(item["ndcg_popular"] for item in results) / len(results)

    LOGGER.info(
        "Average NDCG@%d hybrid=%.4f pairs=%.4f content=%.4f random=%.4f popular=%.4f",
        k,
        avg_hybrid,
        avg_pairs,
        avg_content,
        avg_random,
        avg_popular,
    )

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
