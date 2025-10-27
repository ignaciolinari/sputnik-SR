"""Aplicacion Flask que ofrece una interfaz simple para recomendar discos de Sputnik."""

from __future__ import annotations

from flask import Flask
from flask import abort
from flask import make_response
from flask import redirect
from flask import render_template
from flask import request

from . import recommender


app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True


@app.get("/")
def login_form() -> str:
    return render_template("login.html")


@app.post("/")
def login_submit():
    user_id = (request.form.get("id_usuario") or "").strip()
    if user_id:
        recommender.ensure_user(user_id)
        response = make_response(redirect("/recomendaciones"))
        response.set_cookie("id_usuario", user_id, max_age=60 * 60 * 24 * 30)
        return response
    return render_template("login.html", error="Necesitamos un usuario valido.")


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
    if has_filters:
        search_results = recommender.search_catalog(
            query=search_query or None,
            artist=artist_filter or None,
            genre_id=genre_id,
            release_year=release_year,
            release_type=release_type or None,
            limit=36,
        )

    release_ids = recommender.recommend(user_id)

    for release_id in release_ids:
        recommender.store_interaction(release_id, user_id, 0)

    releases = recommender.release_details(release_ids)
    recommendation_ids = {item["id_release"] for item in releases}
    if search_results:
        search_results = [
            item for item in search_results if item["id_release"] not in recommendation_ids
        ]

    rated_count = len(recommender.rated_release_ids(user_id))
    seen_count = len(recommender.seen_release_ids(user_id))
    explanations = recommender.last_explanations(user_id)
    genre_options = recommender.list_genres()
    year_options = recommender.list_release_years()
    type_options = recommender.list_release_types()

    return render_template(
        "recommendations.html",
        user_id=user_id,
        releases=releases,
        search_results=search_results,
        has_filters=has_filters,
        rated_count=rated_count,
        seen_count=seen_count,
        explanations=explanations,
        filter_values={
            "q": search_query,
            "artist": artist_filter,
            "genre_id": genre_id_raw,
            "year": year_raw,
            "type": release_type,
        },
        genre_options=genre_options,
        year_options=year_options,
        type_options=type_options,
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
    for candidate_id in release_ids:
        recommender.store_interaction(candidate_id, user_id, 0)

    releases = recommender.release_details(release_ids)
    rated_count = len(recommender.rated_release_ids(user_id))
    seen_count = len(recommender.seen_release_ids(user_id))
    explanations = recommender.last_context_explanations(user_id, release_id)

    return render_template(
        "recommendations_release.html",
        user_id=user_id,
        release=release,
        releases=releases,
        rated_count=rated_count,
        seen_count=seen_count,
        explanations=explanations,
    )


@app.post("/recomendaciones")
def submit_ratings():
    user_id = request.cookies.get("id_usuario")
    if not user_id:
        return redirect("/")

    for release_id, value in request.form.items():
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

    return redirect("/recomendaciones")


@app.get("/reset")
def reset_history():
    user_id = request.cookies.get("id_usuario")
    if not user_id:
        return redirect("/")
    recommender.reset_user_history(user_id)
    return redirect("/recomendaciones")


if __name__ == "__main__":
    app.run(debug=True, port=5050)
