from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest  # type: ignore[import]

from crawler.runner import CrawlConfig, _persist_soundoffs
from scraper.soundoffs import SoundoffEntry
from scraper.users import UserProfile


class DummyClient:
    def close(self) -> None:  # pragma: no cover - compatibility only
        return


def _load_schema(connection: sqlite3.Connection) -> None:
    project_root = Path(__file__).resolve().parents[1]
    schema_path = project_root / "data" / "schema.sql"
    connection.executescript(schema_path.read_text(encoding="utf-8"))


def _prepare_database(connection: sqlite3.Connection) -> None:
    _load_schema(connection)
    connection.execute(
        "INSERT INTO artists (id_artist, name) VALUES (?, ?)",
        (1, "Test Artist"),
    )
    connection.execute(
        "INSERT INTO releases (id_release, title, artist_id, release_type) VALUES (?, ?, ?, ?)",
        (42, "Demo Album", 1, "LP"),
    )
    connection.commit()


def _make_config(**overrides: object) -> CrawlConfig:
    base_kwargs: dict[str, object] = {
        "start_year": 2020,
        "end_year": 2020,
        "database_path": Path("data/sputnik.db"),
        "schema_path": Path("data/schema.sql"),
        "timeout": 5.0,
        "max_retries": 0,
        "min_interval": 0.0,
        "dry_run": False,
        "fetch_tracklists": False,
        "fetch_soundoffs": True,
        "max_soundoffs": None,
        "fetch_user_profiles": False,
        "queue_users": True,
        "user_queue_priority": 0,
    }
    base_kwargs.update(overrides)
    return CrawlConfig(**base_kwargs)  # type: ignore[arg-type]


def _make_soundoff(user_id: str, *, role: str | None = None) -> SoundoffEntry:
    return SoundoffEntry(
        album_id=42,
        user_id=user_id,
        user_display=user_id,
        user_role=role,
        rating=4.0,
        rating_label="superb",
        rating_date="2024-01-01",
        soundoff_text=None,
        source_url="https://example.com/soundoff.php?albumid=42",
    )


def test_persist_soundoffs_enqueues_user_when_profiles_disabled() -> None:
    connection = sqlite3.connect(":memory:")
    _prepare_database(connection)

    config = _make_config(fetch_user_profiles=False, queue_users=True, user_queue_priority=7)
    processed: set[str] = set()

    _persist_soundoffs(
        connection,
        [_make_soundoff("user-alpha", role="EMERITUS")],
        DummyClient(),
        processed,
        config=config,
    )
    connection.commit()

    assert "user-alpha" in processed

    user_row = connection.execute(
        "SELECT role FROM users WHERE id_user = ?",
        ("user-alpha",),
    ).fetchone()
    assert user_row is not None
    assert user_row[0] == "EMERITUS"

    queue_row = connection.execute(
        "SELECT status, priority FROM crawl_users WHERE id_user = ?",
        ("user-alpha",),
    ).fetchone()
    assert queue_row == ("pending", 7)

    interaction_row = connection.execute(
        "SELECT rating FROM interactions WHERE id_release = ? AND id_user = ?",
        (42, "user-alpha"),
    ).fetchone()
    assert interaction_row == (4.0,)


def test_persist_soundoffs_marks_queue_done_when_profile_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:")
    _prepare_database(connection)

    profile = UserProfile(
        user_id="user-beta",
        display_name="User Beta",
        role=None,
        join_date="2022-01-01",
        last_active=None,
        soundoffs=15,
        ratings_count=30,
        objectivity_score=87.5,
    )

    def fake_fetch(user_id: str, client: object) -> UserProfile:
        assert user_id == "user-beta"
        return profile

    monkeypatch.setattr("crawler.runner._safe_fetch_user_profile", fake_fetch)

    config = _make_config(fetch_user_profiles=True, queue_users=True, user_queue_priority=2)
    processed: set[str] = set()

    _persist_soundoffs(
        connection,
        [_make_soundoff("user-beta")],
        DummyClient(),
        processed,
        config=config,
    )
    connection.commit()

    user_row = connection.execute(
        "SELECT join_date, soundoffs, ratings_count FROM users WHERE id_user = ?",
        ("user-beta",),
    ).fetchone()
    assert user_row == ("2022-01-01", 15, 30)

    queue_row = connection.execute(
        "SELECT status, priority FROM crawl_users WHERE id_user = ?",
        ("user-beta",),
    ).fetchone()
    assert queue_row == ("done", 2)

    assert "user-beta" in processed
