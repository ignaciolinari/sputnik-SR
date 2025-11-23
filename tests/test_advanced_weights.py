"""Tests para pesos dinámicos de advanced recommendations."""

from app.recommender import Config
from app.recommender import _calculate_advanced_weights


def test_calculate_advanced_weights_level_1():
    """Nivel 1 (20-29 ratings): Solo NMF."""
    # Por debajo del threshold de nivel 2
    nmf_w, tt_w = _calculate_advanced_weights(25)
    assert nmf_w == 1.0
    assert tt_w == 0.0


def test_calculate_advanced_weights_30_50_ratings():
    """30-50 ratings: Balance inicial 50-50."""
    nmf_w, tt_w = _calculate_advanced_weights(30)
    assert nmf_w == 0.5
    assert tt_w == 0.5

    nmf_w, tt_w = _calculate_advanced_weights(50)
    assert nmf_w == 0.5
    assert tt_w == 0.5


def test_calculate_advanced_weights_51_100_ratings():
    """51-100 ratings: Favorece Two Towers (40-60)."""
    nmf_w, tt_w = _calculate_advanced_weights(60)
    assert nmf_w == 0.4
    assert tt_w == 0.6

    nmf_w, tt_w = _calculate_advanced_weights(100)
    assert nmf_w == 0.4
    assert tt_w == 0.6


def test_calculate_advanced_weights_101_200_ratings():
    """101-200 ratings: Más Two Towers (30-70)."""
    nmf_w, tt_w = _calculate_advanced_weights(120)
    assert nmf_w == 0.3
    assert tt_w == 0.7

    nmf_w, tt_w = _calculate_advanced_weights(200)
    assert nmf_w == 0.3
    assert tt_w == 0.7


def test_calculate_advanced_weights_201_plus_ratings():
    """201+ ratings: Dominio Two Towers (20-80)."""
    nmf_w, tt_w = _calculate_advanced_weights(250)
    assert nmf_w == 0.2
    assert tt_w == 0.8

    nmf_w, tt_w = _calculate_advanced_weights(500)
    assert nmf_w == 0.2
    assert tt_w == 0.8


def test_calculate_advanced_weights_sum_to_one():
    """Los pesos siempre deben sumar 1.0."""
    test_cases = [25, 30, 60, 120, 250, 1000]
    for n_ratings in test_cases:
        nmf_w, tt_w = _calculate_advanced_weights(n_ratings)
        assert (
            abs((nmf_w + tt_w) - 1.0) < 0.0001
        ), f"Weights don't sum to 1.0 for {n_ratings} ratings"


def test_calculate_advanced_weights_progression():
    """Two Towers debe aumentar progresivamente con más ratings."""
    weights_30 = _calculate_advanced_weights(30)
    weights_60 = _calculate_advanced_weights(60)
    weights_120 = _calculate_advanced_weights(120)
    weights_250 = _calculate_advanced_weights(250)

    # Two Towers weight debe ser creciente
    assert weights_30[1] <= weights_60[1]
    assert weights_60[1] <= weights_120[1]
    assert weights_120[1] <= weights_250[1]

    # NMF weight debe ser decreciente
    assert weights_30[0] >= weights_60[0]
    assert weights_60[0] >= weights_120[0]
    assert weights_120[0] >= weights_250[0]


def test_calculate_advanced_weights_boundary_conditions():
    """Casos límite en los bordes de cada segmento."""
    # Justo en el threshold de nivel 2
    nmf_w, tt_w = _calculate_advanced_weights(Config.min_advanced_level_2_signals)
    assert nmf_w == 0.5
    assert tt_w == 0.5

    # Justo antes del threshold de nivel 2
    nmf_w, tt_w = _calculate_advanced_weights(Config.min_advanced_level_2_signals - 1)
    assert nmf_w == 1.0
    assert tt_w == 0.0

    # En los límites de cada rango
    boundaries = [51, 101, 201]
    for boundary in boundaries:
        nmf_w, tt_w = _calculate_advanced_weights(boundary)
        assert 0 <= nmf_w <= 1.0
        assert 0 <= tt_w <= 1.0
        assert abs((nmf_w + tt_w) - 1.0) < 0.0001
