"""Funciones para actualizar embeddings Two Towers de usuarios individuales."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path


try:
    import numpy as np
except ImportError:
    np = None  # type: ignore

try:
    from tensorflow import keras
except ImportError:
    keras = None  # type: ignore

LOGGER = logging.getLogger("two_towers_update")


def _extract_user_features(
    connection: sqlite3.Connection, user_id: str, now: datetime | None = None
) -> dict[str, float | int]:
    """Extract user features from database (same as in build_two_towers.py)."""
    if now is None:
        now = datetime.utcnow()

    cursor = connection.execute(
        """
        SELECT role, join_date, last_active, objectivity_score, soundoffs, ratings_count
        FROM users
        WHERE id_user = ?;
        """,
        (user_id,),
    )
    row = cursor.fetchone()

    if not row:
        # Return default features for missing users
        return {
            "role_idx": 0,
            "objectivity_score": 0.5,
            "soundoffs": 0.0,
            "ratings_count": 0.0,
            "days_since_join": 0.0,
            "days_since_active": 0.0,
        }

    role_str = row["role"] or "user"
    join_date_str = row["join_date"]
    last_active_str = row["last_active"]
    objectivity_score = float(row["objectivity_score"] or 50.0)
    soundoffs = int(row["soundoffs"] or 0)
    ratings_count = int(row["ratings_count"] or 0)

    # Map role to index (simplified: 0=user, 1=admin, 2=mod, etc.)
    role_map = {
        "user": 0,
        "admin": 1,
        "moderator": 2,
        "contributor": 3,
        "staff": 4,
    }
    role_idx = role_map.get(role_str.lower(), 0)

    # Normalize objectivity_score (0-100 -> 0-1)
    objectivity_norm = max(0.0, min(1.0, objectivity_score / 100.0))

    # Log normalize soundoffs and ratings_count
    soundoffs_norm = math.log1p(soundoffs)
    ratings_count_norm = math.log1p(ratings_count)

    # Calculate days since join and last active
    days_since_join = 0.0
    if join_date_str:
        try:
            join_date = datetime.fromisoformat(join_date_str.replace("Z", "+00:00"))
            days_since_join = max(0.0, (now - join_date.replace(tzinfo=None)).days)
        except (ValueError, AttributeError):
            days_since_join = 0.0

    days_since_active = 0.0
    if last_active_str:
        try:
            last_active = datetime.fromisoformat(last_active_str.replace("Z", "+00:00"))
            days_since_active = max(0.0, (now - last_active.replace(tzinfo=None)).days)
        except (ValueError, AttributeError):
            days_since_active = 0.0

    # Normalize days (assume max 20 years = 7300 days)
    days_since_join_norm = min(1.0, days_since_join / 7300.0)
    days_since_active_norm = min(1.0, days_since_active / 365.0)  # Max 1 year inactive

    return {
        "role_idx": role_idx,
        "objectivity_score": objectivity_norm,
        "soundoffs": soundoffs_norm,
        "ratings_count": ratings_count_norm,
        "days_since_join": days_since_join_norm,
        "days_since_active": days_since_active_norm,
    }


def _load_user_tower_model(
    connection: sqlite3.Connection,
) -> tuple[keras.Model | None, dict | None]:
    """Load the user tower model for the current database."""
    if keras is None:
        LOGGER.warning("TensorFlow/Keras no está disponible, usando aproximación por promedio")
        return None, None

    try:
        # Get database path from connection
        # Try PRAGMA database_list first
        db_info = connection.execute("PRAGMA database_list").fetchone()
        if db_info and len(db_info) > 2:
            db_path = Path(db_info[2])
        else:
            # Fallback: try to get from connection string or use default
            # For in-memory or attached databases, we might need a different approach
            # Try to infer from the recommender's database resolution
            from app.recommender import _resolve_database_path

            db_path = _resolve_database_path()

        db_name = db_path.stem  # e.g., "sputnik" or "sputnik_lite"

        # Try to load model and metadata
        models_dir = Path(__file__).resolve().parents[1] / "models"
        model_path = models_dir / f"user_tower_{db_name}.keras"
        metadata_path = models_dir / f"user_tower_{db_name}_metadata.json"

        if not model_path.exists() or not metadata_path.exists():
            LOGGER.info(
                "Modelo no encontrado en %s, usando aproximación por promedio",
                model_path,
            )
            return None, None

        LOGGER.info("Cargando modelo desde %s", model_path)
        # Habilitar deserialización insegura para Lambda layers
        # (necesario para el modelo Two Towers)
        import tensorflow as tf

        tf.keras.config.enable_unsafe_deserialization()

        # Definir las funciones custom que usa el modelo (deben coincidir con las del entrenamiento)
        def l2_normalize_user(x):
            return tf.nn.l2_normalize(x, axis=1)

        def l2_normalize_item(x):
            return tf.nn.l2_normalize(x, axis=1)

        def squeeze_score(x):
            return tf.squeeze(x, axis=-1)

        custom_objects = {
            "l2_normalize_user": l2_normalize_user,
            "l2_normalize_item": l2_normalize_item,
            "squeeze_score": squeeze_score,
        }

        try:
            model = keras.models.load_model(
                str(model_path), compile=False, safe_mode=False, custom_objects=custom_objects
            )
        except Exception as e:
            LOGGER.warning("Error cargando modelo keras: %s", e)
            raise

        with open(metadata_path) as f:
            metadata = json.load(f)

        LOGGER.info("Modelo cargado exitosamente (embedding_dim=%d)", metadata.get("embedding_dim"))
        return model, metadata

    except Exception as e:
        LOGGER.warning("Error cargando modelo: %s, usando aproximación por promedio", e)
        return None, None


def update_user_embedding(
    connection: sqlite3.Connection,
    user_id: str,
    min_rating: float = 3.0,  # Debe coincidir con Config.positive_rating_threshold
    embedding_dim: int = 64,  # Se detecta automáticamente desde los embeddings existentes
) -> bool:
    """Actualizar el embedding Two Towers de un usuario específico.

    Intenta usar el modelo entrenado si está disponible, de lo contrario usa
    una aproximación por promedio ponderado de embeddings de releases.

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

    # Intentar cargar el modelo entrenado
    user_tower_model, model_metadata = _load_user_tower_model(connection)

    # Si tenemos el modelo, verificar si tiene sentido usarlo
    if user_tower_model is not None and model_metadata is not None:
        try:
            # Extraer características del usuario
            user_features = _extract_user_features(connection, user_id)

            # Verificar si el usuario tiene características reales o solo valores por defecto
            # Si todas las características están en valores por defecto,
            # usar aproximación por promedio porque el modelo generaría embeddings
            # casi idénticos para todos los usuarios nuevos
            # Nota: ratings_count puede no estar actualizado en la tabla users, así que verificamos
            # directamente las interacciones del usuario
            interaction_count = (
                connection.execute(
                    "SELECT COUNT(*) as count FROM interactions WHERE id_user = ?",
                    (user_id,),
                ).fetchone()["count"]
                or 0
            )

            has_real_features = (
                interaction_count
                > 0  # Tiene interacciones reales (más confiable que ratings_count)
                or user_features["soundoffs"] > 0.0  # Ha escrito soundoffs
                or user_features["objectivity_score"] != 0.5  # Tiene objectivity_score real
                or user_features["days_since_join"] > 0.0  # Tiene fecha de registro
                or user_features["role_idx"] != 0  # No es usuario común
            )

            if not has_real_features:
                LOGGER.info(
                    (
                        "Usuario %s tiene solo características por defecto, "
                        "usando aproximación por promedio"
                    ),
                    user_id,
                )
                # Continuar con aproximación por promedio (código más abajo)
            else:
                # Preparar inputs para el modelo
                role_idx = np.array([user_features["role_idx"]], dtype=np.int32)
                numeric_features = np.array(
                    [
                        [
                            user_features["objectivity_score"],
                            user_features["soundoffs"],
                            user_features["ratings_count"],
                            user_features["days_since_join"],
                            user_features["days_since_active"],
                        ]
                    ],
                    dtype=np.float32,
                )

                # Generar embedding usando el modelo
                user_embedding = user_tower_model.predict([role_idx, numeric_features], verbose=0)[
                    0
                ]

                # El modelo ya normaliza el embedding, así que está listo para usar
                user_embedding_list = user_embedding.tolist()
                detected_dim = model_metadata.get("embedding_dim", embedding_dim)

                # Obtener model_version
                model_version = model_metadata.get("model_version", "1.0")

                # Guardar embedding
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
                    (user_id, json.dumps(user_embedding_list), detected_dim, model_version),
                )
                connection.commit()

                LOGGER.info(
                    "Embedding Two Towers generado usando modelo entrenado para usuario %s",
                    user_id,
                )
                return True

        except Exception as e:
            LOGGER.warning(
                "Error usando modelo entrenado: %s, intentando aproximación por promedio", e
            )
            # Fallback a aproximación por promedio si hay error

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
