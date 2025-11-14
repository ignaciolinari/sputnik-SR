PRAGMA foreign_keys = ON;

------------------------------------------------------------
-- Tablas principales
------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id_user           TEXT PRIMARY KEY,
    role              TEXT,
    join_date         TEXT,
    last_active       TEXT,
    objectivity_score NUMERIC CHECK (objectivity_score BETWEEN 0 AND 100),
    soundoffs         INTEGER DEFAULT 0 CHECK (soundoffs >= 0),
    ratings_count     INTEGER DEFAULT 0 CHECK (ratings_count >= 0),
    member_id         TEXT
);

CREATE TABLE IF NOT EXISTS artists (
    id_artist INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    country   TEXT,
    bio       TEXT
);

CREATE INDEX IF NOT EXISTS idx_artists_name ON artists(name);

CREATE TABLE IF NOT EXISTS releases (
    id_release    INTEGER PRIMARY KEY,
    title         TEXT NOT NULL,
    artist_id     INTEGER NOT NULL REFERENCES artists(id_artist) ON UPDATE CASCADE ON DELETE CASCADE,
    release_type  TEXT NOT NULL CHECK (release_type IN ('LP','EP','Single','Compilation')),
    release_year  INTEGER CHECK (release_year BETWEEN 1900 AND 2100),
    label         TEXT,
    art_url       TEXT,
    avg_rating    NUMERIC CHECK (avg_rating BETWEEN 0 AND 5),
    ratings_count INTEGER DEFAULT 0 CHECK (ratings_count >= 0),
    staff_avg     NUMERIC CHECK (staff_avg BETWEEN 0 AND 5),
    review_count  INTEGER DEFAULT 0 CHECK (review_count >= 0)
);

CREATE TABLE IF NOT EXISTS interactions (
    id_release    INTEGER NOT NULL REFERENCES releases(id_release) ON UPDATE CASCADE ON DELETE CASCADE,
    id_user       TEXT    NOT NULL REFERENCES users(id_user)      ON UPDATE CASCADE ON DELETE CASCADE,
    rating        NUMERIC NOT NULL CHECK (rating >= 0 AND rating <= 5),
    rating_date   TEXT,
    soundoff_text TEXT,
    source_url    TEXT,
    PRIMARY KEY (id_release, id_user)
);

CREATE TABLE IF NOT EXISTS crawl_users (
    id_user     TEXT PRIMARY KEY REFERENCES users(id_user) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','done','error')),
    priority    INTEGER NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error  TEXT,
    last_crawled TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl_releases (
    id_release   INTEGER PRIMARY KEY REFERENCES releases(id_release) ON DELETE CASCADE,
    status       TEXT NOT NULL CHECK (status IN ('seeded','pending','processing','done','error')),
    attempts     INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error   TEXT,
    last_crawled TEXT,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl_artists (
    id_artist    INTEGER PRIMARY KEY REFERENCES artists(id_artist) ON DELETE CASCADE,
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','done','error')),
    attempts     INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error   TEXT,
    last_crawled TEXT,
    updated_at   TEXT NOT NULL
);

------------------------------------------------------------
-- Triggers to keep work queues in sync
------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS trg_users_enqueue
AFTER INSERT ON users
BEGIN
    INSERT INTO crawl_users (id_user, status, priority, attempts, last_error, last_crawled, updated_at)
    VALUES (NEW.id_user, 'pending', 0, 0, NULL, NULL, datetime('now'))
    ON CONFLICT(id_user) DO UPDATE SET updated_at = datetime('now');
END;

CREATE TRIGGER IF NOT EXISTS trg_releases_enqueue
AFTER INSERT ON releases
BEGIN
    INSERT INTO crawl_releases (id_release, status, attempts, last_error, last_crawled, updated_at)
    VALUES (NEW.id_release, 'seeded', 0, NULL, NULL, datetime('now'))
    ON CONFLICT(id_release) DO NOTHING;
END;

CREATE TRIGGER IF NOT EXISTS trg_artists_enqueue
AFTER INSERT ON artists
BEGIN
    INSERT INTO crawl_artists (id_artist, status, attempts, last_error, last_crawled, updated_at)
    VALUES (NEW.id_artist, 'pending', 0, NULL, NULL, datetime('now'))
    ON CONFLICT(id_artist) DO UPDATE SET updated_at = datetime('now');
END;

------------------------------------------------------------
-- Auxiliares para features content-based
------------------------------------------------------------
CREATE TABLE IF NOT EXISTS genres (
    id_genre INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS artist_genres (
    id_artist INTEGER NOT NULL REFERENCES artists(id_artist) ON DELETE CASCADE,
    id_genre  INTEGER NOT NULL REFERENCES genres(id_genre)   ON DELETE CASCADE,
    PRIMARY KEY (id_artist, id_genre)
);

CREATE TABLE IF NOT EXISTS release_genres (
    id_release INTEGER NOT NULL REFERENCES releases(id_release) ON DELETE CASCADE,
    id_genre   INTEGER NOT NULL REFERENCES genres(id_genre)     ON DELETE CASCADE,
    PRIMARY KEY (id_release, id_genre)
);

CREATE TABLE IF NOT EXISTS artist_similars (
    id_artist         INTEGER NOT NULL REFERENCES artists(id_artist) ON DELETE CASCADE,
    similar_artist_id INTEGER NOT NULL REFERENCES artists(id_artist) ON DELETE CASCADE,
    PRIMARY KEY (id_artist, similar_artist_id),
    CHECK (id_artist <> similar_artist_id)
);

CREATE TABLE IF NOT EXISTS release_recommendations (
    release_id             INTEGER NOT NULL REFERENCES releases(id_release) ON DELETE CASCADE,
    recommended_release_id INTEGER NOT NULL REFERENCES releases(id_release) ON DELETE CASCADE,
    PRIMARY KEY (release_id, recommended_release_id),
    CHECK (release_id <> recommended_release_id)
);

CREATE TABLE IF NOT EXISTS release_pairs (
    id_release_1  INTEGER NOT NULL REFERENCES releases(id_release) ON DELETE CASCADE,
    id_release_2  INTEGER NOT NULL REFERENCES releases(id_release) ON DELETE CASCADE,
    pair_count    INTEGER NOT NULL CHECK (pair_count >= 0),
    jaccard       REAL,
    lift          REAL,
    last_built_at TEXT NOT NULL,
    PRIMARY KEY (id_release_1, id_release_2),
    CHECK (id_release_1 <> id_release_2)
);

CREATE TABLE IF NOT EXISTS user_embeddings (
    id_user       TEXT PRIMARY KEY REFERENCES users(id_user) ON DELETE CASCADE,
    embedding_json TEXT NOT NULL,
    n_factors     INTEGER NOT NULL CHECK (n_factors > 0),
    last_updated  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS release_embeddings (
    id_release    INTEGER PRIMARY KEY REFERENCES releases(id_release) ON DELETE CASCADE,
    embedding_json TEXT NOT NULL,
    n_factors     INTEGER NOT NULL CHECK (n_factors > 0),
    last_updated  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_embeddings_dl (
    id_user       TEXT PRIMARY KEY REFERENCES users(id_user) ON DELETE CASCADE,
    embedding_json TEXT NOT NULL,
    embedding_dim INTEGER NOT NULL CHECK (embedding_dim > 0),
    model_version TEXT NOT NULL,
    last_updated  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS release_embeddings_dl (
    id_release    INTEGER PRIMARY KEY REFERENCES releases(id_release) ON DELETE CASCADE,
    embedding_json TEXT NOT NULL,
    embedding_dim INTEGER NOT NULL CHECK (embedding_dim > 0),
    model_version TEXT NOT NULL,
    last_updated  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS release_tracks (
    id_release       INTEGER NOT NULL REFERENCES releases(id_release) ON DELETE CASCADE,
    track_position   INTEGER NOT NULL CHECK (track_position > 0),
    track_title      TEXT    NOT NULL,
    duration_seconds INTEGER CHECK (duration_seconds >= 0),
    PRIMARY KEY (id_release, track_position)
);

CREATE TABLE IF NOT EXISTS lists (
    id_list        INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id    INTEGER UNIQUE,
    owner_user_id  TEXT REFERENCES users(id_user) ON UPDATE CASCADE,
    title          TEXT NOT NULL,
    description    TEXT,
    list_url       TEXT,
    created_at     TEXT
);

CREATE TABLE IF NOT EXISTS list_releases (
    id_list    INTEGER NOT NULL REFERENCES lists(id_list)    ON DELETE CASCADE,
    id_release INTEGER NOT NULL REFERENCES releases(id_release) ON DELETE CASCADE,
    rank       INTEGER,
    PRIMARY KEY (id_list, id_release)
);

CREATE TABLE IF NOT EXISTS staff_reviews (
    id_review    INTEGER PRIMARY KEY AUTOINCREMENT,
    id_release   INTEGER NOT NULL REFERENCES releases(id_release) ON DELETE CASCADE,
    reviewer_id  TEXT,
    reviewer_type TEXT CHECK (reviewer_type IN ('staff','contributor')),
    review_url   TEXT NOT NULL,
    published_at TEXT,
    rating       NUMERIC CHECK (rating IN (0,0.5,1,1.5,2,2.5,3,3.5,4,4.5,5))
);

CREATE TABLE IF NOT EXISTS release_credits (
    id_release  INTEGER NOT NULL REFERENCES releases(id_release) ON DELETE CASCADE,
    credit_name TEXT    NOT NULL,
    credit_role TEXT    NOT NULL,
    PRIMARY KEY (id_release, credit_name, credit_role)
);

------------------------------------------------------------
-- Vistas con JSON (extensión JSON1)
------------------------------------------------------------
CREATE VIEW IF NOT EXISTS artists_enriched AS
SELECT
    a.id_artist,
    a.name,
    a.country,
    COALESCE((
        SELECT json_group_array(name)
        FROM (
            SELECT DISTINCT g.name
            FROM artist_genres ag
            JOIN genres g ON g.id_genre = ag.id_genre
            WHERE ag.id_artist = a.id_artist
            ORDER BY g.name
        )
    ), json('[]')) AS genre_tags,
    a.bio,
    COALESCE((
        SELECT json_group_array(similar_artist_id)
        FROM (
            SELECT DISTINCT similar_artist_id
            FROM artist_similars s
            WHERE s.id_artist = a.id_artist
            ORDER BY similar_artist_id
        )
    ), json('[]')) AS similar_artists
FROM artists a;

CREATE VIEW IF NOT EXISTS releases_enriched AS
SELECT
    r.id_release,
    r.title,
    r.artist_id,
    r.release_type,
    r.release_year,
    r.label,
    r.art_url,
    r.avg_rating,
    r.ratings_count,
    r.staff_avg,
    r.review_count,
    COALESCE((
        SELECT json_group_array(recommended_release_id)
        FROM (
            SELECT DISTINCT recommended_release_id
            FROM release_recommendations rr
            WHERE rr.release_id = r.id_release
            ORDER BY recommended_release_id
        )
    ), json('[]')) AS recommended_ids,
    COALESCE((
        SELECT json_group_array(
                   json_object(
                       'position', track_position,
                       'title',    track_title,
                       'duration_seconds', duration_seconds
                   )
               )
        FROM (
            SELECT track_position, track_title, duration_seconds
            FROM release_tracks t
            WHERE t.id_release = r.id_release
            ORDER BY track_position
        )
    ), json('[]')) AS tracklist
FROM releases r;

------------------------------------------------------------
-- Índices sugeridos
------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_interactions_user        ON interactions(id_user);
CREATE INDEX IF NOT EXISTS idx_interactions_rating_date ON interactions(rating_date);
CREATE INDEX IF NOT EXISTS idx_interactions_release     ON interactions(id_release);
CREATE INDEX IF NOT EXISTS idx_interactions_user_release ON interactions(id_user, id_release);
CREATE INDEX IF NOT EXISTS idx_release_genres_genre     ON release_genres(id_genre);
CREATE INDEX IF NOT EXISTS idx_list_releases_release    ON list_releases(id_release);
CREATE INDEX IF NOT EXISTS idx_staff_reviews_release    ON staff_reviews(id_release);
CREATE INDEX IF NOT EXISTS idx_release_pairs_r1         ON release_pairs(id_release_1);

-- Performance indexes for frequently queried columns
CREATE INDEX IF NOT EXISTS idx_releases_artist_id ON releases(artist_id);
CREATE INDEX IF NOT EXISTS idx_interactions_rating ON interactions(rating);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);

-- Additional useful indexes
CREATE INDEX IF NOT EXISTS idx_releases_release_year ON releases(release_year);
CREATE INDEX IF NOT EXISTS idx_interactions_user_rating ON interactions(id_user, rating);
