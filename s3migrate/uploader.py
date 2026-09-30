"""Bounded-batch uploader: consumes uploadable files from the registry,
uploads with multipart where appropriate, verifies, and records every
attempt. Copy-only — never deletes or renames anything anywhere."""

import base64
import hashlib
import logging
import os
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from botocore.exceptions import BotoCoreError, ClientError

from .s3io import (FATAL_S3_CODES, error_code, head_or_none, make_client,
                   make_transfer_config, whole_object_sha256)
from .scanner import native_path

log = logging.getLogger("s3migrate.upload")
MiB = 1024 * 1024


class FatalMigrationError(Exception):
    """Systemic failure (auth, bucket, endpoint, missing drive)."""


def sha256_file(path, chunk=4 * MiB):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


class Uploader:
    def __init__(self, repo, settings):
        self.repo = repo
        self.settings = settings
        self.client = make_client(settings)
        self.transfer_config = make_transfer_config(settings)
        self.stop_event = threading.Event()
        self.counters = {"verified": 0, "failed": 0, "collisions": 0,
                         "bytes": 0}
        self._lock = threading.Lock()

    def bump(self, key, amount=1):
        with self._lock:
            self.counters[key] += amount

    def preflight(self):
        try:
            self.client.head_bucket(Bucket=self.settings.bucket)
        except (ClientError, BotoCoreError) as exc:
            raise FatalMigrationError(
                f"cannot access bucket {self.settings.bucket}: {exc}")

    # -- one file ----------------------------------------------------

    def process(self, source_root, row, job_id):
        s = self.settings
        repo = self.repo
        rel, key = row["relpath"], row["s3_key"]
        file_id, size, mtime_ns = row["id"], row["size"], row["mtime_ns"]
        path = native_path(source_root, rel)
        attempt_id = repo.start_attempt(file_id, job_id)
        try:
            # Collision policy: an object this registry did not put there is
            # a failure unless --overwrite; our own earlier attempt may be
            # replaced.
            ours = row["status"] in ("uploading", "uploaded", "failed",
                                     "verified", "changed")
            existing = head_or_none(self.client, s.bucket, key)
            if existing is not None and not ours and not s.overwrite:
                msg = ("collision: destination object already exists and was "
                       "not created by this migration")
                repo.set_file_status(file_id, "failed", error=msg)
                repo.finish_attempt(attempt_id, "failed", error=msg)
                self.bump("collisions")
                self.bump("failed")
                log.error("collision key=%s", key)
                return "failed"

            st = os.stat(path)
            if (st.st_size, st.st_mtime_ns) != (size, mtime_ns):
                raise RuntimeError("source changed since scan — re-scan, "
                                   "then re-upload")

            local_sha = None
            if s.verify in ("checksum", "readback"):
                local_sha = sha256_file(path)

            repo.set_file_status(file_id, "uploading", sha256=local_sha)
            self.client.upload_file(
                path, s.bucket, key,
                ExtraArgs={"ChecksumAlgorithm": "SHA256"},
                Config=self.transfer_config)
            repo.set_file_status(file_id, "uploaded", sha256=local_sha)

            st2 = os.stat(path)
            if (st2.st_size, st2.st_mtime_ns) != (size, mtime_ns):
                raise RuntimeError("source changed during upload")

            head = head_or_none(self.client, s.bucket, key)
            outcome, error = self._check(head, size, local_sha, key)
            if outcome != "verified":
                repo.set_file_status(file_id, "failed", error=error)
                repo.finish_attempt(attempt_id, "failed", error=error)
                self.bump("failed")
                log.error("verify-failed key=%s err=%s", key, error)
                return "failed"

            repo.set_file_status(file_id, "verified", sha256=local_sha,
                                 verified=True)
            repo.finish_attempt(
                attempt_id, "verified", remote_etag=head.get("ETag"),
                remote_checksum=head.get("ChecksumSHA256"))
            self.bump("verified")
            self.bump("bytes", size)
            log.info("verified key=%s size=%d", key, size)
            return "verified"

        except (ClientError, BotoCoreError) as exc:
            if error_code(exc) in FATAL_S3_CODES:
                self.stop_event.set()
                repo.finish_attempt(attempt_id, "failed", error=str(exc)[:500])
                raise FatalMigrationError(f"systemic S3 failure on {key}: {exc}")
            self._fail(file_id, attempt_id, str(exc))
            return "failed"
        except FileNotFoundError:
            if not os.path.isdir(source_root):
                self.stop_event.set()
                repo.finish_attempt(attempt_id, "failed",
                                    error="source root disappeared")
                raise FatalMigrationError(
                    "source root disappeared (drive disconnected?) — "
                    "stopping safely")
            self._fail(file_id, attempt_id, "file disappeared from source")
            return "failed"
        except Exception as exc:  # noqa: BLE001 — every failure is journaled
            self._fail(file_id, attempt_id, str(exc))
            return "failed"

    def _fail(self, file_id, attempt_id, message):
        self.repo.set_file_status(file_id, "failed", error=message[:500])
        self.repo.finish_attempt(attempt_id, "failed", error=message[:500])
        self.bump("failed")
        log.error("failed file_id=%s err=%s", file_id, message)

    def _check(self, head, size, local_sha, key):
        """Verification per policy. Multipart ETags/composite checksums are
        never treated as whole-file hashes."""
        if head is None:
            return "failed", "verify: object missing after upload"
        if head["ContentLength"] != size:
            return "failed", (f"verify: remote size {head['ContentLength']} "
                              f"!= local {size}")
        remote = whole_object_sha256(head)
        if local_sha and remote:
            # single-part: S3's stored SHA-256 is the whole-object hash
            expected = base64.b64encode(bytes.fromhex(local_sha)).decode()
            if remote != expected:
                return "failed", "verify: remote SHA-256 mismatch"
        # multipart: parts were SHA-256-validated in transit; composite
        # checksum recorded, whole-file equality provable via readback
        if self.settings.verify == "readback" and local_sha:
            digest = hashlib.sha256()
            body = self.client.get_object(Bucket=self.settings.bucket,
                                          Key=key)["Body"]
            for block in iter(lambda: body.read(4 * MiB), b""):
                digest.update(block)
            if digest.hexdigest() != local_sha:
                return "failed", "verify: read-back SHA-256 mismatch"
        return "verified", None

    # -- the run -----------------------------------------------------

    def run(self, source_id, source_root, job_id):
        """Upload everything uploadable, bounded by settings.batch_files.
        Returns (fatal_error_or_None, batch_hit)."""
        s = self.settings
        batch = s.batch_files
        fatal = None

        def batch_done():
            return batch > 0 and self.counters["verified"] >= batch

        with ThreadPoolExecutor(max_workers=s.max_workers) as pool:
            futures = set()

            def drain():
                nonlocal fatal
                if not futures:
                    return
                done, still = wait(futures, return_when=FIRST_COMPLETED)
                futures.clear()
                futures.update(still)
                for fut in done:
                    try:
                        fut.result()
                    except FatalMigrationError as exc:
                        fatal = fatal or exc

            for row in self.repo.iter_uploadable(source_id):
                if self.stop_event.is_set() or batch_done():
                    break
                # gate on both the concurrency window and the batch budget
                while (len(futures) >= s.max_workers * 2
                       or (batch > 0 and not batch_done()
                           and self.counters["verified"] + len(futures) >= batch)):
                    drain()
                    if self.stop_event.is_set() or batch_done():
                        break
                if self.stop_event.is_set() or batch_done():
                    break
                futures.add(pool.submit(self.process, source_root, row, job_id))
            while futures:
                drain()

        return fatal, batch_done()

    def upload_dir_markers(self, source_id):
        """Optional: materialise empty directories as zero-byte 'key/' objects."""
        count = 0
        for row in self.repo.empty_directories(source_id):
            key = f"{self.settings.prefix}{row['relpath']}/"
            self.client.put_object(Bucket=self.settings.bucket, Key=key, Body=b"")
            log.info("dir-marker key=%s", key)
            count += 1
        return count
