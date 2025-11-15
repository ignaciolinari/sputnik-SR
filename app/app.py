"""Aplicacion Flask que ofrece una interfaz simple para recomendar discos de Sputnik."""

from __future__ import annotations

import math
import os

from flask import Flask
from flask import abort
from flask import g
from flask import jsonify
from flask import make_response
from flask import redirect
from flask import render_template
from flask import request
from flask import url_for

from . import nmf_update
from . import recommender
from . import two_towers_update


app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True


LAST_APP_UPDATE = os.getenv("SPUTNIK_LAST_UPDATE", "15/11/2025")


RATING_CHOICES = [
    {"value": "0.5", "label": "Peor que ruido - 0.5"},
    {"value": "1.0", "label": "Horrible - 1.0"},
    {"value": "1.5", "label": "Muy pobre - 1.5"},
    {"value": "2.0", "label": "Pobre - 2.0"},
    {"value": "2.5", "label": "Promedio - 2.5"},
    {"value": "3.0", "label": "Bueno - 3.0"},
    {"value": "3.5", "label": "Muy bueno - 3.5"},
    {"value": "4.0", "label": "Excelente - 4.0"},
    {"value": "4.5", "label": "Magnifico - 4.5"},
    {"value": "5.0", "label": "Clasico - 5.0"},
]


def _database_variant_options(
    preferred_variant: str | None = None,
) -> tuple[list[dict], str | None]:
    variants = recommender.available_database_variants()
    available_ids = [item["id"] for item in variants if item["available"]]

    selected = preferred_variant if preferred_variant in available_ids else None
    if not selected:
        if available_ids:
            selected = available_ids[0]
        elif variants:
            selected = variants[0]["id"]
    return variants, selected


@app.before_request
def _apply_request_database_variant() -> None:
    preferred = request.cookies.get("db_variant")
    token = recommender.set_request_database_variant(preferred)
    g._db_variant_token = token


@app.teardown_request
def _reset_request_database_variant(_exc: Exception | None) -> None:
    token = getattr(g, "_db_variant_token", None)
    if token is not None:
        recommender.reset_request_database_variant(token)
        g._db_variant_token = None


@app.get("/")
def login_form() -> str:
    preferred_variant = request.cookies.get("db_variant")
    db_variants, selected_variant = _database_variant_options(preferred_variant)
    return render_template(
        "login.html",
        db_variants=db_variants,
        selected_db_variant=selected_variant,
    )


@app.post("/")
def login_submit():
    user_id = (request.form.get("id_usuario") or "").strip()
    requested_variant = (request.form.get("db_variant") or "").strip()

    db_variants, selected_variant = _database_variant_options(requested_variant)

    if not user_id:
        return render_template(
            "login.html",
            error="Necesitamos un usuario valido.",
            db_variants=db_variants,
            selected_db_variant=selected_variant,
        )

    previous_token = getattr(g, "_db_variant_token", None)
    if previous_token is not None:
        recommender.reset_request_database_variant(previous_token)
    new_token = recommender.set_request_database_variant(selected_variant)
    g._db_variant_token = new_token

    recommender.ensure_user(user_id)

    response = make_response(redirect("/recomendaciones"))
    response.set_cookie("id_usuario", user_id, max_age=60 * 60 * 24 * 30, samesite="Lax")
    if selected_variant:
        response.set_cookie(
            "db_variant", selected_variant, max_age=60 * 60 * 24 * 30, samesite="Lax"
        )
    else:
        response.delete_cookie("db_variant")
    return response


@app.get("/recomendaciones")
def recommendations():
    user_id = request.cookies.get("id_usuario")
    if not user_id:
        return redirect("/")
    search_query = (request.args.get("q") or "").strip()
    artist_filter = (request.args.get("artist") or "").strip()
    genre_id_raw = (request.args.get("genre_id") or "").strip()
    year_raw = (request.args.get("year") or "").strip()
    release_type = (request.args.get("type") or "").strip()
    page_raw = (request.args.get("page") or "1").strip()

    genre_id = None
    if genre_id_raw:
        try:
            genre_id = int(genre_id_raw)
        except ValueError:
            genre_id = None

    release_year = None
    if year_raw:
        try:
            release_year = int(year_raw)
        except ValueError:
            release_year = None

    current_page = 1
    if page_raw:
        try:
            current_page_candidate = int(page_raw)
        except ValueError:
            current_page_candidate = 1
        if current_page_candidate > 0:
            current_page = current_page_candidate

    has_filters = any(
        [
            search_query,
            artist_filter,
            genre_id is not None,
            release_year is not None,
            release_type,
        ]
    )

    search_results = []
    catalog_pagination = None
    per_page = 36

    if has_filters:
        total_results = recommender.count_catalog(
            query=search_query or None,
            artist=artist_filter or None,
            genre_id=genre_id,
            release_year=release_year,
            release_type=release_type or None,
        )
        total_pages = math.ceil(total_results / per_page) if total_results else 0
        if total_pages == 0:
            current_page = 1
        elif current_page > total_pages:
            current_page = total_pages

        offset = (current_page - 1) * per_page if total_results else 0

        search_results = recommender.search_catalog(
            query=search_query or None,
            artist=artist_filter or None,
            genre_id=genre_id,
            release_year=release_year,
            release_type=release_type or None,
            limit=per_page,
            offset=offset,
        )

        query_params_base = request.args.to_dict(flat=True)

        has_next = total_pages > 0 and current_page < total_pages
        remaining_pages = max(0, total_pages - current_page)

        next_page_url = None
        if has_next:
            next_query_params = dict(query_params_base)
            next_query_params["page"] = current_page + 1
            next_page_url = url_for("recommendations", **next_query_params)

        has_prev = total_pages > 0 and current_page > 1
        prev_page_url = None
        if has_prev:
            prev_query_params = dict(query_params_base)
            prev_page = current_page - 1
            if prev_page <= 1:
                prev_query_params.pop("page", None)
            else:
                prev_query_params["page"] = prev_page
            prev_page_url = url_for("recommendations", **prev_query_params)

        if total_results > per_page:
            catalog_pagination = {
                "total_results": total_results,
                "page": current_page,
                "per_page": per_page,
                "total_pages": total_pages,
                "remaining_pages": remaining_pages,
                "has_next": has_next,
                "next_page_url": next_page_url,
                "has_prev": has_prev,
                "prev_page_url": prev_page_url,
            }

    release_ids = recommender.recommend(user_id)

    # Batch insert para marcar releases como vistos (más eficiente que loop)
    if release_ids:
        recommender.store_interactions_batch([(rid, user_id, 0.0) for rid in release_ids])

    releases = recommender.release_details(release_ids)
    recommendation_ids = {item["id_release"] for item in releases}
    if search_results:
        search_results = [
            item for item in search_results if item["id_release"] not in recommendation_ids
        ]

    all_release_ids = [item["id_release"] for item in releases] + [
        item["id_release"] for item in search_results
    ]
    user_ratings = recommender.user_ratings_map(user_id, all_release_ids)

    # Optimización: obtener rated_count y seen_count en una sola consulta
    rated_count, seen_count = recommender._user_interaction_counts(user_id)
    explanations = recommender.last_explanations(user_id)

    # Optimización: contar interacciones positivas sin cargar todas las interacciones
    positive_count = recommender._count_positive_interactions(
        user_id, recommender.Config.positive_rating_threshold
    )

    # Obtener nivel de recomendaciones avanzadas
    advanced_level, current_signals, next_level_signals = (
        recommender.get_advanced_recommendations_level(user_id)
    )
    has_advanced_embedding = False
    if advanced_level >= 1:
        has_nmf = recommender.user_has_nmf_embedding(user_id)
        has_tt = (
            recommender.user_has_two_towers_embedding(user_id) if advanced_level >= 2 else False
        )
        has_advanced_embedding = has_nmf or (advanced_level >= 2 and has_tt)

    genre_options = list(recommender.list_genres())
    year_options = list(recommender.list_release_years())
    type_options = list(recommender.list_release_types())

    current_query_params = request.args.to_dict(flat=True)
    if has_filters:
        if current_page > 1 or "page" in current_query_params:
            current_query_params["page"] = current_page
    else:
        current_query_params.pop("page", None)

    if current_query_params:
        next_url = url_for("recommendations", **current_query_params)
    else:
        next_url = request.path

    return render_template(
        "recommendations.html",
        user_id=user_id,
        releases=releases,
        search_results=search_results,
        has_filters=has_filters,
        catalog_pagination=catalog_pagination,
        rated_count=rated_count,
        seen_count=seen_count,
        explanations=explanations,
        filter_values={
            "q": search_query,
            "artist": artist_filter,
            "genre_id": genre_id_raw,
            "year": year_raw,
            "type": release_type,
            "page": str(current_page),
        },
        genre_options=genre_options,
        year_options=year_options,
        type_options=type_options,
        user_ratings=user_ratings,
        rating_choices=RATING_CHOICES,
        next_url=next_url,
        last_update_display=LAST_APP_UPDATE,
        database_info=recommender.current_database_info(),
        active_recommenders=recommender.active_recommendation_systems(),
        positive_ratings_count=positive_count,
        advanced_level=advanced_level,
        advanced_current_signals=current_signals,
        advanced_next_level_signals=next_level_signals,
        has_advanced_embedding=has_advanced_embedding,
    )


@app.get("/recomendaciones/<int:release_id>")
def recommendations_for_release(release_id: int):
    user_id = request.cookies.get("id_usuario")
    if not user_id:
        return redirect("/")
    release = recommender.release_detail(release_id)
    if not release:
        abort(404)

    release_ids = recommender.recommend_context(user_id, release_id)
    # Batch insert para marcar releases como vistos
    if release_ids:
        recommender.store_interactions_batch([(rid, user_id, 0.0) for rid in release_ids])

    releases = recommender.release_details(release_ids)
    rated_count = len(recommender.rated_release_ids(user_id))
    seen_count = len(recommender.seen_release_ids(user_id))
    explanations = recommender.last_context_explanations(user_id, release_id)

    all_release_ids = [release_id] + [item["id_release"] for item in releases]
    user_ratings = recommender.user_ratings_map(user_id, all_release_ids)

    next_url = request.path

    return render_template(
        "recommendations_release.html",
        user_id=user_id,
        release=release,
        releases=releases,
        rated_count=rated_count,
        seen_count=seen_count,
        explanations=explanations,
        user_ratings=user_ratings,
        rating_choices=RATING_CHOICES,
        next_url=next_url,
    )


@app.post("/recomendaciones")
def submit_ratings():
    user_id = request.cookies.get("id_usuario")
    if not user_id:
        return redirect("/")

    for release_id, value in request.form.items():
        if release_id == "next":
            continue
        value = (value or "").strip()
        if not value:
            continue
        try:
            rating_value = float(value)
        except ValueError:
            continue
        if rating_value <= 0:
            continue
        rating_value = max(0.0, min(5.0, rating_value))
        try:
            release_int = int(release_id)
        except ValueError:
            continue
        recommender.store_interaction(release_int, user_id, rating_value)

    next_url = (request.form.get("next") or "").strip()
    if not next_url or not next_url.startswith("/"):
        next_url = "/recomendaciones"

    return redirect(next_url)


@app.get("/reset")
def reset_history():
    user_id = request.cookies.get("id_usuario")
    if not user_id:
        return redirect("/")
    recommender.reset_user_history(user_id)
    return redirect("/recomendaciones")


@app.post("/actualizar-recomendaciones-avanzadas")
def update_advanced_recommendations():
    """Actualizar embeddings avanzados (NMF y/o Two Towers) del usuario actual según su nivel."""
    user_id = request.cookies.get("id_usuario")
    if not user_id:
        return redirect("/")

    # Obtener nivel del usuario
    level, current_signals, next_level_signals = recommender.get_advanced_recommendations_level(
        user_id
    )

    if level == 0:
        remaining = next_level_signals - current_signals
        return jsonify(
            {
                "success": False,
                "message": (
                    f"Necesitás {remaining} calificación{'es' if remaining != 1 else ''} "
                    f"positiva{'s' if remaining != 1 else ''} más "
                    f"para desbloquear recomendaciones avanzadas ({next_level_signals} en total)."
                ),
                "level": 0,
                "current_signals": current_signals,
                "next_level_signals": next_level_signals,
            }
        ), 400

    import sqlite3

    from app.recommender import _connect

    systems_updated = []
    errors = []

    try:
        with _connect() as connection:
            connection.row_factory = sqlite3.Row

            # Nivel 1: Solo NMF
            if level >= 1:
                # Verificar que hay releases con embeddings NMF disponibles
                nmf_release_count_row = connection.execute(
                    "SELECT COUNT(*) as count FROM release_embeddings"
                ).fetchone()
                nmf_release_count = (
                    int(nmf_release_count_row[0])
                    if nmf_release_count_row and nmf_release_count_row[0]
                    else 0
                )

                if nmf_release_count == 0:
                    errors.append(
                        "No hay embeddings NMF de releases disponibles. "
                        "Necesitás ejecutar el script de construcción de embeddings primero."
                    )
                else:
                    success_nmf = nmf_update.update_user_embedding(
                        connection,
                        user_id,
                        min_rating=recommender.Config.positive_rating_threshold,
                        n_components=50,
                    )
                    if success_nmf:
                        systems_updated.append("NMF")
                    else:
                        errors.append("No se pudo actualizar el embedding NMF.")

            # Nivel 2: También Two Towers
            if level >= 2:
                # Verificar que hay releases con embeddings Two Towers disponibles
                tt_release_count_row = connection.execute(
                    "SELECT COUNT(*) as count FROM release_embeddings_dl"
                ).fetchone()
                tt_release_count = (
                    int(tt_release_count_row[0])
                    if tt_release_count_row and tt_release_count_row[0]
                    else 0
                )

                if tt_release_count == 0:
                    errors.append(
                        "No hay embeddings Two Towers de releases disponibles. "
                        "Necesitás ejecutar el script de construcción de embeddings primero."
                    )
                else:
                    success_tt = two_towers_update.update_user_embedding(
                        connection,
                        user_id,
                        min_rating=recommender.Config.positive_rating_threshold,
                        embedding_dim=64,
                    )
                    if success_tt:
                        systems_updated.append("Two Towers")
                    else:
                        errors.append("No se pudo actualizar el embedding Two Towers.")

        # Limpiar cache de recomendaciones para forzar recálculo
        recommender._LAST_EXPLANATIONS.pop(user_id, None)
        recommender._LAST_STRATEGY.pop(user_id, None)

        if systems_updated:
            level_name = "Nivel 1 (NMF)" if level == 1 else "Nivel 2 (NMF + Two Towers)"
            systems_str = " y ".join(systems_updated)
            message = (
                f"Recomendaciones avanzadas ({level_name}) actualizadas exitosamente. "
                f"Sistemas actualizados: {systems_str}."
            )
            if errors:
                message += f" Advertencias: {' '.join(errors)}"

            return jsonify(
                {
                    "success": True,
                    "message": message,
                    "level": level,
                    "systems_updated": systems_updated,
                    "warnings": errors if errors else None,
                }
            )
        else:
            return jsonify(
                {
                    "success": False,
                    "message": (
                        "No se pudieron actualizar los embeddings. " f"Errores: {' '.join(errors)}"
                    ),
                    "level": level,
                    "errors": errors,
                }
            ), 500

    except Exception as e:
        import logging

        logging.exception("Error actualizando recomendaciones avanzadas")
        return jsonify(
            {
                "success": False,
                "message": f"Error al actualizar embeddings: {str(e)}",
                "level": level,
            }
        ), 500


@app.get("/usuarios/<user_id>")
def user_collection_page(user_id: str):
    current_user = request.cookies.get("id_usuario")
    if not current_user:
        return redirect("/")

    target_user = (user_id or "").strip()
    if not target_user:
        abort(404)

    recommender.ensure_user(target_user)
    collection = recommender.user_collection(target_user)

    rated_count = len(recommender.rated_release_ids(target_user))
    seen_count = len(recommender.seen_release_ids(target_user))

    next_url = request.path

    collection_release_ids = [item["id_release"] for item in collection]
    user_ratings = {}
    if current_user == target_user:
        user_ratings = recommender.user_ratings_map(target_user, collection_release_ids)

    viewed_user = {
        "id": target_user,
        "is_self": current_user == target_user,
    }

    return render_template(
        "user_collection.html",
        user_id=current_user,
        viewed_user=viewed_user,
        collection=collection,
        rated_count=rated_count,
        seen_count=seen_count,
        next_url=next_url,
        rating_choices=RATING_CHOICES,
        user_ratings=user_ratings,
    )


@app.get("/artistas/<int:artist_id>")
def artist_page(artist_id: int):
    user_id = request.cookies.get("id_usuario")
    if not user_id:
        return redirect("/")

    artist = recommender.artist_detail(artist_id)
    if not artist:
        abort(404)

    releases = recommender.releases_by_artist(artist_id)
    rated_count = len(recommender.rated_release_ids(user_id))
    seen_count = len(recommender.seen_release_ids(user_id))

    release_ids = [item["id_release"] for item in releases]
    user_ratings = recommender.user_ratings_map(user_id, release_ids)

    next_url = request.path

    return render_template(
        "artist.html",
        user_id=user_id,
        artist=artist,
        releases=releases,
        rated_count=rated_count,
        seen_count=seen_count,
        user_ratings=user_ratings,
        next_url=next_url,
        rating_choices=RATING_CHOICES,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5050)
