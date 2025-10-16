from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable
from typing import List

import pytest  # type: ignore[import]

from crawler.discography import DiscographyConfig
from crawler.discography import expand_discographies
from scraper.discography import ArtistReleaseEntry
from scraper.soundoffs import SoundoffEntry
from scraper.tracklist import TrackEntry


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "data" / "schema.sql"


class StubClient:
    def close(self) -> None:  # pragma: no cover - compatibility only
        return


@pytest.fixture(name="db_path")
def _db_path(tmp_path: Path) -> Iterable[Path]:
    db_file = tmp_path / "discography.db"
    connection = sqlite3.connect(db_file)
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.execute(
        "INSERT INTO artists (id_artist, name) VALUES (?, ?)",
        (1, "Artist One"),
    )
    connection.execute(
        "UPDATE crawl_artists SET status='pending', attempts=0, updated_at=datetime('now') WHERE id_artist = ?",
        (1,),
    )
    connection.commit()
    connection.close()
    yield db_file


def _fetch_discography(artist_id: int, client: StubClient) -> List[ArtistReleaseEntry]:
    assert artist_id == 1
    return [
        ArtistReleaseEntry(
            artist_id=artist_id,
            release_id=1000,
            title="New Release",
            release_type="LP",
            release_year=2024,
            art_url="https://example.com/1000.jpg",
            avg_rating=4.2,
            ratings_count=50,
            source_url="https://example.com/bands/1/",
        )
    ]


def _fetch_tracklist(album_id: int, client: StubClient) -> List[TrackEntry]:
    assert album_id == 1000
    return [
        TrackEntry(position=1, title="Intro", duration_seconds=60),
        TrackEntry(position=2, title="Outro", duration_seconds=120),
    ]


def _fetch_soundoffs(album_id: int, client: StubClient, limit: int | None) -> List[SoundoffEntry]:
    assert album_id == 1000
    return [
        SoundoffEntry(
            album_id=album_id,
            user_id="critic",
            user_display="Critic",
            user_role="EMERITUS",
            rating=4.5,
            rating_label="superb",
            rating_date="2024-07-01",
            soundoff_text=None,
            source_url="https://example.com/soundoff.php?albumid=1000",
        )
    ]


def test_expand_discographies_inserts_releases_and_interactions(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("crawler.discography.fetch_artist_discography", _fetch_discography)
    monkeypatch.setattr("crawler.discography.fetch_tracklist", _fetch_tracklist)
    monkeypatch.setattr("crawler.discography.fetch_soundoffs", _fetch_soundoffs)

    expand_discographies(
        DiscographyConfig(
            database_path=db_path,
            schema_path=SCHEMA_PATH,
            batch_size=1,
            fetch_tracklists=True,
            fetch_soundoffs=True,
        ),
        client=StubClient(),
    )

    with sqlite3.connect(db_path) as connection:
        release_row = connection.execute(
            "SELECT title, release_year, avg_rating, ratings_count FROM releases WHERE id_release = ?",
            (1000,),
        ).fetchone()
        assert release_row == ("New Release", 2024, 4.2, 50)

        track_rows = connection.execute(
            "SELECT track_title, duration_seconds FROM release_tracks WHERE id_release = ? ORDER BY track_position",
            (1000,),
        ).fetchall()
        assert track_rows == [("Intro", 60), ("Outro", 120)]

        interaction_rows = connection.execute(
            "SELECT id_user, rating FROM interactions WHERE id_release = ?",
            (1000,),
        ).fetchall()
        assert interaction_rows == [("critic", 4.5)]

        artist_row = connection.execute(
            "SELECT status FROM crawl_artists WHERE id_artist = ?",
            (1,),
        ).fetchone()
        assert artist_row == ("done",)
