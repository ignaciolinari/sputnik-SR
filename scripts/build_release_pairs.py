"""Utility script to rebuild the release_pairs co-occurrence table."""

from __future__ import annotations

import argparse
import logging
import sqlite3
from pathlib import Path
from time import perf_counter
from typing import Iterable


LOGGER = logging.getLogger("build_release_pairs")


def resolve_database_path(database: str | None) -> Path:
    if database:
        return Path(database).expanduser().resolve()
    env = Path(__file__).resolve().parents[1] / "data" / "sputnik.db"
    return env


def build_pairs(
    connection: sqlite3.Connection,
    min_rating: float,
    min_pair_count: int,
    batch_size: int = 250,
) -> None:
    total_start = perf_counter()
    LOGGER.info("Clearing existing release_pairs rows")
    connection.execute("DELETE FROM release_pairs;")

    LOGGER.info("Building co-occurrence matrix (min_rating=%.2f)", min_rating)

    setup_start = perf_counter()
    connection.execute("DROP TABLE IF EXISTS temp_pair_counts;")
    connection.execute(
        """
        CREATE TEMP TABLE temp_pair_counts (
            id_release_1 INTEGER NOT NULL,
            id_release_2 INTEGER NOT NULL,
            pair_count INTEGER NOT NULL,
            PRIMARY KEY (id_release_1, id_release_2)
        ) WITHOUT ROWID;
        """
    )

    connection.execute("DROP TABLE IF EXISTS temp_base_counts;")
    connection.execute(
        """
        CREATE TEMP TABLE temp_base_counts (
            id_release INTEGER NOT NULL PRIMARY KEY,
            total INTEGER NOT NULL
        ) WITHOUT ROWID;
        """
    )

    LOGGER.debug("Prepared temp tables in %.2fs", perf_counter() - setup_start)

    snapshot_start = perf_counter()
    connection.execute("DROP TABLE IF EXISTS temp_positive;")
    connection.execute(
        """
        CREATE TEMP TABLE temp_positive AS
        SELECT DISTINCT id_user, id_release
        FROM interactions
        WHERE rating >= ?
        """,
        (min_rating,),
    )

    connection.execute(
        "CREATE INDEX temp_positive_user_release ON temp_positive (id_user, id_release);"
    )
    connection.execute(
        "CREATE INDEX temp_positive_release_user ON temp_positive (id_release, id_user);"
    )

    connection.execute(
        """
        INSERT INTO temp_base_counts (id_release, total)
        SELECT id_release, COUNT(DISTINCT id_user) AS total
        FROM temp_positive
        GROUP BY id_release
        """
    )
    connection.commit()

    LOGGER.info(
        "Materialized positive interactions snapshot in %.2fs",
        perf_counter() - snapshot_start,
    )

    release_fetch_start = perf_counter()
    release_ids = [
        row[0]
        for row in connection.execute(
            "SELECT id_release FROM temp_base_counts ORDER BY id_release;"
        )
    ]
    total_releases = len(release_ids)
    LOGGER.debug(
        "Loaded %d release ids in %.2fs",
        total_releases,
        perf_counter() - release_fetch_start,
    )
    LOGGER.info("Found %d releases with qualifying interactions", total_releases)

    processed_releases = 0
    batch_index = 0
    total_pair_updates = 0
    for offset in range(0, total_releases, batch_size):
        batch = release_ids[offset : offset + batch_size]
        placeholders = ",".join("?" for _ in batch)
        batch_start = perf_counter()
        connection.execute(
            f"""
            INSERT INTO temp_pair_counts (id_release_1, id_release_2, pair_count)
            SELECT
                p1.id_release AS r1,
                p2.id_release AS r2,
                COUNT(DISTINCT p1.id_user) AS pair_count
            FROM temp_positive AS p1
            JOIN temp_positive AS p2
                ON p1.id_user = p2.id_user
               AND p1.id_release < p2.id_release
            WHERE p1.id_release IN ({placeholders})
            GROUP BY 1, 2
            ON CONFLICT(id_release_1, id_release_2)
                DO UPDATE SET pair_count = pair_count + excluded.pair_count
            """,
            batch,
        )
        connection.commit()

        changes = connection.execute("SELECT changes();").fetchone()[0]
        total_pair_updates += changes
        batch_elapsed = perf_counter() - batch_start
        batch_index += 1
        processed_releases = min(processed_releases + len(batch), total_releases)
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug(
                "Batch %d processed %d releases in %.2fs (+%d rows, %d/%d total)",
                batch_index,
                len(batch),
                batch_elapsed,
                changes,
                processed_releases,
                total_releases,
            )

    LOGGER.info("Aggregated co-occurrence counts for %d releases", total_releases)
    if LOGGER.isEnabledFor(logging.INFO):
        LOGGER.info(
            "Accumulated %d pair rows across %d batches in %.2fs",
            total_pair_updates,
            batch_index,
            perf_counter() - snapshot_start,
        )

    metrics_start = perf_counter()
    connection.execute(
        """
        WITH metrics AS (
            SELECT
                pairs.id_release_1 AS r1,
                pairs.id_release_2 AS r2,
                pairs.pair_count AS c,
                CAST(pairs.pair_count AS REAL)
                    / NULLIF(b1.total + b2.total - pairs.pair_count, 0) AS jaccard,
                CAST(pairs.pair_count AS REAL)
                    / NULLIF(b1.total * b2.total, 0) AS lift
            FROM temp_pair_counts AS pairs
            JOIN temp_base_counts AS b1 ON b1.id_release = pairs.id_release_1
            JOIN temp_base_counts AS b2 ON b2.id_release = pairs.id_release_2
            WHERE pairs.pair_count >= ?
        )
        INSERT OR REPLACE INTO release_pairs
            (id_release_1, id_release_2, pair_count, jaccard, lift, last_built_at)
        SELECT r1, r2, c, jaccard, lift, datetime('now') FROM metrics
        UNION ALL
        SELECT r2, r1, c, jaccard, lift, datetime('now') FROM metrics;
        """,
        (min_pair_count,),
    )
    connection.commit()

    LOGGER.info(
        "Computed metrics and populated release_pairs in %.2fs",
        perf_counter() - metrics_start,
    )

    connection.execute("DROP TABLE IF EXISTS temp_pair_counts;")
    connection.execute("DROP TABLE IF EXISTS temp_base_counts;")
    connection.execute("DROP TABLE IF EXISTS temp_positive;")

    total_pairs = connection.execute("SELECT COUNT(*) FROM release_pairs;").fetchone()[0]
    LOGGER.info("Rebuilt release_pairs (%d rows)", total_pairs)
    LOGGER.info("Total rebuild time: %.2fs", perf_counter() - total_start)


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
