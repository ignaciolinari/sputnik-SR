#!/usr/bin/env python3
"""Script para analizar y hacer vacuum de las bases de datos de Sputnik SR."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any
from typing import Dict


# Add parent directory to path to import db_health
sys.path.insert(0, str(Path(__file__).parent.parent))
from maintenance.db_health import DBHealthChecker


def get_db_stats(
    connection: sqlite3.Connection, quick: bool = True, include_counts: bool = False
) -> Dict[str, Any]:
    """Obtiene estadísticas generales de la base de datos."""
    cursor = connection.cursor()
    stats = {}

    # Page count and size
    cursor.execute("PRAGMA page_count")
    stats["page_count"] = cursor.fetchone()[0]

    cursor.execute("PRAGMA page_size")
    stats["page_size"] = cursor.fetchone()[0]

    stats["estimated_size_bytes"] = stats["page_count"] * stats["page_size"]
    stats["estimated_size_mb"] = stats["estimated_size_bytes"] / (1024 * 1024)

    # Free pages (before vacuum)
    cursor.execute("PRAGMA freelist_count")
    stats["freelist_count"] = cursor.fetchone()[0]
    stats["free_space_bytes"] = stats["freelist_count"] * stats["page_size"]
    stats["free_space_mb"] = stats["free_space_bytes"] / (1024 * 1024)

    # Integrity check - usar quick_check si es modo rápido
    if quick:
        cursor.execute("PRAGMA quick_check")
        integrity_result = cursor.fetchone()[0]
        stats["integrity_ok"] = integrity_result == "ok"
        stats["integrity_message"] = integrity_result
    else:
        cursor.execute("PRAGMA integrity_check")
        integrity_result = cursor.fetchone()[0]
        stats["integrity_ok"] = integrity_result == "ok"
        stats["integrity_message"] = integrity_result

    # Table list
    cursor.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
    """)
    tables = [row[0] for row in cursor.fetchall()]
    stats["tables"] = tables

    # Table counts - solo si se solicita explícitamente (muy lento en tablas grandes)
    if include_counts:
        table_counts = {}
        for table in tables:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                table_counts[table] = cursor.fetchone()[0]
            except sqlite3.Error as e:
                table_counts[table] = f"Error: {e}"
        stats["table_counts"] = table_counts

    # Database version
    cursor.execute("SELECT sqlite_version()")
    stats["sqlite_version"] = cursor.fetchone()[0]

    return stats


def analyze_database(
    db_path: Path, quick: bool = True, include_counts: bool = False, include_health: bool = False
) -> Dict[str, Any]:
    """Analiza una base de datos y retorna información completa."""
    if not db_path.exists():
        return {"error": f"Database not found: {db_path}"}

    # Get file size before
    file_size_before = db_path.stat().st_size

    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row

    try:
        # Get basic stats (rápido por defecto)
        stats = get_db_stats(connection, quick=quick, include_counts=include_counts)
        stats["file_size_before_bytes"] = file_size_before
        stats["file_size_before_mb"] = file_size_before / (1024 * 1024)

        # Run health checks solo si se solicita (muy lento)
        if include_health:
            checker = DBHealthChecker(connection)
            health_issues = checker.run_checks(sample_limit=3)
            stats["health_issues"] = [
                {
                    "category": issue.category,
                    "severity": issue.severity,
                    "description": issue.description,
                    "count": issue.count,
                }
                for issue in health_issues
            ]
            stats["health_issues_count"] = len(health_issues)
        else:
            stats["health_issues"] = []
            stats["health_issues_count"] = 0

        return stats
    finally:
        connection.close()


def vacuum_database(db_path: Path) -> Dict[str, Any]:
    """Ejecuta VACUUM en una base de datos."""
    if not db_path.exists():
        return {"error": f"Database not found: {db_path}"}

    file_size_before = db_path.stat().st_size

    connection = sqlite3.connect(str(db_path))
    try:
        # Execute VACUUM
        connection.execute("VACUUM")
        connection.commit()

        # Get stats after vacuum
        file_size_after = db_path.stat().st_size

        cursor = connection.cursor()
        cursor.execute("PRAGMA page_count")
        page_count_after = cursor.fetchone()[0]

        cursor.execute("PRAGMA page_size")
        page_size = cursor.fetchone()[0]

        cursor.execute("PRAGMA freelist_count")
        freelist_count_after = cursor.fetchone()[0]

        return {
            "success": True,
            "file_size_before_bytes": file_size_before,
            "file_size_before_mb": file_size_before / (1024 * 1024),
            "file_size_after_bytes": file_size_after,
            "file_size_after_mb": file_size_after / (1024 * 1024),
            "size_reduction_bytes": file_size_before - file_size_after,
            "size_reduction_mb": (file_size_before - file_size_after) / (1024 * 1024),
            "size_reduction_percent": (
                (file_size_before - file_size_after) / file_size_before * 100
            )
            if file_size_before > 0
            else 0,
            "page_count_after": page_count_after,
            "page_size": page_size,
            "freelist_count_after": freelist_count_after,
        }
    except sqlite3.Error as e:
        return {"error": str(e)}
    finally:
        connection.close()


def print_analysis(stats: Dict[str, Any], db_name: str) -> None:
    """Imprime el análisis de forma legible."""
    print(f"\n{'='*70}")
    print(f"ANÁLISIS: {db_name}")
    print(f"{'='*70}")

    if "error" in stats:
        print(f"ERROR: {stats['error']}")
        return

    print("\n📊 Estadísticas Generales:")
    print(f"  SQLite Version: {stats.get('sqlite_version', 'N/A')}")
    print(f"  Tamaño del archivo: {stats.get('file_size_before_mb', 0):.2f} MB")
    print(f"  Páginas totales: {stats.get('page_count', 0):,}")
    print(f"  Tamaño de página: {stats.get('page_size', 0):,} bytes")
    print(f"  Tamaño estimado: {stats.get('estimated_size_mb', 0):.2f} MB")
    print(f"  Páginas libres: {stats.get('freelist_count', 0):,}")
    print(f"  Espacio libre: {stats.get('free_space_mb', 0):.2f} MB")

    print("\n🔍 Integridad:")
    integrity_ok = stats.get("integrity_ok", False)
    status_icon = "✅" if integrity_ok else "❌"
    print(f"  {status_icon} {stats.get('integrity_message', 'N/A')}")

    print("\n📋 Tablas:")
    tables = stats.get("tables", [])
    table_counts = stats.get("table_counts", {})
    if table_counts:
        for table, count in sorted(table_counts.items()):
            print(f"  {table:30s} {count:,}")
    else:
        print(f"  {len(tables)} tablas encontradas (use --include-counts para ver conteos)")
        for table in tables[:10]:  # Mostrar primeras 10
            print(f"  - {table}")
        if len(tables) > 10:
            print(f"  ... y {len(tables) - 10} más")

    print("\n🏥 Health Checks:")
    health_issues = stats.get("health_issues", [])
    if not health_issues:
        print("  ✅ Sin problemas detectados")
    else:
        print(f"  ⚠️  {stats.get('health_issues_count', 0)} problemas encontrados:")
        for issue in health_issues:
            severity_icon = {
                "critical": "🔴",
                "high": "🟠",
                "medium": "🟡",
                "low": "🟢",
            }.get(issue["severity"], "⚪")
            print(f"    {severity_icon} [{issue['severity'].upper()}] {issue['category']}")
            print(f"       {issue['description']} ({issue['count']} afectados)")


def print_vacuum_results(results: Dict[str, Any], db_name: str) -> None:
    """Imprime los resultados del vacuum."""
    print(f"\n{'='*70}")
    print(f"VACUUM: {db_name}")
    print(f"{'='*70}")

    if "error" in results:
        print(f"❌ ERROR: {results['error']}")
        return

    if not results.get("success"):
        print("❌ Vacuum falló")
        return

    print("\n📦 Resultados del Vacuum:")
    print(f"  Tamaño antes:  {results.get('file_size_before_mb', 0):.2f} MB")
    print(f"  Tamaño después: {results.get('file_size_after_mb', 0):.2f} MB")
    print(
        f"  Reducción:     {results.get('size_reduction_mb', 0):.2f} MB "
        f"({results.get('size_reduction_percent', 0):.2f}%)"
    )
    print(f"  Páginas después: {results.get('page_count_after', 0):,}")
    print(f"  Páginas libres después: {results.get('freelist_count_after', 0):,}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analiza y hace vacuum de las bases de datos de Sputnik SR"
    )
    parser.add_argument(
        "--analyze-only",
        action="store_true",
        help="Solo analizar, no hacer vacuum",
    )
    parser.add_argument(
        "--vacuum-only",
        action="store_true",
        help="Solo hacer vacuum, no analizar",
    )
    parser.add_argument(
        "--lite-only",
        action="store_true",
        help="Solo procesar sputnik_lite.db",
    )
    parser.add_argument(
        "--full-only",
        action="store_true",
        help="Solo procesar sputnik.db",
    )
    parser.add_argument(
        "--full-analysis",
        action="store_true",
        help="Incluir análisis completo (counts y health checks - puede ser lento)",
    )
    parser.add_argument(
        "--include-counts",
        action="store_true",
        help="Incluir conteos de filas por tabla (puede ser lento)",
    )
    parser.add_argument(
        "--include-health",
        action="store_true",
        help="Incluir health checks completos (puede ser muy lento)",
    )
    parser.add_argument(
        "--re-analyze-after",
        action="store_true",
        help="Re-analizar después del vacuum (por defecto no se hace)",
    )

    args = parser.parse_args()

    base_dir = Path(__file__).parent.parent
    db_full = base_dir / "data" / "sputnik.db"
    db_lite = base_dir / "data" / "sputnik_lite.db"

    databases = []
    if args.lite_only:
        databases = [("sputnik_lite.db", db_lite)]
    elif args.full_only:
        databases = [("sputnik.db", db_full)]
    else:
        databases = [
            ("sputnik.db", db_full),
            ("sputnik_lite.db", db_lite),
        ]

    should_analyze = not args.vacuum_only
    should_vacuum = not args.analyze_only

    # Determinar qué análisis incluir
    include_counts = args.include_counts or args.full_analysis
    include_health = args.include_health or args.full_analysis
    quick_mode = not args.full_analysis

    for db_name, db_path in databases:
        if not db_path.exists():
            print(f"⚠️  Advertencia: {db_name} no encontrado en {db_path}")
            continue

        if should_analyze:
            mode_str = "rápido" if quick_mode else "completo"
            print(f"\n🔍 Analizando {db_name} (modo {mode_str})...")
            stats = analyze_database(
                db_path,
                quick=quick_mode,
                include_counts=include_counts,
                include_health=include_health,
            )
            print_analysis(stats, db_name)

        if should_vacuum:
            print(f"\n🧹 Ejecutando VACUUM en {db_name}...")
            results = vacuum_database(db_path)
            print_vacuum_results(results, db_name)

            # Re-analyze after vacuum solo si se solicita explícitamente
            if should_analyze and args.re_analyze_after:
                print(f"\n🔍 Re-analizando {db_name} después del vacuum...")
                stats_after = analyze_database(
                    db_path,
                    quick=quick_mode,
                    include_counts=include_counts,
                    include_health=include_health,
                )
                print_analysis(stats_after, f"{db_name} (después de vacuum)")

    print(f"\n{'='*70}")
    print("✅ Proceso completado")
    print(f"{'='*70}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
