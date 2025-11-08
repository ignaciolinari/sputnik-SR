from __future__ import annotations

from typing import Any
from typing import List

import pytest

from app import recommender


def test_list_genres_returns_expected_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_select(query: str, params: List[Any] | None = None) -> List[dict[str, Any]]:
        captured["query"] = query
        captured["params"] = params
        return [
            {"id_genre": 1, "name": "Rock"},
            {"id_genre": 2, "name": "Jazz"},
        ]

    monkeypatch.setattr(recommender, "_select", fake_select)

    result = recommender.list_genres(limit=5)

    assert captured["params"] == [5]
    assert "FROM genres" in captured["query"]
    assert result == [
        {"id_genre": 1, "name": "Rock"},
        {"id_genre": 2, "name": "Jazz"},
    ]


def test_list_release_years_returns_integers(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_select(query: str, params: List[Any] | None = None) -> List[dict[str, Any]]:
        captured["query"] = query
        captured["params"] = params
        return [{"release_year": 2024}, {"release_year": 1998}]

    monkeypatch.setattr(recommender, "_select", fake_select)

    result = recommender.list_release_years(limit=3)

    assert captured["params"] == [3]
    assert "FROM releases" in captured["query"]
    assert result == [2024, 1998]


def test_list_release_types_returns_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_query: dict[str, Any] = {}

    def fake_select(query: str, params: List[Any] | None = None) -> List[dict[str, Any]]:
        captured_query["query"] = query
        captured_query["params"] = params
        return [{"release_type": "LP"}, {"release_type": "EP"}]

    monkeypatch.setattr(recommender, "_select", fake_select)

    result = recommender.list_release_types()

    assert captured_query["params"] is None
    assert "SELECT DISTINCT release_type" in captured_query["query"]
    assert result == ["LP", "EP"]


def test_search_catalog_applies_all_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_select(query: str, params: List[Any]) -> List[dict[str, Any]]:
        captured["query"] = query
        captured["params"] = params
        return [{"id_release": 10}, {"id_release": 20}]

    def fake_release_details(release_ids: List[int]) -> List[dict[str, Any]]:
        captured["release_ids"] = release_ids
        return [
            {"id_release": release_id, "title": f"Album {release_id}"} for release_id in release_ids
        ]

    monkeypatch.setattr(recommender, "_select", fake_select)
    monkeypatch.setattr(recommender, "release_details", fake_release_details)

    result = recommender.search_catalog(
        query="Doom",
        artist="Candlemass",
        genre_id=7,
        release_year=1986,
        release_type="LP",
        limit=12,
    )

    assert "WHERE" in captured["query"]
    assert captured["params"] == [
        "%doom%",
        "%doom%",
        "%candlemass%",
        7,
        7,
        1986,
        "lp",
        12,
        0,
    ]
    assert captured["release_ids"] == [10, 20]
    assert [item["id_release"] for item in result] == [10, 20]


def test_search_catalog_handles_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {"release_ids": None}

    def fake_select(query: str, params: List[Any]) -> List[dict[str, Any]]:
        return []

    def fake_release_details(release_ids: List[int]) -> List[dict[str, Any]]:
        captured["release_ids"] = release_ids
        return []

    monkeypatch.setattr(recommender, "_select", fake_select)
    monkeypatch.setattr(recommender, "release_details", fake_release_details)

    result = recommender.search_catalog(limit=5)

    assert captured["release_ids"] == []
    assert result == []


def test_count_catalog_returns_total(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_select(query: str, params: List[Any]) -> List[dict[str, Any]]:
        captured["query"] = query
        captured["params"] = params
        return [{"total": 42}]

    monkeypatch.setattr(recommender, "_select", fake_select)

    total = recommender.count_catalog(
        query="doom",
        artist="candlemass",
        genre_id=5,
        release_year=1986,
        release_type="LP",
    )

    assert "COUNT(DISTINCT r.id_release)" in captured["query"]
    assert captured["params"] == [
        "%doom%",
        "%doom%",
        "%candlemass%",
        5,
        5,
        1986,
        "lp",
    ]
    assert total == 42
