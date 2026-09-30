-- Registry schema. Plain SQL, portable to PostgreSQL with minor type swaps
-- (INTEGER PRIMARY KEY -> bigserial, TEXT timestamps -> timestamptz).

CREATE TABLE sources (
    id          INTEGER PRIMARY KEY,
    root        TEXT NOT NULL UNIQUE,   -- realpath of the scan root
    volume_dev  TEXT,                   -- st_dev at first scan (drive identity hint)
    label       TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE directories (
    id         INTEGER PRIMARY KEY,
    source_id  INTEGER NOT NULL REFERENCES sources(id),
    relpath    TEXT NOT NULL,           -- '' = the root itself
    parent_id  INTEGER REFERENCES directories(id),
    is_empty   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source_id, relpath)
);
CREATE INDEX idx_directories_parent ON directories (parent_id);

CREATE TABLE files (
    id           INTEGER PRIMARY KEY,
    source_id    INTEGER NOT NULL REFERENCES sources(id),
    directory_id INTEGER REFERENCES directories(id),
    relpath      TEXT NOT NULL,
    filename     TEXT NOT NULL,
    extension    TEXT,                  -- lowercased, without the dot; '' if none
    size         INTEGER,
    mtime_ns     INTEGER,
    sha256       TEXT,
    s3_bucket    TEXT,
    s3_key       TEXT,
    s3_uri       TEXT,
    -- discovered -> pending -> uploading -> uploaded -> verified
    -- failed (retryable) / changed (source modified since last seen)
    status       TEXT NOT NULL DEFAULT 'discovered',
    error        TEXT,
    verified_at  TEXT,
    updated_at   TEXT NOT NULL,
    UNIQUE (source_id, relpath)
);
CREATE INDEX idx_files_status    ON files (status);
CREATE INDEX idx_files_extension ON files (extension);
CREATE INDEX idx_files_filename  ON files (filename);
CREATE INDEX idx_files_s3_key    ON files (s3_key);
CREATE INDEX idx_files_directory ON files (directory_id);
CREATE INDEX idx_files_size      ON files (size);

-- Symlinks, unreadable paths, and other non-regular entries: recorded, never
-- uploaded. S3 representation: none (documented in README).
CREATE TABLE special_entries (
    id        INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    relpath   TEXT NOT NULL,
    kind      TEXT NOT NULL,            -- symlink | error | other
    target    TEXT,                     -- symlink target, if readable
    note      TEXT,
    UNIQUE (source_id, relpath)
);

CREATE TABLE upload_jobs (
    id               INTEGER PRIMARY KEY,
    source_id        INTEGER NOT NULL REFERENCES sources(id),
    kind             TEXT NOT NULL,     -- scan | upload | verify
    config_json      TEXT,              -- public settings only, never credentials
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL DEFAULT 'running', -- running/completed/failed/interrupted
    files_discovered INTEGER NOT NULL DEFAULT 0,
    files_uploaded   INTEGER NOT NULL DEFAULT 0,
    files_verified   INTEGER NOT NULL DEFAULT 0,
    files_failed     INTEGER NOT NULL DEFAULT 0,
    bytes_uploaded   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE upload_attempts (
    id              INTEGER PRIMARY KEY,
    file_id         INTEGER NOT NULL REFERENCES files(id),
    job_id          INTEGER REFERENCES upload_jobs(id),
    attempt         INTEGER NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    outcome         TEXT,               -- verified | failed
    error           TEXT,
    remote_etag     TEXT,
    remote_checksum TEXT
);
CREATE INDEX idx_attempts_file ON upload_attempts (file_id);
CREATE INDEX idx_attempts_job  ON upload_attempts (job_id);

-- Bot/exploration entry point: one row per file with its S3 reference.
CREATE VIEW registry AS
    SELECT f.id, f.relpath, f.filename, f.extension, f.size, f.sha256,
           f.s3_bucket, f.s3_key, f.s3_uri, f.status, f.verified_at,
           d.relpath AS directory, s.root AS source_root
    FROM files f
    LEFT JOIN directories d ON d.id = f.directory_id
    JOIN sources s ON s.id = f.source_id;
