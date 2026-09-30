"""SQLite connection + plain-SQL migrations.

Migrations are numbered .sql files in s3migrate/migrations/, applied in order
and recorded in schema_migrations — the same mechanism ports directly to
PostgreSQL when concurrent writers are needed.
"""

import os
import sqlite3

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "migrations")


def connect(path):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def migrate(conn):
    """Apply outstanding migrations; returns the list applied this call."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                 "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    applied = {row[0] for row in
               conn.execute("SELECT version FROM schema_migrations")}
    new = []
    for name in sorted(os.listdir(MIGRATIONS_DIR)):
        if not name.endswith(".sql") or name in applied:
            continue
        with open(os.path.join(MIGRATIONS_DIR, name), encoding="utf-8") as fh:
            conn.executescript(fh.read())
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?, datetime('now'))", (name,))
        conn.commit()
        new.append(name)
    return new
