"""Utility script to rebuild the release_pairs co-occurrence table."""

from __future__ import annotations

import argparse
import logging
import sqlite3
from pathlib import Path
from typing import Iterable


LOGGER = logging.getLogger("build_release_pairs")


def resolve_database_path(database: str | None) -> Path:
    if database:
        return Path(database).expanduser().resolve()
    env = Path(__file__).resolve().parents[1] / "data" / "sputnik.db"
    return env


def build_pairs(connection: sqlite3.Connection, min_rating: float, min_pair_count: int) -> None:
    LOGGER.info("Clearing existing release_pairs rows")
    LOGGER.info("Building co-occurrence matrix (min_rating=%.2f)", min_rating)
    query = """
    WITH positive AS (
        SELECT id_user, id_release
        FROM interactions
        WHERE rating >= ?
    ),
    user_release AS (
        SELECT id_user, GROUP_CONCAT(id_release) AS releases
        FROM positive
        GROUP BY id_user
    ),
    expanded AS (
        SELECT p1.id_release AS r1, p2.id_release AS r2, p1.id_user
        FROM positive AS p1
        JOIN positive AS p2 ON p1.id_user = p2.id_user
        WHERE p1.id_release <> p2.id_release
    ),
    cooc AS (
        SELECT r1, r2, COUNT(DISTINCT id_user) AS c
        FROM expanded
        GROUP BY 1, 2
        HAVING c >= ?
    ),
    base_counts AS (
        SELECT id_release, COUNT(DISTINCT id_user) AS total
        FROM positive
        GROUP BY id_release
    )
    INSERT OR REPLACE INTO release_pairs
        (id_release_1, id_release_2, pair_count, jaccard, lift, last_built_at)
    SELECT
        cooc.r1,
        cooc.r2,
        cooc.c,
        CAST(cooc.c AS REAL) / NULLIF(b1.total + b2.total - cooc.c, 0),
        CAST(cooc.c AS REAL) / NULLIF(b1.total * b2.total, 0),
        datetime('now')
    FROM cooc
    JOIN base_counts AS b1 ON b1.id_release = cooc.r1
    JOIN base_counts AS b2 ON b2.id_release = cooc.r2;
    """

    with connection:
        connection.execute("DELETE FROM release_pairs;")
        connection.execute(
            query,
            (min_rating, min_pair_count),
        )

    LOGGER.info("Rebuilt release_pairs")


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s - %(message)s")


def parse_arguments(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=str,
        help="Path to the Sputnik SQLite database (defaults to data/sputnik.db)",
    )
    parser.add_argument(
        "--min-rating",
        type=float,
        default=3.0,
        help="Minimum rating considered a positive interaction",
    )
    parser.add_argument(
        "--min-pair-count",
        type=int,
        default=3,
        help="Minimum co-occurrence count required to keep a pair",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_arguments(argv)
    configure_logging(args.verbose)

    database_path = resolve_database_path(args.database)
    LOGGER.info("Using database: %s", database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON;")
        connection.execute("PRAGMA journal_mode = WAL;")
        build_pairs(connection, args.min_rating, args.min_pair_count)

    LOGGER.info("Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
