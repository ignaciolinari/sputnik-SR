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
    release_ids = recommender.recommend(user_id)

    for release_id in release_ids:
        recommender.store_interaction(release_id, user_id, 0)

    releases = recommender.release_details(release_ids)
    rated_count = len(recommender.rated_release_ids(user_id))
    seen_count = len(recommender.seen_release_ids(user_id))
    explanations = recommender.last_explanations(user_id)

    return render_template(
        "recommendations.html",
        user_id=user_id,
        releases=releases,
        rated_count=rated_count,
        seen_count=seen_count,
        explanations=explanations,
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
