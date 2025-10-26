"""Auxiliares de acceso a datos y heuristicas simples para recomendar lanzamientos."""

from __future__ import annotations

import collections
import datetime
import json
import math
import os
import random
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict
from typing import Iterable
from typing import List
from typing import Sequence


class Config:
    positive_rating_threshold: float = 3.0
    max_pairs_signals: int = 5
    genre_weight: float = 1.0
    artist_weight: float = 0.8
    popularity_prior: float = 0.3
    recency_log_base: float = 2.0
    popularity_recent_divisor: float = 50.0
    pairs_limit_multiplier: int = 3
    pairs_table_sample: int = 10
    candidate_pool_multiplier: int = 5


_DEFAULT_POSITIVE_RATING = Config.positive_rating_threshold


_LAST_EXPLANATIONS: Dict[str, List[str]] = {}
_LAST_CONTEXT_EXPLANATIONS: Dict[tuple[str, int], List[str]] = {}
_LAST_STRATEGY: Dict[str, str] = {}
_LAST_CONTEXT_STRATEGY: Dict[tuple[str, int], str] = {}


@dataclass(frozen=True)
class Interaction:
    release_id: int
    rating: float
    rating_date: datetime.datetime | None


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


def _cache_last_explanation(user_id: str, explanations: Sequence[str]) -> None:
    _LAST_EXPLANATIONS[user_id] = list(explanations)


def _cache_last_context_explanation(
    user_id: str, release_id: int, explanations: Sequence[str]
) -> None:
    _LAST_CONTEXT_EXPLANATIONS[(user_id, release_id)] = list(explanations)


def last_explanations(user_id: str) -> List[str]:
    return _LAST_EXPLANATIONS.get(user_id, [])


def last_context_explanations(user_id: str, release_id: int) -> List[str]:
    return _LAST_CONTEXT_EXPLANATIONS.get((user_id, release_id), [])


def _cache_last_strategy(user_id: str, strategy: str) -> None:
    _LAST_STRATEGY[user_id] = strategy


def _cache_last_context_strategy(user_id: str, release_id: int, strategy: str) -> None:
    _LAST_CONTEXT_STRATEGY[(user_id, release_id)] = strategy


def last_strategy(user_id: str) -> str | None:
    return _LAST_STRATEGY.get(user_id)


def last_context_strategy(user_id: str, release_id: int) -> str | None:
    return _LAST_CONTEXT_STRATEGY.get((user_id, release_id))


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


def _user_interactions(user_id: str, min_rating: float | None = None) -> List[Interaction]:
    rows = _select(
        """
        SELECT id_release, rating, rating_date
        FROM interactions
        WHERE id_user = ?
        ORDER BY rating_date DESC;
        """,
        [user_id],
    )
    interactions: List[Interaction] = []
    for row in rows:
        rating = float(row["rating"])
        if min_rating is not None and rating < min_rating:
            continue
        rating_date_raw = row["rating_date"]
        rating_date = datetime.datetime.fromisoformat(rating_date_raw) if rating_date_raw else None
        interactions.append(
            Interaction(
                release_id=int(row["id_release"]),
                rating=rating,
                rating_date=rating_date,
            )
        )
    return interactions


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


def recommend_random(user_id: str, limit: int = 9) -> List[int]:
    return _random_unseen_releases(user_id, limit)


def _pairs_for_release(release_ids: Sequence[int], limit: int = 50) -> List[sqlite3.Row]:
    if not release_ids:
        return []
    placeholders = ",".join("?" for _ in release_ids)
    rows = _select(
        f"""
        SELECT id_release_1, id_release_2, pair_count, jaccard, lift
        FROM release_pairs
        WHERE id_release_1 IN ({placeholders})
        ORDER BY pair_count DESC
        LIMIT ?;
        """,
        list(release_ids) + [limit],
    )
    return rows


def _score_pairs(user_interactions: Sequence[Interaction], limit: int) -> List[int]:
    if not user_interactions:
        return []

    anchor_ids = [interaction.release_id for interaction in user_interactions]
    rows = _pairs_for_release(anchor_ids, limit=limit * Config.pairs_table_sample)

    scores: Dict[int, float] = collections.defaultdict(float)
    now = datetime.datetime.utcnow()

    for row in rows:
        source_id = int(row["id_release_1"])
        target_id = int(row["id_release_2"])
        pair_count = float(row["pair_count"])
        lift = float(row["lift"] or 0.0)
        jaccard = float(row["jaccard"] or 0.0)

        interaction = next(
            (item for item in user_interactions if item.release_id == source_id),
            None,
        )
        if not interaction:
            continue

        rating_weight = max(0.1, interaction.rating / 5.0)

        if interaction.rating_date:
            age_days = max(
                1.0,
                (now - interaction.rating_date).total_seconds() / 86400.0,
            )
            recency_weight = 1 / math.log(age_days + 1, Config.recency_log_base)
        else:
            recency_weight = 1.0

        score = (
            rating_weight * recency_weight * pair_count * (0.7 + 0.3 * lift) * (0.5 + 0.5 * jaccard)
        )
        scores[target_id] += score

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [release_id for release_id, _ in ranked[:limit]]


def _select_json_array(query: str, params: Sequence | None = None) -> List[int]:
    rows = _select(query, params)
    if not rows:
        return []
    values = []
    for row in rows:
        payload = row[0]
        if not payload:
            continue
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, list):
            for item in decoded:
                if isinstance(item, int):
                    values.append(item)
    return values


def _user_profile(user_id: str, min_rating: float) -> dict:
    interactions = _user_interactions(user_id)
    positive = [interaction for interaction in interactions if interaction.rating >= min_rating]
    if not positive:
        return {"genres": {}, "artists": {}, "total_weight": 0.0}

    weight_by_genre: Dict[int, float] = collections.defaultdict(float)
    weight_by_artist: Dict[int, float] = collections.defaultdict(float)

    now = datetime.datetime.utcnow()

    for interaction in positive:
        rating_weight = max(0.1, interaction.rating / 5.0)
        if interaction.rating_date:
            age_days = max(1.0, (now - interaction.rating_date).total_seconds() / 86400.0)
            recency_weight = 1 / math.log(age_days + 1, Config.recency_log_base)
        else:
            recency_weight = 1.0
        base_weight = rating_weight * recency_weight

        genres = release_genres(interaction.release_id)
        artists = [release_artist_id(interaction.release_id)]

        for genre_id in genres:
            weight_by_genre[genre_id] += base_weight / len(genres or [1])

        for artist_id in artists:
            weight_by_artist[artist_id] += base_weight / len(artists or [1])

    total_weight = sum(weight_by_genre.values()) + sum(weight_by_artist.values())
    return {
        "genres": dict(weight_by_genre),
        "artists": dict(weight_by_artist),
        "total_weight": total_weight,
    }


def release_genres(release_id: int) -> List[int]:
    rows = _select(
        """
        SELECT id_genre
        FROM release_genres
        WHERE id_release = ?;
        """,
        [release_id],
    )
    return [int(row["id_genre"]) for row in rows]


def release_artist_id(release_id: int) -> int:
    rows = _select("SELECT artist_id FROM releases WHERE id_release = ?;", [release_id])
    if not rows:
        return -1
    return int(rows[0]["artist_id"])


def _candidate_pool_from_genres(genre_ids: Iterable[int], limit: int) -> List[int]:
    genre_ids = list(dict.fromkeys(int(genre_id) for genre_id in genre_ids))
    if not genre_ids:
        return []
    placeholders = ",".join("?" for _ in genre_ids)
    rows = _select(
        f"""
        SELECT DISTINCT id_release
        FROM release_genres
        WHERE id_genre IN ({placeholders})
        LIMIT ?;
        """,
        genre_ids + [limit],
    )
    return [int(row["id_release"]) for row in rows]


def _candidate_pool_from_artists(artist_ids: Iterable[int], limit: int) -> List[int]:
    artist_ids = list(dict.fromkeys(int(artist_id) for artist_id in artist_ids))
    if not artist_ids:
        return []
    placeholders = ",".join("?" for _ in artist_ids)
    rows = _select(
        f"""
        SELECT DISTINCT id_release
        FROM releases
        WHERE artist_id IN ({placeholders})
        LIMIT ?;
        """,
        artist_ids + [limit],
    )
    return [int(row["id_release"]) for row in rows]


def _content_score(
    release_id: int,
    profile: dict,
    genre_weight: float,
    artist_weight: float,
    popularity_prior: float,
) -> float:
    genres = release_genres(release_id)
    artist_id = release_artist_id(release_id)

    score = 0.0

    for genre_id in genres:
        score += genre_weight * profile["genres"].get(genre_id, 0.0)

    if artist_id in profile["artists"]:
        score += artist_weight * profile["artists"][artist_id]

    release = release_detail(release_id)
    if release:
        ratings_count = release.get("ratings_count") or 0
        avg_rating = release.get("avg_rating") or 0.0
        recent_bonus = 0.0
        release_year = release.get("release_year")
        if release_year:
            years_old = max(0, datetime.datetime.utcnow().year - int(release_year))
            recent_bonus = max(0.0, 1.0 - (years_old / Config.popularity_recent_divisor))
        score += popularity_prior * (
            0.6 * (avg_rating / 5.0) + 0.3 * math.log1p(ratings_count) + 0.1 * recent_bonus
        )
    return score


def recommend_content_based(
    user_id: str,
    *,
    limit: int = 9,
    min_rating: float = _DEFAULT_POSITIVE_RATING,
    genre_weight: float = 1.0,
    artist_weight: float = 0.8,
    popularity_prior: float = 0.3,
) -> List[int]:
    profile = _user_profile(user_id, min_rating)
    if profile["total_weight"] <= 0:
        return []

    genre_ids = list(profile["genres"].keys())
    artist_ids = list(profile["artists"].keys())

    candidates = set(
        _candidate_pool_from_genres(genre_ids, limit * Config.candidate_pool_multiplier)
        + _candidate_pool_from_artists(artist_ids, limit * Config.candidate_pool_multiplier)
    )

    if not candidates:
        return []

    seen_ids = set(rated_release_ids(user_id) + seen_release_ids(user_id))

    scores = {
        candidate: _content_score(
            candidate,
            profile,
            genre_weight,
            artist_weight,
            popularity_prior,
        )
        for candidate in candidates
        if candidate not in seen_ids
    }

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [release_id for release_id, _ in ranked[:limit]]


def recommend_from_pairs(
    user_id: str,
    *,
    limit: int = 9,
    min_rating: float = _DEFAULT_POSITIVE_RATING,
) -> List[int]:
    interactions = _user_interactions(user_id)
    positive = [interaction for interaction in interactions if interaction.rating >= min_rating]
    if not positive:
        return []

    candidates = _score_pairs(positive, limit=limit * Config.pairs_limit_multiplier)
    seen_ids = {interaction.release_id for interaction in interactions}
    result: List[int] = []
    for release_id in candidates:
        if release_id in seen_ids:
            continue
        result.append(release_id)
        if len(result) >= limit:
            break
    return result


def recommend(user_id: str, limit: int = 9) -> List[int]:
    interactions = _user_interactions(user_id)
    positive_interactions = [
        interaction
        for interaction in interactions
        if interaction.rating >= _DEFAULT_POSITIVE_RATING
    ]

    if not positive_interactions:
        candidates = _popular_unseen_releases(user_id, limit)
        strategy = "popular"
        if len(candidates) < limit:
            candidates.extend(_random_unseen_releases(user_id, limit))
            strategy = "popular_random"
        _cache_last_strategy(user_id, strategy)
        return list(dict.fromkeys(candidates))[:limit]

    explanations: List[str] = []

    if len(positive_interactions) <= Config.max_pairs_signals:
        candidates = recommend_from_pairs(user_id, limit=limit)
        if candidates:
            explanations.append("Basado en discos que calificaste positivamente")
            _cache_last_strategy(user_id, "pairs")
    else:
        candidates = recommend_content_based(user_id, limit=limit)
        if candidates:
            explanations.append("Basado en tus géneros y artistas con mejor puntaje")
            _cache_last_strategy(user_id, "content")

    if len(candidates) < limit:
        supplemental = _popular_unseen_releases(user_id, limit)
        candidates = list(dict.fromkeys(candidates + supplemental))
        if supplemental:
            explanations.append("Completamos con lanzamientos populares que aún no viste")
            _cache_last_strategy(user_id, "popular_fallback")

    if len(candidates) < limit:
        random_fallback = _random_unseen_releases(user_id, limit)
        candidates = list(dict.fromkeys(candidates + random_fallback))
        if random_fallback:
            explanations.append("Incluimos algunos discos aleatorios para explorar")
            _cache_last_strategy(user_id, "random_fallback")

    diversified = _diversify_by_artist(candidates, limit=limit)
    _cache_last_explanation(user_id, explanations)
    return diversified[:limit]


def _diversify_by_artist(release_ids: Sequence[int], *, limit: int) -> List[int]:
    diversified: List[int] = []
    seen_artists: set[int] = set()
    secondary_bucket: List[int] = []

    for release_id in release_ids:
        artist_id = release_artist_id(release_id)
        if artist_id not in seen_artists:
            diversified.append(release_id)
            seen_artists.add(artist_id)
        else:
            secondary_bucket.append(release_id)
        if len(diversified) >= limit:
            break

    if len(diversified) < limit:
        diversified.extend(secondary_bucket[: limit - len(diversified)])

    return diversified


def recommend_context(user_id: str, release_id: int, limit: int = 3) -> List[int]:
    """Recomendar lanzamientos vinculados a un lanzamiento dado, excluyendo los vistos."""

    seen_ids = set(rated_release_ids(user_id) + seen_release_ids(user_id))

    candidates: List[int] = []

    direct_rows = _select(
        """
        SELECT rr.recommended_release_id AS candidate_id
        FROM release_recommendations AS rr
        JOIN releases AS r ON r.id_release = rr.recommended_release_id
        WHERE rr.release_id = ?
        ORDER BY
            (r.ratings_count IS NULL),
            r.ratings_count DESC,
            (r.avg_rating IS NULL),
            r.avg_rating DESC
        LIMIT ?;
        """,
        [release_id, limit * 2],
    )
    candidates.extend(int(row["candidate_id"]) for row in direct_rows)

    if len(candidates) < limit:
        pair_rows = _select(
            """
            SELECT id_release_2 AS candidate_id
            FROM release_pairs
            WHERE id_release_1 = ?
            ORDER BY pair_count DESC
            LIMIT ?;
            """,
            [release_id, limit * 5],
        )
        candidates.extend(int(row["candidate_id"]) for row in pair_rows)

    if len(candidates) < limit:
        release_artist = release_artist_id(release_id)
        artist_rows = _select(
            """
            SELECT id_release
            FROM releases
            WHERE artist_id = ? AND id_release <> ?
            ORDER BY release_year DESC NULLS LAST
            LIMIT ?;
            """,
            [release_artist, release_id, limit * 2],
        )
        candidates.extend(int(row["id_release"]) for row in artist_rows)

    filtered = [
        candidate
        for candidate in candidates
        if candidate not in seen_ids and candidate != release_id
    ]

    explanations: List[str] = []

    if len(filtered) < limit:
        fallback = _popular_unseen_releases(user_id, limit)
        filtered.extend(fallback)
        if fallback:
            explanations.append("Completamos con lanzamientos populares relacionados")
            _cache_last_context_strategy(user_id, release_id, "popular_fallback")

    deduped = list(dict.fromkeys(filtered))
    _cache_last_context_explanation(user_id, release_id, explanations)
    return deduped[:limit]


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


def release_details_map(release_ids: Sequence[int]) -> Dict[int, dict]:
    details_list = release_details(release_ids)
    return {item["id_release"]: item for item in details_list}
