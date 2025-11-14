"""Precompute NMF embeddings for users and releases.

This script trains a Non-negative Matrix Factorization model on user-item interactions
and stores the resulting embeddings in the database for fast recommendation inference.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from pathlib import Path
from time import perf_counter
from typing import Dict
from typing import List
from typing import Tuple

import numpy as np
from scipy import sparse
from sklearn.decomposition import NMF


LOGGER = logging.getLogger("build_nmf_embeddings")


def resolve_database_path(database: str | None) -> Path:
    if database:
        return Path(database).expanduser().resolve()
    return Path(__file__).resolve().parents[1] / "data" / "sputnik.db"


def load_user_item_matrix(
    connection: sqlite3.Connection,
    min_rating: float = 3.0,
    min_user_ratings: int = 5,
    min_release_ratings: int = 3,
) -> Tuple[np.ndarray, List[str], List[int], Dict[str, int], Dict[int, int]]:
    """Load user-item interaction matrix from database.

    Args:
        min_user_ratings: Minimum positive ratings per user to include
        min_release_ratings: Minimum positive ratings per release to include

    Returns:
        Tuple of:
        - Matrix (users x items) with ratings
        - List of user IDs in order
        - List of release IDs in order
        - Mapping: user_id -> row index
        - Mapping: release_id -> column index
    """
    LOGGER.info(
        "Loading user-item interactions "
        "(min_rating=%.2f, min_user_ratings=%d, min_release_ratings=%d)...",
        min_rating,
        min_user_ratings,
        min_release_ratings,
    )
    start = perf_counter()

    # Filter users and releases first to reduce matrix size
    LOGGER.info("Filtering users and releases...")
    user_filter_query = """
        SELECT id_user
        FROM interactions
        WHERE rating >= ?
        GROUP BY id_user
        HAVING COUNT(*) >= ?
    """
    eligible_users = {
        row[0] for row in connection.execute(user_filter_query, (min_rating, min_user_ratings))
    }
    LOGGER.info("Found %d eligible users", len(eligible_users))

    release_filter_query = """
        SELECT id_release
        FROM interactions
        WHERE rating >= ?
        GROUP BY id_release
        HAVING COUNT(*) >= ?
    """
    eligible_releases = {
        row[0]
        for row in connection.execute(release_filter_query, (min_rating, min_release_ratings))
    }
    LOGGER.info("Found %d eligible releases", len(eligible_releases))

    if not eligible_users or not eligible_releases:
        raise ValueError("No eligible users or releases found with given filters")

    # Use temporary tables to avoid SQL parameter limits
    LOGGER.info("Creating temporary filter tables...")
    connection.execute("DROP TABLE IF EXISTS temp_eligible_users;")
    connection.execute(
        "CREATE TEMP TABLE temp_eligible_users (id_user TEXT PRIMARY KEY) WITHOUT ROWID;"
    )
    connection.executemany(
        "INSERT INTO temp_eligible_users (id_user) VALUES (?);",
        [(uid,) for uid in eligible_users],
    )

    connection.execute("DROP TABLE IF EXISTS temp_eligible_releases;")
    connection.execute(
        "CREATE TEMP TABLE temp_eligible_releases (id_release INTEGER PRIMARY KEY) WITHOUT ROWID;"
    )
    connection.executemany(
        "INSERT INTO temp_eligible_releases (id_release) VALUES (?);",
        [(rid,) for rid in eligible_releases],
    )
    connection.commit()

    # Load positive interactions only for eligible users/releases
    query = """
        SELECT i.id_user, i.id_release, i.rating
        FROM interactions i
        INNER JOIN temp_eligible_users u ON u.id_user = i.id_user
        INNER JOIN temp_eligible_releases r ON r.id_release = i.id_release
        WHERE i.rating >= ?
        ORDER BY i.id_user, i.id_release
    """

    rows = connection.execute(query, (min_rating,)).fetchall()

    if not rows:
        raise ValueError("No interactions found with rating >= %.2f" % min_rating)

    # Build mappings
    user_ids = sorted(set(row["id_user"] for row in rows))
    release_ids = sorted(set(row["id_release"] for row in rows))

    user_to_idx = {user_id: idx for idx, user_id in enumerate(user_ids)}
    release_to_idx = {release_id: idx for idx, release_id in enumerate(release_ids)}

    # Build sparse matrix (CSR format for efficient NMF)
    LOGGER.info("Building sparse matrix...")
    import gc

    # Process in chunks to avoid memory spikes
    chunk_size = 1000000  # Process 1M rows at a time
    user_indices = []
    release_indices = []
    ratings = []

    for i in range(0, len(rows), chunk_size):
        chunk = rows[i : i + chunk_size]
        for row in chunk:
            user_idx = user_to_idx[row["id_user"]]
            release_idx = release_to_idx[row["id_release"]]
            user_indices.append(user_idx)
            release_indices.append(release_idx)
            ratings.append(float(row["rating"]))

        # Periodic garbage collection for large datasets
        if i > 0 and i % (chunk_size * 5) == 0:
            gc.collect()
            LOGGER.debug("Processed %d/%d rows, memory cleanup", i, len(rows))

    # Free row data before building matrix
    del rows
    gc.collect()

    matrix = sparse.csr_matrix(
        (ratings, (user_indices, release_indices)),
        shape=(len(user_ids), len(release_ids)),
        dtype=np.float32,
    )

    # Free intermediate lists
    del user_indices, release_indices, ratings
    gc.collect()

    elapsed = perf_counter() - start
    density = matrix.nnz / (len(user_ids) * len(release_ids))
    memory_mb = (matrix.data.nbytes + matrix.indices.nbytes + matrix.indptr.nbytes) / 1024**2
    LOGGER.info(
        "Loaded sparse matrix: %d users × %d releases, density=%.4f%%, "
        "nnz=%d, memory=%.1f MB (%.2fs)",
        len(user_ids),
        len(release_ids),
        density * 100,
        matrix.nnz,
        memory_mb,
        elapsed,
    )

    return matrix, user_ids, release_ids, user_to_idx, release_to_idx


def train_nmf(
    matrix: np.ndarray,
    n_components: int = 50,
    max_iter: int = 200,
    random_state: int = 42,
) -> Tuple[np.ndarray, np.ndarray]:
    """Train NMF model on user-item matrix.

    Returns:
        Tuple of (user_embeddings, item_embeddings)
        - user_embeddings: (n_users, n_components)
        - item_embeddings: (n_items, n_components)
    """
    LOGGER.info(
        "Training NMF model (n_components=%d, max_iter=%d)...",
        n_components,
        max_iter,
    )
    start = perf_counter()

    # Use alpha_W and alpha_H for newer sklearn versions, fallback to alpha for older
    try:
        model = NMF(
            n_components=n_components,
            max_iter=max_iter,
            random_state=random_state,
            alpha_W=0.01,  # L2 regularization for W (users)
            alpha_H=0.01,  # L2 regularization for H (items)
            l1_ratio=0.1,  # Mix of L1/L2 regularization
            verbose=1 if LOGGER.isEnabledFor(logging.DEBUG) else 0,
        )
    except TypeError:
        # Fallback for older sklearn versions
        model = NMF(
            n_components=n_components,
            max_iter=max_iter,
            random_state=random_state,
            alpha=0.01,  # L2 regularization
            l1_ratio=0.1,  # Mix of L1/L2 regularization
            verbose=1 if LOGGER.isEnabledFor(logging.DEBUG) else 0,
        )

    # NMF factorizes matrix as: matrix ≈ user_embeddings @ item_embeddings.T
    # So we get: W (users x components) and H (components x items)
    W = model.fit_transform(matrix)  # User embeddings
    H = model.components_  # Item embeddings (transposed)

    elapsed = perf_counter() - start
    reconstruction_error = model.reconstruction_err_

    LOGGER.info(
        "NMF training completed in %.2fs (reconstruction_error=%.4f)",
        elapsed,
        reconstruction_error,
    )

    return W, H.T  # Return item_embeddings as (n_items, n_components)


def save_embeddings(
    connection: sqlite3.Connection,
    user_ids: List[str],
    release_ids: List[int],
    user_embeddings: np.ndarray,
    item_embeddings: np.ndarray,
    n_components: int,
) -> None:
    """Save embeddings to database."""
    LOGGER.info("Saving embeddings to database...")
    start = perf_counter()

    # Create tables if they don't exist
    connection.execute("""
        CREATE TABLE IF NOT EXISTS user_embeddings (
            id_user TEXT PRIMARY KEY REFERENCES users(id_user),
            embedding_json TEXT NOT NULL,
            n_factors INTEGER NOT NULL,
            last_updated TEXT NOT NULL
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS release_embeddings (
            id_release INTEGER PRIMARY KEY REFERENCES releases(id_release),
            embedding_json TEXT NOT NULL,
            n_factors INTEGER NOT NULL,
            last_updated TEXT NOT NULL
        )
    """)

    # Clear existing embeddings
    connection.execute("DELETE FROM user_embeddings;")
    connection.execute("DELETE FROM release_embeddings;")

    # Save user embeddings
    user_data = []
    for idx, user_id in enumerate(user_ids):
        embedding = user_embeddings[idx].tolist()
        user_data.append(
            (
                user_id,
                json.dumps(embedding),
                n_components,
            )
        )

    connection.executemany(
        """
        INSERT INTO user_embeddings (id_user, embedding_json, n_factors, last_updated)
        VALUES (?, ?, ?, datetime('now'))
        """,
        user_data,
    )

    # Save release embeddings
    release_data = []
    for idx, release_id in enumerate(release_ids):
        embedding = item_embeddings[idx].tolist()
        release_data.append(
            (
                release_id,
                json.dumps(embedding),
                n_components,
            )
        )

    connection.executemany(
        """
        INSERT INTO release_embeddings (id_release, embedding_json, n_factors, last_updated)
        VALUES (?, ?, ?, datetime('now'))
        """,
        release_data,
    )

    connection.commit()

    elapsed = perf_counter() - start
    LOGGER.info(
        "Saved %d user embeddings and %d release embeddings (%.2fs)",
        len(user_ids),
        len(release_ids),
        elapsed,
    )


def build_embeddings(
    connection: sqlite3.Connection,
    min_rating: float = 3.0,
    n_components: int = 30,
    max_iter: int = 200,
    random_state: int = 42,
    min_user_ratings: int = 15,
    min_release_ratings: int = 10,
) -> None:
    """Main function to build and save NMF embeddings."""
    import gc

    total_start = perf_counter()

    # Load matrix
    matrix, user_ids, release_ids, _, _ = load_user_item_matrix(
        connection, min_rating, min_user_ratings, min_release_ratings
    )

    # Force garbage collection to free memory before training
    gc.collect()

    # Train NMF
    user_embeddings, item_embeddings = train_nmf(
        matrix, n_components=n_components, max_iter=max_iter, random_state=random_state
    )

    # Free matrix memory before saving
    del matrix
    gc.collect()

    # Save embeddings
    save_embeddings(
        connection,
        user_ids,
        release_ids,
        user_embeddings,
        item_embeddings,
        n_components,
    )

    LOGGER.info("Total time: %.2fs", perf_counter() - total_start)


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s - %(message)s")


def parse_arguments(argv: List[str] | None = None) -> argparse.Namespace:
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
        "--n-components",
        type=int,
        default=30,
        help="Number of latent factors (default: 30, reduced for low-memory systems)",
    )
    parser.add_argument(
        "--max-iter",
        type=int,
        default=200,
        help="Maximum iterations for NMF (default: 200)",
    )
    parser.add_argument(
        "--random-state",
        type=int,
        default=42,
        help="Random seed for reproducibility",
    )
    parser.add_argument(
        "--min-user-ratings",
        type=int,
        default=15,
        help=(
            "Minimum positive ratings per user to include "
            "(default: 15, increased for low-memory systems)"
        ),
    )
    parser.add_argument(
        "--min-release-ratings",
        type=int,
        default=10,
        help=(
            "Minimum positive ratings per release to include "
            "(default: 10, increased for low-memory systems)"
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = parse_arguments(argv)
    configure_logging(args.verbose)

    database_path = resolve_database_path(args.database)
    LOGGER.info("Using database: %s", database_path)

    if not database_path.exists():
        LOGGER.error("Database not found: %s", database_path)
        return 1

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
        connection.execute("PRAGMA journal_mode = WAL;")

        build_embeddings(
            connection,
            min_rating=args.min_rating,
            n_components=args.n_components,
            max_iter=args.max_iter,
            random_state=args.random_state,
            min_user_ratings=args.min_user_ratings,
            min_release_ratings=args.min_release_ratings,
        )

    LOGGER.info("Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
