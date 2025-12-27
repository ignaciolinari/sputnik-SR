# Comparison Report: Database Maintenance Scripts

## Executive Summary

This project has two maintenance scripts with different purposes:

1. **`db_health.py`** - To verify issues during the crawling/scraping process
2. **`analyze_and_vacuum.py`** - For post-population maintenance (analysis and optimization)

---

## 1. `maintenance/db_health.py`

### Purpose

A tool to audit database health **during the crawling/scraping process**. It detects problems related to data collection.

### Main Features

#### Checks it performs:

1. **Errors in the user queue** (`crawl_users`)

  - Detects users with status 'error'
  - Classifies errors: 404, rate limiting, timeouts, connection, etc.
  - Can remove or reset users depending on the error type

2. **Incomplete user profiles**

  - Users with `role` or `join_date` set to NULL
  - Suggests re-queueing to complete data

3. **User rating mismatches**

  - Compares `users.ratings_count` vs the real count in `interactions`
  - Detects missing ratings

4. **Errors in the release queue** (`crawl_releases`)

  - Similar to users; detects releases with errors
  - Classifies and suggests actions

5. **Incomplete release metadata**

  - Releases without `release_year` or with inconsistent ratings
  - Suggests re-queueing to complete

6. **Release rating mismatches**

  - Compares `releases.ratings_count` vs the real count in `interactions`

7. **Artists without genres**

  - Artists marked as 'done' but without assigned genres

### Key Features:

- **Automatic repair**: Can apply fixes with `--fix` and `--apply`
- **Severity classification**: critical, high, medium, low
- **Problem samples**: Shows examples of each issue type
- **Dry-run by default**: Does not apply changes unless you pass `--apply`
- **JSON output**: Supports `--format json` for integration

### When to use it:

- **During crawling**: To monitor and resolve scraping process issues
- **After major failures**: To identify and repair problems after crashes
- **Queue maintenance**: To clean or reset entries in crawler queues

### Example usage:

```bash
# Inspect problems
python maintenance/db_health.py --db data/sputnik.db

# JSON output
python maintenance/db_health.py --db data/sputnik.db --format json

# Fix temporary errors (dry-run)
python maintenance/db_health.py --db data/sputnik.db --fix users.error.timeout

# Apply fixes
python maintenance/db_health.py --db data/sputnik.db --fix users.error.timeout --apply

# Fix everything automatically
python maintenance/db_health.py --db data/sputnik.db --fix-all --apply
```

### Performance:

- **Can be slow** on large databases due to complex queries with JOINs and GROUP BY
- **Scans large tables** like `interactions` to detect mismatches

---

## 2. `maintenance/analyze_and_vacuum.py`

### Purpose

Script for **post-population maintenance**: general statistics analysis and optimization via VACUUM.

### Main Features

#### What it analyzes:

1. **Size statistics**

  - File size on disk
  - Total pages and page size
  - Free pages (wasted space)

2. **Integrity verification**

  - `PRAGMA quick_check` (fast by default)
  - `PRAGMA integrity_check` (full if requested)

3. **Table list**

  - Lists all tables
  - Optionally counts rows (can be slow)

4. **Optional health checks**

  - Can run `db_health.py` if requested via `--include-health`
  - Not recommended for regular usage (very slow)

#### Vacuum:

- Runs `VACUUM` to defragment and optimize the database
- Shows size reduction before/after
- Post-vacuum statistics

### Key Features:

- **Fast by default**: Uses `quick_check` and avoids expensive operations
- **Fast mode**: Does not count rows or run health checks by default
- **Optimization**: Runs VACUUM to defragment
- **Flexible**: Options for full analysis when needed

### When to use it:

- **Regular maintenance**: Verify integrity and optimize space
- **After large operations**: After massive DELETEs or structural changes
- **Size monitoring**: Track growth and wasted space
- **Before backups**: Ensure the database is optimized

### Example usage:

```bash
# Quick analysis + vacuum (recommended)
python maintenance/analyze_and_vacuum.py

# Vacuum only (faster)
python maintenance/analyze_and_vacuum.py --vacuum-only

# Analyze only
python maintenance/analyze_and_vacuum.py --analyze-only

# Full analysis (includes counts - slow)
python maintenance/analyze_and_vacuum.py --include-counts

# Single database only
python maintenance/analyze_and_vacuum.py --lite-only
python maintenance/analyze_and_vacuum.py --full-only
```

### Performance:

- **Fast by default**: Basic SQLite operations only
- **VACUUM can take time**: On large DBs (8GB+) it may take several minutes
- **Counts are slow**: `--include-counts` runs COUNT(*) on every table

---

## Direct Comparison

| Aspect | `db_health.py` | `analyze_and_vacuum.py` |
|---------|----------------|-------------------------|
| **Purpose** | Verify crawling issues | Post-population maintenance |
| **Focus** | Data and queue issues | Statistics and integrity |
| **Speed** | Slow (complex queries) | Fast (by default) |
| **Repair** | Yes, with `--fix --apply` | No, analysis only |
| **VACUUM** | No | Yes |
| **Integrity** | No | Yes (quick_check) |
| **Typical usage** | During scraping | Regular maintenance |

---

## Recommended Workflow

### During Crawling/Scraping:

```bash
# Monitor process issues
python maintenance/db_health.py --db data/sputnik.db

# Repair detected problems
python maintenance/db_health.py --db data/sputnik.db --fix-all --apply
```

### Post-Population Maintenance:

```bash
# Quick analysis + optimization
python maintenance/analyze_and_vacuum.py

# If there are integrity problems, run a full analysis
python maintenance/analyze_and_vacuum.py --full-analysis
```

### Regular Maintenance (Monthly):

```bash
# Verify both databases
python maintenance/analyze_and_vacuum.py

# If there is wasted space, vacuum will optimize it
```

---

## Important Notes

1. **`db_health.py` is specific to the crawling process**: its checks assume tables like `crawl_users`, `crawl_releases`, etc. It does not make sense to use it on databases that are not being actively populated.

2. **`analyze_and_vacuum.py` is generic**: it works with any SQLite database and is useful for general maintenance.

3. **VACUUM locks the database**: during VACUUM, the DB is locked for writes. In production, run it during low-traffic hours.

4. **Health checks are optional**: `analyze_and_vacuum.py` can run health checks, but it's better to use `db_health.py` directly if you need that functionality.

---

## Conclusion

- **Use `db_health.py`** when you are crawling/scraping and need to detect/repair issues in the ingestion process.
- **Use `analyze_and_vacuum.py`** for regular maintenance, integrity verification, and space optimization.

Both scripts are complementary and serve different purposes in the database lifecycle.
