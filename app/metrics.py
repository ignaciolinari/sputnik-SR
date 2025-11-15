"""Utilidades de metricas de ranking usadas por la interfaz de recomendaciones."""

from __future__ import annotations

import math
from typing import Iterable
from typing import Sequence
from typing import Set


def discounted_cumulative_gain(relevance_scores: Sequence[float] | Iterable[float]) -> float:
    """Calcular la DCG para una lista ordenada de puntajes de relevancia."""
    scores = list(relevance_scores)
    if not scores:
        return 0.0

    dcg = 0.0
    for index, relevance in enumerate(scores):
        dcg += float(relevance) / math.log2(index + 2)
    return dcg


def ideal_discounted_cumulative_gain(relevance_scores: Sequence[float] | Iterable[float]) -> float:
    """Calcular la DCG ideal ordenando los puntajes de forma descendente."""
    scores = sorted((float(score) for score in relevance_scores), reverse=True)
    return discounted_cumulative_gain(scores)


def normalized_discounted_cumulative_gain(
    relevance_scores: Sequence[float] | Iterable[float],
) -> float:
    """Devolver la NDCG, controlando el caso de puntaje ideal igual a cero."""
    dcg = discounted_cumulative_gain(relevance_scores)
    idcg = ideal_discounted_cumulative_gain(relevance_scores)
    if idcg == 0:
        return 0.0
    return dcg / idcg


def precision_at_k(
    recommended: Sequence[int] | Iterable[int],
    relevant: Set[int] | Sequence[int],
    k: int,
) -> float:
    """Calcular Precision@k: proporción de recomendaciones relevantes en top-k."""
    recommended_list = list(recommended)[:k]
    if not recommended_list:
        return 0.0
    relevant_set = set(relevant) if not isinstance(relevant, Set) else relevant
    relevant_count = sum(1 for item in recommended_list if item in relevant_set)
    return relevant_count / len(recommended_list)


def recall_at_k(
    recommended: Sequence[int] | Iterable[int],
    relevant: Set[int] | Sequence[int],
    k: int,
) -> float:
    """Calcular Recall@k: proporción de items relevantes recuperados en top-k."""
    recommended_list = list(recommended)[:k]
    relevant_set = set(relevant) if not isinstance(relevant, Set) else relevant
    if not relevant_set:
        return 0.0
    relevant_count = sum(1 for item in recommended_list if item in relevant_set)
    return relevant_count / len(relevant_set)


def f1_at_k(
    recommended: Sequence[int] | Iterable[int],
    relevant: Set[int] | Sequence[int],
    k: int,
) -> float:
    """Calcular F1@k: media armónica de Precision@k y Recall@k."""
    precision = precision_at_k(recommended, relevant, k)
    recall = recall_at_k(recommended, relevant, k)
    if precision + recall == 0:
        return 0.0
    return 2 * (precision * recall) / (precision + recall)


def mean_reciprocal_rank(
    recommended: Sequence[int] | Iterable[int],
    relevant: Set[int] | Sequence[int],
) -> float:
    """Calcular MRR: inverso de la posición del primer item relevante."""
    recommended_list = list(recommended)
    relevant_set = set(relevant) if not isinstance(relevant, Set) else relevant
    for idx, item in enumerate(recommended_list, start=1):
        if item in relevant_set:
            return 1.0 / idx
    return 0.0


def genre_diversity(
    recommended: Sequence[int] | Iterable[int],
    release_to_genres: dict[int, list[int]],
) -> float:
    """Calcular diversidad de géneros: proporción de géneros únicos en las recomendaciones."""
    recommended_list = list(recommended)
    if not recommended_list:
        return 0.0
    unique_genres: Set[int] = set()
    for release_id in recommended_list:
        genres = release_to_genres.get(release_id, [])
        unique_genres.update(genres)
    # Normalizar por número de recomendaciones
    # (máximo sería 1.0 si cada release tiene géneros únicos)
    # Usamos una métrica más interpretable: géneros únicos / número de releases
    if not unique_genres:
        return 0.0
    return len(unique_genres) / len(recommended_list)


def artist_diversity(
    recommended: Sequence[int] | Iterable[int],
    release_to_artist: dict[int, int],
) -> float:
    """Calcular diversidad de artistas: proporción de artistas únicos en las recomendaciones."""
    recommended_list = list(recommended)
    if not recommended_list:
        return 0.0
    unique_artists: Set[int] = set()
    for release_id in recommended_list:
        artist_id = release_to_artist.get(release_id)
        if artist_id and artist_id > 0:
            unique_artists.add(artist_id)
    if not unique_artists:
        return 0.0
    return len(unique_artists) / len(recommended_list)


def novelty(
    recommended: Sequence[int] | Iterable[int],
    release_to_ratings_count: dict[int, int],
    max_ratings_count: int,
) -> float:
    """Calcular novedad: promedio de -log2(popularity) de los items recomendados.

    Items menos populares tienen mayor novedad. Retorna 0 si no hay recomendaciones.
    """
    recommended_list = list(recommended)
    if not recommended_list or max_ratings_count == 0:
        return 0.0

    novelty_scores = []
    for release_id in recommended_list:
        ratings_count = release_to_ratings_count.get(release_id, 0)
        # Normalizar popularidad a [0, 1]
        popularity = max(1, ratings_count) / max(1, max_ratings_count)
        # Calcular novedad: -log2(popularity)
        # Items con popularity=1 (más populares) tienen novedad=0
        # Items con popularity cercana a 0 tienen alta novedad
        if popularity > 0:
            novelty_score = -math.log2(popularity)
            novelty_scores.append(novelty_score)

    if not novelty_scores:
        return 0.0
    return sum(novelty_scores) / len(novelty_scores)


def coverage(
    all_recommended: Set[int] | Sequence[int],
    catalog_size: int,
) -> float:
    """Calcular cobertura: proporción del catálogo que puede ser recomendado.

    Args:
        all_recommended: Set de todos los releases únicos recomendados
            (acumulado de todos los usuarios)
        catalog_size: Tamaño total del catálogo
    """
    if catalog_size == 0:
        return 0.0
    recommended_set = (
        set(all_recommended) if not isinstance(all_recommended, Set) else all_recommended
    )
    return len(recommended_set) / catalog_size
