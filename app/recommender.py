"""Auxiliares de acceso a datos y heuristicas simples para recomendar lanzamientos."""

from __future__ import annotations

import collections
import datetime
import functools
import json
import math
import os
import random
import sqlite3
from contextvars import ContextVar
from contextvars import Token
from dataclasses import dataclass
from pathlib import Path
from typing import Dict
from typing import Iterable
from typing import List
from typing import Sequence


try:
    import numpy as np
except ImportError:
    np = None  # type: ignore


class Config:
    positive_rating_threshold: float = 3.0
    max_pairs_signals: int = 8
    min_nmf_signals: int = 20  # Minimum positive ratings to use NMF
    min_two_towers_signals: int = 10  # Minimum positive ratings to use Two Towers
    genre_weight: float = 1.0
    artist_weight: float = 0.8
    popularity_prior: float = 0.3
    recency_log_base: float = 2.0
    popularity_recent_divisor: float = 50.0
    pairs_limit_multiplier: int = 3
    pairs_table_sample: int = 10
    candidate_pool_multiplier: int = 5


_DEFAULT_POSITIVE_RATING = Config.positive_rating_threshold


_DATA_DIR = Path(__file__).resolve().parents[1] / "data"

_VARIANT_DEFAULTS = {
    "full": (_DATA_DIR / "sputnik.db").resolve(),
    "lite": (_DATA_DIR / "sputnik_lite.db").resolve(),
}

_VARIANT_ALIASES = {
    "lite": "lite",
    "light": "lite",
    "lite-lite": "lite",
    "lite_db": "lite",
    "lite-db": "lite",
    "full": "full",
    "completa": "full",
    "completo": "full",
    "complete": "full",
    "default": "full",
    "original": "full",
    "override": "override",
}

_VARIANT_METADATA = {
    "lite": {
        "label": "Lite",
        "description": "Base reducida para pruebas y despliegues livianos.",
    },
    "full": {
        "label": "Completa",
        "description": "Base completa con el catálogo y señales originales.",
    },
}

_PYTHONANYWHERE_HINT_ENV_VARS = (
    "PYTHONANYWHERE_DOMAIN",
    "PYTHONANYWHERE_SITE",
    "PYTHONANYWHERE_HOSTING_ACCOUNT",
    "PYTHONANYWHERE_USER",
)

_COUNT_LABELS = [
    ("users", "Usuarios"),
    ("artists", "Artistas"),
    ("releases", "Discos"),
    ("interactions", "Puntuaciones"),
]

_REQUEST_VARIANT: ContextVar[str | None] = ContextVar("sputnik_request_db_variant", default=None)


def set_request_database_variant(variant: str | None) -> Token:
    normalized = _normalize_variant(variant)
    if normalized == "override":
        normalized = None
    if normalized not in _VARIANT_DEFAULTS:
        normalized = None
    return _REQUEST_VARIANT.set(normalized)


def reset_request_database_variant(token: Token | None) -> None:
    if token is None:
        return
    try:
        _REQUEST_VARIANT.reset(token)
    except (LookupError, ValueError):
        pass


def current_request_database_variant() -> str | None:
    return _REQUEST_VARIANT.get()


def available_database_variants() -> List[dict]:
    override = os.getenv("SPUTNIK_DB")
    variants: List[dict] = []

    if override:
        override_path = Path(override).expanduser()
        try:
            resolved_path = override_path.resolve()
        except OSError:
            resolved_path = override_path
        exists = resolved_path.exists()
        variants.append(
            {
                "id": "override",
                "label": "Configuración personalizada",
                "description": "Ruta fija definida por la variable SPUTNIK_DB.",
                "filename": resolved_path.name,
                "path": str(resolved_path),
                "available": exists,
                "unavailable_reason": None if exists else "No se encontró el archivo configurado.",
            }
        )
        return variants

    python_anywhere = _running_on_pythonanywhere()
    for key, default_path in _VARIANT_DEFAULTS.items():
        meta = _VARIANT_METADATA.get(key, {"label": key.title(), "description": ""})
        exists = default_path.exists()
        available = exists and (not python_anywhere or key != "full")
        reason = None
        if not exists:
            reason = "Archivo no disponible en este entorno."
        elif python_anywhere and key == "full":
            reason = "No disponible en este entorno; usá la versión Lite."
        variants.append(
            {
                "id": key,
                "label": meta["label"],
                "description": meta["description"],
                "filename": default_path.name,
                "path": str(default_path),
                "available": available,
                "unavailable_reason": reason,
            }
        )

    return variants


_LAST_EXPLANATIONS: Dict[str, List[str]] = {}
_LAST_CONTEXT_EXPLANATIONS: Dict[tuple[str, int], List[str]] = {}
_LAST_STRATEGY: Dict[str, str] = {}
_LAST_CONTEXT_STRATEGY: Dict[tuple[str, int], str] = {}


@dataclass(frozen=True)
class Interaction:
    release_id: int
    rating: float
    rating_date: datetime.datetime | None


def _normalize_variant(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    return _VARIANT_ALIASES.get(normalized, normalized)


def _running_on_pythonanywhere() -> bool:
    return any(os.getenv(env_var) for env_var in _PYTHONANYWHERE_HINT_ENV_VARS)


def _variant_path_from_hint(variant: str | None) -> Path | None:
    if not variant:
        return None
    normalized = _normalize_variant(variant)
    if normalized in _VARIANT_DEFAULTS:
        return _VARIANT_DEFAULTS[normalized]

    # Detectar nombres de archivo comunes que deberían mapearse a variantes
    # Esto previene que se cree sputnik.lite.db en lugar de sputnik_lite.db
    filename = Path(variant).name.lower()

    # Detectar variantes lite por nombre de archivo
    if "lite" in filename and filename.endswith(".db"):
        if "sputnik" in filename or filename == "lite.db":
            return _VARIANT_DEFAULTS["lite"]

    # Detectar variantes full por nombre de archivo
    if filename in ("sputnik.db", "full.db"):
        return _VARIANT_DEFAULTS["full"]

    candidate = Path(variant).expanduser()
    if not candidate.is_absolute():
        candidate = (_DATA_DIR / candidate).resolve()
    else:
        candidate = candidate.resolve()
    return candidate


def _format_bytes(num_bytes: int | float | None) -> str:
    if not num_bytes or num_bytes <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def _format_int(value: int | None) -> str:
    if value is None:
        return "N/D"
    return f"{value:,}".replace(",", ".")


@functools.lru_cache(maxsize=8)
def _collect_database_stats_cached(path_str: str, signature: float):
    path = Path(path_str)
    if not path.exists():
        counts_data = tuple((key, None) for key, _ in _COUNT_LABELS)
        counts_display = tuple((key, "N/D") for key, _ in _COUNT_LABELS)
        return 0, "0 B", counts_data, counts_display

    size_bytes = path.stat().st_size

    counts: Dict[str, int | None] = {}
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.OperationalError:
        counts_data = tuple((key, None) for key, _ in _COUNT_LABELS)
        counts_display = tuple((key, "N/D") for key, _ in _COUNT_LABELS)
        return size_bytes, _format_bytes(size_bytes), counts_data, counts_display

    try:
        for table_key, _ in _COUNT_LABELS:
            try:
                cursor = connection.execute(f"SELECT COUNT(*) AS total FROM {table_key};")
                row = cursor.fetchone()
                counts[table_key] = int(row["total"]) if row and row["total"] is not None else 0
            except sqlite3.OperationalError:
                counts[table_key] = None
    finally:
        connection.close()

    counts_data = tuple((key, counts.get(key)) for key, _ in _COUNT_LABELS)
    counts_display = tuple((key, _format_int(counts.get(key))) for key, _ in _COUNT_LABELS)

    return size_bytes, _format_bytes(size_bytes), counts_data, counts_display


def _collect_database_stats(path: Path) -> dict:
    signature = path.stat().st_mtime if path.exists() else 0.0
    size_bytes, size_display, counts_data, counts_display_data = _collect_database_stats_cached(
        str(path), signature
    )
    counts = {key: value for key, value in counts_data}
    counts_display = {key: value for key, value in counts_display_data}
    return {
        "size_bytes": size_bytes,
        "size_display": size_display,
        "counts": counts,
        "counts_display": counts_display,
    }


def _build_reduction_summary(
    current_stats: dict, reference_stats: dict, current_label: str, reference_label: str
) -> dict | None:
    if not current_stats or not reference_stats:
        return None

    size_current = current_stats.get("size_bytes") or 0
    size_reference = reference_stats.get("size_bytes") or 0
    ratio = None
    if size_reference > 0 and size_current >= 0:
        ratio = size_current / size_reference

    size_ratio_display = f"{ratio * 100:.1f}%" if ratio is not None else "N/D"
    reduction_percent = max(0.0, (1 - ratio) * 100) if ratio is not None else None
    reduction_display = f"{reduction_percent:.1f}%" if reduction_percent is not None else "N/D"

    size_summary = (
        f"{current_label} pesa {current_stats['size_display']} vs {reference_stats['size_display']}"
        f"({size_ratio_display} del tamaño de {reference_label}, reducción {reduction_display})."
    )

    counts_summary = []
    for key, label in _COUNT_LABELS:
        current_display = current_stats["counts_display"].get(key, "N/D")
        reference_display = reference_stats["counts_display"].get(key, "N/D")
        current_value = current_stats["counts"].get(key)
        reference_value = reference_stats["counts"].get(key)
        if (
            isinstance(current_value, int)
            and isinstance(reference_value, int)
            and reference_value > 0
        ):
            counts_ratio = current_value / reference_value
            counts_ratio_display = f"{counts_ratio * 100:.1f}%"
        else:
            counts_ratio_display = "N/D"
        counts_summary.append(
            {
                "label": label,
                "current": current_display,
                "reference": reference_display,
                "ratio_display": counts_ratio_display,
            }
        )

    return {
        "size": {
            "current": current_stats["size_display"],
            "reference": reference_stats["size_display"],
            "ratio_display": size_ratio_display,
            "reduction_display": reduction_display,
            "summary": size_summary,
        },
        "counts": counts_summary,
    }


def _resolve_database_path() -> Path:
    """Resolver la ruta de la base SQLite de Sputnik respetando la variable SPUTNIK_DB."""
    request_variant = _REQUEST_VARIANT.get()
    if request_variant:
        request_candidate = _variant_path_from_hint(request_variant)
        if request_candidate and request_candidate.exists():
            return request_candidate

    override = os.getenv("SPUTNIK_DB")
    if override:
        candidate = Path(override).expanduser().resolve()
    else:
        variant_hint = _normalize_variant(os.getenv("SPUTNIK_DB_VARIANT"))
        if not variant_hint and _running_on_pythonanywhere():
            variant_hint = "lite"

        candidate_path = _variant_path_from_hint(variant_hint) if variant_hint else None
        if not candidate_path:
            candidate_path = _VARIANT_DEFAULTS["full"]
        candidate = candidate_path

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
    # Optimizaciones de rendimiento
    connection.execute("PRAGMA foreign_keys = ON;")
    connection.execute("PRAGMA journal_mode = WAL;")  # Write-Ahead Logging para mejor concurrencia
    connection.execute("PRAGMA synchronous = NORMAL;")  # Balance entre seguridad y rendimiento
    connection.execute("PRAGMA cache_size = -64000;")  # 64MB cache (negativo = KB)
    connection.execute("PRAGMA temp_store = MEMORY;")  # Usar memoria para temp tables
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


def store_interactions_batch(interactions: List[tuple[int, str, float]]) -> None:
    """Insertar o actualizar múltiples calificaciones en una sola transacción.

    Args:
        interactions: Lista de tuplas (release_id, user_id, rating)
    """
    if not interactions:
        return
    with _connect() as connection:
        connection.executemany(
            """
            INSERT INTO interactions (id_release, id_user, rating, rating_date)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT (id_release, id_user)
            DO UPDATE SET
                rating = excluded.rating,
                rating_date = datetime('now');
            """,
            [(release_id, user_id, float(rating)) for release_id, user_id, rating in interactions],
        )
        connection.commit()


def reset_user_history(user_id: str) -> None:
    """Eliminar todas las interacciones guardadas del usuario."""
    _execute("DELETE FROM interactions WHERE id_user = ?;", [user_id])
    # Eliminar embedding NMF del usuario si existe (ya no tiene sentido sin interacciones)
    _execute("DELETE FROM user_embeddings WHERE id_user = ?;", [user_id])
    # Eliminar embedding Two Towers (DL) del usuario si existe
    _execute("DELETE FROM user_embeddings_dl WHERE id_user = ?;", [user_id])
    # Limpiar explicaciones y estrategias almacenadas en memoria
    _LAST_EXPLANATIONS.pop(user_id, None)
    _LAST_STRATEGY.pop(user_id, None)
    # Limpiar explicaciones contextuales para este usuario
    context_keys_to_remove = [key for key in _LAST_CONTEXT_EXPLANATIONS.keys() if key[0] == user_id]
    for key in context_keys_to_remove:
        _LAST_CONTEXT_EXPLANATIONS.pop(key, None)
        _LAST_CONTEXT_STRATEGY.pop(key, None)
    # Invalidar cachés relacionados con releases (géneros y artistas pueden cambiar)
    release_genres.cache_clear()
    release_artist_id.cache_clear()


def user_collection(user_id: str) -> List[dict]:
    """Devolver la coleccion puntuada de un usuario con metadata de cada disco."""

    rows = _select(
        """
        SELECT
            i.id_release,
            i.rating,
            i.rating_date,
            r.title,
            r.release_year,
            r.release_type,
            r.label,
            r.avg_rating,
            r.ratings_count,
            r.art_url,
            r.artist_id,
            a.name AS artist_name
        FROM interactions AS i
        JOIN releases AS r ON r.id_release = i.id_release
        JOIN artists AS a ON a.id_artist = r.artist_id
        WHERE i.id_user = ? AND i.rating > 0
        ORDER BY i.rating_date DESC
        ;
        """,
        [user_id],
    )

    collection: List[dict] = []
    for row in rows:
        rating_value = float(row["rating"])
        rating_date_raw = row["rating_date"]
        rating_date_iso: str | None = None
        rating_date_display: str | None = None
        if rating_date_raw:
            try:
                rating_date_dt = datetime.datetime.fromisoformat(rating_date_raw)
            except ValueError:
                rating_date_dt = None
            if rating_date_dt:
                rating_date_iso = rating_date_dt.isoformat()
                rating_date_display = rating_date_dt.strftime("%d/%m/%Y")
            else:
                rating_date_iso = str(rating_date_raw)

        collection.append(
            {
                "id_release": int(row["id_release"]),
                "title": row["title"],
                "release_year": row["release_year"],
                "release_type": row["release_type"],
                "label": row["label"],
                "avg_rating": row["avg_rating"],
                "ratings_count": row["ratings_count"],
                "art_url": row["art_url"],
                "artist_id": int(row["artist_id"]),
                "artist_name": row["artist_name"],
                "rating": rating_value,
                "rating_date": rating_date_iso,
                "rating_date_display": rating_date_display,
            }
        )

    return collection


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


def user_ratings_map(user_id: str, release_ids: Sequence[int] | None = None) -> Dict[int, float]:
    """Devolver un diccionario de id_release -> rating para un usuario dado."""

    if release_ids is not None:
        normalized = [int(release_id) for release_id in release_ids if release_id is not None]
        if not normalized:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        rows = _select(
            f"""
            SELECT id_release, rating
            FROM interactions
            WHERE id_user = ? AND id_release IN ({placeholders})
            """,
            [user_id] + normalized,
        )
    else:
        rows = _select(
            """
            SELECT id_release, rating
            FROM interactions
            WHERE id_user = ?;
            """,
            [user_id],
        )

    ratings: Dict[int, float] = {}
    for row in rows:
        try:
            release_id = int(row["id_release"])
            ratings[release_id] = float(row["rating"])
        except (TypeError, ValueError):
            continue
    return ratings


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


def _user_seen_release_ids(user_id: str) -> set[int]:
    """Devolver un set con todos los ids de lanzamientos con los que el usuario interactuó."""
    rows = _select(
        """
        SELECT id_release
        FROM interactions
        WHERE id_user = ? AND rating >= 0;
        """,
        [user_id],
    )
    return {int(row["id_release"]) for row in rows}


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

    # Pre-cargar géneros y artistas en batch para todas las interacciones positivas
    release_ids = [interaction.release_id for interaction in positive]
    if not release_ids:
        return {"genres": {}, "artists": {}, "total_weight": 0.0}

    # Batch load géneros
    placeholders = ",".join("?" for _ in release_ids)
    genre_rows = _select(
        f"""
        SELECT id_release, id_genre
        FROM release_genres
        WHERE id_release IN ({placeholders});
        """,
        release_ids,
    )
    release_to_genres: Dict[int, List[int]] = collections.defaultdict(list)
    for row in genre_rows:
        release_id = int(row["id_release"])
        genre_id = int(row["id_genre"])
        release_to_genres[release_id].append(genre_id)

    # Batch load artistas
    artist_rows = _select(
        f"""
        SELECT id_release, artist_id
        FROM releases
        WHERE id_release IN ({placeholders});
        """,
        release_ids,
    )
    release_to_artist: Dict[int, int] = {}
    for row in artist_rows:
        release_id = int(row["id_release"])
        artist_id = int(row["artist_id"])
        if artist_id > 0:
            release_to_artist[release_id] = artist_id

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

        genres = list(release_to_genres.get(interaction.release_id, []))
        artist_id = release_to_artist.get(interaction.release_id)

        for genre_id in genres:
            weight_by_genre[genre_id] += base_weight / len(genres or [1])

        if artist_id:
            weight_by_artist[artist_id] += base_weight

    total_weight = sum(weight_by_genre.values()) + sum(weight_by_artist.values())
    return {
        "genres": dict(weight_by_genre),
        "artists": dict(weight_by_artist),
        "total_weight": total_weight,
    }


@functools.lru_cache(maxsize=10000)
def release_genres(release_id: int) -> tuple:
    rows = _select(
        """
        SELECT id_genre
        FROM release_genres
        WHERE id_release = ?;
        """,
        [release_id],
    )
    return tuple(int(row["id_genre"]) for row in rows)


@functools.lru_cache(maxsize=10000)
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
    release_data: dict | None = None,
    genres: List[int] | None = None,
    artist_id: int | None = None,
) -> float:
    # Usar géneros y artist_id pre-cargados si están disponibles, sino cargar
    if genres is None:
        genres_list = list(release_genres(release_id))
    else:
        genres_list = genres
    if artist_id is None:
        artist_id = release_artist_id(release_id)

    score = 0.0

    for genre_id in genres_list:
        score += genre_weight * profile["genres"].get(genre_id, 0.0)

    if artist_id and artist_id > 0 and artist_id in profile["artists"]:
        score += artist_weight * profile["artists"][artist_id]

    release = release_data if release_data is not None else release_detail(release_id)
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
    interactions: List[Interaction] | None = None,
) -> List[int]:
    if interactions is None:
        interactions = _user_interactions(user_id)
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

    seen_ids = _user_seen_release_ids(user_id)
    filtered_candidates = [c for c in candidates if c not in seen_ids]

    if not filtered_candidates:
        return []

    # Pre-cargar todos los detalles de releases, géneros y artistas en batch
    releases_map = release_details_map(filtered_candidates)

    # Batch load géneros para todos los candidatos
    placeholders = ",".join("?" for _ in filtered_candidates)
    genre_rows = _select(
        f"""
        SELECT id_release, id_genre
        FROM release_genres
        WHERE id_release IN ({placeholders});
        """,
        filtered_candidates,
    )
    release_to_genres: Dict[int, List[int]] = collections.defaultdict(list)
    for row in genre_rows:
        release_id = int(row["id_release"])
        genre_id = int(row["id_genre"])
        release_to_genres[release_id].append(genre_id)

    # Los artist_id ya están en releases_map, extraerlos
    release_to_artist: Dict[int, int] = {}
    for release_id, release_data in releases_map.items():
        artist_id = release_data.get("artist_id")
        if artist_id:
            release_to_artist[release_id] = int(artist_id)

    scores = {
        candidate: _content_score(
            candidate,
            profile,
            genre_weight,
            artist_weight,
            popularity_prior,
            release_data=releases_map.get(candidate),
            genres=release_to_genres.get(candidate, []),
            artist_id=release_to_artist.get(candidate),
        )
        for candidate in filtered_candidates
    }

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [release_id for release_id, _ in ranked[:limit]]


def recommend_from_pairs(
    user_id: str,
    *,
    limit: int = 9,
    min_rating: float = _DEFAULT_POSITIVE_RATING,
    interactions: List[Interaction] | None = None,
) -> List[int]:
    if interactions is None:
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
    # Limpiar cache de explicaciones/estrategia para este usuario al inicio
    # para evitar mostrar explicaciones de otra base de datos o estado anterior
    _LAST_EXPLANATIONS.pop(user_id, None)
    _LAST_STRATEGY.pop(user_id, None)

    interactions = _user_interactions(user_id)
    positive_interactions = [
        interaction
        for interaction in interactions
        if interaction.rating >= _DEFAULT_POSITIVE_RATING
    ]

    if not positive_interactions:
        candidates = _popular_unseen_releases(user_id, limit)
        strategy = "popular"
        explanations: List[str] = []
        if len(candidates) < limit:
            candidates.extend(_random_unseen_releases(user_id, limit))
            strategy = "popular_random"
        _cache_last_strategy(user_id, strategy)
        _cache_last_explanation(user_id, explanations)  # Cachear explicaciones vacías también
        return list(dict.fromkeys(candidates))[:limit]

    explanations: List[str] = []
    candidates: List[int] = []  # Inicializar para evitar errores si ninguna rama se ejecuta

    if len(positive_interactions) <= Config.max_pairs_signals:
        candidates = recommend_from_pairs(user_id, limit=limit, interactions=interactions)
        if candidates:
            explanations.append("Basado en discos que calificaste positivamente")
        _cache_last_strategy(user_id, "pairs")
    elif len(positive_interactions) >= Config.min_nmf_signals:
        # Try NMF first for users with sufficient history
        nmf_candidates = recommend_nmf(user_id, limit=limit)
        if nmf_candidates:
            candidates = nmf_candidates
            explanations.append("Basado en patrones latentes de tus preferencias")
            _cache_last_strategy(user_id, "nmf")
        else:
            # Try Two Towers as fallback if NMF not available
            two_towers_candidates = recommend_two_towers(user_id, limit=limit)
            if two_towers_candidates:
                candidates = two_towers_candidates
                explanations.append("Basado en aprendizaje profundo de tus preferencias")
                _cache_last_strategy(user_id, "two_towers")
            else:
                # Fallback to content-based if neither NMF nor Two Towers available
                candidates = recommend_content_based(
                    user_id, limit=limit, interactions=interactions
                )
                if candidates:
                    explanations.append("Basado en tus géneros y artistas con mejor puntaje")
                _cache_last_strategy(user_id, "content")
    elif len(positive_interactions) >= Config.min_two_towers_signals:
        # Try Two Towers for users with moderate history
        two_towers_candidates = recommend_two_towers(user_id, limit=limit)
        if two_towers_candidates:
            candidates = two_towers_candidates
            explanations.append("Basado en aprendizaje profundo de tus preferencias")
            _cache_last_strategy(user_id, "two_towers")
        else:
            # Fallback to content-based if Two Towers not available
            candidates = recommend_content_based(user_id, limit=limit, interactions=interactions)
            if candidates:
                explanations.append("Basado en tus géneros y artistas con mejor puntaje")
            _cache_last_strategy(user_id, "content")
    else:
        candidates = recommend_content_based(user_id, limit=limit, interactions=interactions)
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
    if not release_ids:
        return []

    # Pre-cargar artist_ids en una sola query
    unique_ids = list(dict.fromkeys(release_ids))
    placeholders = ",".join("?" for _ in unique_ids)
    rows = _select(
        f"""
        SELECT id_release, artist_id
        FROM releases
        WHERE id_release IN ({placeholders});
        """,
        unique_ids,
    )
    release_to_artist: Dict[int, int] = {
        int(row["id_release"]): int(row["artist_id"]) for row in rows
    }

    diversified: List[int] = []
    seen_artists: set[int] = set()
    secondary_bucket: List[int] = []

    for release_id in release_ids:
        artist_id = release_to_artist.get(release_id, -1)
        if artist_id <= 0:
            # Si no encontramos el artista, lo ponemos en el bucket secundario
            secondary_bucket.append(release_id)
            continue

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


def user_has_nmf_embedding(user_id: str) -> bool:
    """Verificar si el usuario tiene un embedding NMF disponible y válido."""
    rows = _select(
        """
        SELECT embedding_json, n_factors
        FROM user_embeddings
        WHERE id_user = ?;
        """,
        [user_id],
    )

    if not rows:
        return False

    # Verificar que el embedding es válido (tiene el formato correcto)
    try:
        embedding_json = rows[0]["embedding_json"]
        n_factors = int(rows[0]["n_factors"])
        embedding = json.loads(embedding_json)

        # Verificar que el embedding tiene la longitud correcta
        if not isinstance(embedding, list) or len(embedding) != n_factors:
            return False

        # Verificar que hay releases con embeddings disponibles
        # (no tiene sentido tener un embedding de usuario si no hay releases)
        release_count_rows = _select("SELECT COUNT(*) as count FROM release_embeddings")
        if release_count_rows and release_count_rows[0]["count"] > 0:
            return True

        return False
    except (json.JSONDecodeError, ValueError, KeyError, TypeError):
        return False


def recommend_nmf(user_id: str, limit: int = 9) -> List[int]:
    """Recomendar usando embeddings NMF precomputados.

    Esta función requiere que los embeddings hayan sido precomputados usando
    offline_recommender/build_nmf_embeddings.py.

    Returns empty list if embeddings are not available or user has insufficient history.
    """
    if np is None:
        return []

    try:
        rows = _select(
            """
            SELECT embedding_json, n_factors
            FROM user_embeddings
            WHERE id_user = ?;
            """,
            [user_id],
        )

        if not rows:
            return []

        user_embedding_json = rows[0]["embedding_json"]
        n_factors = int(rows[0]["n_factors"])
        user_embedding = json.loads(user_embedding_json)

        if len(user_embedding) != n_factors:
            return []

        # Get release embeddings (excluding seen releases for efficiency)
        seen_ids = _user_seen_release_ids(user_id)

        # SQLite has a limit on number of parameters (typically 999)
        # For large seen sets, load all and filter in Python
        SQLITE_MAX_PARAMS = 999
        if len(seen_ids) > 0 and len(seen_ids) <= SQLITE_MAX_PARAMS:
            # Use SQL to exclude seen releases when feasible
            placeholders = ",".join("?" for _ in seen_ids)
            release_rows = _select(
                f"""
                SELECT id_release, embedding_json
                FROM release_embeddings
                WHERE id_release NOT IN ({placeholders});
                """,
                list(seen_ids),
            )
        else:
            # Load all and filter in Python for very large or empty seen sets
            release_rows = _select(
                """
                SELECT id_release, embedding_json
                FROM release_embeddings;
                """,
            )

        if not release_rows:
            return []

        # Calculate cosine similarities
        scores: Dict[int, float] = {}
        user_vec = np.array(user_embedding, dtype=np.float32)
        user_norm = np.linalg.norm(user_vec)

        if user_norm == 0:
            return []

        for row in release_rows:
            release_id = int(row["id_release"])
            # Filter seen releases if we loaded all (for large seen sets)
            if len(seen_ids) > SQLITE_MAX_PARAMS:
                if release_id in seen_ids:
                    continue

            try:
                release_embedding = json.loads(row["embedding_json"])
            except (json.JSONDecodeError, TypeError):
                continue

            release_vec = np.array(release_embedding, dtype=np.float32)
            release_norm = np.linalg.norm(release_vec)

            if release_norm == 0:
                continue

            # Cosine similarity
            similarity = np.dot(user_vec, release_vec) / (user_norm * release_norm)
            scores[release_id] = float(similarity)

        # Sort by similarity and return top-k
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        return [release_id for release_id, _ in ranked[:limit]]

    except (json.JSONDecodeError, ValueError, KeyError, ImportError):
        # Silently fail if embeddings not available or numpy not installed
        return []


def recommend_two_towers(user_id: str, limit: int = 9) -> List[int]:
    """Recomendar usando embeddings Two Towers (Deep Learning) precomputados.

    Esta función requiere que los embeddings hayan sido precomputados usando
    offline_recommender/build_two_towers.py.

    Returns empty list if embeddings are not available or user has insufficient history.
    """
    if np is None:
        return []

    try:
        rows = _select(
            """
            SELECT embedding_json, embedding_dim
            FROM user_embeddings_dl
            WHERE id_user = ?;
            """,
            [user_id],
        )

        if not rows:
            return []

        user_embedding_json = rows[0]["embedding_json"]
        embedding_dim = int(rows[0]["embedding_dim"])
        user_embedding = json.loads(user_embedding_json)

        if len(user_embedding) != embedding_dim:
            return []

        # Get release embeddings (excluding seen releases for efficiency)
        seen_ids = _user_seen_release_ids(user_id)

        # SQLite has a limit on number of parameters (typically 999)
        # For large seen sets, load all and filter in Python
        SQLITE_MAX_PARAMS = 999
        if len(seen_ids) > 0 and len(seen_ids) <= SQLITE_MAX_PARAMS:
            # Use SQL to exclude seen releases when feasible
            placeholders = ",".join("?" for _ in seen_ids)
            release_rows = _select(
                f"""
                SELECT id_release, embedding_json
                FROM release_embeddings_dl
                WHERE id_release NOT IN ({placeholders});
                """,
                list(seen_ids),
            )
        else:
            # Load all and filter in Python for very large or empty seen sets
            release_rows = _select(
                """
                SELECT id_release, embedding_json
                FROM release_embeddings_dl;
                """,
            )

        if not release_rows:
            return []

        # Calculate cosine similarities (embeddings are already L2-normalized)
        # So dot product = cosine similarity
        scores: Dict[int, float] = {}
        user_vec = np.array(user_embedding, dtype=np.float32)

        for row in release_rows:
            release_id = int(row["id_release"])
            # Filter seen releases if we loaded all (for large seen sets)
            if len(seen_ids) > SQLITE_MAX_PARAMS:
                if release_id in seen_ids:
                    continue

            try:
                release_embedding = json.loads(row["embedding_json"])
            except (json.JSONDecodeError, TypeError):
                continue

            release_vec = np.array(release_embedding, dtype=np.float32)

            # Since embeddings are L2-normalized, dot product = cosine similarity
            similarity = np.dot(user_vec, release_vec)
            scores[release_id] = float(similarity)

        # Sort by similarity and return top-k
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        return [release_id for release_id, _ in ranked[:limit]]

    except (json.JSONDecodeError, ValueError, KeyError, ImportError):
        # Silently fail if embeddings not available or numpy not installed
        return []


def user_has_two_towers_embedding(user_id: str) -> bool:
    """Verificar si el usuario tiene un embedding Two Towers disponible y válido."""
    rows = _select(
        """
        SELECT embedding_json, embedding_dim
        FROM user_embeddings_dl
        WHERE id_user = ?;
        """,
        [user_id],
    )

    if not rows:
        return False

    # Verificar que el embedding es válido (tiene el formato correcto)
    try:
        embedding_json = rows[0]["embedding_json"]
        embedding_dim = int(rows[0]["embedding_dim"])
        embedding = json.loads(embedding_json)

        # Verificar que el embedding tiene la longitud correcta
        if not isinstance(embedding, list) or len(embedding) != embedding_dim:
            return False

        # Verificar que hay releases con embeddings disponibles
        release_count_rows = _select("SELECT COUNT(*) as count FROM release_embeddings_dl")
        if release_count_rows and release_count_rows[0]["count"] > 0:
            return True

        return False
    except (json.JSONDecodeError, ValueError, KeyError, TypeError):
        return False


def recommend_context(user_id: str, release_id: int, limit: int = 3) -> List[int]:
    """Recomendar lanzamientos vinculados a un lanzamiento dado, excluyendo los vistos."""

    # Limpiar cache contextual para este usuario/release al inicio
    context_key = (user_id, release_id)
    _LAST_CONTEXT_EXPLANATIONS.pop(context_key, None)
    _LAST_CONTEXT_STRATEGY.pop(context_key, None)

    seen_ids = _user_seen_release_ids(user_id)

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
        if release_artist > 0:
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
            r.artist_id,
            r.release_type,
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
            "artist_id": int(row["artist_id"]),
            "release_type": row["release_type"],
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
    """Devolver un diccionario de id_release -> dict con metadata, optimizado."""
    ids = [int(release_id) for release_id in release_ids if release_id is not None]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = _select(
        f"""
        SELECT
            r.id_release,
            r.title,
            r.artist_id,
            r.release_type,
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
    return {
        int(row["id_release"]): {
            "id_release": int(row["id_release"]),
            "title": row["title"],
            "artist_id": int(row["artist_id"]),
            "release_type": row["release_type"],
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


def artist_detail(artist_id: int) -> dict | None:
    rows = _select(
        """
        SELECT id_artist, name, country, bio, genre_tags
        FROM artists_enriched
        WHERE id_artist = ?;
        """,
        [artist_id],
    )
    if not rows:
        return None
    row = rows[0]
    genre_tags_raw = row["genre_tags"]
    try:
        genre_tags = json.loads(genre_tags_raw) if genre_tags_raw else []
    except (TypeError, json.JSONDecodeError):
        genre_tags = []
    return {
        "id_artist": int(row["id_artist"]),
        "name": row["name"],
        "country": row["country"],
        "bio": row["bio"],
        "genre_tags": genre_tags,
    }


def releases_by_artist(artist_id: int) -> List[dict]:
    rows = _select(
        """
        SELECT
            r.id_release,
            r.title,
            r.release_year,
            r.release_type,
            r.label,
            r.avg_rating,
            r.ratings_count,
            r.art_url,
            a.name AS artist_name
        FROM releases AS r
        JOIN artists AS a ON a.id_artist = r.artist_id
        WHERE r.artist_id = ?
        ORDER BY
            r.release_year IS NULL,
            r.release_year DESC,
            r.title ASC;
        """,
        [artist_id],
    )
    return [
        {
            "id_release": int(row["id_release"]),
            "title": row["title"],
            "release_year": row["release_year"],
            "release_type": row["release_type"],
            "label": row["label"],
            "avg_rating": row["avg_rating"],
            "ratings_count": row["ratings_count"],
            "art_url": row["art_url"],
            "artist_id": int(artist_id),
            "artist_name": row["artist_name"],
        }
        for row in rows
    ]


@functools.lru_cache(maxsize=1)
def list_genres(limit: int = 200) -> List[dict]:
    """Devolver una lista ordenada de géneros disponibles."""
    rows = _select(
        """
        SELECT id_genre, name
        FROM genres
        ORDER BY name ASC
        LIMIT ?;
        """,
        [limit],
    )
    result = [
        {
            "id_genre": int(row["id_genre"]),
            "name": row["name"],
        }
        for row in rows
    ]
    return result


@functools.lru_cache(maxsize=1)
def list_release_years(limit: int = 120) -> List[int]:
    """Listar años de lanzamiento disponibles ordenados de forma descendente."""
    rows = _select(
        """
        SELECT DISTINCT release_year
        FROM releases
        WHERE release_year IS NOT NULL
        ORDER BY release_year DESC
        LIMIT ?;
        """,
        [limit],
    )
    return [int(row["release_year"]) for row in rows]


@functools.lru_cache(maxsize=1)
def list_release_types() -> List[str]:
    """Listar los tipos de lanzamiento disponibles (LP, EP, etc)."""
    rows = _select(
        """
        SELECT DISTINCT release_type
        FROM releases
        WHERE release_type IS NOT NULL
        ORDER BY release_type ASC;
        """
    )
    return [str(row["release_type"]) for row in rows]


_CATALOG_BASE_FROM = """
    FROM releases AS r
    JOIN artists AS a ON a.id_artist = r.artist_id
    LEFT JOIN release_genres AS rg ON rg.id_release = r.id_release
    LEFT JOIN artist_genres AS ag ON ag.id_artist = a.id_artist
"""


def _build_catalog_filters(
    query: str | None = None,
    *,
    artist: str | None = None,
    genre_id: int | None = None,
    release_year: int | None = None,
    release_type: str | None = None,
) -> tuple[str, List[object]]:
    conditions: List[str] = []
    parameters: List[object] = []

    if query:
        pattern = f"%{query.lower()}%"
        conditions.append("(LOWER(r.title) LIKE ? OR LOWER(a.name) LIKE ?)")
        parameters.extend([pattern, pattern])

    if artist:
        conditions.append("LOWER(a.name) LIKE ?")
        parameters.append(f"%{artist.lower()}%")

    if genre_id is not None:
        conditions.append("(rg.id_genre = ? OR ag.id_genre = ?)")
        parameters.extend([int(genre_id), int(genre_id)])
    if release_year is not None:
        conditions.append("r.release_year = ?")
        parameters.append(int(release_year))

    if release_type:
        conditions.append("LOWER(r.release_type) = ?")
        parameters.append(release_type.lower())

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    return where_clause, parameters


def search_catalog(
    query: str | None = None,
    *,
    artist: str | None = None,
    genre_id: int | None = None,
    release_year: int | None = None,
    release_type: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> List[dict]:
    """Buscar lanzamientos del catálogo aplicando filtros opcionales."""

    where_clause, parameters = _build_catalog_filters(
        query,
        artist=artist,
        genre_id=genre_id,
        release_year=release_year,
        release_type=release_type,
    )

    sql = """
        SELECT DISTINCT r.id_release
    """
    sql += _CATALOG_BASE_FROM

    if where_clause:
        sql += f" {where_clause}"

    sql += """
        ORDER BY
            (r.ratings_count IS NULL),
            r.ratings_count DESC,
            (r.avg_rating IS NULL),
            r.avg_rating DESC,
            r.title ASC
        LIMIT ? OFFSET ?;
    """

    rows = _select(sql, parameters + [limit, max(int(offset), 0)])

    release_ids = [int(row["id_release"]) for row in rows]
    return release_details(release_ids)


def count_catalog(
    query: str | None = None,
    *,
    artist: str | None = None,
    genre_id: int | None = None,
    release_year: int | None = None,
    release_type: str | None = None,
) -> int:
    """Contar la cantidad total de lanzamientos que cumplen con los filtros."""

    where_clause, parameters = _build_catalog_filters(
        query,
        artist=artist,
        genre_id=genre_id,
        release_year=release_year,
        release_type=release_type,
    )

    sql = """
        SELECT COUNT(DISTINCT r.id_release) AS total
    """
    sql += _CATALOG_BASE_FROM

    if where_clause:
        sql += f" {where_clause}"

    rows = _select(sql, parameters)
    if not rows:
        return 0
    total = rows[0]["total"]
    if total is None:
        return 0
    return int(total)


def current_database_info() -> dict:
    """Informar la base de datos actual y su variante (lite o completa)."""

    path = _resolve_database_path()
    filename = path.name
    variant_key = "lite" if "lite" in filename.lower() else "full"
    variant_meta = _VARIANT_METADATA.get(variant_key, _VARIANT_METADATA["full"])

    stats = _collect_database_stats(path)

    reference_info = None
    reduction_info = None

    if variant_key == "lite":
        full_path = _VARIANT_DEFAULTS.get("full")
        if full_path and full_path.exists() and full_path != path:
            full_stats = _collect_database_stats(full_path)
            reference_info = {
                "path": str(full_path),
                "filename": full_path.name,
                "variant": "full",
                "variant_label": _VARIANT_METADATA["full"]["label"],
                "stats": full_stats,
            }
            reduction_info = _build_reduction_summary(
                stats, full_stats, variant_meta["label"], _VARIANT_METADATA["full"]["label"]
            )

    return {
        "path": str(path),
        "filename": filename,
        "variant": variant_key,
        "variant_label": variant_meta["label"],
        "variant_description": variant_meta["description"],
        "stats": stats,
        "reference": reference_info,
        "reduction": reduction_info,
    }


_ACTIVE_RECOMMENDER_SYSTEMS: List[dict] = [
    {
        "id": "hybrid",
        "name": "Motor híbrido",
        "description": "Combina estrategias según tu historial para priorizar la señal más fuerte.",
    },
    {
        "id": "pairs",
        "name": "Co-ocurrencia (release_pairs)",
        "description": (
            "Aprovecha discos que suelen aparecer juntos cuando tenés hasta "
            "8 calificaciones positivas."
        ),
    },
    {
        "id": "nmf",
        "name": "Factorización matricial (NMF)",
        "description": (
            "Usa patrones latentes aprendidos de tus preferencias cuando tenés "
            "20 o más calificaciones positivas."
        ),
    },
    {
        "id": "two_towers",
        "name": "Two Towers (Deep Learning)",
        "description": (
            "Modelo de aprendizaje profundo que aprende embeddings de usuarios e items "
            "usando características y preferencias. "
            "Se activa con 10 o más calificaciones positivas."
        ),
    },
    {
        "id": "content",
        "name": "Perfiles de contenido",
        "description": (
            "Utiliza tus géneros y artistas mejor puntuados para encontrar " "lanzamientos afines."
        ),
    },
    {
        "id": "popular",
        "name": "Popularidad",
        "description": (
            "Rellena con lanzamientos populares que todavía no viste cuando " "faltan candidatos."
        ),
    },
    {
        "id": "random",
        "name": "Exploración aleatoria",
        "description": (
            "Agrega muestras controladas para descubrir discos fuera de tu " "zona habitual."
        ),
    },
]


def active_recommendation_systems() -> List[dict]:
    """Devolver la lista de estrategias activas en el motor de recomendaciones."""

    return list(_ACTIVE_RECOMMENDER_SYSTEMS)
