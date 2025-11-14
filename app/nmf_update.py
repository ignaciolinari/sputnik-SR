"""Funciones para actualizar embeddings NMF de usuarios individuales."""

from __future__ import annotations

import json
import logging
import sqlite3


try:
    import numpy as np
except ImportError:
    np = None  # type: ignore

LOGGER = logging.getLogger("nmf_update")


def update_user_embedding(
    connection: sqlite3.Connection,
    user_id: str,
    min_rating: float = 3.0,
    n_components: int = 50,
) -> bool:
    """Actualizar el embedding NMF de un usuario específico.

    Esta función recalcula el embedding del usuario basándose en sus calificaciones
    actuales y los embeddings de releases existentes. Es mucho más rápido que
    reentrenar todo el modelo.

    Args:
        connection: Conexión a la base de datos
        user_id: ID del usuario
        min_rating: Rating mínimo para considerar positiva una interacción
        n_components: Número de componentes latentes (debe coincidir con embeddings existentes)

    Returns:
        True si se actualizó exitosamente, False si no se pudo actualizar
    """
    if np is None:
        LOGGER.error("numpy no está disponible")
        return False

    # Configurar row_factory si no está configurado
    if connection.row_factory is None:
        connection.row_factory = sqlite3.Row

    # Verificar que hay embeddings de releases disponibles
    release_count_row = connection.execute(
        "SELECT COUNT(*) as count FROM release_embeddings"
    ).fetchone()
    release_count = int(release_count_row[0]) if release_count_row and release_count_row[0] else 0

    if release_count == 0:
        LOGGER.warning("No hay embeddings de releases disponibles")
        return False

    # Obtener calificaciones positivas del usuario
    cursor = connection.execute(
        """
        SELECT id_release, rating
        FROM interactions
        WHERE id_user = ? AND rating >= ?
        ORDER BY rating DESC
        """,
        (user_id, min_rating),
    )
    rows = cursor.fetchall()

    if len(rows) < 10:  # Mínimo razonable para calcular embedding
        LOGGER.warning("Usuario tiene muy pocas calificaciones positivas: %d", len(rows))
        return False

    # Cargar embeddings de releases que el usuario calificó
    release_ids = [int(row["id_release"]) for row in rows]
    ratings = [float(row["rating"]) for row in rows]

    if not release_ids:
        LOGGER.warning("No hay releases calificados por el usuario")
        return False

    placeholders = ",".join("?" for _ in release_ids)
    embedding_cursor = connection.execute(
        f"""
        SELECT id_release, embedding_json, n_factors
        FROM release_embeddings
        WHERE id_release IN ({placeholders})
        """,
        release_ids,
    )
    embedding_rows = embedding_cursor.fetchall()

    if not embedding_rows:
        LOGGER.warning("No se encontraron embeddings para los releases calificados")
        return False

    # Verificar que todos tienen el mismo número de factores
    n_factors = int(embedding_rows[0]["n_factors"])
    if n_factors != n_components:
        LOGGER.warning(
            "Número de componentes no coincide: esperado %d, encontrado %d",
            n_components,
            n_factors,
        )
        n_components = n_factors

    # Construir matriz de embeddings de releases y vector de ratings
    release_embeddings = []
    rating_weights = []

    release_to_rating = {rid: rating for rid, rating in zip(release_ids, ratings, strict=False)}

    for row in embedding_rows:
        release_id = int(row["id_release"])
        embedding_json = row["embedding_json"]
        embedding = json.loads(embedding_json)

        if len(embedding) != n_components:
            continue

        release_embeddings.append(embedding)
        # Peso según rating (normalizado a 0-1)
        rating = release_to_rating.get(release_id, min_rating)
        weight = max(0.1, rating / 5.0)  # Mínimo 0.1, máximo 1.0
        rating_weights.append(weight)

    if not release_embeddings:
        LOGGER.warning("No se pudieron cargar embeddings válidos")
        return False

    # Calcular embedding del usuario como promedio ponderado
    # de los embeddings de releases que calificó
    release_embeddings_array = np.array(release_embeddings, dtype=np.float32)
    rating_weights_array = np.array(rating_weights, dtype=np.float32)

    # Normalizar pesos
    rating_weights_array = rating_weights_array / rating_weights_array.sum()

    # Calcular promedio ponderado
    user_embedding = np.dot(rating_weights_array, release_embeddings_array)

    # Normalizar el embedding resultante (opcional, pero ayuda con similitud coseno)
    embedding_norm = np.linalg.norm(user_embedding)
    if embedding_norm > 0:
        user_embedding = user_embedding / embedding_norm

    # Guardar embedding actualizado
    user_embedding_list = user_embedding.tolist()

    connection.execute(
        """
        INSERT INTO user_embeddings (id_user, embedding_json, n_factors, last_updated)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(id_user) DO UPDATE SET
            embedding_json = excluded.embedding_json,
            n_factors = excluded.n_factors,
            last_updated = datetime('now')
        """,
        (user_id, json.dumps(user_embedding_list), n_components),
    )
    connection.commit()

    LOGGER.info(
        "Embedding actualizado para usuario %s usando %d calificaciones",
        user_id,
        len(release_embeddings),
    )

    return True
