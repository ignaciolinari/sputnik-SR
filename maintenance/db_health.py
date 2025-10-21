#!/usr/bin/env python3
"""Herramienta para auditar la salud de la base de datos de Sputnik SR."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Dict
from typing import Iterable
from typing import List
from typing import Sequence


Severity = str


@dataclass
class HealthIssue:
    category: str
    entity: str
    severity: Severity
    description: str
    count: int
    sample: List[Dict[str, Any]]
    suggested_fix: str | None = None
    fix: Callable[[bool], int] | None = None

    def has_fix(self) -> bool:
        return self.fix is not None

    def apply_fix(self, dry_run: bool = True) -> int:
        if not self.fix:
            raise RuntimeError(f"La categoria {self.category} no expone accion automatica")
        return self.fix(dry_run)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "entity": self.entity,
            "severity": self.severity,
            "description": self.description,
            "count": self.count,
            "sample": self.sample,
            "suggested_fix": self.suggested_fix,
            "has_fix": self.has_fix(),
        }


class DBHealthChecker:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.connection.row_factory = sqlite3.Row

    def run_checks(self, sample_limit: int = 5) -> List[HealthIssue]:
        issues: List[HealthIssue] = []
        issues.extend(self._check_user_queue_errors(sample_limit))
        issues.extend(self._check_user_incomplete_profiles(sample_limit))
        issues.extend(self._check_user_rating_mismatches(sample_limit))
        issues.extend(self._check_release_queue_errors(sample_limit))
        issues.extend(self._check_release_metadata_gaps(sample_limit))
        issues.extend(self._check_release_rating_mismatches(sample_limit))
        issues.extend(self._check_artist_genre_gaps(sample_limit))
        return issues

    def _check_user_queue_errors(self, sample_limit: int) -> List[HealthIssue]:
        cursor = self.connection.execute(
            """
            SELECT id_user, attempts, last_error, last_crawled, updated_at
            FROM crawl_users
            WHERE status = 'error'
            ORDER BY updated_at DESC
            """
        )
        rows = cursor.fetchall()
        if not rows:
            return []

        grouped: Dict[str, List[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            group_key = self._classify_error(row["last_error"])
            grouped[group_key].append(row)

        issues: List[HealthIssue] = []
        for key, group in grouped.items():
            ids = [row["id_user"] for row in group]
            sample = self._build_sample(
                group[:sample_limit],
                ("id_user", "attempts", "last_error", "updated_at"),
            )
            if key == "not_found":
                severity: Severity = "medium"
                description = f"{len(group)} usuarios devuelven 404 en crawl_users"
                suggested_fix = "Eliminar del queue para no reintentar"
                fix_fn = (
                    (lambda dry_run, ids=ids: self._delete_queue_users(ids, dry_run))
                    if ids
                    else None
                )
            elif key in {"rate_limited", "timeout", "connection", "incomplete_read"}:
                severity = "high"
                description = f"{len(group)} usuarios con error temporal ({key})"
                suggested_fix = "Resetear a pending para reintentar"
                fix_fn = (
                    (lambda dry_run, ids=ids: self._reset_queue_users(ids, dry_run))
                    if ids
                    else None
                )
            else:
                severity = "medium"
                description = f"{len(group)} usuarios con error desconocido"
                suggested_fix = "Revisar mensaje y posiblemente reintentar"
                fix_fn = (
                    (lambda dry_run, ids=ids: self._reset_queue_users(ids, dry_run))
                    if ids
                    else None
                )

            issues.append(
                HealthIssue(
                    category=f"users.error.{key}",
                    entity="user",
                    severity=severity,
                    description=description,
                    count=len(group),
                    sample=sample,
                    suggested_fix=suggested_fix,
                    fix=fix_fn,
                )
            )
        return issues

    def _check_user_incomplete_profiles(self, sample_limit: int) -> List[HealthIssue]:
        cursor = self.connection.execute(
            """
            SELECT u.id_user, u.role, u.join_date, u.last_active, u.ratings_count,
                   cu.status AS crawl_status, cu.updated_at
            FROM users AS u
            LEFT JOIN crawl_users AS cu ON cu.id_user = u.id_user
            WHERE u.role IS NULL OR u.join_date IS NULL
            ORDER BY COALESCE(cu.updated_at, '1970-01-01') DESC
            """
        )
        rows = cursor.fetchall()
        if not rows:
            return []

        ids = [row["id_user"] for row in rows]
        sample = self._build_sample(
            rows[:sample_limit],
            ("id_user", "role", "join_date", "crawl_status", "updated_at"),
        )
        return [
            HealthIssue(
                category="users.incomplete.profile",
                entity="user",
                severity="medium",
                description=f"{len(rows)} usuarios tienen metadata incompleta",
                count=len(rows),
                sample=sample,
                suggested_fix="Reencolar en crawl_users para completar datos",
                fix=(lambda dry_run, ids=ids: self._enqueue_users(ids, dry_run)) if ids else None,
            )
        ]

    def _check_user_rating_mismatches(self, sample_limit: int) -> List[HealthIssue]:
        cursor = self.connection.execute(
            """
            SELECT u.id_user,
                   u.ratings_count AS expected_ratings,
                   COUNT(i.id_release) AS stored_ratings
            FROM users AS u
            LEFT JOIN interactions AS i ON i.id_user = u.id_user
            GROUP BY u.id_user
            HAVING expected_ratings > stored_ratings
            ORDER BY (expected_ratings - stored_ratings) DESC
            """
        )
        rows = cursor.fetchall()
        if not rows:
            return []

        ids = [row["id_user"] for row in rows]
        sample = self._build_sample(
            rows[:sample_limit],
            ("id_user", "expected_ratings", "stored_ratings"),
        )
        return [
            HealthIssue(
                category="users.incomplete.ratings",
                entity="user",
                severity="high",
                description=f"{len(rows)} usuarios tienen ratings faltantes en interactions",
                count=len(rows),
                sample=sample,
                suggested_fix="Reencolar usuarios para refrescar ratings",
                fix=(lambda dry_run, ids=ids: self._enqueue_users(ids, dry_run)) if ids else None,
            )
        ]

    def _check_release_queue_errors(self, sample_limit: int) -> List[HealthIssue]:
        cursor = self.connection.execute(
            """
            SELECT id_release, attempts, last_error, last_crawled, updated_at
            FROM crawl_releases
            WHERE status = 'error'
            ORDER BY updated_at DESC
            """
        )
        rows = cursor.fetchall()
        if not rows:
            return []

        grouped: Dict[str, List[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            group_key = self._classify_error(row["last_error"])
            grouped[group_key].append(row)

        issues: List[HealthIssue] = []
        for key, group in grouped.items():
            ids = [row["id_release"] for row in group]
            sample = self._build_sample(
                group[:sample_limit],
                ("id_release", "attempts", "last_error", "updated_at"),
            )
            if key == "not_found":
                severity: Severity = "medium"
                description = f"{len(group)} releases devuelven 404 en crawl_releases"
                suggested_fix = "Eliminar del queue o revisar registro"
                fix_fn = (
                    (lambda dry_run, ids=ids: self._delete_queue_releases(ids, dry_run))
                    if ids
                    else None
                )
            elif key in {"rate_limited", "timeout", "connection", "incomplete_read"}:
                severity = "high"
                description = f"{len(group)} releases con error temporal ({key})"
                suggested_fix = "Resetear a pending"
                fix_fn = (
                    (lambda dry_run, ids=ids: self._reset_queue_releases(ids, dry_run))
                    if ids
                    else None
                )
            else:
                severity = "medium"
                description = f"{len(group)} releases con error desconocido"
                suggested_fix = "Revisar mensaje y evaluar reintento"
                fix_fn = (
                    (lambda dry_run, ids=ids: self._reset_queue_releases(ids, dry_run))
                    if ids
                    else None
                )

            issues.append(
                HealthIssue(
                    category=f"releases.error.{key}",
                    entity="release",
                    severity=severity,
                    description=description,
                    count=len(group),
                    sample=sample,
                    suggested_fix=suggested_fix,
                    fix=fix_fn,
                )
            )
        return issues

    def _check_release_metadata_gaps(self, sample_limit: int) -> List[HealthIssue]:
        cursor = self.connection.execute(
            """
            SELECT r.id_release, r.title, r.release_year, r.avg_rating,
                   r.ratings_count, r.staff_avg, r.review_count,
                   cr.status AS crawl_status, cr.updated_at
            FROM releases AS r
            LEFT JOIN crawl_releases AS cr ON cr.id_release = r.id_release
            WHERE r.release_year IS NULL
               OR (r.ratings_count > 0 AND r.avg_rating IS NULL)
               OR (r.review_count > 0 AND r.staff_avg IS NULL)
            ORDER BY COALESCE(cr.updated_at, '1970-01-01') DESC
            """
        )
        rows = cursor.fetchall()
        if not rows:
            return []

        ids = [row["id_release"] for row in rows]
        sample = self._build_sample(
            rows[:sample_limit],
            (
                "id_release",
                "title",
                "release_year",
                "avg_rating",
                "ratings_count",
                "staff_avg",
                "review_count",
                "crawl_status",
            ),
        )
        return [
            HealthIssue(
                category="releases.incomplete.metadata",
                entity="release",
                severity="medium",
                description=f"{len(rows)} releases tienen metadata incompleta",
                count=len(rows),
                sample=sample,
                suggested_fix="Reencolar releases para completar metadata",
                fix=(lambda dry_run, ids=ids: self._enqueue_releases(ids, dry_run))
                if ids
                else None,
            )
        ]

    def _check_release_rating_mismatches(self, sample_limit: int) -> List[HealthIssue]:
        cursor = self.connection.execute(
            """
            SELECT r.id_release,
                   r.ratings_count AS expected_ratings,
                   COUNT(i.id_user) AS stored_ratings
            FROM releases AS r
            LEFT JOIN interactions AS i ON i.id_release = r.id_release
            GROUP BY r.id_release
            HAVING expected_ratings > stored_ratings
            ORDER BY (expected_ratings - stored_ratings) DESC
            """
        )
        rows = cursor.fetchall()
        if not rows:
            return []

        ids = [row["id_release"] for row in rows]
        sample = self._build_sample(
            rows[:sample_limit],
            ("id_release", "expected_ratings", "stored_ratings"),
        )
        return [
            HealthIssue(
                category="releases.incomplete.ratings",
                entity="release",
                severity="high",
                description=f"{len(rows)} releases tienen ratings inconsistentes",
                count=len(rows),
                sample=sample,
                suggested_fix="Reencolar releases para recalcular ratings",
                fix=(lambda dry_run, ids=ids: self._enqueue_releases(ids, dry_run))
                if ids
                else None,
            )
        ]

    def _check_artist_genre_gaps(self, sample_limit: int) -> List[HealthIssue]:
        cursor = self.connection.execute(
            """
            SELECT a.id_artist,
                   a.name,
                   COALESCE(ca.status, 'unknown') AS crawl_status,
                   ca.updated_at,
                   COUNT(ag.id_genre) AS genre_count
            FROM artists AS a
            LEFT JOIN crawl_artists AS ca ON ca.id_artist = a.id_artist
            LEFT JOIN artist_genres AS ag ON ag.id_artist = a.id_artist
            WHERE COALESCE(ca.status, 'pending') = 'done'
            GROUP BY a.id_artist, a.name, ca.status, ca.updated_at
            HAVING genre_count = 0
            ORDER BY COALESCE(ca.updated_at, '1970-01-01') DESC
            """
        )
        rows = cursor.fetchall()
        if not rows:
            return []

        ids = [row["id_artist"] for row in rows]
        sample = self._build_sample(
            rows[:sample_limit],
            ("id_artist", "name", "crawl_status", "updated_at"),
        )
        return [
            HealthIssue(
                category="artists.missing.genres",
                entity="artist",
                severity="medium",
                description=f"{len(rows)} artistas sin generos asignados tras finalizar el crawl",
                count=len(rows),
                sample=sample,
                suggested_fix="Reencolar artistas para completar generos",
                fix=(lambda dry_run, ids=ids: self._enqueue_artists(ids, dry_run)) if ids else None,
            )
        ]

    @staticmethod
    def _classify_error(error_message: Any) -> str:
        if not error_message:
            return "unknown"
        lowered = str(error_message).lower()
        if "404" in lowered:
            return "not_found"
        if "429" in lowered or "too many" in lowered:
            return "rate_limited"
        if "timeout" in lowered:
            return "timeout"
        if "incomplet" in lowered:
            return "incomplete_read"
        if "connection" in lowered or "reset" in lowered or "broken" in lowered:
            return "connection"
        return "other"

    @staticmethod
    def _build_sample(rows: Sequence[sqlite3.Row], fields: Iterable[str]) -> List[Dict[str, Any]]:
        sample: List[Dict[str, Any]] = []
        for row in rows:
            entry: Dict[str, Any] = {}
            for field in fields:
                if field not in row.keys():
                    continue
                value = row[field]
                if isinstance(value, str):
                    entry[field] = DBHealthChecker._truncate(value)
                else:
                    entry[field] = value
            sample.append(entry)
        return sample

    @staticmethod
    def _truncate(value: str, limit: int = 120) -> str:
        if len(value) <= limit:
            return value
        return value[: limit - 3] + "..."

    def _delete_queue_users(self, ids: Sequence[str], dry_run: bool) -> int:
        if not ids:
            return 0
        if dry_run:
            return len(ids)
        with self.connection:
            for user_id in ids:
                self.connection.execute(
                    "DELETE FROM crawl_users WHERE id_user = ?",
                    (user_id,),
                )
        return len(ids)

    def _reset_queue_users(
        self, ids: Sequence[str], dry_run: bool, target_status: str = "pending"
    ) -> int:
        if not ids:
            return 0
        if dry_run:
            return len(ids)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        updated = 0
        with self.connection:
            for user_id in ids:
                cursor = self.connection.execute(
                    """
                    UPDATE crawl_users
                    SET status = ?, attempts = 0, last_error = NULL, updated_at = ?
                    WHERE id_user = ?
                    """,
                    (target_status, now, user_id),
                )
                updated += cursor.rowcount
        return updated

    def _enqueue_users(self, ids: Sequence[str], dry_run: bool) -> int:
        if not ids:
            return 0
        if dry_run:
            return len(ids)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.connection:
            for user_id in ids:
                self.connection.execute(
                    (
                        "INSERT INTO crawl_users "
                        "(id_user, status, priority, attempts, last_error, "
                        "last_crawled, updated_at) "
                        "VALUES (?, 'pending', 0, 0, NULL, NULL, ?) "
                        "ON CONFLICT(id_user) DO UPDATE SET "
                        "status = 'pending', "
                        "attempts = 0, "
                        "last_error = NULL, "
                        "updated_at = excluded.updated_at"
                    ),
                    (user_id, now),
                )
        return len(ids)

    def _delete_queue_releases(self, ids: Sequence[int], dry_run: bool) -> int:
        if not ids:
            return 0
        if dry_run:
            return len(ids)
        with self.connection:
            for release_id in ids:
                self.connection.execute(
                    "DELETE FROM crawl_releases WHERE id_release = ?",
                    (release_id,),
                )
        return len(ids)

    def _reset_queue_releases(
        self, ids: Sequence[int], dry_run: bool, target_status: str = "pending"
    ) -> int:
        if not ids:
            return 0
        if dry_run:
            return len(ids)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        updated = 0
        with self.connection:
            for release_id in ids:
                cursor = self.connection.execute(
                    """
                    UPDATE crawl_releases
                    SET status = ?, attempts = 0, last_error = NULL, updated_at = ?
                    WHERE id_release = ?
                    """,
                    (target_status, now, release_id),
                )
                updated += cursor.rowcount
        return updated

    def _enqueue_releases(self, ids: Sequence[int], dry_run: bool) -> int:
        if not ids:
            return 0
        if dry_run:
            return len(ids)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.connection:
            for release_id in ids:
                self.connection.execute(
                    (
                        "INSERT INTO crawl_releases "
                        "(id_release, status, attempts, last_error, last_crawled, updated_at) "
                        "VALUES (?, 'pending', 0, NULL, NULL, ?) "
                        "ON CONFLICT(id_release) DO UPDATE SET "
                        "status = 'pending', "
                        "attempts = 0, "
                        "last_error = NULL, "
                        "updated_at = excluded.updated_at"
                    ),
                    (release_id, now),
                )
        return len(ids)

    def _enqueue_artists(self, ids: Sequence[int], dry_run: bool) -> int:
        if not ids:
            return 0
        if dry_run:
            return len(ids)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.connection:
            for artist_id in ids:
                self.connection.execute(
                    (
                        "INSERT INTO crawl_artists "
                        "(id_artist, status, attempts, last_error, last_crawled, updated_at) "
                        "VALUES (?, 'pending', 0, NULL, NULL, ?) "
                        "ON CONFLICT(id_artist) DO UPDATE SET "
                        "status = 'pending', "
                        "attempts = 0, "
                        "last_error = NULL, "
                        "updated_at = excluded.updated_at"
                    ),
                    (artist_id, now),
                )
        return len(ids)


SEVERITY_ORDER: Dict[Severity, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "unknown": 4,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Chequeador de salud para la base de datos de Sputnik SR",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data/sputnik.db"),
        help="Ruta al archivo SQLite (default: data/sputnik.db)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Ejemplos maximos por categoria",
    )
    parser.add_argument(
        "--format",
        choices=["pretty", "json"],
        default="pretty",
        help="Formato de salida",
    )
    parser.add_argument(
        "--fix",
        nargs="*",
        default=[],
        help="Categorias a reparar automaticamente",
    )
    parser.add_argument(
        "--fix-all",
        action="store_true",
        help="Intentar reparar todas las categorias con solucion disponible",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Aplica los cambios (por defecto solo muestra que se haria)",
    )
    return parser.parse_args()


def print_pretty(issues: Sequence[HealthIssue]) -> None:
    if not issues:
        print("OK Base sin problemas aparentes")
        return

    sorted_issues = sorted(
        issues,
        key=lambda issue: (SEVERITY_ORDER.get(issue.severity, 99), issue.category),
    )

    for issue in sorted_issues:
        print(f"[{issue.severity.upper()}] {issue.category}")
        print(f"  {issue.description}")
        print(f"  Total afectados: {issue.count}")
        if issue.sample:
            print("  Ejemplos:")
            for sample in issue.sample:
                serialized = ", ".join(f"{key}={value}" for key, value in sample.items())
                print(f"    - {serialized}")
        if issue.suggested_fix:
            print(f"  Sugerencia: {issue.suggested_fix}")
        if issue.has_fix():
            print(f"  Reparacion: usar --fix {issue.category}")
        print()


def main() -> int:
    args = parse_args()

    if not args.db.exists():
        print(f"Error: base de datos no encontrada en {args.db}")
        return 1

    connection = sqlite3.connect(str(args.db))
    checker = DBHealthChecker(connection)
    issues = checker.run_checks(sample_limit=args.limit)

    if args.format == "json":
        payload = [issue.to_dict() for issue in issues]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print_pretty(issues)

    categories_to_fix: List[str] = []
    if args.fix_all:
        categories_to_fix = [issue.category for issue in issues if issue.has_fix()]
    elif args.fix:
        categories_to_fix = list(dict.fromkeys(args.fix))

    dry_run = not args.apply
    applied_any = False

    if categories_to_fix:
        issues_by_category = {issue.category: issue for issue in issues}
        for category in categories_to_fix:
            issue = issues_by_category.get(category)
            if not issue:
                print(f"Aviso: categoria {category} no encontrada en el analisis actual")
                continue
            if not issue.has_fix():
                print(f"Aviso: categoria {category} no tiene reparacion automatica")
                continue
            changed = issue.apply_fix(dry_run=dry_run)
            mode = "(dry-run)" if dry_run else ""
            print(f"{mode} {category}: {changed} registros procesados")
            applied_any = True

        if applied_any and dry_run:
            print("Para aplicar los cambios ejecute nuevamente con --apply")
        elif applied_any:
            print("Cambios aplicados. Reejecute el chequeador para validar.")

    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
