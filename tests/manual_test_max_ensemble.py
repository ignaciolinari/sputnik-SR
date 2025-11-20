#!/usr/bin/env python3
"""
Script manual para comparar Max-Ensemble vs Híbrido actual.

Este NO es un test automatizado de pytest. Es un script de testing manual
que requiere un user_id real para comparar recomendaciones.

Uso:
    python tests/manual_test_max_ensemble.py USER_ID

Ejemplo:
    python tests/manual_test_max_ensemble.py ignacio
"""

import sys
from pathlib import Path


# Agregar el directorio app al path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import recommender


def test_max_ensemble(user_id: str):
    """Prueba básica de max-ensemble vs híbrido actual."""

    print("=" * 80)
    print(f"TESTING MAX-ENSEMBLE vs HÍBRIDO para usuario: {user_id}")
    print("=" * 80)

    # Información del usuario
    try:
        interactions = recommender._user_interactions(user_id)
        positive = [i for i in interactions if i.rating >= 3.0]
        print(f"\nUsuario: {user_id}")
        print(f"  - Total interacciones: {len(interactions)}")
        print(f"  - Interacciones positivas: {len(positive)}")
    except Exception as e:
        print(f"  - No se pudo obtener info del usuario: {e}")

    print("\n" + "-" * 80)
    print("1. HÍBRIDO ACTUAL (recommend):")
    print("-" * 80)
    try:
        recs_hybrid = recommender.recommend(user_id, limit=9)
        print(f"Recomendaciones: {len(recs_hybrid)}")
        for i, rid in enumerate(recs_hybrid, 1):
            print(f"  {i}. Release ID: {rid}")

        # Obtener explicaciones
        explanations = recommender.last_explanations(user_id)
        if explanations:
            print("\nExplicaciones:")
            for exp in explanations:
                print(f"  • {exp}")
    except Exception as e:
        print(f"❌ Error: {e}")

    print("\n" + "-" * 80)
    print("2. MAX-ENSEMBLE (recommend_max_ensemble):")
    print("-" * 80)
    try:
        recs_max = recommender.recommend_max_ensemble(user_id, limit=9)
        print(f"Recomendaciones: {len(recs_max)}")
        for i, rid in enumerate(recs_max, 1):
            print(f"  {i}. Release ID: {rid}")

        # Obtener explicaciones
        explanations = recommender.last_explanations(user_id)
        if explanations:
            print("\nExplicaciones:")
            for exp in explanations:
                print(f"  • {exp}")
    except Exception as e:
        print(f"❌ Error: {e}")

    print("\n" + "-" * 80)
    print("3. COMPARACIÓN:")
    print("-" * 80)
    try:
        overlap = set(recs_hybrid) & set(recs_max)
        print(f"Releases en común: {len(overlap)} de {min(len(recs_hybrid), len(recs_max))}")

        if overlap:
            print(f"IDs en común: {sorted(overlap)}")

        unique_hybrid = set(recs_hybrid) - set(recs_max)
        unique_max = set(recs_max) - set(recs_hybrid)

        print(f"\nÚnicos del híbrido: {len(unique_hybrid)}")
        if unique_hybrid:
            print(f"  {sorted(unique_hybrid)}")

        print(f"\nÚnicos del max-ensemble: {len(unique_max)}")
        if unique_max:
            print(f"  {sorted(unique_max)}")

    except Exception as e:
        print(f"Error en comparación: {e}")

    print("\n" + "=" * 80)
    print("✅ Testing completado")
    print("=" * 80)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python tests/manual_test_max_ensemble.py USER_ID")
        print("\nEjemplo: python tests/manual_test_max_ensemble.py ignacio")
        sys.exit(1)

    user_id = sys.argv[1]
    test_max_ensemble(user_id)
