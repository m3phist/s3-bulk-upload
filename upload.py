#!/usr/bin/env python3
"""Resumable bounded-batch external-drive -> S3 migration.

Implements the migration proposal: streaming discovery, SQLite journal,
file-level resume, collision protection, bounded batches, checksum
verification. Copy-only: never deletes, renames, or modifies the source,
and never deletes destination objects.

Run with the bundled venv:  ./.venv/bin/python upload.py --help
"""

import argparse
import base64
import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import stat as statmod
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import PurePosixPath

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    import boto3
    from boto3.s3.transfer import TransferConfig
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        EndpointConnectionError,
    )
except ImportError:
    sys.exit(
        "boto3 not found. Run with the bundled venv:\n"
        f"  {SCRIPT_DIR}/.venv/bin/python {sys.argv[0]} ..."
    )

MiB = 1024 * 1024

# macOS / filesystem junk never worth migrating
JUNK_NAMES = {
    ".DS_Store", ".apdisk", ".VolumeIcon.icns",
    ".Trashes", ".Spotlight-V100", ".fseventsd", ".TemporaryItems",
    ".DocumentRevisions-V100", "$RECYCLE.BIN", "System Volume Information",
    "Thumbs.db", "desktop.ini",
}
JUNK_PREFIXES = ("._",)

# Errors that mean the whole run must stop, not just one file
FATAL_S3_CODES = {
    "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch",
    "ExpiredToken", "TokenRefreshRequired", "NoSuchBucket",
    "PermanentRedirect", "301",
}

STATE_DISCOVERED = "discovered"
STATE_UPLOADING = "uploading"
STATE_UPLOADED = "uploaded"
STATE_VERIFIED = "verified"
STATE_FAILED = "failed"

log = logging.getLogger("migrate")


# ---------------------------------------------------------------- config

def load_env(path):
    """Minimal .env parser — KEY=VALUE lines, # comments, no interpolation."""
    values = {}
    if not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Resumable external-drive -> S3 migration (copy-only)."
    )
    p.add_argument("--source", help="source root (e.g. /Volumes/MyDrive); "
                                    "default: SOURCE_DIR from the env file")
    p.add_argument("--prefix", default=None,
                   help="destination key prefix; default: S3_PREFIX from the "
                        "env file, else empty (bucket root)")
    p.add_argument("--env-file", default=os.path.join(SCRIPT_DIR, ".env"))
    p.add_argument("--journal", default=os.path.join(SCRIPT_DIR, "journal.sqlite3"))
    p.add_argument("--batch-files", type=int, default=100,
                   help="stop after this many NEWLY verified files "
                        "(0 = no limit); default 100")
    p.add_argument("--max-workers", type=int, default=4)
    p.add_argument("--multipart-threshold", type=int, default=64,
                   help="MiB; files at/above this use multipart")
    p.add_argument("--multipart-chunk", type=int, default=64, help="MiB per part")
    p.add_argument("--verify", choices=["size", "checksum", "readback"],
                   default="checksum",
                   help="size: remote size only; checksum (default): local "
                        "SHA-256 + S3 checksum validation; readback: also "
                        "download and re-hash every object")
    p.add_argument("--dry-run", action="store_true",
                   help="walk, map keys, estimate — upload nothing, journal nothing")
    p.add_argument("--overwrite", action="store_true",
                   help="allow replacing destination objects this journal did "
                        "not create (default: collision = failure)")
    p.add_argument("--force-source", action="store_true",
                   help="accept a source/bucket/prefix differing from what the "
                        "journal was created with")
    p.add_argument("--report", action="store_true",
                   help="print journal summary and exit (no scan, no upload)")
    return p.parse_args(argv)


# ---------------------------------------------------------------- journal

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    relpath        TEXT PRIMARY KEY,
    s3_key         TEXT NOT NULL,
    size           INTEGER,
    mtime_ns       INTEGER,
    sha256         TEXT,
    state          TEXT NOT NULL,
    attempts       INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    remote_etag    TEXT,
    remote_checksum TEXT,
    verified_at    TEXT,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_state ON files (state);
"""


class Journal:
    """SQLite journal; one writer lock serialises worker threads."""

    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def get_meta(self, key):
        row = self.conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        with self.lock:
            self.conn.execute(
                "INSERT INTO meta(k,v) VALUES(?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
            self.conn.commit()

    def get(self, relpath):
        cur = self.conn.execute(
            "SELECT relpath,s3_key,size,mtime_ns,sha256,state,attempts "
            "FROM files WHERE relpath=?", (relpath,))
        row = cur.fetchone()
        if not row:
            return None
        keys = ("relpath", "s3_key", "size", "mtime_ns", "sha256", "state", "attempts")
        return dict(zip(keys, row))

    def upsert(self, relpath, s3_key, size, mtime_ns, state,
               sha256=None, error=None, remote_etag=None, remote_checksum=None,
               bump_attempts=False, verified=False):
        with self.lock:
            self.conn.execute(
                """INSERT INTO files (relpath,s3_key,size,mtime_ns,sha256,state,
                                      attempts,error,remote_etag,remote_checksum,
                                      verified_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(relpath) DO UPDATE SET
                     s3_key=excluded.s3_key, size=excluded.size,
                     mtime_ns=excluded.mtime_ns,
                     sha256=COALESCE(excluded.sha256, files.sha256),
                     state=excluded.state,
                     attempts=files.attempts + ?,
                     error=excluded.error,
                     remote_etag=COALESCE(excluded.remote_etag, files.remote_etag),
                     remote_checksum=COALESCE(excluded.remote_checksum, files.remote_checksum),
                     verified_at=COALESCE(excluded.verified_at, files.verified_at),
                     updated_at=excluded.updated_at""",
                (relpath, s3_key, size, mtime_ns, sha256, state,
                 1 if bump_attempts else 0, error, remote_etag, remote_checksum,
                 self._now() if verified else None, self._now(),
                 1 if bump_attempts else 0))
            self.conn.commit()

    def counts(self):
        cur = self.conn.execute(
            "SELECT state, COUNT(*), COALESCE(SUM(size),0) FROM files GROUP BY state")
        return {row[0]: {"files": row[1], "bytes": row[2]} for row in cur}

    def failures(self, limit=50):
        cur = self.conn.execute(
            "SELECT relpath, attempts, error FROM files WHERE state=? "
            "ORDER BY updated_at DESC LIMIT ?", (STATE_FAILED, limit))
        return cur.fetchall()


def acquire_lock(journal_path):
    """Single-instance lock next to the journal; held for process lifetime."""
    lock_path = journal_path + ".lock"
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(f"Another instance holds {lock_path} — refusing to run twice "
                 "against the same journal.")
    fh.write(str(os.getpid()))
    fh.flush()
    return fh  # keep the handle alive


# ---------------------------------------------------------------- discovery

def is_junk(name):
    return name in JUNK_NAMES or name.startswith(JUNK_PREFIXES)


def walk_source(root, on_error, self_paths):
    """Depth-first streaming walk. Yields (relpath, size, mtime_ns).

    Never follows symlinks. Junk and this tool's own files are skipped;
    symlinks and unreadable entries are reported through on_error.
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError as exc:
            on_error(os.path.relpath(current, root), f"scandir: {exc}")
            continue
        for entry in entries:
            rel = os.path.relpath(entry.path, root)
            if is_junk(entry.name):
                continue
            if os.path.abspath(entry.path) in self_paths:
                continue
            try:
                if entry.is_symlink():
                    on_error(rel, "symlink: not followed (out of scope)")
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                on_error(rel, f"stat: {exc}")
                continue
            if not statmod.S_ISREG(st.st_mode):
                on_error(rel, f"not a regular file (mode {oct(st.st_mode)})")
                continue
            yield rel, st.st_size, st.st_mtime_ns


def rel_to_key(relpath, prefix):
    posix = PurePosixPath(*relpath.split(os.sep))
    if ".." in posix.parts:
        raise ValueError(f"path escapes source root: {relpath}")
    return f"{prefix}{posix}"


def sha256_file(path, chunk=4 * MiB):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest


# ---------------------------------------------------------------- transfer

class FatalMigrationError(Exception):
    """Systemic failure (auth, bucket, endpoint) — abort the run."""


class Uploader:
    def __init__(self, args, env, journal):
        self.args = args
        self.journal = journal
        self.bucket = env["STORAGE_BUCKET_NAME"]
        endpoint = env.get("STORAGE_HOST", "").strip()
        client_kwargs = {
            "region_name": env["STORAGE_REGION"],
            "aws_access_key_id": env["STORAGE_ACCESS_KEY"],
            "aws_secret_access_key": env["STORAGE_SECRET_KEY"],
            "config": BotoConfig(retries={"max_attempts": 10, "mode": "adaptive"}),
        }
        if endpoint:
            if not endpoint.startswith("http"):
                endpoint = "https://" + endpoint
            client_kwargs["endpoint_url"] = endpoint
        self.client = boto3.client("s3", **client_kwargs)
        self.transfer_config = TransferConfig(
            multipart_threshold=args.multipart_threshold * MiB,
            multipart_chunksize=args.multipart_chunk * MiB,
            max_concurrency=4,
        )
        self.counters = {
            "discovered": 0, "skipped_verified": 0, "verified_new": 0,
            "failed": 0, "collisions": 0, "bytes_uploaded": 0, "walk_errors": 0,
        }
        self.counter_lock = threading.Lock()
        self.stop_event = threading.Event()

    def bump(self, key, amount=1):
        with self.counter_lock:
            self.counters[key] += amount

    # -- preflight -------------------------------------------------

    def preflight(self, source):
        real = os.path.realpath(source)
        if not os.path.isdir(real):
            raise FatalMigrationError(
                f"source not found or not a directory: {source} "
                "(is the drive mounted?)")
        try:
            next(iter(os.scandir(real)))
        except StopIteration:
            raise FatalMigrationError(
                f"source {real} is empty — refusing to treat a missing mount "
                "as an empty drive")
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except (ClientError, BotoCoreError) as exc:
            raise FatalMigrationError(f"cannot access bucket {self.bucket}: {exc}")

        j = self.journal
        prefix = self.args.prefix
        if j.get_meta("migration_id") is None:
            j.set_meta("migration_id", str(uuid.uuid4()))
            j.set_meta("source_root", real)
            j.set_meta("bucket", self.bucket)
            j.set_meta("prefix", prefix)
            j.set_meta("source_dev", str(os.stat(real).st_dev))
            j.set_meta("created_at", datetime.now(timezone.utc).isoformat())
        else:
            recorded = (j.get_meta("source_root"), j.get_meta("bucket"),
                        j.get_meta("prefix"))
            if recorded != (real, self.bucket, prefix) and not self.args.force_source:
                raise FatalMigrationError(
                    "journal identity mismatch —\n"
                    f"  journal: source={recorded[0]} bucket={recorded[1]} "
                    f"prefix={recorded[2]!r}\n"
                    f"  now:     source={real} bucket={self.bucket} "
                    f"prefix={prefix!r}\n"
                    "This journal belongs to a different migration. Use a new "
                    "--journal path, or --force-source if this is intentional.")
        log.info("migration %s  bucket=s3://%s/%s  source=%s",
                 j.get_meta("migration_id"), self.bucket, prefix, real)
        return real

    # -- per-file --------------------------------------------------

    @staticmethod
    def _code(exc):
        if isinstance(exc, ClientError):
            return exc.response.get("Error", {}).get("Code", "")
        return ""

    def _head(self, key):
        try:
            return self.client.head_object(
                Bucket=self.bucket, Key=key, ChecksumMode="ENABLED")
        except ClientError as exc:
            if self._code(exc) in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def process_file(self, source_root, rel, key, size, mtime_ns, record):
        """Upload + verify one file. Returns final state string."""
        j = self.journal
        path = os.path.join(source_root, rel)
        try:
            # Collision policy: an object we did not put there is a failure
            # unless --overwrite. Our own earlier attempt may be replaced.
            our_attempt = record is not None and record["state"] in (
                STATE_UPLOADING, STATE_UPLOADED, STATE_FAILED, STATE_VERIFIED)
            existing = self._head(key)
            if existing is not None and not our_attempt and not self.args.overwrite:
                j.upsert(rel, key, size, mtime_ns, STATE_FAILED,
                         error="collision: destination object already exists "
                               "and was not created by this migration",
                         bump_attempts=True)
                self.bump("collisions")
                self.bump("failed")
                log.error("COLLISION %s (exists remotely; use --overwrite or a "
                          "dedicated --prefix)", key)
                return STATE_FAILED

            local_sha = None
            if self.args.verify in ("checksum", "readback"):
                local_sha = sha256_file(path).hexdigest()

            # Source stability: metadata must match the scan before and after
            st = os.stat(path)
            if (st.st_size, st.st_mtime_ns) != (size, mtime_ns):
                raise RuntimeError("source changed since scan; will retry")

            j.upsert(rel, key, size, mtime_ns, STATE_UPLOADING,
                     sha256=local_sha, bump_attempts=True)
            extra = {"ChecksumAlgorithm": "SHA256"}
            self.client.upload_file(path, self.bucket, key,
                                    ExtraArgs=extra, Config=self.transfer_config)
            j.upsert(rel, key, size, mtime_ns, STATE_UPLOADED, sha256=local_sha)

            st2 = os.stat(path)
            if (st2.st_size, st2.st_mtime_ns) != (size, mtime_ns):
                raise RuntimeError("source changed during upload; will retry")

            state = self._verify(path, rel, key, size, mtime_ns, local_sha)
            if state == STATE_VERIFIED:
                self.bump("verified_new")
                self.bump("bytes_uploaded", size)
                log.info("verified %s (%s bytes)", key, f"{size:,}")
            return state

        except (ClientError, EndpointConnectionError, BotoCoreError) as exc:
            if self._code(exc) in FATAL_S3_CODES:
                self.stop_event.set()
                raise FatalMigrationError(f"systemic S3 failure on {key}: {exc}")
            j.upsert(rel, key, size, mtime_ns, STATE_FAILED,
                     error=str(exc)[:500], bump_attempts=True)
            self.bump("failed")
            log.error("FAILED %s: %s", rel, exc)
            return STATE_FAILED
        except FileNotFoundError:
            # Drive vanished mid-run — systemic, not per-file
            if not os.path.isdir(source_root):
                self.stop_event.set()
                raise FatalMigrationError("source root disappeared (drive "
                                          "disconnected?) — stopping safely")
            j.upsert(rel, key, size, mtime_ns, STATE_FAILED,
                     error="file disappeared", bump_attempts=True)
            self.bump("failed")
            return STATE_FAILED
        except Exception as exc:  # noqa: BLE001 — journal every failure
            j.upsert(rel, key, size, mtime_ns, STATE_FAILED,
                     error=str(exc)[:500], bump_attempts=True)
            self.bump("failed")
            log.error("FAILED %s: %s", rel, exc)
            return STATE_FAILED

    def _verify(self, path, rel, key, size, mtime_ns, local_sha):
        head = self._head(key)
        if head is None or head["ContentLength"] != size:
            got = "missing" if head is None else head["ContentLength"]
            self.journal.upsert(rel, key, size, mtime_ns, STATE_FAILED,
                                error=f"verify: remote size {got} != {size}")
            self.bump("failed")
            return STATE_FAILED

        remote_checksum = head.get("ChecksumSHA256", "")
        if self.args.verify in ("checksum", "readback") and remote_checksum \
                and "-" not in remote_checksum:
            # Single-part upload: S3's stored SHA-256 is the whole-object hash
            expected = base64.b64encode(bytes.fromhex(local_sha)).decode()
            if remote_checksum != expected:
                self.journal.upsert(rel, key, size, mtime_ns, STATE_FAILED,
                                    error="verify: remote SHA-256 mismatch")
                self.bump("failed")
                return STATE_FAILED
        # Multipart objects carry a composite checksum ("...-N"): each part was
        # SHA-256-validated by S3 in transit, but the composite is not the
        # whole-file hash. --verify readback closes that gap.
        if self.args.verify == "readback":
            digest = hashlib.sha256()
            body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
            for block in iter(lambda: body.read(4 * MiB), b""):
                digest.update(block)
            if digest.hexdigest() != local_sha:
                self.journal.upsert(rel, key, size, mtime_ns, STATE_FAILED,
                                    error="verify: read-back SHA-256 mismatch")
                self.bump("failed")
                return STATE_FAILED

        self.journal.upsert(rel, key, size, mtime_ns, STATE_VERIFIED,
                            sha256=local_sha, remote_etag=head.get("ETag"),
                            remote_checksum=remote_checksum or None,
                            verified=True)
        return STATE_VERIFIED


# ---------------------------------------------------------------- main

def print_report(journal):
    counts = journal.counts()
    total_files = sum(v["files"] for v in counts.values())
    print(f"journal: {journal.path}")
    print(f"migration: {journal.get_meta('migration_id')}  "
          f"bucket=s3://{journal.get_meta('bucket')}/{journal.get_meta('prefix') or ''}  "
          f"source={journal.get_meta('source_root')}")
    for state in (STATE_VERIFIED, STATE_UPLOADED, STATE_UPLOADING,
                  STATE_DISCOVERED, STATE_FAILED):
        info = counts.get(state, {"files": 0, "bytes": 0})
        print(f"  {state:>10}: {info['files']:>9,} files  "
              f"{info['bytes'] / MiB / 1024:>10.2f} GiB")
    print(f"  {'total':>10}: {total_files:>9,} files")
    failures = journal.failures()
    if failures:
        print("\nrecent failures (retried automatically on the next run):")
        for rel, attempts, error in failures:
            print(f"  [{attempts}x] {rel}: {error}")


def main(argv=None):
    args = parse_args(argv)
    env = load_env(args.env_file)

    log_path = os.path.join(
        SCRIPT_DIR, f"migrate-{datetime.now():%Y%m%d-%H%M%S}.log")
    handlers = [logging.StreamHandler(sys.stderr)]
    if not args.report:
        handlers.append(logging.FileHandler(log_path))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")

    journal = Journal(args.journal)
    if args.report:
        print_report(journal)
        return 0

    for var in ("STORAGE_REGION", "STORAGE_BUCKET_NAME",
                "STORAGE_ACCESS_KEY", "STORAGE_SECRET_KEY"):
        if not env.get(var):
            sys.exit(f"{var} missing from {args.env_file}")

    source = args.source or env.get("SOURCE_DIR")
    if not source:
        sys.exit("no source: pass --source or set SOURCE_DIR in the env file")
    prefix = args.prefix if args.prefix is not None else env.get("S3_PREFIX", "")
    prefix = prefix.strip("/")
    args.prefix = f"{prefix}/" if prefix else ""

    lock = acquire_lock(args.journal)  # noqa: F841 — held until exit

    uploader = Uploader(args, env, journal)
    try:
        source_root = uploader.preflight(source)
    except FatalMigrationError as exc:
        sys.exit(f"preflight failed: {exc}")

    self_paths = {os.path.abspath(p) for p in (
        args.journal, args.journal + "-wal", args.journal + "-shm",
        args.journal + ".lock", args.env_file, log_path)}

    def on_walk_error(rel, message):
        uploader.bump("walk_errors")
        log.error("WALK %s: %s", rel, message)
        if not args.dry_run:
            journal.upsert(rel, rel_to_key(rel, args.prefix), None, None,
                           STATE_FAILED, error=message)

    batch_limit = args.batch_files
    start = time.monotonic()

    def batch_done():
        return batch_limit > 0 and uploader.counters["verified_new"] >= batch_limit

    if args.dry_run:
        total_bytes = 0
        shown = 0
        for rel, size, mtime_ns in walk_source(source_root, on_walk_error, self_paths):
            uploader.bump("discovered")
            total_bytes += size
            rec = journal.get(rel)
            already = (rec and rec["state"] == STATE_VERIFIED
                       and rec["size"] == size and rec["mtime_ns"] == mtime_ns)
            if shown < 25:
                marker = "skip " if already else "would"
                print(f"  {marker} s3://{uploader.bucket}/{rel_to_key(rel, args.prefix)}"
                      f"  ({size:,} B)")
                shown += 1
            elif shown == 25:
                print("  ... (further mappings elided)")
                shown += 1
        c = uploader.counters
        print(f"\ndry run: {c['discovered']:,} files, "
              f"{total_bytes / MiB / 1024:,.2f} GiB total, "
              f"{c['walk_errors']} walk errors. Nothing uploaded.")
        return 0

    fatal = None
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        futures = set()

        def drain(block):
            nonlocal fatal
            if not futures:
                return
            done, still = wait(futures, return_when=FIRST_COMPLETED) \
                if block else wait(futures, timeout=0)
            futures.clear()
            futures.update(still)
            for fut in done:
                try:
                    fut.result()
                except FatalMigrationError as exc:
                    fatal = fatal or exc

        for rel, size, mtime_ns in walk_source(source_root, on_walk_error, self_paths):
            if uploader.stop_event.is_set() or batch_done():
                break
            uploader.bump("discovered")
            key = rel_to_key(rel, args.prefix)
            rec = journal.get(rel)
            if (rec and rec["state"] == STATE_VERIFIED
                    and rec["size"] == size and rec["mtime_ns"] == mtime_ns):
                uploader.bump("skipped_verified")
                continue
            if rec is None:
                journal.upsert(rel, key, size, mtime_ns, STATE_DISCOVERED)
            # Gate submission on both the concurrency window and the batch
            # budget (verified so far + in flight), so a batch of N never
            # completes more than N new files.
            while (len(futures) >= args.max_workers * 2
                   or (batch_limit > 0 and not batch_done()
                       and uploader.counters["verified_new"] + len(futures)
                       >= batch_limit)):
                drain(block=True)
                if uploader.stop_event.is_set() or batch_done():
                    break
            if uploader.stop_event.is_set() or batch_done():
                break
            futures.add(pool.submit(uploader.process_file, source_root,
                                    rel, key, size, mtime_ns, rec))
        while futures:
            drain(block=True)

    elapsed = time.monotonic() - start
    c = uploader.counters
    summary = {**c, "elapsed_s": round(elapsed, 1),
               "verified_gib_this_run": round(c["bytes_uploaded"] / MiB / 1024, 3)}
    log.info("run summary: %s", json.dumps(summary))
    print()
    print_report(journal)

    if fatal:
        print(f"\nSTOPPED on systemic failure: {fatal}", file=sys.stderr)
        return 2
    if batch_done():
        print(f"\nbatch limit reached ({batch_limit} newly verified). "
              "Run again for the next batch.")
        return 0
    if c["failed"] or c["walk_errors"]:
        print(f"\nscan complete but {c['failed']} failures / "
              f"{c['walk_errors']} walk errors remain — NOT complete. "
              "Re-run to retry; see the failure list above.", file=sys.stderr)
        return 1
    print("\nfull scan finished with no outstanding files. "
          "Run reconcile.py before declaring the migration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
