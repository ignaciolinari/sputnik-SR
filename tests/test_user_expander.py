from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable
from typing import List
from typing import Optional

import pytest  # type: ignore[import]

from crawler.user_expander import ExpansionConfig
from crawler.user_expander import expand_users
from scraper.user_ratings import UserRatingEntry
from scraper.users import UserProfile


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "data" / "schema.sql"


class StubClient:
    def close(self) -> None:  # pragma: no cover - compatibility only
        return


@pytest.fixture(name="db_path")
def _db_path(tmp_path: Path) -> Iterable[Path]:
    db_file = tmp_path / "expander.db"
    connection = sqlite3.connect(db_file)
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.execute(
        "INSERT INTO artists (id_artist, name) VALUES (?, ?)",
        (1, "Existing Artist"),
    )
    connection.execute(
        "INSERT INTO releases (id_release, title, artist_id, release_type) VALUES (?, ?, ?, ?)",
        (200, "Existing Album", 1, "LP"),
    )
    connection.commit()
    connection.close()
    yield db_file


def _profile_fetcher(user_id: str, client: StubClient) -> Optional[UserProfile]:
    return UserProfile(
        user_id=user_id,
        display_name=user_id,
        role="USER",
        join_date="2020-01-01",
        last_active="2024-01-01T00:00:00",
        soundoffs=10,
        ratings_count=20,
        objectivity_score=75.0,
        member_id="4242",
    )


def _ratings_fetcher(
    user_id: str, client: StubClient, member_id: Optional[str], max_pages: Optional[int]
) -> List[UserRatingEntry]:
    assert member_id == "4242"
    return [
        UserRatingEntry(
            user_id=user_id,
            release_id=100,
            release_title="Sample Album",
            artist_name="New Artist",
            rating=4.5,
            rating_date=None,
            url="https://example.com/uservote.php?memberid=test",
        ),
        UserRatingEntry(
            user_id=user_id,
            release_id=200,
            release_title="Existing Album",
            artist_name="Existing Artist",
            rating=3.0,
            rating_date=None,
            url="https://example.com/uservote.php?memberid=test",
        ),
    ]


def test_expand_users_updates_profiles_and_interactions(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO users (id_user) VALUES (?)",
            ("test-user",),
        )
        connection.execute(
            "UPDATE crawl_users SET priority = 5, attempts = 0 WHERE id_user = ?",
            ("test-user",),
        )
        connection.commit()

    expand_users(
        ExpansionConfig(
            database_path=db_path,
            schema_path=SCHEMA_PATH,
            batch_size=1,
            fetch_profiles=True,
        ),
        profile_fetcher=_profile_fetcher,  # type: ignore[arg-type]
        ratings_fetcher=_ratings_fetcher,  # type: ignore[arg-type]
        client=StubClient(),  # type: ignore[arg-type]
    )

    with sqlite3.connect(db_path) as connection:
        user_row = connection.execute(
            "SELECT role, join_date, ratings_count, member_id FROM users WHERE id_user = ?",
            ("test-user",),
        ).fetchone()
        assert user_row == ("USER", "2020-01-01", 20, "4242")

        release_rows = connection.execute(
            "SELECT title, artist_id FROM releases WHERE id_release IN (100, 200) ORDER BY id_release",
        ).fetchall()
        assert release_rows == [("Sample Album", 2), ("Existing Album", 1)]

        interaction_rows = connection.execute(
            "SELECT id_release, rating, rating_date FROM interactions WHERE id_user = ? ORDER BY id_release",
            ("test-user",),
        ).fetchall()
        assert interaction_rows == [(100, 4.5, None), (200, 3.0, None)]

        queue_row = connection.execute(
            "SELECT status, attempts FROM crawl_users WHERE id_user = ?",
            ("test-user",),
        ).fetchone()
        assert queue_row == ("done", 1)
