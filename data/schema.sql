PRAGMA foreign_keys = ON;

------------------------------------------------------------
-- Tablas principales
------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id_user           TEXT PRIMARY KEY,
    join_date         TEXT,
    last_active       TEXT,
    objectivity_score NUMERIC CHECK (objectivity_score BETWEEN 0 AND 100),
    soundoffs         INTEGER DEFAULT 0 CHECK (soundoffs >= 0),
    ratings_count     INTEGER DEFAULT 0 CHECK (ratings_count >= 0)
);

CREATE TABLE IF NOT EXISTS artists (
    id_artist INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    country   TEXT,
    bio       TEXT
);

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
    rating        NUMERIC NOT NULL CHECK (rating IN (0,0.5,1,1.5,2,2.5,3,3.5,4,4.5,5)),
    rating_date   TEXT,
    soundoff_text TEXT,
    source_url    TEXT,
    PRIMARY KEY (id_release, id_user)
);

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
CREATE INDEX IF NOT EXISTS idx_release_genres_genre     ON release_genres(id_genre);
CREATE INDEX IF NOT EXISTS idx_list_releases_release    ON list_releases(id_release);
CREATE INDEX IF NOT EXISTS idx_staff_reviews_release    ON staff_reviews(id_release);
