#!/usr/bin/env python3
"""Independent source -> S3 reconciliation for the drive migration.

Walks the (frozen) source, lists the destination prefix, and compares
relative keys and sizes — independently of the upload journal. Optionally
spot-checks whole-file SHA-256 by downloading a sample of objects.

Exit code 0 = manifests match; 1 = discrepancies (see the exceptions CSV).

Run with the bundled venv:  ./.venv/bin/python reconcile.py --help
"""

import argparse
import csv
import hashlib
import json
import os
import random
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from upload import (  # noqa: E402 — shared walker/config keeps the two in lockstep
    MiB, load_env, rel_to_key, sha256_file, walk_source,
)

try:
    import boto3
    from botocore.config import Config as BotoConfig
except ImportError:
    sys.exit("boto3 not found. Run with the bundled venv:\n"
             f"  {SCRIPT_DIR}/.venv/bin/python {sys.argv[0]} ...")


def parse_args(argv):
    p = argparse.ArgumentParser(description="Compare source drive and S3 manifests.")
    p.add_argument("--source", help="source root; default SOURCE_DIR from env file")
    p.add_argument("--prefix", default=None,
                   help="destination prefix; default S3_PREFIX from env file")
    p.add_argument("--env-file", default=os.path.join(SCRIPT_DIR, ".env"))
    p.add_argument("--out", default=os.path.join(SCRIPT_DIR, "reconcile-exceptions.csv"),
                   help="exceptions CSV path")
    p.add_argument("--manifest", default=None,
                   help="also write the full matched manifest to this CSV")
    p.add_argument("--sample-hash", type=int, default=0, metavar="N",
                   help="download N random matched objects and compare "
                        "whole-file SHA-256 against the source")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    env = load_env(args.env_file)
    for var in ("STORAGE_REGION", "STORAGE_BUCKET_NAME",
                "STORAGE_ACCESS_KEY", "STORAGE_SECRET_KEY"):
        if not env.get(var):
            sys.exit(f"{var} missing from {args.env_file}")

    source = args.source or env.get("SOURCE_DIR")
    if not source:
        sys.exit("no source: pass --source or set SOURCE_DIR in the env file")
    source = os.path.realpath(source)
    if not os.path.isdir(source):
        sys.exit(f"source not found: {source} (is the drive mounted?)")

    prefix = args.prefix if args.prefix is not None else env.get("S3_PREFIX", "")
    prefix = prefix.strip("/")
    prefix = f"{prefix}/" if prefix else ""
    bucket = env["STORAGE_BUCKET_NAME"]

    client_kwargs = {
        "region_name": env["STORAGE_REGION"],
        "aws_access_key_id": env["STORAGE_ACCESS_KEY"],
        "aws_secret_access_key": env["STORAGE_SECRET_KEY"],
        "config": BotoConfig(retries={"max_attempts": 10, "mode": "adaptive"}),
    }
    host = env.get("STORAGE_HOST", "").strip()
    if host:
        client_kwargs["endpoint_url"] = host if host.startswith("http") else f"https://{host}"
    client = boto3.client("s3", **client_kwargs)

    # --- source manifest (streaming walk, same exclusions as the uploader)
    walk_errors = []
    local = {}
    for rel, size, _mtime in walk_source(
            source, lambda r, m: walk_errors.append((r, m)), self_paths=set()):
        local[rel_to_key(rel, prefix)] = size
    local_bytes = sum(local.values())
    print(f"source:      {len(local):,} files, {local_bytes / MiB / 1024:,.2f} GiB "
          f"({len(walk_errors)} walk errors)")

    # --- destination manifest
    remote = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            remote[obj["Key"]] = obj["Size"]
    remote_bytes = sum(remote.values())
    print(f"destination: {len(remote):,} objects, "
          f"{remote_bytes / MiB / 1024:,.2f} GiB under s3://{bucket}/{prefix}")

    # --- compare
    missing = sorted(k for k in local if k not in remote)
    unexpected = sorted(k for k in remote if k not in local)
    mismatched = sorted(k for k in local
                        if k in remote and remote[k] != local[k])
    matched = [k for k in local if k in remote and remote[k] == local[k]]

    hash_failures = []
    if args.sample_hash and matched:
        sample = random.sample(matched, min(args.sample_hash, len(matched)))
        print(f"hashing {len(sample)} sampled objects for byte-level comparison...")
        for key in sample:
            rel = key[len(prefix):] if prefix else key
            local_hash = sha256_file(
                os.path.join(source, *rel.split("/"))).hexdigest()
            digest = hashlib.sha256()
            body = client.get_object(Bucket=bucket, Key=key)["Body"]
            for block in iter(lambda: body.read(4 * MiB), b""):
                digest.update(block)
            if digest.hexdigest() != local_hash:
                hash_failures.append(key)
                print(f"  HASH MISMATCH {key}", file=sys.stderr)

    # --- exceptions report
    rows = (
        [("missing_from_destination", k, local[k], "") for k in missing]
        + [("size_mismatch", k, local[k], remote[k]) for k in mismatched]
        + [("unexpected_in_destination", k, "", remote[k]) for k in unexpected]
        + [("readback_hash_mismatch", k, local[k], remote[k]) for k in hash_failures]
        + [("source_walk_error", rel, "", msg) for rel, msg in walk_errors]
    )
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["issue", "key", "source_size", "destination_size_or_error"])
        writer.writerows(rows)

    if args.manifest:
        with open(args.manifest, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["key", "size"])
            for key in sorted(matched):
                writer.writerow([key, local[key]])

    summary = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_files": len(local), "source_bytes": local_bytes,
        "destination_objects": len(remote), "destination_bytes": remote_bytes,
        "matched": len(matched), "missing": len(missing),
        "size_mismatch": len(mismatched), "unexpected": len(unexpected),
        "sampled_hashes": args.sample_hash, "hash_mismatch": len(hash_failures),
        "source_walk_errors": len(walk_errors),
    }
    print(json.dumps(summary, indent=2))

    if missing or mismatched or unexpected or hash_failures or walk_errors:
        print(f"\nDISCREPANCIES FOUND — see {args.out}", file=sys.stderr)
        return 1
    print("\nManifests match: every source file has exactly one destination "
          "object of the same size" +
          (" (sampled hashes verified)." if args.sample_hash else "."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
