"""Precompute Two Towers embeddings for users and releases using Keras/TensorFlow.

This script trains a Two Towers deep learning model that learns separate embeddings
for users and items based on their features and interactions.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Dict
from typing import List
from typing import Tuple

import numpy as np


try:
    import tensorflow as tf
    from tensorflow import keras
    from tensorflow.keras import layers
except ImportError:
    tf = None
    keras = None
    layers = None


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


def extract_item_features(
    connection: sqlite3.Connection, release_id: int
) -> Dict[str, float | int | List[int]]:
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


def build_user_tower(num_roles: int = 10, embedding_dim: int = 64) -> keras.Model:
    """Build user tower model.

    Args:
        num_roles: Number of distinct roles
        embedding_dim: Dimension of output embedding

    Returns:
        Keras Model for user tower
    """
    # Inputs
    role_input = layers.Input(shape=(), name="user_role", dtype="int32")
    numeric_input = layers.Input(shape=(5,), name="user_numeric")

    # Role embedding
    role_emb = layers.Embedding(num_roles, 8, name="role_embedding")(role_input)
    role_emb = layers.Flatten()(role_emb)

    # Numeric features processing
    numeric_emb = layers.Dense(32, activation="relu", name="numeric_dense_1")(numeric_input)
    numeric_emb = layers.Dropout(0.2, name="numeric_dropout_1")(numeric_emb)
    numeric_emb = layers.Dense(32, activation="relu", name="numeric_dense_2")(numeric_emb)

    # Combine
    combined = layers.Concatenate(name="user_combine")([role_emb, numeric_emb])
    output = layers.Dense(embedding_dim, activation="relu", name="user_dense_1")(combined)
    output = layers.Dropout(0.2, name="user_dropout")(output)
    output = layers.Dense(embedding_dim, name="user_output")(output)

    # L2 normalization for cosine similarity
    output = layers.Lambda(lambda x: tf.nn.l2_normalize(x, axis=1), name="user_normalize")(output)

    return keras.Model(inputs=[role_input, numeric_input], outputs=output, name="user_tower")


def build_item_tower(num_artists: int, num_genres: int, embedding_dim: int = 64) -> keras.Model:
    """Build item tower model.

    Args:
        num_artists: Number of distinct artists
        num_genres: Number of distinct genres
        embedding_dim: Dimension of output embedding

    Returns:
        Keras Model for item tower
    """
    # Inputs
    artist_input = layers.Input(shape=(), name="item_artist", dtype="int32")
    type_input = layers.Input(shape=(), name="item_type", dtype="int32")
    genres_input = layers.Input(shape=(None,), name="item_genres", dtype="int32")
    numeric_input = layers.Input(shape=(3,), name="item_numeric")

    # Artist embedding
    artist_emb = layers.Embedding(num_artists, 16, name="artist_embedding")(artist_input)
    artist_emb = layers.Flatten()(artist_emb)

    # Release type embedding
    type_emb = layers.Embedding(4, 4, name="type_embedding")(
        type_input
    )  # LP, EP, Single, Compilation
    type_emb = layers.Flatten()(type_emb)

    # Genres embedding (multi-hot with pooling)
    genres_emb = layers.Embedding(num_genres, 8, name="genre_embedding")(genres_input)
    genres_emb = layers.GlobalAveragePooling1D(name="genre_pooling")(genres_emb)

    # Numeric features processing
    numeric_emb = layers.Dense(16, activation="relu", name="item_numeric_dense_1")(numeric_input)
    numeric_emb = layers.Dropout(0.2, name="item_numeric_dropout_1")(numeric_emb)
    numeric_emb = layers.Dense(16, activation="relu", name="item_numeric_dense_2")(numeric_emb)

    # Combine
    combined = layers.Concatenate(name="item_combine")(
        [artist_emb, type_emb, genres_emb, numeric_emb]
    )
    output = layers.Dense(embedding_dim, activation="relu", name="item_dense_1")(combined)
    output = layers.Dropout(0.2, name="item_dropout")(output)
    output = layers.Dense(embedding_dim, name="item_output")(output)

    # L2 normalization for cosine similarity
    output = layers.Lambda(lambda x: tf.nn.l2_normalize(x, axis=1), name="item_normalize")(output)

    return keras.Model(
        inputs=[artist_input, type_input, genres_input, numeric_input],
        outputs=output,
        name="item_tower",
    )


def build_two_tower_model(
    num_roles: int,
    num_artists: int,
    num_genres: int,
    embedding_dim: int = 64,
) -> Tuple[keras.Model, keras.Model, keras.Model]:
    """Build complete Two Towers model.

    Returns:
        Tuple of (combined_model, user_tower, item_tower)
    """
    user_tower = build_user_tower(num_roles=num_roles, embedding_dim=embedding_dim)
    item_tower = build_item_tower(
        num_artists=num_artists, num_genres=num_genres, embedding_dim=embedding_dim
    )

    # Combined model for training
    user_role_input = layers.Input(shape=(), name="user_role", dtype="int32")
    user_numeric_input = layers.Input(shape=(5,), name="user_numeric")
    item_artist_input = layers.Input(shape=(), name="item_artist", dtype="int32")
    item_type_input = layers.Input(shape=(), name="item_type", dtype="int32")
    item_genres_input = layers.Input(shape=(None,), name="item_genres", dtype="int32")
    item_numeric_input = layers.Input(shape=(3,), name="item_numeric")

    user_emb = user_tower([user_role_input, user_numeric_input])
    item_emb = item_tower(
        [item_artist_input, item_type_input, item_genres_input, item_numeric_input]
    )

    # Dot product (score)
    score = layers.Dot(axes=1, normalize=False, name="score")([user_emb, item_emb])

    combined_model = keras.Model(
        inputs=[
            user_role_input,
            user_numeric_input,
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
    Dict[int, Dict[str, float | int | List[int]]],
    List[Tuple[str, int, float]],
    Dict[str, int],
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

    # Load interactions
    placeholders_users = ",".join("?" for _ in eligible_users)
    placeholders_releases = ",".join("?" for _ in eligible_releases)

    interactions_query = f"""
        SELECT id_user, id_release, rating
        FROM interactions
        WHERE id_user IN ({placeholders_users})
          AND id_release IN ({placeholders_releases})
          AND rating >= ?
        ORDER BY rating DESC
    """
    if sample_size:
        interactions_query += f" LIMIT {sample_size}"

    interactions_rows = connection.execute(
        interactions_query, list(eligible_users) + list(eligible_releases) + [min_rating]
    ).fetchall()

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

    # Extract item features
    LOGGER.info("Extracting item features...")
    item_features = {}
    release_ids = sorted(set(release_id for _, release_id, _ in interactions))
    for release_id in release_ids:
        item_features[release_id] = extract_item_features(connection, release_id)

    # Get unique artist IDs from interactions (not all artists in DB)
    unique_artist_ids = sorted(
        set(item_features[release_id]["artist_id"] for release_id in release_ids)
    )
    artist_to_idx = {artist_id: idx for idx, artist_id in enumerate(unique_artist_ids)}
    num_artists = len(unique_artist_ids)

    # Get vocabulary sizes for genres (use all genres in DB)
    num_genres_query = "SELECT COUNT(*) FROM genres"
    num_genres = connection.execute(num_genres_query).fetchone()[0] or 1

    # Create mappings
    user_to_idx = {user_id: idx for idx, user_id in enumerate(user_ids)}
    release_to_idx = {release_id: idx for idx, release_id in enumerate(release_ids)}

    elapsed = perf_counter() - start
    LOGGER.info("Data loading completed in %.2fs", elapsed)

    return (
        user_features,
        item_features,
        interactions,
        user_to_idx,
        release_to_idx,
        artist_to_idx,
        num_artists,
        num_genres,
    )


def prepare_batch_data(
    interactions: List[Tuple[str, int, float]],
    user_features: Dict[str, Dict[str, float | int]],
    item_features: Dict[int, Dict[str, float | int | List[int]]],
    artist_to_idx: Dict[int, int],
    max_genres: int = 10,
) -> Dict[str, np.ndarray]:
    """Prepare batch data for training.

    Args:
        interactions: List of (user_id, release_id, rating)
        user_features: User features dictionary
        item_features: Item features dictionary
        max_genres: Maximum number of genres to pad/truncate

    Returns:
        Dictionary with batched features and ratings
    """
    # User features
    user_roles = np.array(
        [user_features[user_id]["role_idx"] for user_id, _, _ in interactions], dtype=np.int32
    )
    user_numeric = np.array(
        [
            [
                user_features[user_id]["objectivity_score"],
                user_features[user_id]["soundoffs"],
                user_features[user_id]["ratings_count"],
                user_features[user_id]["days_since_join"],
                user_features[user_id]["days_since_active"],
            ]
            for user_id, _, _ in interactions
        ],
        dtype=np.float32,
    )

    # Item features - map artist_id to consecutive index
    item_artists = np.array(
        [
            artist_to_idx.get(item_features[release_id]["artist_id"], 0)
            for _, release_id, _ in interactions
        ],
        dtype=np.int32,
    )
    item_types = np.array(
        [item_features[release_id]["release_type_idx"] for _, release_id, _ in interactions],
        dtype=np.int32,
    )

    # Genres (pad/truncate to max_genres)
    item_genres_list = []
    for _, release_id, _ in interactions:
        genre_ids = item_features[release_id]["genre_ids"]
        if len(genre_ids) > max_genres:
            genre_ids = genre_ids[:max_genres]
        elif len(genre_ids) < max_genres:
            genre_ids = genre_ids + [0] * (max_genres - len(genre_ids))
        item_genres_list.append(genre_ids)
    item_genres = np.array(item_genres_list, dtype=np.int32)

    item_numeric = np.array(
        [
            [
                item_features[release_id]["release_year_norm"],
                item_features[release_id]["avg_rating_norm"],
                item_features[release_id]["ratings_count_norm"],
            ]
            for _, release_id, _ in interactions
        ],
        dtype=np.float32,
    )

    # Ratings
    ratings = np.array([rating for _, _, rating in interactions], dtype=np.float32)

    return {
        "user_role": user_roles,
        "user_numeric": user_numeric,
        "item_artist": item_artists,
        "item_type": item_types,
        "item_genres": item_genres,
        "item_numeric": item_numeric,
        "ratings": ratings,
    }


def train_model(
    model: keras.Model,
    train_data: Dict[str, np.ndarray],
    val_data: Dict[str, np.ndarray] | None = None,
    epochs: int = 10,
    batch_size: int = 1024,
    learning_rate: float = 0.001,
) -> keras.callbacks.History:
    """Train the Two Towers model.

    Args:
        model: Combined Two Towers model
        train_data: Training data dictionary
        val_data: Validation data dictionary (optional)
        epochs: Number of training epochs
        batch_size: Batch size
        learning_rate: Learning rate

    Returns:
        Training history
    """
    LOGGER.info("Compiling model...")
    model.compile(
        optimizer=keras.optimizers.Adam(learning_rate=learning_rate),
        loss="mse",
        metrics=["mae"],
    )

    callbacks = [
        keras.callbacks.EarlyStopping(
            monitor="val_loss" if val_data else "loss",
            patience=3,
            restore_best_weights=True,
            verbose=1,
        ),
        keras.callbacks.ReduceLROnPlateau(
            monitor="val_loss" if val_data else "loss",
            patience=2,
            factor=0.5,
            min_lr=1e-6,
            verbose=1,
        ),
    ]

    LOGGER.info("Training model (epochs=%d, batch_size=%d)...", epochs, batch_size)
    start = perf_counter()

    history = model.fit(
        x=[
            train_data["user_role"],
            train_data["user_numeric"],
            train_data["item_artist"],
            train_data["item_type"],
            train_data["item_genres"],
            train_data["item_numeric"],
        ],
        y=train_data["ratings"],
        epochs=epochs,
        batch_size=batch_size,
        validation_data=(
            [
                val_data["user_role"],
                val_data["user_numeric"],
                val_data["item_artist"],
                val_data["item_type"],
                val_data["item_genres"],
                val_data["item_numeric"],
            ],
            val_data["ratings"],
        )
        if val_data
        else None,
        callbacks=callbacks,
        verbose=1,
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
    item_features: Dict[int, Dict[str, float | int | List[int]]],
    artist_to_idx: Dict[int, int],
    embedding_dim: int,
    model_version: str = "1.0",
    max_genres: int = 10,
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
            [user_roles_batch, user_numeric_batch], verbose=0, batch_size=batch_size
        )

        for user_id, embedding in zip(batch_user_ids, embeddings, strict=False):
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

        item_artists_batch = np.array(
            [artist_to_idx.get(if_["artist_id"], 0) for if_ in batch_item_features],
            dtype=np.int32,
        )
        item_types_batch = np.array(
            [if_["release_type_idx"] for if_ in batch_item_features], dtype=np.int32
        )

        # Prepare genres (pad/truncate)
        item_genres_batch_list = []
        for if_ in batch_item_features:
            genre_ids = if_["genre_ids"]
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
            [item_artists_batch, item_types_batch, item_genres_batch, item_numeric_batch],
            verbose=0,
            batch_size=batch_size,
        )

        for release_id, embedding in zip(batch_release_ids, embeddings, strict=False):
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

    # Build model
    LOGGER.info(
        "Building Two Towers model (embedding_dim=%d, num_artists=%d, num_genres=%d)...",
        embedding_dim,
        num_artists,
        num_genres,
    )
    num_roles = 10  # Fixed: 0-9 role indices
    combined_model, user_tower, item_tower = build_two_tower_model(
        num_roles=num_roles,
        num_artists=num_artists,
        num_genres=num_genres,
        embedding_dim=embedding_dim,
    )

    LOGGER.info("Model architecture:")
    combined_model.summary()

    # Prepare training data
    LOGGER.info("Preparing training data...")
    all_data = prepare_batch_data(interactions, user_features, item_features, artist_to_idx)

    # Split train/val
    n_train = int(len(interactions) * (1 - validation_split))
    train_data = {key: val[:n_train] for key, val in all_data.items()}
    val_data = {key: val[n_train:] for key, val in all_data.items()}

    LOGGER.info("Train samples: %d, Val samples: %d", n_train, len(interactions) - n_train)

    # Train model
    train_model(
        combined_model,
        train_data,
        val_data=val_data,
        epochs=epochs,
        batch_size=batch_size,
        learning_rate=learning_rate,
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
        embedding_dim,
        model_version="1.0",
    )

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
            )
        except Exception as e:
            LOGGER.exception("Error building embeddings: %s", e)
            return 1

    LOGGER.info("Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
