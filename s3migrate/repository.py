"""Repository: the only module that speaks SQL.

Single-process SQLite implementation; a threading.Lock serialises writers.
To move to PostgreSQL for multi-worker access, implement the same public
methods over psycopg (swap ? placeholders for %s, INTEGER PRIMARY KEY for
bigserial) — no caller touches SQL directly.
"""

import json
import threading
from datetime import datetime, timezone

from . import db

UPLOADABLE_STATUSES = ("discovered", "pending", "uploading", "uploaded",
                       "changed", "failed")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Repository:
    def __init__(self, path):
        self.path = path
        self.conn = db.connect(path)
        db.migrate(self.conn)
        self.lock = threading.Lock()
        self._scan_tracking = False

    # -- sources -----------------------------------------------------

    def get_or_create_source(self, root, volume_dev=None, label=None):
        with self.lock:
            self.conn.execute(
                "INSERT INTO sources (root, volume_dev, label, created_at) "
                "VALUES (?,?,?,?) ON CONFLICT(root) DO NOTHING",
                (root, str(volume_dev), label, _now()))
            self.conn.commit()
        return self.conn.execute(
            "SELECT * FROM sources WHERE root=?", (root,)).fetchone()

    # -- directories -------------------------------------------------

    def upsert_directory(self, source_id, relpath, parent_id, is_empty):
        with self.lock:
            self.conn.execute(
                "INSERT INTO directories (source_id, relpath, parent_id, is_empty) "
                "VALUES (?,?,?,?) ON CONFLICT(source_id, relpath) DO UPDATE SET "
                "parent_id=excluded.parent_id, is_empty=excluded.is_empty",
                (source_id, relpath, parent_id, int(is_empty)))
            self.conn.commit()
        return self.conn.execute(
            "SELECT id FROM directories WHERE source_id=? AND relpath=?",
            (source_id, relpath)).fetchone()["id"]

    def mark_directory_empty(self, dir_id, empty=True):
        with self.lock:
            self.conn.execute("UPDATE directories SET is_empty=? WHERE id=?",
                              (int(empty), dir_id))
            self.conn.commit()

    # -- files -------------------------------------------------------

    def upsert_file(self, source_id, directory_id, relpath, filename,
                    extension, size, mtime_ns, s3_bucket, s3_key, s3_uri):
        """Idempotent upsert. Returns (file_id, status, is_new).

        Unchanged files keep their status (verified stays verified); a file
        whose size or mtime moved is re-marked 'changed' for re-upload.
        """
        with self.lock:
            row = self.conn.execute(
                "SELECT id, size, mtime_ns, status FROM files "
                "WHERE source_id=? AND relpath=?",
                (source_id, relpath)).fetchone()
            if row is None:
                cur = self.conn.execute(
                    "INSERT INTO files (source_id, directory_id, relpath, "
                    " filename, extension, size, mtime_ns, s3_bucket, s3_key, "
                    " s3_uri, status, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?, 'discovered', ?)",
                    (source_id, directory_id, relpath, filename, extension,
                     size, mtime_ns, s3_bucket, s3_key, s3_uri, _now()))
                self._note_seen(cur.lastrowid)
                self.conn.commit()
                return cur.lastrowid, "discovered", True
            if row["size"] == size and row["mtime_ns"] == mtime_ns:
                # metadata unchanged — refresh mapping, keep status; a row
                # previously retired as 'missing' has reappeared: re-upload it
                status = "changed" if row["status"] == "missing" else row["status"]
                self.conn.execute(
                    "UPDATE files SET directory_id=?, s3_bucket=?, s3_key=?, "
                    "s3_uri=?, status=?, updated_at=? WHERE id=?",
                    (directory_id, s3_bucket, s3_key, s3_uri, status, _now(),
                     row["id"]))
                self._note_seen(row["id"])
                self.conn.commit()
                return row["id"], status, False
            self.conn.execute(
                "UPDATE files SET directory_id=?, size=?, mtime_ns=?, "
                "sha256=NULL, verified_at=NULL, s3_bucket=?, s3_key=?, "
                "s3_uri=?, status='changed', error=NULL, updated_at=? "
                "WHERE id=?",
                (directory_id, size, mtime_ns, s3_bucket, s3_key, s3_uri,
                 _now(), row["id"]))
            self._note_seen(row["id"])
            self.conn.commit()
            return row["id"], "changed", False

    # -- scan tracking: retire rows that vanish from the source -------

    def _note_seen(self, file_id):
        if self._scan_tracking:
            self.conn.execute(
                "INSERT OR IGNORE INTO temp.scan_seen VALUES (?)", (file_id,))

    def begin_scan_tracking(self):
        with self.lock:
            self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS scan_seen "
                              "(file_id INTEGER PRIMARY KEY)")
            self.conn.execute("DELETE FROM temp.scan_seen")
            self._scan_tracking = True

    def finish_scan_tracking(self, source_id):
        """Mark rows this scan did not see (deleted from the drive, or newly
        excluded) as 'missing' — not uploadable, kept for the audit trail.
        Returns how many were retired."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE files SET status='missing', updated_at=? "
                "WHERE source_id=? AND status != 'missing' "
                "AND id NOT IN (SELECT file_id FROM temp.scan_seen)",
                (_now(), source_id))
            self.conn.commit()
            self._scan_tracking = False
            return cur.rowcount

    def set_file_status(self, file_id, status, error=None, sha256=None,
                        verified=False):
        with self.lock:
            self.conn.execute(
                "UPDATE files SET status=?, error=?, "
                "sha256=COALESCE(?, sha256), "
                "verified_at=CASE WHEN ? THEN ? ELSE verified_at END, "
                "updated_at=? WHERE id=?",
                (status, error, sha256, int(verified), _now(), _now(), file_id))
            self.conn.commit()

    def get_file(self, file_id):
        return self.conn.execute(
            "SELECT * FROM files WHERE id=?", (file_id,)).fetchone()

    def iter_uploadable(self, source_id, chunk=500):
        """Keyset-paginated stream of files needing (re-)upload."""
        return self.iter_by_status(source_id, UPLOADABLE_STATUSES, chunk)

    def iter_by_status(self, source_id, statuses, chunk=500):
        marks = ",".join("?" * len(statuses))
        last_id = 0
        while True:
            with self.lock:  # worker threads write on this connection
                rows = self.conn.execute(
                    f"SELECT * FROM files WHERE source_id=? AND id>? "
                    f"AND status IN ({marks}) ORDER BY id LIMIT ?",
                    (source_id, last_id, *statuses, chunk)).fetchall()
            if not rows:
                return
            yield from rows
            last_id = rows[-1]["id"]

    # -- specials ----------------------------------------------------

    def record_special(self, source_id, relpath, kind, target=None, note=None):
        with self.lock:
            self.conn.execute(
                "INSERT INTO special_entries (source_id, relpath, kind, target, note) "
                "VALUES (?,?,?,?,?) ON CONFLICT(source_id, relpath) DO UPDATE SET "
                "kind=excluded.kind, target=excluded.target, note=excluded.note",
                (source_id, relpath, kind, target, note))
            self.conn.commit()

    # -- jobs & attempts ---------------------------------------------

    def create_job(self, source_id, kind, config):
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO upload_jobs (source_id, kind, config_json, started_at) "
                "VALUES (?,?,?,?)",
                (source_id, kind, json.dumps(config), _now()))
            self.conn.commit()
        return cur.lastrowid

    def finish_job(self, job_id, status, **counters):
        cols = ", ".join(f"{k}=?" for k in counters)
        with self.lock:
            self.conn.execute(
                f"UPDATE upload_jobs SET status=?, finished_at=?"
                f"{', ' + cols if cols else ''} WHERE id=?",
                (status, _now(), *counters.values(), job_id))
            self.conn.commit()

    def start_attempt(self, file_id, job_id):
        with self.lock:
            prior = self.conn.execute(
                "SELECT COALESCE(MAX(attempt),0) FROM upload_attempts "
                "WHERE file_id=?", (file_id,)).fetchone()[0]
            cur = self.conn.execute(
                "INSERT INTO upload_attempts (file_id, job_id, attempt, started_at) "
                "VALUES (?,?,?,?)", (file_id, job_id, prior + 1, _now()))
            self.conn.commit()
        return cur.lastrowid

    def finish_attempt(self, attempt_id, outcome, error=None,
                       remote_etag=None, remote_checksum=None):
        with self.lock:
            self.conn.execute(
                "UPDATE upload_attempts SET finished_at=?, outcome=?, error=?, "
                "remote_etag=?, remote_checksum=? WHERE id=?",
                (_now(), outcome, error, remote_etag, remote_checksum, attempt_id))
            self.conn.commit()

    # -- queries (status / list-files / report) ----------------------

    def status_counts(self, source_id=None):
        where, params = ("WHERE source_id=?", (source_id,)) if source_id else ("", ())
        cur = self.conn.execute(
            f"SELECT status, COUNT(*), COALESCE(SUM(size),0) FROM files "
            f"{where} GROUP BY status", params)
        return {r[0]: {"files": r[1], "bytes": r[2]} for r in cur}

    def extension_breakdown(self, source_id, limit=20):
        return self.conn.execute(
            "SELECT COALESCE(NULLIF(extension,''),'(none)') AS ext, COUNT(*), "
            "COALESCE(SUM(size),0) FROM files WHERE source_id=? "
            "GROUP BY ext ORDER BY 2 DESC LIMIT ?", (source_id, limit)).fetchall()

    def recent_failures(self, source_id, limit=25):
        return self.conn.execute(
            "SELECT relpath, error, updated_at FROM files "
            "WHERE source_id=? AND status='failed' "
            "ORDER BY updated_at DESC LIMIT ?", (source_id, limit)).fetchall()

    def specials(self, source_id):
        return self.conn.execute(
            "SELECT relpath, kind, target, note FROM special_entries "
            "WHERE source_id=? ORDER BY relpath", (source_id,)).fetchall()

    def empty_directories(self, source_id):
        return self.conn.execute(
            "SELECT relpath FROM directories WHERE source_id=? AND is_empty=1 "
            "ORDER BY relpath", (source_id,)).fetchall()

    def list_files(self, source_id=None, name_like=None, path_like=None,
                   extension=None, status=None, min_size=None, max_size=None,
                   limit=100):
        clauses, params = [], []
        if source_id is not None:
            clauses.append("f.source_id=?"); params.append(source_id)
        if name_like:
            clauses.append("f.filename LIKE ?"); params.append(f"%{name_like}%")
        if path_like:
            clauses.append("f.relpath LIKE ?"); params.append(f"%{path_like}%")
        if extension:
            clauses.append("f.extension=?")
            params.append(extension.lower().lstrip("."))
        if status:
            clauses.append("f.status=?"); params.append(status)
        if min_size is not None:
            clauses.append("f.size>=?"); params.append(min_size)
        if max_size is not None:
            clauses.append("f.size<=?"); params.append(max_size)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self.conn.execute(
            f"SELECT f.relpath, f.filename, f.extension, f.size, f.status, "
            f"f.s3_uri, f.sha256, f.verified_at FROM files f {where} "
            f"ORDER BY f.relpath LIMIT ?", params).fetchall()

    def close(self):
        self.conn.close()
