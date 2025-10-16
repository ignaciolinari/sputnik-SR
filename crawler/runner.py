from __future__ import annotations

import argparse
import logging
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set

import requests

from scraper import (
    ChartEntry,
    SoundoffEntry,
    SputnikClient,
    TrackEntry,
    UserProfile,
    fetch_best_albums,
    fetch_soundoffs,
    fetch_tracklist,
    fetch_user_profile,
)

LOGGER = logging.getLogger(__name__)

_UNSET = object()


@dataclass(slots=True)
class CrawlConfig:
    start_year: int
    end_year: int
    database_path: Path
    schema_path: Path
    timeout: float
    max_retries: int
    min_interval: float
    dry_run: bool
    fetch_tracklists: bool
    fetch_soundoffs: bool
    max_soundoffs: Optional[int] = None
    fetch_user_profiles: bool = True
    queue_users: bool = True
    user_queue_priority: int = 0


def crawl_years(config: CrawlConfig) -> None:
    years = range(config.start_year, config.end_year + 1)
    database_path = config.database_path
    database_path.parent.mkdir(parents=True, exist_ok=True)

    LOGGER.info("Connecting to SQLite database at %s", database_path)
    connection = sqlite3.connect(database_path)
    try:
        _configure_connection(connection)
        _ensure_schema(connection, config.schema_path)
        _ensure_progress_table(connection)
        _ensure_work_queues(connection)

        if config.dry_run:
            LOGGER.warning("Running in dry-run mode. No database mutations will be committed.")

        with SputnikClient(
            timeout=config.timeout,
            max_retries=config.max_retries,
            min_interval=config.min_interval,
        ) as client:
            processed_releases: Set[int] = set()
            processed_users: Set[str] = set()

            for year in years:
                LOGGER.info("Fetching chart for %s", year)
                _update_progress(connection, year, status="IN_PROGRESS", note="fetching chart")
                entries = _safe_fetch_year(year, client)
                if not entries:
                    LOGGER.warning("No entries fetched for %s. Skipping.", year)
                    _update_progress(connection, year, status="EMPTY", note="no entries fetched")
                    continue

                if config.dry_run:
                    LOGGER.info("Dry-run: fetched %s entries for %s", len(entries), year)
                    _update_progress(
                        connection,
                        year,
                        status="DRY_RUN",
                        note=f"fetched {len(entries)} entries (dry-run)",
                    )
                    continue

                list_id = _upsert_chart_list(connection, year, entries)
                for entry in entries:
                    LOGGER.info(
                        "Processing album %s - %s (rank %s, id %s)",
                        entry.artist_name,
                        entry.album_title,
                        entry.rank,
                        entry.album_id,
                    )
                    _update_progress(
                        connection,
                        year,
                        album_id=entry.album_id,
                        album_title=entry.album_title,
                        note="persisting chart entry",
                    )
                    artist_id = _ensure_artist(connection, entry.artist_name)
                    _upsert_release(connection, entry, artist_id)
                    _upsert_list_release(connection, list_id, entry)

                    if entry.album_id in processed_releases:
                        continue

                    _enrich_release(
                        connection,
                        entry,
                        client,
                        year=year,
                        config=config,
                        processed_users=processed_users,
                    )
                    processed_releases.add(entry.album_id)

                connection.commit()
                LOGGER.info("Persisted %s entries for %s", len(entries), year)
                _update_progress(
                    connection,
                    year,
                    status="DONE",
                    note=f"year completed with {len(entries)} entries",
                    album_id=None,
                    album_title=None,
                )
    finally:
        connection.close()


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON;")
    connection.execute("PRAGMA journal_mode = WAL;")
    connection.execute("PRAGMA synchronous = NORMAL;")


def _ensure_schema(connection: sqlite3.Connection, schema_path: Path) -> None:
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    LOGGER.debug("Applying schema from %s", schema_path)
    schema_sql = schema_path.read_text(encoding="utf-8")
    connection.executescript(schema_sql)


def _ensure_work_queues(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO crawl_users (
            id_user, status, priority, attempts, last_error,
            last_crawled, updated_at
        )
        SELECT id_user, 'pending', 0, 0, NULL, NULL, datetime('now')
        FROM users
        WHERE id_user NOT IN (SELECT id_user FROM crawl_users)
        """
    )
    connection.execute(
        """
        INSERT INTO crawl_releases (
            id_release, status, attempts, last_error, last_crawled, updated_at
        )
        SELECT id_release, 'seeded', 0, NULL, NULL, datetime('now')
        FROM releases
        WHERE id_release NOT IN (SELECT id_release FROM crawl_releases)
        """
    )
    connection.execute(
        """
        INSERT INTO crawl_artists (
            id_artist, status, attempts, last_error, last_crawled, updated_at
        )
        SELECT id_artist, 'pending', 0, NULL, NULL, datetime('now')
        FROM artists
        WHERE id_artist NOT IN (SELECT id_artist FROM crawl_artists)
        """
    )


def _parse_log_level(value: str) -> int:
    level_name = value.strip().upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        raise ValueError(f"Invalid log level: {value}")
    return level


def _ensure_progress_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS crawl_state (
            year INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_album_id INTEGER,
            last_album_title TEXT,
            last_note TEXT
        )
        """
    )


def _update_progress(
    connection: sqlite3.Connection,
    year: int,
    *,
    status: object = _UNSET,
    album_id: object = _UNSET,
    album_title: object = _UNSET,
    note: object = _UNSET,
) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection.execute(
        "INSERT INTO crawl_state (year, status, updated_at) VALUES (?, 'PENDING', ?)"
        " ON CONFLICT(year) DO NOTHING",
        (year, now),
    )

    assignments = ["updated_at = ?"]
    params: List[object] = [now]

    if status is not _UNSET:
        assignments.append("status = ?")
        params.append(status)
    if album_id is not _UNSET:
        assignments.append("last_album_id = ?")
        params.append(album_id)
    if album_title is not _UNSET:
        assignments.append("last_album_title = ?")
        params.append(album_title)
    if note is not _UNSET:
        assignments.append("last_note = ?")
        params.append(note)

    params.append(year)
    connection.execute(
        f"UPDATE crawl_state SET {', '.join(assignments)} WHERE year = ?",
        params,
    )
    connection.commit()


def _safe_fetch_year(year: int, client: SputnikClient) -> Sequence[ChartEntry]:
    try:
        return fetch_best_albums(year, client=client)
    except (requests.RequestException, sqlite3.Error, ValueError, AttributeError) as e:
        LOGGER.exception("Failed to fetch chart for %s: %s", year, type(e).__name__)
        return []


def _enrich_release(
    connection: sqlite3.Connection,
    entry: ChartEntry,
    client: SputnikClient,
    *,
    year: int,
    config: CrawlConfig,
    processed_users: Set[str],
) -> None:
    if config.fetch_tracklists:
        tracks = _safe_fetch_tracklist(entry.album_id, client)
        if tracks:
            _replace_tracklist(connection, entry.album_id, tracks)
            LOGGER.info("Stored %s tracks for album %s", len(tracks), entry.album_id)
            _update_progress(connection, year, note=f"tracklist stored ({len(tracks)} tracks)")
        else:
            _update_progress(connection, year, note="tracklist unavailable")
    else:
        _update_progress(connection, year, note="tracklist skipped (disabled)")

    if config.fetch_soundoffs:
        soundoffs = _safe_fetch_soundoffs(entry.album_id, client, limit=config.max_soundoffs)
        if soundoffs:
            _persist_soundoffs(
                connection,
                soundoffs,
                client,
                processed_users,
                config=config,
            )
            LOGGER.info("Stored %s soundoffs for album %s", len(soundoffs), entry.album_id)
            _update_progress(connection, year, note=f"soundoffs stored ({len(soundoffs)} ratings)")
        else:
            _update_progress(connection, year, note="soundoffs unavailable")
    else:
        _update_progress(connection, year, note="soundoffs skipped (disabled)")


def _safe_fetch_tracklist(album_id: int, client: SputnikClient) -> List[TrackEntry]:
    try:
        return fetch_tracklist(album_id, client=client)
    except (requests.RequestException, sqlite3.Error, ValueError, AttributeError) as e:
        LOGGER.exception("Failed to fetch tracklist for album %s: %s", album_id, type(e).__name__)
        return []


def _safe_fetch_soundoffs(
    album_id: int, client: SputnikClient, *, limit: Optional[int]
) -> List[SoundoffEntry]:
    try:
        return fetch_soundoffs(album_id, client=client, limit=limit)
    except (requests.RequestException, sqlite3.Error, ValueError, AttributeError) as e:
        LOGGER.exception("Failed to fetch soundoffs for album %s: %s", album_id, type(e).__name__)
        return []


def _safe_fetch_user_profile(user_id: str, client: SputnikClient) -> Optional[UserProfile]:
    try:
        return fetch_user_profile(user_id, client=client)
    except (requests.RequestException, sqlite3.Error, ValueError, AttributeError) as e:
        LOGGER.exception("Failed to fetch profile for user %s: %s", user_id, type(e).__name__)
        return None


def _replace_tracklist(
    connection: sqlite3.Connection, album_id: int, tracks: Sequence[TrackEntry]
) -> None:
    LOGGER.debug("Persisting %s tracks for album %s", len(tracks), album_id)
    connection.execute("DELETE FROM release_tracks WHERE id_release = ?", (album_id,))
    connection.executemany(
        """
        INSERT INTO release_tracks (id_release, track_position, track_title, duration_seconds)
        VALUES (?, ?, ?, ?)
        """,
        [(album_id, track.position, track.title, track.duration_seconds) for track in tracks],
    )


def _persist_soundoffs(
    connection: sqlite3.Connection,
    soundoffs: Sequence[SoundoffEntry],
    client: SputnikClient,
    processed_users: Set[str],
    *,
    config: CrawlConfig,
) -> None:
    LOGGER.debug("Persisting %s soundoffs for album %s", len(soundoffs), soundoffs[0].album_id)
    for soundoff in soundoffs:
        LOGGER.debug(
            "Processing soundoff: user=%s rating=%s date=%s role=%s",
            soundoff.user_id,
            soundoff.rating,
            soundoff.rating_date,
            soundoff.user_role,
        )

        first_time_seen = soundoff.user_id not in processed_users
        profile: Optional[UserProfile] = None

        if first_time_seen and config.fetch_user_profiles:
            LOGGER.debug("Fetching user profile for %s", soundoff.user_id)
            profile = _safe_fetch_user_profile(soundoff.user_id, client)
            if profile:
                if soundoff.user_role and not profile.role:
                    profile = replace(profile, role=soundoff.user_role)
                _upsert_user(connection, profile, role_hint=soundoff.user_role)
                if config.queue_users:
                    _enqueue_user(
                        connection,
                        soundoff.user_id,
                        priority=config.user_queue_priority,
                        status="done",
                    )
            else:
                _ensure_user_placeholder(connection, soundoff.user_id, soundoff.user_role)
                if config.queue_users:
                    _enqueue_user(
                        connection,
                        soundoff.user_id,
                        priority=config.user_queue_priority,
                    )
        elif first_time_seen:
            _ensure_user_placeholder(connection, soundoff.user_id, soundoff.user_role)
            if config.queue_users:
                _enqueue_user(connection, soundoff.user_id, priority=config.user_queue_priority)

        if first_time_seen:
            processed_users.add(soundoff.user_id)

        if soundoff.user_role:
            _ensure_user_role_hint(connection, soundoff.user_id, soundoff.user_role)

        _upsert_interaction(connection, soundoff)


def _ensure_user_role_hint(
    connection: sqlite3.Connection, user_id: str, role: Optional[str]
) -> None:
    if not role:
        return
    connection.execute(
        """
        UPDATE users
        SET role = CASE
            WHEN role IS NULL OR role = '' THEN ?
            ELSE role
        END
        WHERE id_user = ?
        """,
        (role, user_id),
    )


def _ensure_user_placeholder(
    connection: sqlite3.Connection, user_id: str, role_hint: Optional[str]
) -> None:
    connection.execute(
        """
        INSERT INTO users (id_user, role)
        VALUES (?, ?)
        ON CONFLICT(id_user) DO UPDATE SET
            role = COALESCE(users.role, excluded.role)
        """,
        (user_id, role_hint),
    )


def _enqueue_user(
    connection: sqlite3.Connection,
    user_id: str,
    *,
    priority: int,
    status: str = "pending",
) -> None:
    connection.execute(
        """
        INSERT INTO crawl_users (
            id_user, status, priority, attempts, last_error, last_crawled, updated_at
        )
        VALUES (?, ?, ?, 0, NULL, NULL, datetime('now'))
        ON CONFLICT(id_user) DO UPDATE SET
            status = CASE
                WHEN crawl_users.status = 'done' THEN crawl_users.status
                ELSE excluded.status
            END,
            priority = MAX(crawl_users.priority, excluded.priority),
            updated_at = datetime('now')
        """,
        (user_id, status, priority),
    )


def _ensure_artist(connection: sqlite3.Connection, artist_name: str) -> int:
    cursor = connection.execute(
        "SELECT id_artist FROM artists WHERE name = ?",
        (artist_name,),
    )
    row = cursor.fetchone()
    if row:
        return int(row[0])

    cursor = connection.execute(
        "INSERT INTO artists (name) VALUES (?)",
        (artist_name,),
    )
    new_id = cursor.lastrowid
    if new_id is None:  # pragma: no cover - SQLite should always set lastrowid
        raise RuntimeError("Failed to retrieve lastrowid for newly inserted artist")
    return int(new_id)


def _upsert_release(connection: sqlite3.Connection, entry: ChartEntry, artist_id: int) -> None:
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
            title=excluded.title,
            artist_id=excluded.artist_id,
            release_type=excluded.release_type,
            release_year=excluded.release_year,
            art_url=excluded.art_url,
            avg_rating=excluded.avg_rating,
            ratings_count=excluded.ratings_count
        """,
        (
            entry.album_id,
            entry.album_title,
            artist_id,
            "LP",
            entry.year,
            entry.art_url,
            entry.rating,
            entry.votes,
        ),
    )


def _upsert_chart_list(
    connection: sqlite3.Connection, year: int, entries: Sequence[ChartEntry]
) -> int:
    title = f"Sputnikmusic Best Albums {year}"
    list_url = entries[0].source_url if entries else None

    cursor = connection.execute(
        "SELECT id_list FROM lists WHERE external_id = ?",
        (year,),
    )
    row = cursor.fetchone()
    if row:
        list_id = int(row[0])
        connection.execute(
            "UPDATE lists SET title = ?, list_url = ?, description = ? WHERE id_list = ?",
            (
                title,
                list_url,
                "Best albums chart captured via crawler.",
                list_id,
            ),
        )
        return list_id

    cursor = connection.execute(
        """
        INSERT INTO lists (
            external_id,
            owner_user_id,
            title,
            description,
            list_url,
            created_at
        ) VALUES (?, NULL, ?, ?, ?, ?)
        """,
        (
            year,
            title,
            "Best albums chart captured via crawler.",
            list_url,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    new_id = cursor.lastrowid
    if new_id is None:  # pragma: no cover - SQLite should always set lastrowid
        raise RuntimeError("Failed to retrieve lastrowid for newly inserted list")
    return int(new_id)


def _upsert_list_release(connection: sqlite3.Connection, list_id: int, entry: ChartEntry) -> None:
    connection.execute(
        """
        INSERT INTO list_releases (id_list, id_release, rank)
        VALUES (?, ?, ?)
        ON CONFLICT(id_list, id_release) DO UPDATE SET
            rank=excluded.rank
        """,
        (
            list_id,
            entry.album_id,
            entry.rank,
        ),
    )


def _upsert_user(
    connection: sqlite3.Connection,
    profile: UserProfile,
    *,
    role_hint: Optional[str] = None,
) -> None:
    role_value = role_hint or profile.role
    connection.execute(
        """
        INSERT INTO users (
            id_user,
            role,
            join_date,
            last_active,
            objectivity_score,
            soundoffs,
            ratings_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id_user) DO UPDATE SET
            join_date=COALESCE(excluded.join_date, users.join_date),
            last_active=COALESCE(excluded.last_active, users.last_active),
            objectivity_score=COALESCE(excluded.objectivity_score, users.objectivity_score),
            soundoffs=COALESCE(excluded.soundoffs, users.soundoffs),
            ratings_count=COALESCE(excluded.ratings_count, users.ratings_count),
            role=COALESCE(excluded.role, users.role)
        """,
        (
            profile.user_id,
            role_value,
            profile.join_date,
            profile.last_active,
            profile.objectivity_score,
            profile.soundoffs,
            profile.ratings_count,
        ),
    )


def _upsert_interaction(connection: sqlite3.Connection, soundoff: SoundoffEntry) -> None:
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


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crawl Sputnik charts into SQLite")
    parser.add_argument(
        "--start-year", type=int, required=True, help="First year to fetch (inclusive)"
    )
    parser.add_argument(
        "--end-year", type=int, required=True, help="Last year to fetch (inclusive)"
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
        "--dry-run",
        action="store_true",
        help="Fetch charts but skip database writes",
    )
    parser.add_argument(
        "--skip-tracklists",
        action="store_true",
        help="Disable tracklist fetching during the crawl",
    )
    parser.add_argument(
        "--skip-soundoffs",
        action="store_true",
        help="Disable user interaction fetching during the crawl",
    )
    parser.add_argument(
        "--skip-user-profiles",
        action="store_true",
        help="Do not fetch user profiles while processing soundoffs (only enqueue)",
    )
    parser.add_argument(
        "--no-queue-users",
        action="store_true",
        help="Avoid enqueuing users discovered in soundoffs",
    )
    parser.add_argument(
        "--user-queue-priority",
        type=int,
        default=0,
        help="Priority assigned to users enqueued from soundoffs (default: 0)",
    )
    parser.add_argument(
        "--max-soundoffs",
        type=int,
        help="Optional cap on soundoffs processed per album (useful for smoke tests)",
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

    if args.end_year < args.start_year:
        parser.error("--end-year must be greater than or equal to --start-year")

    logging.basicConfig(
        level=_parse_log_level(args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = CrawlConfig(
        start_year=args.start_year,
        end_year=args.end_year,
        database_path=args.database,
        schema_path=args.schema,
        timeout=args.timeout,
        max_retries=args.max_retries,
        min_interval=args.min_interval,
        dry_run=args.dry_run,
        fetch_tracklists=not args.skip_tracklists,
        fetch_soundoffs=not args.skip_soundoffs,
        fetch_user_profiles=not args.skip_user_profiles,
        queue_users=not args.no_queue_users,
        user_queue_priority=args.user_queue_priority,
        max_soundoffs=args.max_soundoffs,
    )
    crawl_years(config)


if __name__ == "__main__":
    main()
