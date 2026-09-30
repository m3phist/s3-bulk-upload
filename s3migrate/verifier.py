"""Destination verification and full source<->S3 reconciliation.

`verify` re-checks registry rows against live HEAD requests; `--full` also
lists the destination prefix to find unexpected or missing objects,
independently of per-file state.
"""

import base64
import hashlib
import logging
import os
import random

from .s3io import head_or_none, make_client, whole_object_sha256
from .scanner import native_path
from .uploader import MiB, sha256_file

log = logging.getLogger("s3migrate.verify")


def verify_files(repo, settings, source_id):
    """HEAD-verify every uploaded/verified file; returns counters."""
    client = make_client(settings)
    counters = {"checked": 0, "ok": 0, "mismatch": 0, "missing": 0}
    for row in repo.iter_by_status(source_id, ("uploaded", "verified")):
        counters["checked"] += 1
        head = head_or_none(client, settings.bucket, row["s3_key"])
        if head is None:
            counters["missing"] += 1
            repo.set_file_status(row["id"], "failed",
                                 error="verify: object missing from destination")
            log.error("missing key=%s", row["s3_key"])
            continue
        if head["ContentLength"] != row["size"]:
            counters["mismatch"] += 1
            repo.set_file_status(
                row["id"], "failed",
                error=f"verify: remote size {head['ContentLength']} != "
                      f"local {row['size']}")
            log.error("size-mismatch key=%s", row["s3_key"])
            continue
        remote = whole_object_sha256(head)
        if row["sha256"] and remote:
            expected = base64.b64encode(bytes.fromhex(row["sha256"])).decode()
            if remote != expected:
                counters["mismatch"] += 1
                repo.set_file_status(row["id"], "failed",
                                     error="verify: remote SHA-256 mismatch")
                log.error("checksum-mismatch key=%s", row["s3_key"])
                continue
        counters["ok"] += 1
        repo.set_file_status(row["id"], "verified", verified=True)
    return counters


def reconcile_full(repo, settings, source_id, sample_hash=0, source_root=None):
    """Compare registry expectations against a live bucket listing.

    Returns (counters, problems) where problems is a list of
    (issue, key) tuples. Unexpected = objects under the prefix that no
    registry row claims (never deleted, only reported).
    """
    client = make_client(settings)
    remote = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=settings.bucket,
                                   Prefix=settings.prefix):
        for obj in page.get("Contents", []):
            remote[obj["Key"]] = obj["Size"]

    expected = {}
    for row in repo.iter_by_status(
            source_id, ("discovered", "pending", "uploading", "uploaded",
                        "verified", "failed", "changed")):
        expected[row["s3_key"]] = row

    problems = []
    for key, row in expected.items():
        if row["status"] != "verified":
            problems.append(("not_verified", key))
        elif key not in remote:
            problems.append(("missing_from_destination", key))
        elif remote[key] != row["size"]:
            problems.append(("size_mismatch", key))
    dir_markers = {f"{settings.prefix}{r['relpath']}/"
                   for r in repo.empty_directories(source_id)}
    for key in remote:
        if key not in expected and key not in dir_markers:
            problems.append(("unexpected_in_destination", key))

    hash_checked = hash_failed = 0
    matched = [k for k, row in expected.items()
               if row["status"] == "verified" and remote.get(k) == row["size"]]
    if sample_hash and matched and source_root:
        for key in random.sample(matched, min(sample_hash, len(matched))):
            hash_checked += 1
            rel = expected[key]["relpath"]
            local = sha256_file(native_path(source_root, rel))
            digest = hashlib.sha256()
            body = client.get_object(Bucket=settings.bucket, Key=key)["Body"]
            for block in iter(lambda: body.read(4 * MiB), b""):
                digest.update(block)
            if digest.hexdigest() != local:
                hash_failed += 1
                problems.append(("readback_hash_mismatch", key))

    counters = {
        "registry_files": len(expected), "remote_objects": len(remote),
        "matched": len(matched), "problems": len(problems),
        "sampled_hashes": hash_checked, "hash_mismatch": hash_failed,
    }
    return counters, problems
