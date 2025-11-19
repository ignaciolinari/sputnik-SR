"""Apply trained Two Towers models to generate embeddings for local database.

This script loads the trained Keras models and index mappings, computes embeddings
for all users and releases in the local database, and saves them to the
user_embeddings_dl and release_embeddings_dl tables.
"""

import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np


# Add repo root to path to import from offline_recommender
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    import tensorflow as tf
    from tensorflow import keras
except ImportError:
    # Linter workaround: define symbols as Any or None before exit
    tf = None  # type: ignore
    keras = None  # type: ignore
    print("TensorFlow is required. Install with: pip install tensorflow")
    sys.exit(1)

from offline_recommender.build_two_towers import extract_item_features  # noqa: E402
from offline_recommender.build_two_towers import extract_user_features  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
LOGGER = logging.getLogger("apply_two_towers")


# Define custom layers/functions used in the model for deserialization
def l2_normalize_user(x):
    return tf.nn.l2_normalize(x, axis=1)


def l2_normalize_item(x):
    return tf.nn.l2_normalize(x, axis=1)


def masked_genre_average(inputs):
    embeddings, ids = inputs
    mask = tf.cast(tf.not_equal(ids, 0), tf.float32)
    mask = tf.expand_dims(mask, axis=-1)
    summed = tf.reduce_sum(embeddings * mask, axis=1)
    counts = tf.reduce_sum(mask, axis=1)
    counts = tf.maximum(counts, tf.ones_like(counts))
    return summed / counts


def load_json(path: Path) -> Any:
    with open(path, "r") as f:
        return json.load(f)


def main():
    # Paths
    models_dir = REPO_ROOT / "models" / "Two Towers"
    db_path = REPO_ROOT / "data" / "sputnik.db"

    if not db_path.exists():
        LOGGER.error("Database not found at %s", db_path)
        return

    # Load artifacts
    LOGGER.info("Loading models and indices from %s...", models_dir)

    custom_objects = {
        "l2_normalize_user": l2_normalize_user,
        "l2_normalize_item": l2_normalize_item,
        "masked_genre_average": masked_genre_average,
    }

    try:
        user_tower = keras.models.load_model(
            models_dir / "user_tower_sputnik.keras", custom_objects=custom_objects
        )
        item_tower = keras.models.load_model(
            models_dir / "item_tower_sputnik.keras", custom_objects=custom_objects
        )

        user_to_idx = load_json(models_dir / "two_towers_sputnik_user_index.json")
        release_to_idx = load_json(models_dir / "two_towers_sputnik_release_index.json")
        artist_to_idx = load_json(models_dir / "two_towers_sputnik_artist_index.json")
        # genre_to_idx = load_json(...) # Not strictly needed for inference if we just map IDs

        # We need genre_to_idx to map raw genre IDs to the model's vocabulary
        genre_to_idx = load_json(models_dir / "two_towers_sputnik_genre_index.json")

        metadata = load_json(models_dir / "two_towers_sputnik_metadata.json")
        embedding_dim = metadata.get("embedding_dim", 32)
        max_genres = metadata.get("max_genres", 10)
        model_version = metadata.get("model_version", "1.0")

    except Exception as e:
        LOGGER.error("Failed to load artifacts: %s", e)
        return

    LOGGER.info("Artifacts loaded. Connecting to database...")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # Create tables
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_embeddings_dl (
                id_user TEXT PRIMARY KEY REFERENCES users(id_user),
                embedding_json TEXT NOT NULL,
                embedding_dim INTEGER NOT NULL,
                model_version TEXT NOT NULL,
                last_updated TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS release_embeddings_dl (
                id_release INTEGER PRIMARY KEY REFERENCES releases(id_release),
                embedding_json TEXT NOT NULL,
                embedding_dim INTEGER NOT NULL,
                model_version TEXT NOT NULL,
                last_updated TEXT NOT NULL
            )
        """)

        # Clear existing
        conn.execute("DELETE FROM user_embeddings_dl")
        conn.execute("DELETE FROM release_embeddings_dl")

        # --- Process Users ---
        LOGGER.info("Processing users...")
        cursor = conn.execute("SELECT id_user FROM users")
        all_users = [row[0] for row in cursor.fetchall()]

        batch_size = 1000
        user_embeddings_data = []
        now = datetime.utcnow()

        for i in range(0, len(all_users), batch_size):
            batch_ids = all_users[i : i + batch_size]

            # Prepare features
            user_idx_batch = []
            user_roles_batch = []
            user_numeric_batch = []

            for uid in batch_ids:
                feats = extract_user_features(conn, uid, now)
                # Map to model index (0 if unknown/new user)
                user_idx_batch.append(user_to_idx.get(uid, 0))
                user_roles_batch.append(feats["role_idx"])
                user_numeric_batch.append(
                    [
                        feats["objectivity_score"],
                        feats["soundoffs"],
                        feats["ratings_count"],
                        feats["days_since_join"],
                        feats["days_since_active"],
                    ]
                )

            # Predict
            embeddings = user_tower.predict(
                [
                    np.array(user_idx_batch, dtype=np.int32),
                    np.array(user_roles_batch, dtype=np.int32),
                    np.array(user_numeric_batch, dtype=np.float32),
                ],
                verbose=0,
                batch_size=batch_size,
            )

            # Collect results
            timestamp = datetime.utcnow().isoformat()
            for uid, emb in zip(batch_ids, embeddings, strict=False):
                user_embeddings_data.append(
                    (uid, json.dumps(emb.tolist()), embedding_dim, model_version, timestamp)
                )

            if (i + batch_size) % 10000 == 0:
                LOGGER.info("Processed %d/%d users", i + batch_size, len(all_users))

        # Insert users
        conn.executemany(
            "INSERT INTO user_embeddings_dl VALUES (?, ?, ?, ?, ?)", user_embeddings_data
        )
        LOGGER.info("Saved %d user embeddings", len(user_embeddings_data))

        # --- Process Releases ---
        LOGGER.info("Processing releases...")
        cursor = conn.execute("SELECT id_release FROM releases")
        all_releases = [row[0] for row in cursor.fetchall()]

        release_embeddings_data = []

        for i in range(0, len(all_releases), batch_size):
            batch_ids = all_releases[i : i + batch_size]

            item_id_batch = []
            item_artists_batch = []
            item_types_batch = []
            item_genres_batch_list = []
            item_numeric_batch = []

            for rid in batch_ids:
                feats = extract_item_features(conn, rid)

                item_id_batch.append(release_to_idx.get(str(rid), 0))  # JSON keys are strings
                # Note: release_to_idx keys in JSON are strings, but rid is int.
                # We try str(rid) first. If not found, try int(rid) just in case, or 0.
                # Actually, let's check how json loads keys. It loads them as strings.

                # Artist mapping
                item_artists_batch.append(artist_to_idx.get(str(feats["artist_id"]), 0))

                item_types_batch.append(feats["release_type_idx"])

                # Genres
                raw_genres = feats["genre_ids"]
                # Map raw genre IDs to model genre indices
                mapped_genres = [genre_to_idx.get(str(gid), 0) for gid in raw_genres]

                # Pad/Truncate
                if len(mapped_genres) > max_genres:
                    mapped_genres = mapped_genres[:max_genres]
                else:
                    mapped_genres = mapped_genres + [0] * (max_genres - len(mapped_genres))
                item_genres_batch_list.append(mapped_genres)

                item_numeric_batch.append(
                    [
                        feats["release_year_norm"],
                        feats["avg_rating_norm"],
                        feats["ratings_count_norm"],
                    ]
                )

            # Predict
            embeddings = item_tower.predict(
                [
                    np.array(item_id_batch, dtype=np.int32),
                    np.array(item_artists_batch, dtype=np.int32),
                    np.array(item_types_batch, dtype=np.int32),
                    np.array(item_genres_batch_list, dtype=np.int32),
                    np.array(item_numeric_batch, dtype=np.float32),
                ],
                verbose=0,
                batch_size=batch_size,
            )

            timestamp = datetime.utcnow().isoformat()
            for rid, emb in zip(batch_ids, embeddings, strict=False):
                release_embeddings_data.append(
                    (rid, json.dumps(emb.tolist()), embedding_dim, model_version, timestamp)
                )

            if (i + batch_size) % 10000 == 0:
                LOGGER.info("Processed %d/%d releases", i + batch_size, len(all_releases))

        # Insert releases
        conn.executemany(
            "INSERT INTO release_embeddings_dl VALUES (?, ?, ?, ?, ?)", release_embeddings_data
        )
        conn.commit()
        LOGGER.info("Saved %d release embeddings", len(release_embeddings_data))

    LOGGER.info("Done! Embeddings updated in sputnik.db")


if __name__ == "__main__":
    main()
