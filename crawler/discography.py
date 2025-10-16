from __future__ import annotations

import argparse
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from scraper import (
    ArtistReleaseEntry,
    SoundoffEntry,
    SputnikClient,
    TrackEntry,
    fetch_artist_discography,
    fetch_soundoffs,
    fetch_tracklist,
)

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class DiscographyConfig:
    database_path: Path
    schema_path: Path
    batch_size: int = 10
    timeout: float = 20.0
    max_retries: int = 3
    min_interval: float = 1.0
    fetch_tracklists: bool = True
    fetch_soundoffs: bool = True
    max_soundoffs: Optional[int] = None


def expand_discographies(
    config: DiscographyConfig,
    *,
    client: Optional[SputnikClient] = None,
) -> None:
    database_path = config.database_path
    database_path.parent.mkdir(parents=True, exist_ok=True)

    LOGGER.info("Expanding discographies using SQLite database at %s", database_path)
    connection = sqlite3.connect(database_path)
    try:
        _configure_connection(connection)
        _ensure_schema(connection, config.schema_path)

        owns_client = client is None
        active_client = client or SputnikClient(
            timeout=config.timeout,
            max_retries=config.max_retries,
            min_interval=config.min_interval,
        )
        try:
            while True:
                pending_artists = _claim_artists(connection, config.batch_size)
                if not pending_artists:
                    LOGGER.info("No pending artists found. Discography expansion complete.")
                    break

                for artist_id in pending_artists:
                    try:
                        _process_artist(connection, artist_id, active_client, config)
                        _mark_artist_done(connection, artist_id)
                    except Exception as exc:  # pragma: no cover - defensive logging
                        LOGGER.exception("Failed to expand artist %s", artist_id)
                        _mark_artist_error(connection, artist_id, str(exc))

                connection.commit()
        finally:
            if owns_client:
                active_client.close()
    finally:
        connection.close()


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON;")
    try:
        connection.execute("PRAGMA journal_mode = WAL;")
        connection.execute("PRAGMA synchronous = NORMAL;")
    except sqlite3.OperationalError:
        LOGGER.debug("PRAGMA configuration skipped (connection in transaction)")


def _ensure_schema(connection: sqlite3.Connection, schema_path: Path) -> None:
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    schema_sql = schema_path.read_text(encoding="utf-8")
    connection.executescript(schema_sql)


def _claim_artists(connection: sqlite3.Connection, limit: int) -> List[int]:
    cursor = connection.execute(
        """
        SELECT id_artist
        FROM crawl_artists
        WHERE status IN ('pending', 'seeded')
        ORDER BY updated_at ASC
        LIMIT ?
        """,
        (limit,),
    )
    artists = [int(row[0]) for row in cursor.fetchall()]
    if not artists:
        return []

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection.executemany(
        """
        UPDATE crawl_artists
        SET status = 'processing', attempts = attempts + 1, updated_at = ?, last_error = NULL
        WHERE id_artist = ?
        """,
        [(now, artist_id) for artist_id in artists],
    )
    return artists


def _process_artist(
    connection: sqlite3.Connection,
    artist_id: int,
    client: SputnikClient,
    config: DiscographyConfig,
) -> None:
    LOGGER.info("Processing artist %s", artist_id)
    releases = fetch_artist_discography(artist_id, client=client)
    if not releases:
        LOGGER.debug("No releases found for artist %s", artist_id)
        return

    for entry in releases:
        _upsert_release(connection, entry)
        if config.fetch_tracklists:
            tracks = _safe_fetch_tracklist(entry.release_id, client)
            if tracks:
                _replace_tracklist(connection, entry.release_id, tracks)
        if config.fetch_soundoffs:
            soundoffs = _safe_fetch_soundoffs(entry.release_id, client, config.max_soundoffs)
            if soundoffs:
                _persist_soundoffs(connection, soundoffs)


def _mark_artist_done(connection: sqlite3.Connection, artist_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection.execute(
        """
        UPDATE crawl_artists
        SET status = 'done', last_crawled = ?, updated_at = ?, last_error = NULL
        WHERE id_artist = ?
        """,
        (now, now, artist_id),
    )


def _mark_artist_error(connection: sqlite3.Connection, artist_id: int, error: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection.execute(
        """
        UPDATE crawl_artists
        SET status = 'error', last_error = ?, updated_at = ?
        WHERE id_artist = ?
        """,
        (error[:500], now, artist_id),
    )


def _upsert_release(connection: sqlite3.Connection, entry: ArtistReleaseEntry) -> None:
    connection.execute(
        """
        INSERT INTO releases (
            id_release,
            title,
            artist_id,
            release_type,
            release_year,
            label,
            art_url,
            avg_rating,
            ratings_count,
            staff_avg,
            review_count
        ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL)
        ON CONFLICT(id_release) DO UPDATE SET
            title=COALESCE(excluded.title, releases.title),
            artist_id=COALESCE(excluded.artist_id, releases.artist_id),
            release_type=COALESCE(excluded.release_type, releases.release_type),
            release_year=COALESCE(excluded.release_year, releases.release_year),
            art_url=COALESCE(excluded.art_url, releases.art_url),
            avg_rating=COALESCE(excluded.avg_rating, releases.avg_rating),
            ratings_count=COALESCE(excluded.ratings_count, releases.ratings_count)
        """,
        (
            entry.release_id,
            entry.title,
            entry.artist_id,
            entry.release_type or "LP",
            entry.release_year,
            entry.art_url,
            entry.avg_rating,
            entry.ratings_count,
        ),
    )


def _safe_fetch_tracklist(album_id: int, client: SputnikClient) -> List[TrackEntry]:
    try:
        return fetch_tracklist(album_id, client=client)
    except Exception:  # pragma: no cover - defensive logging
        LOGGER.exception("Failed to fetch tracklist for album %s", album_id)
        return []


def _replace_tracklist(
    connection: sqlite3.Connection,
    album_id: int,
    tracks: List[TrackEntry],
) -> None:
    connection.execute("DELETE FROM release_tracks WHERE id_release = ?", (album_id,))
    connection.executemany(
        """
        INSERT INTO release_tracks (id_release, track_position, track_title, duration_seconds)
        VALUES (?, ?, ?, ?)
        """,
        [(album_id, track.position, track.title, track.duration_seconds) for track in tracks],
    )


def _safe_fetch_soundoffs(
    album_id: int,
    client: SputnikClient,
    max_soundoffs: Optional[int],
) -> List[SoundoffEntry]:
    try:
        return fetch_soundoffs(album_id, client=client, limit=max_soundoffs)
    except Exception:  # pragma: no cover - defensive logging
        LOGGER.exception("Failed to fetch soundoffs for album %s", album_id)
        return []


def _persist_soundoffs(connection: sqlite3.Connection, soundoffs: List[SoundoffEntry]) -> None:
    for soundoff in soundoffs:
        _ensure_user_stub(connection, soundoff.user_id, soundoff.user_role)
        connection.execute(
            """
            INSERT INTO interactions (
                id_release,
                id_user,
                rating,
                rating_date,
                soundoff_text,
                source_url
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id_release, id_user) DO UPDATE SET
                rating=excluded.rating,
                rating_date=COALESCE(excluded.rating_date, interactions.rating_date),
                soundoff_text=COALESCE(excluded.soundoff_text, interactions.soundoff_text),
                source_url=COALESCE(excluded.source_url, interactions.source_url)
            """,
            (
                soundoff.album_id,
                soundoff.user_id,
                soundoff.rating,
                soundoff.rating_date,
                soundoff.soundoff_text,
                soundoff.source_url,
            ),
        )


def _ensure_user_stub(connection: sqlite3.Connection, user_id: str, role: Optional[str]) -> None:
    connection.execute(
        """
        INSERT INTO users (id_user, role)
        VALUES (?, ?)
        ON CONFLICT(id_user) DO UPDATE SET
            role = COALESCE(users.role, excluded.role)
        """,
        (user_id, role),
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Expand artist discographies and enqueue associated data."
    )
    parser.add_argument(
        "--db",
        dest="database",
        type=Path,
        default=Path("data/sputnik.db"),
        help="Path to the SQLite database (default: data/sputnik.db)",
    )
    parser.add_argument(
        "--schema",
        dest="schema",
        type=Path,
        default=Path("data/schema.sql"),
        help="Path to the SQL schema file (default: data/schema.sql)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Maximum number of artists to process per batch (default: 10)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout in seconds (default: 20)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum HTTP retries per request (default: 3)",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=1.0,
        help="Minimum delay between requests in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--skip-tracklists",
        action="store_true",
        help="Skip tracklist fetching for each release",
    )
    parser.add_argument(
        "--skip-soundoffs",
        action="store_true",
        help="Skip soundoff fetching for each release",
    )
    parser.add_argument(
        "--max-soundoffs",
        type=int,
        help="Optional cap on soundoffs processed per release",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (DEBUG, INFO, WARNING, ERROR). Default: INFO",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> None:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = DiscographyConfig(
        database_path=args.database,
        schema_path=args.schema,
        batch_size=args.batch_size,
        timeout=args.timeout,
        max_retries=args.max_retries,
        min_interval=args.min_interval,
        fetch_tracklists=not args.skip_tracklists,
        fetch_soundoffs=not args.skip_soundoffs,
        max_soundoffs=args.max_soundoffs,
    )
    expand_discographies(config)


if __name__ == "__main__":
    main()
