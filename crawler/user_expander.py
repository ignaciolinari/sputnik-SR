from __future__ import annotations

import argparse
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from scraper import SputnikClient
from scraper.user_ratings import UserRatingEntry, fetch_user_ratings
from scraper.users import UserProfile, fetch_user_profile

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ExpansionConfig:
    database_path: Path
    schema_path: Path
    batch_size: int = 10
    timeout: float = 20.0
    max_retries: int = 3
    min_interval: float = 1.0
    fetch_profiles: bool = True
    max_rating_pages: Optional[int] = None


def expand_users(
    config: ExpansionConfig,
    *,
    profile_fetcher: Callable[[str, SputnikClient], Optional[UserProfile]] = fetch_user_profile,
    ratings_fetcher: Callable[
        [str, SputnikClient, Optional[int]], List[UserRatingEntry]
    ] = fetch_user_ratings,
    client: Optional[SputnikClient] = None,
) -> None:
    database_path = config.database_path
    database_path.parent.mkdir(parents=True, exist_ok=True)

    LOGGER.info("Expanding users from SQLite database at %s", database_path)
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
                pending_users = _claim_users(connection, config.batch_size)
                if not pending_users:
                    LOGGER.info("No pending users found. Expansion complete.")
                    break

                for user_id in pending_users:
                    try:
                        _process_user(
                            connection,
                            user_id=user_id,
                            client=active_client,
                            config=config,
                            profile_fetcher=profile_fetcher,
                            ratings_fetcher=ratings_fetcher,
                        )
                        _mark_user_done(connection, user_id)
                    except Exception as exc:  # pragma: no cover - defensive logging
                        LOGGER.exception("Failed to expand user %s", user_id)
                        _mark_user_error(connection, user_id, str(exc))

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


def _claim_users(connection: sqlite3.Connection, limit: int) -> List[str]:
    cursor = connection.execute(
        """
        SELECT id_user
        FROM crawl_users
        WHERE status = 'pending'
        ORDER BY priority DESC, updated_at ASC
        LIMIT ?
        """,
        (limit,),
    )
    users = [row[0] for row in cursor.fetchall()]
    if not users:
        return []

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection.executemany(
        """
        UPDATE crawl_users
        SET status = 'processing', attempts = attempts + 1, updated_at = ?, last_error = NULL
        WHERE id_user = ?
        """,
        [(now, user_id) for user_id in users],
    )
    return users


def _process_user(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    client: SputnikClient,
    config: ExpansionConfig,
    profile_fetcher: Callable[[str, SputnikClient], Optional[UserProfile]],
    ratings_fetcher: Callable[[str, SputnikClient, Optional[int]], List[UserRatingEntry]],
) -> None:
    LOGGER.info("Processing user %s", user_id)
    if config.fetch_profiles:
        profile = profile_fetcher(user_id, client)
        if profile:
            _upsert_user_profile(connection, profile)

    rating_entries = ratings_fetcher(user_id, client, config.max_rating_pages)
    if not rating_entries:
        LOGGER.debug("No ratings found for user %s", user_id)
        return

    for entry in rating_entries:
        artist_id = _ensure_artist(connection, entry.artist_name)
        _ensure_release_stub(connection, entry, artist_id)
        _upsert_rating_interaction(connection, entry)


def _mark_user_done(connection: sqlite3.Connection, user_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection.execute(
        """
        UPDATE crawl_users
        SET status = 'done', last_crawled = ?, updated_at = ?, last_error = NULL
        WHERE id_user = ?
        """,
        (now, now, user_id),
    )


def _mark_user_error(connection: sqlite3.Connection, user_id: str, error: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection.execute(
        """
        UPDATE crawl_users
        SET status = 'error', last_error = ?, updated_at = ?
        WHERE id_user = ?
        """,
        (error[:500], now, user_id),
    )


def _upsert_user_profile(connection: sqlite3.Connection, profile: UserProfile) -> None:
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
            profile.role,
            profile.join_date,
            profile.last_active,
            profile.objectivity_score,
            profile.soundoffs,
            profile.ratings_count,
        ),
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


def _ensure_release_stub(
    connection: sqlite3.Connection,
    entry: UserRatingEntry,
    artist_id: int,
) -> None:
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
        ) VALUES (?, ?, ?, 'LP', NULL, NULL, NULL, NULL, NULL, NULL, NULL)
        ON CONFLICT(id_release) DO UPDATE SET
            title=COALESCE(releases.title, excluded.title),
            artist_id=COALESCE(releases.artist_id, excluded.artist_id),
            release_type=COALESCE(releases.release_type, excluded.release_type),
            release_year=COALESCE(releases.release_year, excluded.release_year),
            label=COALESCE(releases.label, excluded.label),
            art_url=COALESCE(releases.art_url, excluded.art_url),
            avg_rating=COALESCE(releases.avg_rating, excluded.avg_rating),
            ratings_count=COALESCE(releases.ratings_count, excluded.ratings_count),
            staff_avg=COALESCE(releases.staff_avg, excluded.staff_avg),
            review_count=COALESCE(releases.review_count, excluded.review_count)
        """,
        (
            entry.release_id,
            entry.release_title,
            artist_id,
        ),
    )


def _upsert_rating_interaction(connection: sqlite3.Connection, entry: UserRatingEntry) -> None:
    connection.execute(
        """
        INSERT INTO interactions (
            id_release,
            id_user,
            rating,
            rating_date,
            soundoff_text,
            source_url
        ) VALUES (?, ?, ?, ?, NULL, ?)
        ON CONFLICT(id_release, id_user) DO UPDATE SET
            rating=COALESCE(excluded.rating, interactions.rating),
            rating_date=COALESCE(excluded.rating_date, interactions.rating_date),
            source_url=COALESCE(excluded.source_url, interactions.source_url)
        """,
        (
            entry.release_id,
            entry.user_id,
            entry.rating,
            entry.rating_date,
            entry.url,
        ),
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Expand queued Sputnik users with their ratings.")
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
        help="Maximum number of users to process per batch (default: 10)",
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
        "--max-rating-pages",
        type=int,
        help="Optional cap on rating pages per user",
    )
    parser.add_argument(
        "--skip-profiles",
        action="store_true",
        help="Skip fetching user profiles during expansion",
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

    config = ExpansionConfig(
        database_path=args.database,
        schema_path=args.schema,
        batch_size=args.batch_size,
        timeout=args.timeout,
        max_retries=args.max_retries,
        min_interval=args.min_interval,
        fetch_profiles=not args.skip_profiles,
        max_rating_pages=args.max_rating_pages,
    )
    expand_users(config)


if __name__ == "__main__":
    main()
