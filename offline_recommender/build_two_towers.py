"""Precompute Two Towers embeddings for users and releases using Keras/TensorFlow.

This script trains a Two Towers deep learning model that learns separate embeddings
for users and items based on their features and interactions.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from typing import Dict
from typing import List
from typing import Tuple

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


try:
    import tensorflow as tf
    from tensorflow import keras
    from tensorflow.keras import layers
    from tensorflow.keras import regularizers
except ImportError:
    tf = None
    keras = None
    layers = None
    regularizers = None


try:
    from app import metrics as app_metrics
except ImportError:
    app_metrics = None


LOGGER = logging.getLogger("build_two_towers")


def resolve_database_path(database: str | None) -> Path:
    """Resolve database path."""
    if database:
        return Path(database).expanduser().resolve()
    return Path(__file__).resolve().parents[1] / "data" / "sputnik.db"


def extract_user_features(
    connection: sqlite3.Connection, user_id: str, now: datetime | None = None
) -> Dict[str, float | int]:
    """Extract user features from database.

    Returns:
        Dictionary with user features:
        - role_idx: integer index for role (0-9)
        - objectivity_score: normalized (0-1)
        - soundoffs: log normalized
        - ratings_count: log normalized
        - days_since_join: normalized
        - days_since_active: normalized
    """
    if now is None:
        now = datetime.utcnow()

    cursor = connection.execute(
        """
        SELECT role, join_date, last_active, objectivity_score, soundoffs, ratings_count
        FROM users
        WHERE id_user = ?;
        """,
        (user_id,),
    )
    row = cursor.fetchone()

    if not row:
        # Return default features for missing users
        return {
            "role_idx": 0,
            "objectivity_score": 0.5,
            "soundoffs": 0.0,
            "ratings_count": 0.0,
            "days_since_join": 0.0,
            "days_since_active": 0.0,
        }

    role_str = row["role"] or "user"
    join_date_str = row["join_date"]
    last_active_str = row["last_active"]
    objectivity_score = float(row["objectivity_score"] or 50.0)
    soundoffs = int(row["soundoffs"] or 0)
    ratings_count = int(row["ratings_count"] or 0)

    # Map role to index (simplified: 0=user, 1=admin, 2=mod, etc.)
    role_map = {
        "user": 0,
        "admin": 1,
        "moderator": 2,
        "contributor": 3,
        "staff": 4,
    }
    role_idx = role_map.get(role_str.lower(), 0)

    # Normalize objectivity_score (0-100 -> 0-1)
    objectivity_norm = max(0.0, min(1.0, objectivity_score / 100.0))

    # Log normalize soundoffs and ratings_count
    soundoffs_norm = math.log1p(soundoffs)
    ratings_count_norm = math.log1p(ratings_count)

    # Calculate days since join and last active
    days_since_join = 0.0
    if join_date_str:
        try:
            join_date = datetime.fromisoformat(join_date_str.replace("Z", "+00:00"))
            days_since_join = max(0.0, (now - join_date.replace(tzinfo=None)).days)
        except (ValueError, AttributeError):
            days_since_join = 0.0

    days_since_active = 0.0
    if last_active_str:
        try:
            last_active = datetime.fromisoformat(last_active_str.replace("Z", "+00:00"))
            days_since_active = max(0.0, (now - last_active.replace(tzinfo=None)).days)
        except (ValueError, AttributeError):
            days_since_active = 0.0

    # Normalize days (assume max 20 years = 7300 days)
    days_since_join_norm = min(1.0, days_since_join / 7300.0)
    days_since_active_norm = min(1.0, days_since_active / 365.0)  # Max 1 year inactive

    return {
        "role_idx": role_idx,
        "objectivity_score": objectivity_norm,
        "soundoffs": soundoffs_norm,
        "ratings_count": ratings_count_norm,
        "days_since_join": days_since_join_norm,
        "days_since_active": days_since_active_norm,
    }


def extract_item_features(connection: sqlite3.Connection, release_id: int) -> Dict[str, Any]:
    """Extract item (release) features from database.

    Returns:
        Dictionary with item features:
        - artist_id: integer artist ID
        - release_type_idx: integer index (0=LP, 1=EP, 2=Single, 3=Compilation)
        - genre_ids: list of genre IDs
        - release_year_norm: normalized year (0-1)
        - avg_rating_norm: normalized average rating (0-1)
        - ratings_count_norm: log normalized ratings count
    """
    cursor = connection.execute(
        """
        SELECT artist_id, release_type, release_year, avg_rating, ratings_count
        FROM releases
        WHERE id_release = ?;
        """,
        (release_id,),
    )
    row = cursor.fetchone()

    if not row:
        return {
            "artist_id": 0,
            "release_type_idx": 0,
            "genre_ids": [],
            "release_year_norm": 0.5,
            "avg_rating_norm": 0.5,
            "ratings_count_norm": 0.0,
        }

    artist_id = int(row["artist_id"] or 0)
    release_type_str = row["release_type"] or "LP"
    release_year = row["release_year"]
    avg_rating = float(row["avg_rating"] or 0.0)
    ratings_count = int(row["ratings_count"] or 0)

    # Map release_type to index
    type_map = {"LP": 0, "EP": 1, "Single": 2, "Compilation": 3}
    release_type_idx = type_map.get(release_type_str, 0)

    # Normalize release_year (assume 1900-2100 range -> 0-1)
    release_year_norm = 0.5
    if release_year:
        release_year_norm = max(0.0, min(1.0, (release_year - 1900) / 200.0))

    # Normalize avg_rating (0-5 -> 0-1)
    avg_rating_norm = max(0.0, min(1.0, avg_rating / 5.0))

    # Log normalize ratings_count
    ratings_count_norm = math.log1p(ratings_count)

    # Get genre IDs
    genre_cursor = connection.execute(
        """
        SELECT id_genre
        FROM release_genres
        WHERE id_release = ?;
        """,
        (release_id,),
    )
    genre_ids = [int(row["id_genre"]) for row in genre_cursor.fetchall()]

    return {
        "artist_id": artist_id,
        "release_type_idx": release_type_idx,
        "genre_ids": genre_ids,
        "release_year_norm": release_year_norm,
        "avg_rating_norm": avg_rating_norm,
        "ratings_count_norm": ratings_count_norm,
    }


def build_user_tower(
    num_users: int,
    num_roles: int = 10,
    embedding_dim: int = 64,
) -> keras.Model:
    """Build user tower model with ID embeddings."""

    user_id_input = layers.Input(shape=(), name="user_id", dtype="int32")
    role_input = layers.Input(shape=(), name="user_role", dtype="int32")
    numeric_input = layers.Input(shape=(5,), name="user_numeric")

    user_id_emb = layers.Embedding(
        num_users,
        embedding_dim,
        embeddings_regularizer=regularizers.l2(1e-6),
        name="user_id_embedding",
    )(user_id_input)

    role_emb = layers.Embedding(num_roles, 8, name="role_embedding")(role_input)
    role_emb = layers.Flatten()(role_emb)

    numeric_emb = layers.Dense(32, activation="relu", name="numeric_dense_1")(numeric_input)
    numeric_emb = layers.Dropout(0.2, name="numeric_dropout_1")(numeric_emb)
    numeric_emb = layers.Dense(32, activation="relu", name="numeric_dense_2")(numeric_emb)

    combined = layers.Concatenate(name="user_combine")([user_id_emb, role_emb, numeric_emb])
    combined = layers.Dense(embedding_dim, activation="relu", name="user_dense_1")(combined)
    combined = layers.Dropout(0.3, name="user_dropout")(combined)
    output = layers.Dense(embedding_dim, name="user_output")(combined)

    def l2_normalize_user(x):
        return tf.nn.l2_normalize(x, axis=1)

    output = layers.Lambda(l2_normalize_user, output_shape=(embedding_dim,), name="user_normalize")(
        output
    )

    return keras.Model(
        inputs=[user_id_input, role_input, numeric_input], outputs=output, name="user_tower"
    )


def build_item_tower(
    num_releases: int,
    num_artists: int,
    num_genres: int,
    embedding_dim: int = 64,
) -> keras.Model:
    """Build item tower model with ID embeddings and masked genre pooling."""

    item_id_input = layers.Input(shape=(), name="item_id", dtype="int32")
    artist_input = layers.Input(shape=(), name="item_artist", dtype="int32")
    type_input = layers.Input(shape=(), name="item_type", dtype="int32")
    genres_input = layers.Input(shape=(None,), name="item_genres", dtype="int32")
    numeric_input = layers.Input(shape=(3,), name="item_numeric")

    item_id_emb = layers.Embedding(
        num_releases,
        embedding_dim,
        embeddings_regularizer=regularizers.l2(1e-6),
        name="item_id_embedding",
    )(item_id_input)

    artist_emb = layers.Embedding(num_artists, 16, name="artist_embedding")(artist_input)
    artist_emb = layers.Flatten()(artist_emb)

    type_emb = layers.Embedding(4, 4, name="type_embedding")(type_input)
    type_emb = layers.Flatten()(type_emb)

    genre_emb = layers.Embedding(num_genres, 8, name="genre_embedding")(genres_input)

    def masked_genre_average(inputs):
        embeddings, ids = inputs
        mask = tf.cast(tf.not_equal(ids, 0), tf.float32)
        mask = tf.expand_dims(mask, axis=-1)
        summed = tf.reduce_sum(embeddings * mask, axis=1)
        counts = tf.reduce_sum(mask, axis=1)
        counts = tf.maximum(counts, tf.ones_like(counts))
        return summed / counts

    genres_emb = layers.Lambda(masked_genre_average, name="genre_pooling")(
        [genre_emb, genres_input]
    )

    numeric_emb = layers.Dense(16, activation="relu", name="item_numeric_dense_1")(numeric_input)
    numeric_emb = layers.Dropout(0.2, name="item_numeric_dropout_1")(numeric_emb)
    numeric_emb = layers.Dense(16, activation="relu", name="item_numeric_dense_2")(numeric_emb)

    combined = layers.Concatenate(name="item_combine")(
        [item_id_emb, artist_emb, type_emb, genres_emb, numeric_emb]
    )
    combined = layers.Dense(embedding_dim, activation="relu", name="item_dense_1")(combined)
    combined = layers.Dropout(0.3, name="item_dropout")(combined)
    output = layers.Dense(embedding_dim, name="item_output")(combined)

    def l2_normalize_item(x):
        return tf.nn.l2_normalize(x, axis=1)

    output = layers.Lambda(l2_normalize_item, output_shape=(embedding_dim,), name="item_normalize")(
        output
    )

    return keras.Model(
        inputs=[item_id_input, artist_input, type_input, genres_input, numeric_input],
        outputs=output,
        name="item_tower",
    )


def build_two_tower_model(
    num_users: int,
    num_roles: int,
    num_releases: int,
    num_artists: int,
    num_genres: int,
    embedding_dim: int = 64,
) -> Tuple[keras.Model, keras.Model, keras.Model]:
    """Build complete Two Towers model.

    Returns:
        Tuple of (combined_model, user_tower, item_tower)
    """
    user_tower = build_user_tower(
        num_users=num_users, num_roles=num_roles, embedding_dim=embedding_dim
    )
    item_tower = build_item_tower(
        num_releases=num_releases,
        num_artists=num_artists,
        num_genres=num_genres,
        embedding_dim=embedding_dim,
    )

    # Combined model for training
    user_id_input = layers.Input(shape=(), name="user_id", dtype="int32")
    user_role_input = layers.Input(shape=(), name="user_role", dtype="int32")
    user_numeric_input = layers.Input(shape=(5,), name="user_numeric")
    item_id_input = layers.Input(shape=(), name="item_id", dtype="int32")
    item_artist_input = layers.Input(shape=(), name="item_artist", dtype="int32")
    item_type_input = layers.Input(shape=(), name="item_type", dtype="int32")
    item_genres_input = layers.Input(shape=(None,), name="item_genres", dtype="int32")
    item_numeric_input = layers.Input(shape=(3,), name="item_numeric")

    user_emb = user_tower([user_id_input, user_role_input, user_numeric_input])
    item_emb = item_tower(
        [item_id_input, item_artist_input, item_type_input, item_genres_input, item_numeric_input]
    )

    # Dot product (score) - embeddings are L2 normalized, so dot product is in [-1, 1]
    dot_score = layers.Dot(axes=1, normalize=False, name="dot_score")([user_emb, item_emb])

    # Map dot product [-1, 1] to rating range using a learned transformation
    # Add bias and scale to learn the mapping from similarity to rating
    # Output shape: (batch_size, 1)
    score = layers.Dense(1, activation=None, use_bias=True, name="score")(dot_score)

    combined_model = keras.Model(
        inputs=[
            user_id_input,
            user_role_input,
            user_numeric_input,
            item_id_input,
            item_artist_input,
            item_type_input,
            item_genres_input,
            item_numeric_input,
        ],
        outputs=score,
        name="two_tower_model",
    )

    return combined_model, user_tower, item_tower


def load_training_data(
    connection: sqlite3.Connection,
    min_rating: float = 3.0,
    min_user_ratings: int = 5,
    min_release_ratings: int = 3,
    sample_size: int | None = None,
) -> Tuple[
    Dict[str, Dict[str, float | int]],
    Dict[int, Dict[str, Any]],
    List[Tuple[str, int, float]],
    Dict[str, int],
    Dict[int, int],
    Dict[int, int],
    Dict[int, int],
    int,
    int,
]:
    """Load training data from database.

    Returns:
        Tuple of:
        - user_features: Dict[user_id, features]
        - item_features: Dict[release_id, features]
        - interactions: List[(user_id, release_id, rating)]
        - user_to_idx: Mapping user_id -> index
        - release_to_idx: Mapping release_id -> index
        - artist_to_idx: Mapping artist_id -> consecutive index (0, 1, 2, ...)
        - num_artists: Number of unique artists in interactions
        - num_genres: Total number of genres
    """
    LOGGER.info(
        "Loading training data (min_rating=%.2f, min_user_ratings=%d, min_release_ratings=%d)...",
        min_rating,
        min_user_ratings,
        min_release_ratings,
    )
    start = perf_counter()

    # Filter eligible users and releases
    user_count = connection.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT id_user
            FROM interactions
            WHERE rating >= ?
            GROUP BY id_user
            HAVING COUNT(*) >= ?
        )
        """,
        (min_rating, min_user_ratings),
    ).fetchone()[0]
    LOGGER.info("Found %d eligible users", user_count)

    release_count = connection.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT id_release
            FROM interactions
            WHERE rating >= ?
            GROUP BY id_release
            HAVING COUNT(*) >= ?
        )
        """,
        (min_rating, min_release_ratings),
    ).fetchone()[0]
    LOGGER.info("Found %d eligible releases", release_count)

    if not user_count or not release_count:
        raise ValueError("No eligible users or releases found with given filters")

    # Load interactions using CTEs to avoid massive IN clauses
    interactions_query = """
        WITH eligible_users AS (
            SELECT id_user
            FROM interactions
            WHERE rating >= :rating_threshold
            GROUP BY id_user
            HAVING COUNT(*) >= :min_user_ratings
        ),
        eligible_releases AS (
            SELECT id_release
            FROM interactions
            WHERE rating >= :rating_threshold
            GROUP BY id_release
            HAVING COUNT(*) >= :min_release_ratings
        )
        SELECT i.id_user, i.id_release, i.rating
        FROM interactions AS i
        JOIN eligible_users eu ON eu.id_user = i.id_user
        JOIN eligible_releases er ON er.id_release = i.id_release
        WHERE i.rating >= :rating_threshold
        ORDER BY i.rating DESC
    """
    if sample_size:
        interactions_query += " LIMIT :sample_size"

    query_params = {
        "rating_threshold": min_rating,
        "min_user_ratings": min_user_ratings,
        "min_release_ratings": min_release_ratings,
    }
    if sample_size:
        query_params["sample_size"] = sample_size

    interactions_rows = connection.execute(interactions_query, query_params).fetchall()

    interactions = [
        (row["id_user"], int(row["id_release"]), float(row["rating"])) for row in interactions_rows
    ]
    LOGGER.info("Loaded %d interactions", len(interactions))

    # Extract user features
    LOGGER.info("Extracting user features...")
    now = datetime.utcnow()
    user_features = {}
    user_ids = sorted(set(user_id for user_id, _, _ in interactions))
    for user_id in user_ids:
        user_features[user_id] = extract_user_features(connection, user_id, now)

    # Build genre vocabulary (reserve 0 for padding / unknown)
    LOGGER.info("Building genre vocabulary...")
    genre_rows = connection.execute(
        """
        SELECT id_genre
        FROM genres
        ORDER BY id_genre
        """
    ).fetchall()
    genre_to_idx = {int(row[0]): idx + 1 for idx, row in enumerate(genre_rows)}
    num_genres = (len(genre_to_idx) + 1) if genre_to_idx else 1

    # Extract item features
    LOGGER.info("Extracting item features...")
    item_features = {}
    release_ids = sorted(set(release_id for _, release_id, _ in interactions))
    for release_id in release_ids:
        release_features = extract_item_features(connection, release_id)
        raw_genres = release_features.get("genre_ids", [])
        release_features["genre_ids"] = [genre_to_idx.get(gid, 0) for gid in raw_genres]
        item_features[release_id] = release_features

    # Get unique artist IDs from interactions (not all artists in DB)
    unique_artist_ids = sorted(
        set(item_features[release_id]["artist_id"] for release_id in release_ids)
    )
    artist_to_idx = {artist_id: idx + 1 for idx, artist_id in enumerate(unique_artist_ids)}
    num_artists = len(artist_to_idx) + 1  # Reserve 0 for unknown artists

    # Create mappings
    user_to_idx = {user_id: idx + 1 for idx, user_id in enumerate(user_ids)}
    release_to_idx = {release_id: idx + 1 for idx, release_id in enumerate(release_ids)}

    elapsed = perf_counter() - start
    LOGGER.info("Data loading completed in %.2fs", elapsed)

    return (
        user_features,
        item_features,
        interactions,
        user_to_idx,
        release_to_idx,
        artist_to_idx,
        genre_to_idx,
        num_artists,
        num_genres,
    )


def prepare_batch_data(
    interactions: List[Tuple[str, int, float]],
    user_features: Dict[str, Dict[str, float | int]],
    item_features: Dict[int, Dict[str, Any]],
    user_to_idx: Dict[str, int],
    release_to_idx: Dict[int, int],
    artist_to_idx: Dict[int, int],
    release_ids: List[int],
    num_negatives: int,
    max_genres: int,
    seed: int,
) -> Dict[str, np.ndarray]:
    """Prepare positive + sampled negative pairs for training."""

    rng = np.random.default_rng(seed)
    release_pool = np.array(release_ids, dtype=np.int32)

    batch_data: Dict[str, List[Any]] = {
        "user_id_idx": [],
        "user_role": [],
        "user_numeric": [],
        "item_id_idx": [],
        "item_artist": [],
        "item_type": [],
        "item_genres": [],
        "item_numeric": [],
        "labels": [],
    }

    def pad_genres(raw_ids: List[int] | None) -> List[int]:
        genre_ids = list(raw_ids or [])
        if len(genre_ids) >= max_genres:
            return genre_ids[:max_genres]
        return genre_ids + [0] * (max_genres - len(genre_ids))

    def append_example(user_id: str, release_id: int, label: float) -> None:
        user_feat = user_features[user_id]
        item_feat = item_features[release_id]

        batch_data["user_id_idx"].append(user_to_idx.get(user_id, 0))
        batch_data["user_role"].append(int(user_feat["role_idx"]))
        batch_data["user_numeric"].append(
            [
                float(user_feat["objectivity_score"]),
                float(user_feat["soundoffs"]),
                float(user_feat["ratings_count"]),
                float(user_feat["days_since_join"]),
                float(user_feat["days_since_active"]),
            ]
        )

        batch_data["item_id_idx"].append(release_to_idx.get(release_id, 0))
        batch_data["item_artist"].append(artist_to_idx.get(int(item_feat["artist_id"]), 0))
        batch_data["item_type"].append(int(item_feat["release_type_idx"]))
        raw_genres = item_feat.get("genre_ids", [])
        genre_list = raw_genres if isinstance(raw_genres, list) else []
        batch_data["item_genres"].append(pad_genres(genre_list))
        batch_data["item_numeric"].append(
            [
                float(item_feat["release_year_norm"]),
                float(item_feat["avg_rating_norm"]),
                float(item_feat["ratings_count_norm"]),
            ]
        )
        batch_data["labels"].append(label)

    for user_id, release_id, _ in interactions:
        append_example(user_id, release_id, 1.0)

        for _ in range(num_negatives):
            neg_release = int(rng.choice(release_pool))
            # Avoid sampling the same positive item
            while neg_release == release_id:
                neg_release = int(rng.choice(release_pool))
            append_example(user_id, neg_release, 0.0)

    # Convert to numpy arrays
    np_data: Dict[str, np.ndarray] = {
        "user_id_idx": np.array(batch_data["user_id_idx"], dtype=np.int32),
        "user_role": np.array(batch_data["user_role"], dtype=np.int32),
        "user_numeric": np.array(batch_data["user_numeric"], dtype=np.float32),
        "item_id_idx": np.array(batch_data["item_id_idx"], dtype=np.int32),
        "item_artist": np.array(batch_data["item_artist"], dtype=np.int32),
        "item_type": np.array(batch_data["item_type"], dtype=np.int32),
        "item_genres": np.array(batch_data["item_genres"], dtype=np.int32),
        "item_numeric": np.array(batch_data["item_numeric"], dtype=np.float32),
        "labels": np.array(batch_data["labels"], dtype=np.float32).reshape(-1, 1),
    }

    # Shuffle to avoid ordering effects
    indices = rng.permutation(np_data["labels"].shape[0])
    for key in np_data:
        np_data[key] = np_data[key][indices]

    return np_data


def build_positive_lookup(interactions: List[Tuple[str, int, float]]) -> Dict[str, set[int]]:
    """Return positive interactions per user for filtering during evaluation."""

    positives: Dict[str, set[int]] = defaultdict(set)
    for user_id, release_id, _ in interactions:
        positives[user_id].add(release_id)
    return positives


def split_interactions_for_ndcg(
    interactions: List[Tuple[str, int, float]],
    holdout_fraction: float,
    min_test_items: int,
    seed: int,
) -> Tuple[List[Tuple[str, int, float]], Dict[str, set[int]]]:
    """Split per-user interactions into train and evaluation subsets."""

    if not 0.0 < holdout_fraction < 1.0:
        raise ValueError("holdout_fraction must be between 0 and 1")

    rng = random.Random(seed)
    per_user: Dict[str, List[Tuple[int, float]]] = defaultdict(list)
    for user_id, release_id, rating in interactions:
        per_user[user_id].append((release_id, rating))

    train_interactions: List[Tuple[str, int, float]] = []
    holdout_items: Dict[str, set[int]] = {}

    for user_id, items in per_user.items():
        if len(items) <= min_test_items:
            train_interactions.extend((user_id, release_id, rating) for release_id, rating in items)
            continue

        items_copy = list(items)
        rng.shuffle(items_copy)

        desired_holdout = max(min_test_items, int(round(len(items_copy) * holdout_fraction)))
        if desired_holdout >= len(items_copy):
            desired_holdout = len(items_copy) - 1

        if desired_holdout <= 0:
            train_subset = items_copy
            holdout_subset: List[Tuple[int, float]] = []
        else:
            holdout_subset = items_copy[:desired_holdout]
            train_subset = items_copy[desired_holdout:]

        if not train_subset:
            train_subset = items_copy[-1:]
            holdout_subset = items_copy[:-1]

        if holdout_subset:
            holdout_items[user_id] = {release_id for release_id, _ in holdout_subset}

        train_interactions.extend(
            (user_id, release_id, rating) for release_id, rating in train_subset
        )

    return train_interactions, holdout_items


def evaluate_two_tower_ndcg(
    user_tower: keras.Model,
    item_tower: keras.Model,
    user_features: Dict[str, Dict[str, float | int]],
    item_features: Dict[int, Dict[str, Any]],
    user_to_idx: Dict[str, int],
    release_to_idx: Dict[int, int],
    artist_to_idx: Dict[int, int],
    train_positive_items: Dict[str, set[int]],
    holdout_items: Dict[str, set[int]],
    max_genres: int,
    k: int,
    max_users: int | None = None,
) -> Tuple[float, int]:
    """Compute NDCG@k on holdout positives."""

    if app_metrics is None:
        raise ImportError(
            "app.metrics is required for NDCG evaluation. Make sure the app module is importable."
        )

    if not holdout_items:
        LOGGER.warning("NDCG evaluation requested but no holdout interactions were generated")
        return 0.0, 0

    release_ids = sorted(release_to_idx.keys())
    if not release_ids:
        return 0.0, 0

    def pad_genres(raw_ids: List[int] | None) -> List[int]:
        genre_ids = list(raw_ids or [])
        if len(genre_ids) >= max_genres:
            return genre_ids[:max_genres]
        return genre_ids + [0] * (max_genres - len(genre_ids))

    release_array = np.array(release_ids, dtype=np.int32)
    release_idx_lookup = {release_id: idx for idx, release_id in enumerate(release_ids)}

    item_id_idx = np.array(
        [release_to_idx[release_id] for release_id in release_ids], dtype=np.int32
    )
    item_artist = np.array(
        [
            artist_to_idx.get(int(item_features[release_id]["artist_id"]), 0)
            for release_id in release_ids
        ],
        dtype=np.int32,
    )
    item_type = np.array(
        [int(item_features[release_id]["release_type_idx"]) for release_id in release_ids],
        dtype=np.int32,
    )
    item_genres = np.array(
        [pad_genres(item_features[release_id].get("genre_ids", [])) for release_id in release_ids],
        dtype=np.int32,
    )
    item_numeric = np.array(
        [
            [
                float(item_features[release_id]["release_year_norm"]),
                float(item_features[release_id]["avg_rating_norm"]),
                float(item_features[release_id]["ratings_count_norm"]),
            ]
            for release_id in release_ids
        ],
        dtype=np.float32,
    )

    item_embeddings = item_tower.predict(
        [item_id_idx, item_artist, item_type, item_genres, item_numeric],
        batch_size=2048,
        verbose=0,
    )

    ndcg_scores: List[float] = []
    evaluated_users = 0

    for user_id, test_items in holdout_items.items():
        if max_users is not None and evaluated_users >= max_users:
            break

        user_feat = user_features.get(user_id)
        if not user_feat:
            continue

        train_items = train_positive_items.get(user_id, set())
        candidate_mask = np.ones(len(release_ids), dtype=bool)
        for release_id in train_items:
            idx = release_idx_lookup.get(release_id)
            if idx is not None:
                candidate_mask[idx] = False

        if not candidate_mask.any():
            continue

        user_inputs = [
            np.array([user_to_idx.get(user_id, 0)], dtype=np.int32),
            np.array([int(user_feat["role_idx"])], dtype=np.int32),
            np.array(
                [
                    [
                        float(user_feat["objectivity_score"]),
                        float(user_feat["soundoffs"]),
                        float(user_feat["ratings_count"]),
                        float(user_feat["days_since_join"]),
                        float(user_feat["days_since_active"]),
                    ]
                ],
                dtype=np.float32,
            ),
        ]

        user_embedding = user_tower.predict(user_inputs, verbose=0)[0]

        candidate_embeddings = item_embeddings[candidate_mask]
        candidate_ids = release_array[candidate_mask]

        if candidate_embeddings.size == 0:
            continue

        scores = candidate_embeddings @ user_embedding

        if scores.size <= k:
            ranking_indices = np.argsort(scores)[::-1]
        else:
            top_idx = np.argpartition(scores, -k)[-k:]
            ranking_indices = top_idx[np.argsort(scores[top_idx])[::-1]]

        top_k_ids = candidate_ids[ranking_indices][:k]
        relevance = [1.0 if release_id in test_items else 0.0 for release_id in top_k_ids]

        ndcg_scores.append(app_metrics.normalized_discounted_cumulative_gain(relevance))
        evaluated_users += 1

    if not ndcg_scores:
        return 0.0, 0

    return float(sum(ndcg_scores) / len(ndcg_scores)), evaluated_users


def train_model(
    model: keras.Model,
    train_data: Dict[str, np.ndarray],
    val_data: Dict[str, np.ndarray] | None = None,
    epochs: int = 10,
    batch_size: int = 1024,
    learning_rate: float = 0.001,
    positive_class_weight: float = 1.0,
    checkpoint_path: Path | None = None,
) -> keras.callbacks.History:
    """Train the Two Towers model with binary cross-entropy."""

    LOGGER.info("Compiling model...")
    model.compile(
        optimizer=keras.optimizers.Adam(learning_rate=learning_rate),
        loss=keras.losses.BinaryCrossentropy(from_logits=True),
        metrics=[
            keras.metrics.BinaryAccuracy(name="binary_accuracy", threshold=0.0),
            keras.metrics.AUC(name="auc"),
        ],
    )

    monitor_metric = "val_auc" if val_data is not None else "auc"
    callbacks = [
        keras.callbacks.EarlyStopping(
            monitor=monitor_metric,
            patience=4,
            restore_best_weights=True,
            mode="max",
            verbose=1,
        ),
        keras.callbacks.ReduceLROnPlateau(
            monitor=monitor_metric,
            patience=3,
            factor=0.5,
            min_lr=1e-6,
            mode="max",
            verbose=1,
        ),
    ]

    if checkpoint_path is not None:
        checkpoint_path = checkpoint_path.expanduser()
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        callbacks.append(
            keras.callbacks.ModelCheckpoint(
                filepath=str(checkpoint_path),
                monitor=monitor_metric,
                mode="max",
                save_best_only=True,
                save_weights_only=True,
                verbose=1,
            )
        )

    positive_weight = max(1.0, positive_class_weight)
    class_weight = {0: 1.0, 1: positive_weight}

    LOGGER.info("Training model (epochs=%d, batch_size=%d)...", epochs, batch_size)
    start = perf_counter()

    train_inputs = [
        train_data["user_id_idx"],
        train_data["user_role"],
        train_data["user_numeric"],
        train_data["item_id_idx"],
        train_data["item_artist"],
        train_data["item_type"],
        train_data["item_genres"],
        train_data["item_numeric"],
    ]

    if val_data:
        val_inputs = [
            val_data["user_id_idx"],
            val_data["user_role"],
            val_data["user_numeric"],
            val_data["item_id_idx"],
            val_data["item_artist"],
            val_data["item_type"],
            val_data["item_genres"],
            val_data["item_numeric"],
        ]
        validation = (val_inputs, val_data["labels"])
    else:
        validation = None

    history = model.fit(
        x=train_inputs,
        y=train_data["labels"],
        epochs=epochs,
        batch_size=batch_size,
        validation_data=validation,
        callbacks=callbacks,
        verbose=2,
        class_weight=class_weight,
    )

    elapsed = perf_counter() - start
    LOGGER.info("Training completed in %.2fs", elapsed)

    return history


def save_embeddings(
    connection: sqlite3.Connection,
    user_tower: keras.Model,
    item_tower: keras.Model,
    user_ids: List[str],
    release_ids: List[int],
    user_features: Dict[str, Dict[str, float | int]],
    item_features: Dict[int, Dict[str, Any]],
    artist_to_idx: Dict[int, int],
    user_to_idx: Dict[str, int],
    release_to_idx: Dict[int, int],
    embedding_dim: int,
    model_version: str = "1.0",
    max_genres: int = 10,
    num_negatives: int = 4,
    random_seed: int = 2024,
) -> None:
    """Save embeddings to database.

    Creates tables user_embeddings_dl and release_embeddings_dl if they don't exist.
    """
    LOGGER.info("Saving embeddings to database...")
    start = perf_counter()

    # Create tables if they don't exist
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS user_embeddings_dl (
            id_user TEXT PRIMARY KEY REFERENCES users(id_user),
            embedding_json TEXT NOT NULL,
            embedding_dim INTEGER NOT NULL,
            model_version TEXT NOT NULL,
            last_updated TEXT NOT NULL
        )
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS release_embeddings_dl (
            id_release INTEGER PRIMARY KEY REFERENCES releases(id_release),
            embedding_json TEXT NOT NULL,
            embedding_dim INTEGER NOT NULL,
            model_version TEXT NOT NULL,
            last_updated TEXT NOT NULL
        )
        """
    )

    # Clear existing embeddings
    connection.execute("DELETE FROM user_embeddings_dl;")
    connection.execute("DELETE FROM release_embeddings_dl;")

    # Save user embeddings
    LOGGER.info("Computing user embeddings...")
    user_embeddings_data = []
    batch_size = 1000

    for i in range(0, len(user_ids), batch_size):
        batch_user_ids = user_ids[i : i + batch_size]
        batch_user_features = [user_features[uid] for uid in batch_user_ids]

        user_idx_batch = np.array(
            [user_to_idx.get(uid, 0) for uid in batch_user_ids], dtype=np.int32
        )
        user_roles_batch = np.array([uf["role_idx"] for uf in batch_user_features], dtype=np.int32)
        user_numeric_batch = np.array(
            [
                [
                    uf["objectivity_score"],
                    uf["soundoffs"],
                    uf["ratings_count"],
                    uf["days_since_join"],
                    uf["days_since_active"],
                ]
                for uf in batch_user_features
            ],
            dtype=np.float32,
        )

        embeddings = user_tower.predict(
            [user_idx_batch, user_roles_batch, user_numeric_batch],
            verbose=0,
            batch_size=batch_size,
        )

        # Validate that we got the expected number of embeddings
        if len(embeddings) != len(batch_user_ids):
            LOGGER.error(
                "Mismatch: generados %d embeddings pero esperados %d usuarios",
                len(embeddings),
                len(batch_user_ids),
            )
            continue

        # Nota: No usar strict=False aquí para compatibilidad con Python < 3.10 (PythonAnywhere)
        for user_id, embedding in zip(batch_user_ids, embeddings):  # noqa: B905
            user_embeddings_data.append(
                (
                    user_id,
                    json.dumps(embedding.tolist()),
                    embedding_dim,
                    model_version,
                    datetime.utcnow().isoformat(),
                )
            )

    connection.executemany(
        """
        INSERT INTO user_embeddings_dl (
            id_user, embedding_json, embedding_dim, model_version, last_updated
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        user_embeddings_data,
    )

    # Save release embeddings
    LOGGER.info("Computing release embeddings...")
    release_embeddings_data = []

    for i in range(0, len(release_ids), batch_size):
        batch_release_ids = release_ids[i : i + batch_size]
        batch_item_features = [item_features[rid] for rid in batch_release_ids]

        item_id_batch = np.array(
            [release_to_idx.get(rid, 0) for rid in batch_release_ids], dtype=np.int32
        )
        item_artists_batch = np.array(
            [artist_to_idx.get(int(if_["artist_id"]), 0) for if_ in batch_item_features],
            dtype=np.int32,
        )
        item_types_batch = np.array(
            [if_["release_type_idx"] for if_ in batch_item_features], dtype=np.int32
        )

        # Prepare genres (pad/truncate)
        item_genres_batch_list = []
        for if_ in batch_item_features:
            raw_genres = if_.get("genre_ids", [])
            genre_ids = raw_genres if isinstance(raw_genres, list) else []
            if len(genre_ids) > max_genres:
                genre_ids = genre_ids[:max_genres]
            elif len(genre_ids) < max_genres:
                genre_ids = genre_ids + [0] * (max_genres - len(genre_ids))
            item_genres_batch_list.append(genre_ids)
        item_genres_batch = np.array(item_genres_batch_list, dtype=np.int32)

        item_numeric_batch = np.array(
            [
                [
                    if_["release_year_norm"],
                    if_["avg_rating_norm"],
                    if_["ratings_count_norm"],
                ]
                for if_ in batch_item_features
            ],
            dtype=np.float32,
        )

        embeddings = item_tower.predict(
            [
                item_id_batch,
                item_artists_batch,
                item_types_batch,
                item_genres_batch,
                item_numeric_batch,
            ],
            verbose=0,
            batch_size=batch_size,
        )

        # Validate that we got the expected number of embeddings
        if len(embeddings) != len(batch_release_ids):
            LOGGER.error(
                "Mismatch: generados %d embeddings pero esperados %d releases",
                len(embeddings),
                len(batch_release_ids),
            )
            continue

        # Nota: No usar strict=False aquí para compatibilidad con Python < 3.10 (PythonAnywhere)
        for release_id, embedding in zip(batch_release_ids, embeddings):  # noqa: B905
            release_embeddings_data.append(
                (
                    release_id,
                    json.dumps(embedding.tolist()),
                    embedding_dim,
                    model_version,
                    datetime.utcnow().isoformat(),
                )
            )

    connection.executemany(
        """
        INSERT INTO release_embeddings_dl (
            id_release, embedding_json, embedding_dim, model_version, last_updated
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        release_embeddings_data,
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
    embedding_dim: int = 64,
    epochs: int = 10,
    batch_size: int = 1024,
    learning_rate: float = 0.001,
    min_user_ratings: int = 5,
    min_release_ratings: int = 3,
    sample_size: int | None = None,
    validation_split: float = 0.2,
    num_negatives: int = 4,
    max_genres: int = 10,
    random_seed: int = 2024,
    evaluate_ndcg: bool = False,
    ndcg_holdout_fraction: float = 0.2,
    ndcg_k: int = 9,
    ndcg_min_test_items: int = 1,
    ndcg_max_users: int | None = None,
    checkpoint_path: Path | None = None,
    resume_from_checkpoint: Path | None = None,
) -> None:
    """Main function to build and save Two Towers embeddings."""
    if tf is None or keras is None:
        raise ImportError("TensorFlow/Keras is required. Install with: pip install tensorflow")

    total_start = perf_counter()

    # Load training data
    (
        user_features,
        item_features,
        interactions,
        user_to_idx,
        release_to_idx,
        artist_to_idx,
        genre_to_idx,
        num_artists,
        num_genres,
    ) = load_training_data(
        connection,
        min_rating=min_rating,
        min_user_ratings=min_user_ratings,
        min_release_ratings=min_release_ratings,
        sample_size=sample_size,
    )

    if not interactions:
        raise ValueError("No interactions found for training")

    training_interactions = interactions
    ndcg_holdout_items: Dict[str, set[int]] = {}
    if evaluate_ndcg:
        training_interactions, ndcg_holdout_items = split_interactions_for_ndcg(
            interactions,
            holdout_fraction=ndcg_holdout_fraction,
            min_test_items=ndcg_min_test_items,
            seed=random_seed,
        )
        total_holdout = sum(len(items) for items in ndcg_holdout_items.values())
        LOGGER.info(
            "Reserved %d holdout interactions across %d users for NDCG (fraction=%.2f)",
            total_holdout,
            len(ndcg_holdout_items),
            ndcg_holdout_fraction,
        )

        if not training_interactions:
            raise ValueError(
                "All interactions ended up in the holdout set. Lower ndcg_holdout_fraction."
            )

    train_positive_lookup = build_positive_lookup(training_interactions)

    # Build model
    num_roles = 10  # Fixed: 0-9 role indices
    num_users = len(user_to_idx) + 1  # Reserve 0 for unknown
    num_releases = len(release_to_idx) + 1

    LOGGER.info(
        (
            "Building Two Towers model "
            "(embedding_dim=%d, num_users=%d, num_releases=%d, "
            "num_artists=%d, num_genres=%d)..."
        ),
        embedding_dim,
        num_users,
        num_releases,
        num_artists,
        num_genres,
    )
    combined_model, user_tower, item_tower = build_two_tower_model(
        num_users=num_users,
        num_roles=num_roles,
        num_releases=num_releases,
        num_artists=num_artists,
        num_genres=num_genres,
        embedding_dim=embedding_dim,
    )

    LOGGER.info("Model architecture:")
    combined_model.summary()

    if resume_from_checkpoint is not None:
        resume_path = resume_from_checkpoint.expanduser()
        if resume_path.exists():
            try:
                LOGGER.info("Loading weights from checkpoint: %s", resume_path)
                combined_model.load_weights(str(resume_path))
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning(
                    "Unable to load checkpoint %s (%s). Continuing from scratch.",
                    resume_path,
                    exc,
                )
        else:
            LOGGER.warning("Resume checkpoint not found: %s", resume_path)

    # Prepare training data
    LOGGER.info("Preparing training data (negatives por positivo=%d)...", num_negatives)
    release_ids = sorted(release_to_idx.keys())
    all_data = prepare_batch_data(
        training_interactions,
        user_features,
        item_features,
        user_to_idx,
        release_to_idx,
        artist_to_idx,
        release_ids,
        num_negatives=num_negatives,
        max_genres=max_genres,
        seed=random_seed,
    )

    # Split train/val
    total_samples = all_data["labels"].shape[0]
    n_train = int(total_samples * (1 - validation_split))
    train_data = {key: val[:n_train] for key, val in all_data.items()}
    val_data = {key: val[n_train:] for key, val in all_data.items()}

    LOGGER.info("Train samples: %d, Val samples: %d", n_train, total_samples - n_train)

    # Train model
    train_model(
        combined_model,
        train_data,
        val_data=val_data,
        epochs=epochs,
        batch_size=batch_size,
        learning_rate=learning_rate,
        positive_class_weight=float(num_negatives),
        checkpoint_path=checkpoint_path,
    )

    # Save embeddings
    user_ids = sorted(user_to_idx.keys())
    release_ids = sorted(release_to_idx.keys())

    save_embeddings(
        connection,
        user_tower,
        item_tower,
        user_ids,
        release_ids,
        user_features,
        item_features,
        artist_to_idx,
        user_to_idx,
        release_to_idx,
        embedding_dim,
        model_version="1.0",
        max_genres=max_genres,
        num_negatives=num_negatives,
        random_seed=random_seed,
    )

    evaluation_summary: Dict[str, Any] | None = None
    if evaluate_ndcg:
        try:
            ndcg_score, evaluated_users = evaluate_two_tower_ndcg(
                user_tower=user_tower,
                item_tower=item_tower,
                user_features=user_features,
                item_features=item_features,
                user_to_idx=user_to_idx,
                release_to_idx=release_to_idx,
                artist_to_idx=artist_to_idx,
                train_positive_items=train_positive_lookup,
                holdout_items=ndcg_holdout_items,
                max_genres=max_genres,
                k=ndcg_k,
                max_users=ndcg_max_users,
            )
            LOGGER.info(
                "Two Towers validation NDCG@%d = %.4f (%d usuarios)",
                ndcg_k,
                ndcg_score,
                evaluated_users,
            )
            evaluation_summary = {
                "ndcg_k": ndcg_k,
                "ndcg_score": ndcg_score,
                "evaluated_users": evaluated_users,
                "holdout_fraction": ndcg_holdout_fraction,
                "min_test_items": ndcg_min_test_items,
                "max_users": ndcg_max_users,
            }
        except ImportError as exc:
            LOGGER.warning("Skipping NDCG evaluation: %s", exc)

    # Save model for later use in generating new user embeddings
    models_dir = Path(__file__).resolve().parents[1] / "models" / "Two Towers"
    models_dir.mkdir(parents=True, exist_ok=True)

    # Determine model filename based on database
    # Try to get database path from connection
    try:
        db_info = connection.execute("PRAGMA database_list").fetchone()
        if db_info and len(db_info) > 2:
            db_path = Path(db_info[2])
        else:
            # Fallback: use database parameter or default
            db_path = resolve_database_path(None)
    except Exception:
        # Fallback: use default database path
        db_path = resolve_database_path(None)

    db_name = db_path.stem  # e.g., "sputnik" or "sputnik_lite"
    user_model_path = models_dir / f"user_tower_{db_name}.keras"
    item_model_path = models_dir / f"item_tower_{db_name}.keras"
    metadata_path = models_dir / f"two_towers_{db_name}_metadata.json"

    def save_model_artifact(model: keras.Model, target_path: Path, label: str) -> None:
        LOGGER.info("Saving %s model to %s", label, target_path)
        try:
            model.save(str(target_path), save_format="keras")
        except Exception as exc:  # pragma: no cover - fallback path
            LOGGER.warning(
                "Error guardando %s en formato keras (%s), intentando SavedModel", label, exc
            )
            fallback_path = target_path.parent / f"{target_path.stem}_savedmodel"
            model.save(str(fallback_path), save_format="tf")
            import shutil

            shutil.copy(str(fallback_path / "saved_model.pb"), str(target_path))

    save_model_artifact(user_tower, user_model_path, "user tower")
    save_model_artifact(item_tower, item_model_path, "item tower")

    # Persist vocabularies for reproducible inference
    user_index_path = models_dir / f"two_towers_{db_name}_user_index.json"
    release_index_path = models_dir / f"two_towers_{db_name}_release_index.json"
    artist_index_path = models_dir / f"two_towers_{db_name}_artist_index.json"
    genre_index_path = models_dir / f"two_towers_{db_name}_genre_index.json"

    index_payloads = [
        (user_index_path, user_to_idx),
        (release_index_path, release_to_idx),
        (artist_index_path, artist_to_idx),
        (genre_index_path, genre_to_idx),
    ]
    for path, mapping in index_payloads:
        with open(path, "w") as f:
            json.dump(mapping, f)

    metadata = {
        "model_version": "1.0",
        "embedding_dim": embedding_dim,
        "num_roles": num_roles,
        "num_users": num_users,
        "num_releases": num_releases,
        "num_artists": num_artists,
        "num_genres": num_genres,
        "max_genres": max_genres,
        "num_negatives": num_negatives,
        "random_seed": random_seed,
        "database_name": db_name,
        "database_path": str(db_path),
        "saved_at": datetime.utcnow().isoformat(),
        "artifacts": {
            "user_tower": str(user_model_path),
            "item_tower": str(item_model_path),
        },
        "vocabularies": {
            "user_to_idx": str(user_index_path),
            "release_to_idx": str(release_index_path),
            "artist_to_idx": str(artist_index_path),
            "genre_to_idx": str(genre_index_path),
        },
    }

    if evaluation_summary:
        metadata["evaluation"] = evaluation_summary

    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)

    LOGGER.info("Modelos y metadatos guardados en %s", metadata_path)

    LOGGER.info("Total time: %.2fs", perf_counter() - total_start)


def configure_logging(verbose: bool) -> None:
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s - %(message)s")


def parse_arguments(argv: List[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
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
        "--embedding-dim",
        type=int,
        default=64,
        help="Dimension of embeddings (default: 64)",
    )
    parser.add_argument(
        "--epochs",
        type=int,
        default=10,
        help="Number of training epochs (default: 10)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1024,
        help="Batch size for training (default: 1024)",
    )
    parser.add_argument(
        "--learning-rate",
        type=float,
        default=0.001,
        help="Learning rate (default: 0.001)",
    )
    parser.add_argument(
        "--min-user-ratings",
        type=int,
        default=5,
        help="Minimum positive ratings per user to include (default: 5)",
    )
    parser.add_argument(
        "--min-release-ratings",
        type=int,
        default=3,
        help="Minimum positive ratings per release to include (default: 3)",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=None,
        help="Limit number of interactions for faster training (default: None, use all)",
    )
    parser.add_argument(
        "--validation-split",
        type=float,
        default=0.2,
        help="Fraction of data to use for validation (default: 0.2)",
    )
    parser.add_argument(
        "--num-negatives",
        type=int,
        default=4,
        help="Negative samples por interacción positiva (default: 4)",
    )
    parser.add_argument(
        "--max-genres",
        type=int,
        default=10,
        help="Cantidad máxima de géneros por release para el embedding (default: 10)",
    )
    parser.add_argument(
        "--random-seed",
        type=int,
        default=2024,
        help="Semilla para muestreo y shuffles (default: 2024)",
    )
    parser.add_argument(
        "--evaluate-ndcg",
        action="store_true",
        help="Reserva un holdout por usuario y calcula NDCG@k al finalizar el entrenamiento",
    )
    parser.add_argument(
        "--ndcg-holdout",
        type=float,
        default=0.2,
        help="Fracción de interacciones positivas reservadas por usuario para NDCG (default: 0.2)",
    )
    parser.add_argument(
        "--ndcg-k",
        type=int,
        default=9,
        help="Valor de k para NDCG@k (default: 9)",
    )
    parser.add_argument(
        "--ndcg-min-test-items",
        type=int,
        default=1,
        help="Mínimo de ítems en holdout por usuario para incluirlo en la métrica (default: 1)",
    )
    parser.add_argument(
        "--ndcg-max-users",
        type=int,
        default=None,
        help="Límite máximo de usuarios evaluados para acelerar la métrica (default: todos)",
    )
    parser.add_argument(
        "--checkpoint-path",
        type=str,
        help=(
            "Ruta donde guardar pesos (save_weights) en cada epoch. "
            "Si no se indica, no se generan checkpoints."
        ),
    )
    parser.add_argument(
        "--resume-from-checkpoint",
        type=str,
        help="Ruta de pesos guardados previamente para reanudar entrenamiento",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    """Main entry point."""
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

        try:
            checkpoint_path = (
                Path(args.checkpoint_path).expanduser() if args.checkpoint_path else None
            )
            resume_from_checkpoint = (
                Path(args.resume_from_checkpoint).expanduser()
                if args.resume_from_checkpoint
                else None
            )
            build_embeddings(
                connection,
                min_rating=args.min_rating,
                embedding_dim=args.embedding_dim,
                epochs=args.epochs,
                batch_size=args.batch_size,
                learning_rate=args.learning_rate,
                min_user_ratings=args.min_user_ratings,
                min_release_ratings=args.min_release_ratings,
                sample_size=args.sample_size,
                validation_split=args.validation_split,
                num_negatives=args.num_negatives,
                max_genres=args.max_genres,
                random_seed=args.random_seed,
                evaluate_ndcg=args.evaluate_ndcg,
                ndcg_holdout_fraction=args.ndcg_holdout,
                ndcg_k=args.ndcg_k,
                ndcg_min_test_items=args.ndcg_min_test_items,
                ndcg_max_users=args.ndcg_max_users,
                checkpoint_path=checkpoint_path,
                resume_from_checkpoint=resume_from_checkpoint,
            )
        except Exception as e:
            LOGGER.exception("Error building embeddings: %s", e)
            return 1

    LOGGER.info("Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
