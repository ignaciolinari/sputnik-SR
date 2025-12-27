# Data Extraction

This document describes in detail the scraping and crawling pipeline used to extract data from Sputnikmusic.

## Table of Contents

1. [High-level Architecture](#high-level-architecture)
2. [Scraper CLI](#scraper-cli)
3. [Bulk Crawler](#bulk-crawler)
4. [Staged Ingestion Flow](#staged-ingestion-flow)
5. [Monitoring and Tracking](#monitoring-and-tracking)
6. [Database Maintenance](#database-maintenance)
7. [Data Schema](#data-schema)
8. [Ethical Considerations](#ethical-considerations)

---

## High-level Architecture

The extraction system is composed of two main layers:

### Scraping Layer (`scraper/`)

Low-level modules for HTML parsing and HTTP communication:

| Module | Description |
|--------|-------------|
| `charts.py` | Extracts yearly album rankings |
| `soundoffs.py` | Parses user comments and ratings on releases |
| `tracklist.py` | Extracts track listings |
| `users.py` | Fetches public user profiles |
| `user_ratings.py` | Extracts user rating histories |
| `discography.py` | Parses full artist discographies |
| `http.py` | HTTP client with rate limiting and retries |

### Crawling Layer (`crawler/`)

High-level orchestrators that coordinate scraping and persist into SQLite:

| Module | Description |
|--------|-------------|
| `runner.py` | Main crawler: charts → releases → soundoffs |
| `discography.py` | Expands discographies of queued artists |
| `user_expander.py` | Fetches full rating histories of queued users |

---

## Scraper CLI

To fetch specific data without persisting it to the database:

```bash
# Yearly chart as JSON
python -m scraper --year 2024 --pretty > data/best_albums_2024.json

# More verbose output
python -m scraper --year 2024 --pretty --verbose
```

### Scraper Options

| Flag | Description |
|------|-------------|
| `--year` | Chart year to extract |
| `--pretty` | Pretty-printed JSON |
| `--verbose` | Verbose logging |

### Programmatic Example

See `examples/fetch_latest.py` for an example of how to use the scraper programmatically:

```python
from scraper import charts

# Fetch chart for a given year
albums = charts.fetch_year_chart(2024)

for album in albums:
    print(f"{album['artist']} - {album['title']} ({album['rating']})")
```

---

## Bulk Crawler

The main crawler ingests data systematically and persists it in SQLite.

### Basic Command

```bash
python -m crawler \
    --start-year 1960 \
    --end-year 2025 \
    --db data/sputnik.db \
    --schema data/schema.sql \
    --log-level INFO
```

### Main Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--start-year` | Crawl start year | - |
| `--end-year` | Crawl end year | - |
| `--db` | SQLite database path | `data/sputnik.db` |
| `--schema` | SQL schema path | `data/schema.sql` |
| `--log-level` | Logging level | `INFO` |

### Control Flags

| Parameter | Description |
|-----------|-------------|
| `--skip-tracklists` | Skips tracklist extraction |
| `--skip-soundoffs` | Skips soundoffs extraction |
| `--skip-user-profiles` | Does not fetch user profiles during the crawl |
| `--max-soundoffs N` | Limits soundoffs per album |
| `--dry-run` | Validates without writing to the database |
| `--no-queue-users` | Does not queue detected users |
| `--user-queue-priority N` | Priority for queued users |

### Crawler Characteristics

- **Configurable rate limiting**: Respects `min_interval` between requests
- **Idempotent**: Uses `ON CONFLICT` to avoid duplicates
- **Resumable**: Persists state in `crawl_state` (status per year)
- **Enrichment**: Detects user roles (EMERITUS, STAFF, etc.)

### Resuming

To resume an interrupted crawl, simply re-run the command:

```bash
# Completed years are marked as DONE in crawl_state
python -m crawler --start-year 1960 --end-year 2025 --db data/sputnik.db
```

---

## Staged Ingestion Flow

To populate the database efficiently, it is recommended to split ingestion into three stages:

### Stage 1: Seeds (Charts + Soundoffs)

Captures yearly rankings and visible interactions on each album.

```bash
python -m crawler \
    --start-year 2000 \
    --end-year 2024 \
    --db data/sputnik.db \
    --schema data/schema.sql \
    --skip-tracklists \
    --skip-user-profiles \
    --user-queue-priority 5 \
    --log-level INFO
```

**Alternative script:** `scripts/seed_charts.sh`

**Result:**
- Artists and releases from the yearly top chart
- Soundoffs interactions
- Users queued in `crawl_users`
- Artists queued in `crawl_artists`

### Stage 2: Expand Discographies

Expands the full discography of each detected artist.

```bash
python -m crawler.discography \
    --db data/sputnik.db \
    --schema data/schema.sql \
    --batch-size 25 \
    --max-soundoffs 100 \
    --log-level INFO
```

**Alternative script:** `scripts/expand_discographies.sh`

**Parameters:**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--batch-size` | Artists per batch | 25 |
| `--max-soundoffs` | Soundoffs per release | 100 |
| `--skip-tracklists` | Skips tracklists | false |
| `--skip-soundoffs` | Skips soundoffs | false |

**Recommendation:** Run this before expanding users so future interactions point to releases that are already populated.

### Stage 3: Expand Users

Fetches the full rating history for each queued user.

```bash
python -m crawler.user_expander \
    --db data/sputnik.db \
    --schema data/schema.sql \
    --batch-size 25 \
    --max-rating-pages none \
    --log-level INFO
```

**Alternative script:** `scripts/expand_users.sh`

**Parameters:**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--batch-size` | Users per batch | 25 |
| `--max-rating-pages` | Rating pages per user | `none` (all) |
| `--priority-min` | Minimum priority to process | 0 |

**Note:** The `uservote.php` endpoint does not expose the exact vote date, only the rating.

### General Recommendations

- **Small batches**: Makes it easier to control rate limiting
- **Monitor queues**: Check `crawl_users`, `crawl_artists`, `crawl_releases`
- **Re-run without fear**: Tables use `ON CONFLICT` to avoid duplicates
- **Environment variables**: Scripts honor `DB_PATH`, `SCHEMA_PATH`, etc.

---

## Monitoring and Tracking

### Interactive Monitoring Script

```bash
scripts/monitor_crawler.sh data/sputnik.db logs/crawler-full.log
```

**Features:**
- Tail the log in real time
- Active crawler processes
- Per-year status (`crawl_state`)
- Database stats (releases, users, interactions)
- Rating distribution
- Top releases by number of votes
- Queue status and latest errors

### Useful Queries

```sql
-- Crawl status by year
SELECT year, status, last_album, note FROM crawl_state ORDER BY year;

-- Pending users
SELECT COUNT(*) FROM crawl_users WHERE status = 'pending';

-- Artists with errors
SELECT id_artist, last_error FROM crawl_artists WHERE status = 'error' LIMIT 10;

-- Rating distribution
SELECT ROUND(rating, 1) as rating, COUNT(*) as count
FROM interactions
WHERE rating > 0
GROUP BY ROUND(rating, 1)
ORDER BY rating;
```

---

## Database Maintenance

### Health Check

Detects and fixes common problems:

```bash
# Diagnose
./scripts/db_health.sh

# JSON output
python maintenance/db_health.py --db data/sputnik.db --format json

# Fix a specific category
./scripts/db_health.sh --fix users.error.timeout --apply

# Fix everything
./scripts/db_health.sh --fix-all --apply
```

**Detected issue categories:**

| Category | Description |
|-----------|-------------|
| `users.error.*` | Users with errors (404, timeout, connection) |
| `users.incomplete` | Profiles missing role or join_date |
| `users.ratings_mismatch` | Inconsistent ratings_count |
| `releases.error.*` | Releases with errors |
| `releases.incomplete` | Incomplete metadata |
| `artists.no_genres` | Artists without assigned genres |

### Optimization

After large ingestions or fixes:

```bash
# VACUUM to defragment
sqlite3 data/sputnik.db "VACUUM;"

# ANALYZE to update planner statistics
sqlite3 data/sputnik.db "ANALYZE;"

# Full analysis + optimization script
python maintenance/analyze_and_vacuum.py
```

See [maintenance/README.en.md](../maintenance/README.en.md) for more details.

---

## Data Schema

### Main Tables

```
users              # User profiles
artists            # Artists
releases           # Albums, EPs, Singles, Compilations
interactions       # User ratings and soundoffs
```

### Crawling Tables

```
crawl_state        # Crawl status per year
crawl_users        # Queue of users to expand
crawl_artists      # Queue of artists to expand
crawl_releases     # Queue of releases to process
```

### Auxiliary Tables

```
genres             # Genre catalog
artist_genres      # Genres per artist
release_genres     # Genres per release
release_tracks     # Tracklists
artist_similars    # Similar artists
release_recommendations  # Precomputed recommendations
release_pairs      # Co-occurrences for recommendations
```

### Embedding Tables

```
user_embeddings       # NMF user embeddings
release_embeddings    # NMF release embeddings
user_embeddings_dl    # Two Towers user embeddings
release_embeddings_dl # Two Towers release embeddings
```

### JSON Views

```
artists_enriched   # Artists with genres and similars in JSON
releases_enriched  # Releases with recommendations and tracklist in JSON
```

### Simplified Diagram

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   artists   │────<│  releases   │────<│interactions │
└─────────────┘     └─────────────┘     └─────────────┘
       │                   │                   │
       │                   │                   │
       ▼                   ▼                   ▼
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│artist_genres│     │release_genres│     │    users    │
└─────────────┘     └──────────────┘     └─────────────┘
```

---

## Ethical Considerations

### Rate Limiting

The system implements rate limiting to avoid overloading Sputnikmusic:

- **Minimum interval**: Configurable via `min_interval`
- **Burst control**: Limit on consecutive requests
- **Exponential backoff**: In case of errors or rate limiting

### Respecting robots.txt

The scraper respects the site directives and avoids protected endpoints.

### Data Usage

- Data is for personal analysis only
- Datasets are not redistributed
- The project is educational only

### Best Practices

1. **Use small batches**: Reduces load on the server
2. **Monitor errors**: Detect and respect rate limiting
3. **Off-peak hours**: Prefer crawling at night
4. **Cache results**: Avoid re-crawling data you already fetched

---

## Troubleshooting

### Error: "database is locked"

SQLite does not support concurrent writes. Fixes:
- Use WAL mode: `PRAGMA journal_mode=WAL;`
- Reduce concurrency in the crawler
- Wait and retry automatically

### Error: Rate limiting (429)

The server is throttling requests:
- Increase `min_interval`
- Reduce `batch_size`
- Wait before retrying

### Users/Releases with status 'error'

Use the health script to diagnose:

```bash
./scripts/db_health.sh --format json | jq '.users.errors'
```

Fix according to the error type:
- **404**: User/release deleted → remove from queue
- **timeout**: Temporary issue → re-queue
- **connection**: Network issue → retry

### Incomplete Data

Check with:

```sql
-- Releases without year
SELECT COUNT(*) FROM releases WHERE release_year IS NULL;

-- Users without role
SELECT COUNT(*) FROM users WHERE role IS NULL;
```

Re-queue to complete:

```bash
./scripts/db_health.sh --fix releases.incomplete --apply
```

---

## References

- **Scraping modules**: `scraper/`
- **Crawling modules**: `crawler/`
- **Utility scripts**: `scripts/`
- **Maintenance**: `maintenance/`
- **SQL schema**: `data/schema.sql`
