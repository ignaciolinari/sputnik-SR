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

    all_release_ids = [item["id_release"] for item in releases] + [
        item["id_release"] for item in search_results
    ]
    user_ratings = recommender.user_ratings_map(user_id, all_release_ids)

    rated_count = len(recommender.rated_release_ids(user_id))
    seen_count = len(recommender.seen_release_ids(user_id))
    explanations = recommender.last_explanations(user_id)
    genre_options = recommender.list_genres()
    year_options = recommender.list_release_years()
    type_options = recommender.list_release_types()

    query_string = request.query_string.decode()
    next_url = request.path
    if query_string:
        next_url = f"{request.path}?{query_string}"

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
        user_ratings=user_ratings,
        rating_choices=RATING_CHOICES,
        next_url=next_url,
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
