"""Funciones para actualizar embeddings Two Towers de usuarios individuales."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter


try:
    import numpy as np
except ImportError:
    np = None  # type: ignore

LOGGER = logging.getLogger("two_towers_update")


def update_user_embedding(
    connection: sqlite3.Connection,
    user_id: str,
    min_rating: float = 3.0,  # Debe coincidir con Config.positive_rating_threshold
    embedding_dim: int = 64,  # Se detecta automáticamente desde los embeddings existentes
) -> bool:
    """Actualizar el embedding Two Towers de un usuario específico.

    Esta función recalcula el embedding del usuario basándose en sus calificaciones
    actuales y los embeddings de releases existentes. Usa un enfoque híbrido: calcula
    el embedding del usuario como promedio ponderado de los embeddings de releases
    que calificó positivamente, similar a cómo funciona NMF.

    Esta aproximación funciona porque:
    - Los embeddings de releases están normalizados con L2
    - El producto escalar entre embeddings normalizados = similitud coseno
    - Un promedio ponderado de embeddings normalizados sigue siendo un embedding válido

    Args:
        connection: Conexión a la base de datos
        user_id: ID del usuario
        min_rating: Rating mínimo para considerar positiva una interacción
        embedding_dim: Dimensión de embeddings (se detecta automáticamente)

    Returns:
        True si se actualizó exitosamente, False si no se pudo actualizar
    """
    if np is None:
        LOGGER.error("numpy no está disponible")
        return False

    # Configurar row_factory si no está configurado
    if connection.row_factory is None:
        connection.row_factory = sqlite3.Row

    # Verificar que hay embeddings de releases disponibles y detectar embedding_dim
    release_count_row = connection.execute(
        "SELECT COUNT(*) as count FROM release_embeddings_dl"
    ).fetchone()
    release_count = int(release_count_row[0]) if release_count_row and release_count_row[0] else 0

    if release_count == 0:
        LOGGER.warning("No hay embeddings de releases disponibles")
        return False

    # Verificar que hay suficientes releases con embeddings antes de continuar
    if release_count < 10:
        LOGGER.warning(
            "Muy pocos releases con embeddings disponibles: %d (mínimo recomendado: 10)",
            release_count,
        )
        # Continuamos de todas formas, pero es una advertencia

    # Detectar embedding_dim automáticamente desde los embeddings existentes
    dim_row = connection.execute(
        "SELECT embedding_dim FROM release_embeddings_dl LIMIT 1"
    ).fetchone()
    if dim_row and dim_row["embedding_dim"]:
        detected_dim = int(dim_row["embedding_dim"])
        if detected_dim != embedding_dim:
            LOGGER.info(
                "Ajustando embedding_dim de %d a %d basado en embeddings existentes",
                embedding_dim,
                detected_dim,
            )
            embedding_dim = detected_dim

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

    # Usar el mismo umbral que el sistema de recomendación para consistencia
    # Importar aquí para evitar dependencia circular
    from .recommender import Config

    min_required = Config.min_two_towers_signals
    if len(rows) < min_required:
        LOGGER.warning(
            "Usuario tiene muy pocas calificaciones positivas: %d (requerido: %d)",
            len(rows),
            min_required,
        )
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
        SELECT id_release, embedding_json, embedding_dim
        FROM release_embeddings_dl
        WHERE id_release IN ({placeholders})
        """,
        release_ids,
    )
    embedding_rows = embedding_cursor.fetchall()

    if not embedding_rows:
        LOGGER.warning("No se encontraron embeddings para los releases calificados")
        return False

    # Verificar que tenemos suficientes embeddings para calcular un perfil útil
    if len(embedding_rows) < 5:
        LOGGER.warning(
            (
                "Muy pocos embeddings disponibles para los releases calificados: "
                "%d (mínimo recomendado: 5)"
            ),
            len(embedding_rows),
        )
        # Continuamos de todas formas, pero es una advertencia

    # Verificar que todos tienen la misma dimensión
    dims_found = set()
    for row in embedding_rows:
        dims_found.add(int(row["embedding_dim"]))

    if len(dims_found) > 1:
        LOGGER.warning(
            "Se encontraron múltiples dimensiones de embeddings: %s. Usando la más común.",
            dims_found,
        )
        # Usar la dimensión más común
        dim_counter = Counter(int(row["embedding_dim"]) for row in embedding_rows)
        embedding_dim = dim_counter.most_common(1)[0][0]
    elif len(dims_found) == 1:
        detected_dim = dims_found.pop()
        if detected_dim != embedding_dim:
            LOGGER.info(
                "Ajustando embedding_dim de %d a %d basado en embeddings existentes",
                embedding_dim,
                detected_dim,
            )
            embedding_dim = detected_dim

    # Construir matriz de embeddings de releases y vector de ratings
    release_embeddings = []
    rating_weights = []

    # release_ids y ratings vienen de la misma query, así que deben tener la misma longitud
    if len(release_ids) != len(ratings):
        LOGGER.error(
            "Inconsistencia: release_ids tiene %d elementos pero ratings tiene %d",
            len(release_ids),
            len(ratings),
        )
        return False

    # Construir diccionario de release_id -> rating
    release_to_rating = {rid: rating for rid, rating in zip(release_ids, ratings, strict=False)}

    for row in embedding_rows:
        release_id = int(row["id_release"])
        embedding_json = row["embedding_json"]

        try:
            embedding = json.loads(embedding_json)
        except (json.JSONDecodeError, TypeError) as e:
            LOGGER.warning("Error parseando embedding JSON para release %d: %s", release_id, e)
            continue

        # Validar que el embedding tiene la dimensión correcta
        if not isinstance(embedding, list):
            LOGGER.warning("Embedding para release %d no es una lista", release_id)
            continue

        row_dim = int(row["embedding_dim"])
        if len(embedding) != embedding_dim or row_dim != embedding_dim:
            LOGGER.debug(
                "Saltando release %d: dimensión del embedding (%d) no coincide con esperado (%d)",
                release_id,
                len(embedding),
                embedding_dim,
            )
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

    # Normalizar pesos (evitar división por cero)
    weights_sum = rating_weights_array.sum()
    if weights_sum == 0:
        LOGGER.warning("Suma de pesos es cero, usando pesos uniformes")
        rating_weights_array = np.ones_like(rating_weights_array) / len(rating_weights_array)
    else:
        rating_weights_array = rating_weights_array / weights_sum

    # Calcular promedio ponderado
    user_embedding = np.dot(rating_weights_array, release_embeddings_array)

    # Normalizar el embedding resultante (L2 normalization)
    # Esto es importante porque los embeddings de releases están normalizados
    embedding_norm = np.linalg.norm(user_embedding)
    if embedding_norm > 0:
        user_embedding = user_embedding / embedding_norm
    else:
        LOGGER.warning("Norma del embedding es cero, usando embedding uniforme")
        user_embedding = np.ones(embedding_dim, dtype=np.float32) / np.sqrt(embedding_dim)

    # Guardar embedding actualizado
    user_embedding_list = user_embedding.tolist()

    # Obtener model_version de los embeddings existentes (si hay alguno del mismo usuario)
    model_version_row = connection.execute(
        """
        SELECT model_version FROM user_embeddings_dl WHERE id_user = ?
        """,
        (user_id,),
    ).fetchone()
    model_version = model_version_row["model_version"] if model_version_row else "1.0"

    connection.execute(
        """
        INSERT INTO user_embeddings_dl (
            id_user, embedding_json, embedding_dim, model_version, last_updated
        )
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(id_user) DO UPDATE SET
            embedding_json = excluded.embedding_json,
            embedding_dim = excluded.embedding_dim,
            model_version = excluded.model_version,
            last_updated = datetime('now')
        """,
        (user_id, json.dumps(user_embedding_list), embedding_dim, model_version),
    )
    connection.commit()

    LOGGER.info(
        "Embedding Two Towers actualizado para usuario %s usando %d calificaciones",
        user_id,
        len(release_embeddings),
    )

    return True
