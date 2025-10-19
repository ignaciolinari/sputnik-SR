"""Auxiliares de acceso a datos y heuristicas simples para recomendar lanzamientos."""

from __future__ import annotations

import os
import random
import sqlite3
from pathlib import Path
from typing import List
from typing import Sequence


def _resolve_database_path() -> Path:
    """Resolver la ruta de la base SQLite de Sputnik respetando la variable SPUTNIK_DB."""
    override = os.getenv("SPUTNIK_DB")
    if override:
        candidate = Path(override).expanduser().resolve()
    else:
        candidate = Path(__file__).resolve().parents[1] / "data" / "sputnik.db"
    if not candidate.exists():
        raise FileNotFoundError(
            f"Sputnik database not found at {candidate}. Set SPUTNIK_DB to override the path."
        )
    return candidate


def _connect() -> sqlite3.Connection:
    """Devolver una conexion SQLite con acceso por columnas configurado."""
    db_path = _resolve_database_path()
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection


def _execute(query: str, params: Sequence | None = None) -> None:
    with _connect() as connection:
        connection.execute(query, tuple(params or ()))
        connection.commit()


def _select(query: str, params: Sequence | None = None) -> list:
    with _connect() as connection:
        cursor = connection.execute(query, tuple(params or ()))
        rows = cursor.fetchall()
    return rows


def ensure_user(user_id: str) -> None:
    """Insertar el usuario si no existe y mantener los metadatos actuales."""
    if not user_id:
        raise ValueError("user_id must be a non-empty string")
    _execute(
        """
        INSERT INTO users (id_user)
        VALUES (?)
        ON CONFLICT (id_user) DO NOTHING;
        """,
        [user_id],
    )


def store_interaction(release_id: int, user_id: str, rating: float) -> None:
    """Insertar o actualizar la calificacion de un usuario para un lanzamiento."""
    _execute(
        """
        INSERT INTO interactions (id_release, id_user, rating, rating_date)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT (id_release, id_user)
        DO UPDATE SET
            rating = excluded.rating,
            rating_date = datetime('now');
        """,
        [release_id, user_id, float(rating)],
    )


def reset_user_history(user_id: str) -> None:
    """Eliminar todas las interacciones guardadas del usuario."""
    _execute("DELETE FROM interactions WHERE id_user = ?;", [user_id])


def rated_release_ids(user_id: str) -> List[int]:
    """Devolver los ids de lanzamientos con calificacion explicita (> 0)."""
    rows = _select(
        """
        SELECT id_release
        FROM interactions
        WHERE id_user = ? AND rating > 0
        ORDER BY rating_date DESC;
        """,
        [user_id],
    )
    return [int(row["id_release"]) for row in rows]


def seen_release_ids(user_id: str) -> List[int]:
    """Devolver los ids de lanzamientos marcados como vistos (rating == 0)."""
    rows = _select(
        """
        SELECT id_release
        FROM interactions
        WHERE id_user = ? AND rating = 0
        ORDER BY rating_date DESC;
        """,
        [user_id],
    )
    return [int(row["id_release"]) for row in rows]


def _popular_unseen_releases(user_id: str, limit: int) -> List[int]:
    """Devolver lanzamientos populares con los que el usuario no interactuo."""
    rows = _select(
        """
        SELECT r.id_release
        FROM releases AS r
        LEFT JOIN interactions AS i
            ON i.id_release = r.id_release AND i.id_user = ?
        WHERE i.id_user IS NULL
        ORDER BY
            (r.ratings_count IS NULL),
            r.ratings_count DESC,
            (r.avg_rating IS NULL),
            r.avg_rating DESC,
            r.id_release DESC
        LIMIT ?;
        """,
        [user_id, limit],
    )
    return [int(row["id_release"]) for row in rows]


def _random_unseen_releases(user_id: str, limit: int) -> List[int]:
    """Realizar una seleccion aleatoria entre lanzamientos sin interacciones."""
    rows = _select(
        """
        SELECT r.id_release
        FROM releases AS r
        WHERE NOT EXISTS (
            SELECT 1
            FROM interactions AS i
            WHERE i.id_release = r.id_release AND i.id_user = ?
        );
        """,
        [user_id],
    )
    ids = [int(row["id_release"]) for row in rows]
    if not ids:
        return []
    take = min(limit, len(ids))
    return random.sample(ids, take)


def recommend(user_id: str, limit: int = 9) -> List[int]:
    """Recomendar lanzamientos populares no vistos, con respaldo aleatorio si hace falta."""
    popular = _popular_unseen_releases(user_id, limit)
    if len(popular) >= limit:
        return popular[:limit]
    random_fallback = _random_unseen_releases(user_id, limit)
    merged = list(dict.fromkeys(popular + random_fallback))
    return merged[:limit]


def recommend_context(user_id: str, release_id: int, limit: int = 3) -> List[int]:
    """Recomendar lanzamientos vinculados a un lanzamiento dado, excluyendo los vistos."""
    rows = _select(
        """
        SELECT rr.recommended_release_id AS candidate_id
        FROM release_recommendations AS rr
        JOIN releases AS r ON r.id_release = rr.recommended_release_id
        LEFT JOIN interactions AS i
            ON i.id_release = rr.recommended_release_id AND i.id_user = ?
        WHERE rr.release_id = ? AND i.id_user IS NULL
        ORDER BY
            (r.ratings_count IS NULL),
            r.ratings_count DESC,
            (r.avg_rating IS NULL),
            r.avg_rating DESC
        LIMIT ?;
        """,
        [user_id, release_id, limit],
    )
    candidates = [
        int(row["candidate_id"]) for row in rows if int(row["candidate_id"]) != release_id
    ]
    if len(candidates) >= limit:
        return candidates

    fallback = _popular_unseen_releases(user_id, limit)
    merged = list(dict.fromkeys(candidates + fallback))
    filtered = [release for release in merged if release != release_id]
    return filtered[:limit]


def release_details(release_ids: Sequence[int]) -> List[dict]:
    """Devolver la metadata de lanzamientos en el mismo orden que los ids de entrada."""
    ids = [int(release_id) for release_id in release_ids if release_id is not None]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = _select(
        f"""
        SELECT
            r.id_release,
            r.title,
            r.release_year,
            r.label,
            r.avg_rating,
            r.ratings_count,
            r.art_url,
            a.name AS artist_name,
            a.country AS artist_country
        FROM releases AS r
        JOIN artists AS a ON a.id_artist = r.artist_id
        WHERE r.id_release IN ({placeholders});
        """,
        ids,
    )
    by_id = {
        int(row["id_release"]): {
            "id_release": int(row["id_release"]),
            "title": row["title"],
            "release_year": row["release_year"],
            "label": row["label"],
            "avg_rating": row["avg_rating"],
            "ratings_count": row["ratings_count"],
            "art_url": row["art_url"],
            "artist_name": row["artist_name"],
            "artist_country": row["artist_country"],
        }
        for row in rows
    }
    return [by_id[release_id] for release_id in ids if release_id in by_id]


def release_detail(release_id: int) -> dict | None:
    """Devolver la metadata de un lanzamiento o None si no existe."""
    details = release_details([release_id])
    return details[0] if details else None
