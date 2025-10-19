"""Utilidades de metricas de ranking usadas por la interfaz de recomendaciones."""

from __future__ import annotations

import math
from typing import Iterable
from typing import Sequence


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
