"""CLI: scan | dry-run | upload | resume | status | verify | list-files.

Exit codes: 0 success · 1 completed with failures/discrepancies ·
2 systemic/config error · 3 another instance holds the lock.
"""

import argparse
import csv
import json
import logging
import os
import sys
from datetime import datetime

from . import locking
from .config import ConfigError, build_settings
from .repository import Repository
from .scanner import scan
from .uploader import FatalMigrationError, MiB, Uploader
from .verifier import reconcile_full, verify_files

SCRIPT_DIR = os.getcwd()
log = logging.getLogger("s3migrate")


def add_common(p):
    p.add_argument("--env-file", default=".env")
    p.add_argument("--db", default="registry.sqlite3",
                   help="SQLite registry path (default registry.sqlite3; keep "
                        "it OFF the external drive)")
    p.add_argument("--source", help="source root; default SOURCE_DIR from env")
    p.add_argument("--prefix", default=None,
                   help="destination key prefix; default S3_PREFIX from env")
    p.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                   help="fnmatch pattern against relative path or basename; "
                        "matches are skipped (repeatable)")


def add_upload_opts(p):
    p.add_argument("--batch-files", type=int, default=100,
                   help="newly VERIFIED files per run; 0 = unlimited")
    p.add_argument("--max-workers", type=int, default=4)
    p.add_argument("--multipart-threshold", type=int, default=64, metavar="MiB")
    p.add_argument("--multipart-chunk", type=int, default=64, metavar="MiB")
    p.add_argument("--verify", choices=["size", "checksum", "readback"],
                   default="checksum")
    p.add_argument("--overwrite", action="store_true",
                   help="allow replacing destination objects this registry "
                        "did not create")
    p.add_argument("--dir-markers", action="store_true",
                   help="also create zero-byte 'path/' objects for empty dirs")


def parse_args(argv):
    p = argparse.ArgumentParser(prog="s3migrate", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("scan", help="discover the source into the registry")
    add_common(sp)

    sp = sub.add_parser("dry-run", help="scan + print the exact S3 key for "
                                        "every file; uploads nothing")
    add_common(sp)
    sp.add_argument("--manifest", default="dry-run-manifest.csv",
                    help="write the full mapping here")

    for name, help_text in (("upload", "scan, then upload a batch"),
                            ("resume", "alias of upload — the registry makes "
                                       "every run a resume")):
        sp = sub.add_parser(name, help=help_text)
        add_common(sp)
        add_upload_opts(sp)
        sp.add_argument("--no-scan", action="store_true",
                        help="skip the pre-upload rescan (trust the registry)")

    sp = sub.add_parser("status", help="registry summary and recent failures")
    add_common(sp)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("verify", help="re-verify destination objects; "
                                       "--full compares a live bucket listing")
    add_common(sp)
    sp.add_argument("--full", action="store_true")
    sp.add_argument("--sample-hash", type=int, default=0, metavar="N")
    sp.add_argument("--out", default="verify-exceptions.csv")

    sp = sub.add_parser("list-files", help="query the registry")
    add_common(sp)
    sp.add_argument("--name", help="filename substring")
    sp.add_argument("--path", help="relative-path substring")
    sp.add_argument("--ext", help="extension, e.g. pdf")
    sp.add_argument("--status", choices=["discovered", "pending", "uploading",
                                         "uploaded", "verified", "failed",
                                         "changed", "missing"])
    sp.add_argument("--min-size", type=int)
    sp.add_argument("--max-size", type=int)
    sp.add_argument("--limit", type=int, default=100)
    sp.add_argument("--json", action="store_true")

    return p.parse_args(argv)


def setup_logging(command):
    handlers = [logging.StreamHandler(sys.stderr)]
    if command in ("scan", "upload", "resume", "verify", "dry-run"):
        handlers.append(logging.FileHandler(
            f"s3migrate-{datetime.now():%Y%m%d-%H%M%S}.log"))
    logging.basicConfig(
        level=logging.INFO, handlers=handlers,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")


def acquire_lock(db_path):
    fh = locking.acquire(db_path + ".lock")
    if fh is None:
        print(f"another instance holds {db_path}.lock — refusing to run "
              "concurrently against the same registry", file=sys.stderr)
        sys.exit(3)
    return fh


def self_paths(args, settings):
    return {os.path.abspath(p) for p in (
        settings.db_path, settings.db_path + "-wal", settings.db_path + "-shm",
        settings.db_path + ".lock", args.env_file)}


def require_source(settings):
    if not settings.source:
        print("no source: pass --source or set SOURCE_DIR in the env file",
              file=sys.stderr)
        sys.exit(2)
    return os.path.realpath(settings.source)


def print_scan_summary(repo, source_id, counters):
    print(f"scanned: {counters['files']:,} files "
          f"({counters['bytes'] / MiB:,.1f} MiB) in {counters['dirs']:,} dirs; "
          f"{counters['new']:,} new, {counters['changed']:,} changed, "
          f"{counters['empty_dirs']} empty dirs, "
          f"{counters['symlinks']} symlinks, {counters['errors']} errors, "
          f"{counters['missing']} gone from source")
    print("top extensions:")
    for ext, count, size in repo.extension_breakdown(source_id, 12):
        print(f"  {ext:>10}: {count:>6,} files  {size / MiB:>10.1f} MiB")


def print_status(repo, source_id, as_json=False):
    counts = repo.status_counts(source_id)
    if as_json:
        print(json.dumps(counts, indent=2))
        return
    total = {"files": 0, "bytes": 0}
    for state in ("verified", "uploaded", "uploading", "pending", "discovered",
                  "changed", "failed", "missing"):
        info = counts.get(state, {"files": 0, "bytes": 0})
        total["files"] += info["files"]
        total["bytes"] += info["bytes"]
        print(f"  {state:>10}: {info['files']:>9,} files  "
              f"{info['bytes'] / MiB:>12,.1f} MiB")
    print(f"  {'total':>10}: {total['files']:>9,} files  "
          f"{total['bytes'] / MiB:>12,.1f} MiB")
    failures = repo.recent_failures(source_id)
    if failures:
        print("\nrecent failures (retried on the next upload run):")
        for row in failures:
            print(f"  {row['relpath']}: {row['error']}")
    specials = repo.specials(source_id)
    if specials:
        print(f"\nnon-regular entries recorded, not uploaded "
              f"({len(specials)}):")
        for row in specials[:10]:
            print(f"  [{row['kind']}] {row['relpath']}"
                  + (f" -> {row['target']}" if row["target"] else ""))


def main(argv=None):
    # Windows consoles may default to a legacy codepage; the registry holds
    # arbitrary Unicode filenames.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    setup_logging(args.command)
    try:
        settings = build_settings(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    repo = Repository(settings.db_path)
    cmd = args.command

    if cmd == "list-files":
        rows = repo.list_files(
            name_like=args.name, path_like=args.path, extension=args.ext,
            status=args.status, min_size=args.min_size,
            max_size=args.max_size, limit=args.limit)
        if args.json:
            print(json.dumps([dict(r) for r in rows], indent=2,
                             ensure_ascii=False))
        else:
            for r in rows:
                print(f"{r['status']:>10}  {r['size'] or 0:>12,}  "
                      f"{r['s3_uri'] or '-':<60}  {r['relpath']}")
            print(f"({len(rows)} rows)")
        return 0

    if cmd == "status":
        source_id = None
        if settings.source:
            src = repo.conn.execute(
                "SELECT id FROM sources WHERE root=?",
                (os.path.realpath(settings.source),)).fetchone()
            source_id = src["id"] if src else None
        print_status(repo, source_id, args.json)
        return 0

    # everything below touches the source and/or the bucket
    lock = acquire_lock(settings.db_path)  # noqa: F841 — held until exit
    source_root = require_source(settings)

    if cmd == "scan":
        source_id, counters = scan(repo, settings, self_paths(args, settings))
        print_scan_summary(repo, source_id, counters)
        return 1 if counters["errors"] else 0

    if cmd == "dry-run":
        source_id, counters = scan(repo, settings, self_paths(args, settings))
        print_scan_summary(repo, source_id, counters)
        with open(args.manifest, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["relpath", "s3_uri", "size", "status"])
            shown = 0
            for row in repo.list_files(source_id=source_id, limit=10**9):
                writer.writerow([row["relpath"], row["s3_uri"],
                                 row["size"], row["status"]])
                if shown < 20:
                    print(f"  {row['relpath']}  ->  {row['s3_uri']}")
                    shown += 1
        print(f"\nfull manifest: {args.manifest}. Nothing was uploaded.")
        return 0

    if cmd in ("upload", "resume"):
        uploader = Uploader(repo, settings)
        try:
            uploader.preflight()
            if args.no_scan:
                src = repo.get_or_create_source(source_root)
                source_id = src["id"]
            else:
                source_id, counters = scan(repo, settings,
                                           self_paths(args, settings))
                print_scan_summary(repo, source_id, counters)
        except (FatalMigrationError, FileNotFoundError, RuntimeError) as exc:
            print(f"preflight failed: {exc}", file=sys.stderr)
            return 2

        job_id = repo.create_job(source_id, "upload", settings.public_config())
        try:
            fatal, batch_hit = uploader.run(source_id, source_root, job_id)
        except KeyboardInterrupt:
            repo.finish_job(job_id, "interrupted", **_job_counts(uploader))
            print("\ninterrupted — the registry preserved progress; "
                  "run `resume` to continue", file=sys.stderr)
            return 1
        if settings.dir_markers and not fatal:
            made = uploader.upload_dir_markers(source_id)
            print(f"created {made} empty-directory markers")

        c = uploader.counters
        repo.finish_job(job_id, "failed" if fatal else "completed",
                        **_job_counts(uploader))
        print_status(repo, source_id)
        print(f"\nthis run: {c['verified']:,} verified "
              f"({c['bytes'] / MiB:,.1f} MiB), {c['failed']} failed, "
              f"{c['collisions']} collisions")
        if fatal:
            print(f"STOPPED on systemic failure: {fatal}", file=sys.stderr)
            return 2
        if batch_hit:
            print(f"batch limit reached ({settings.batch_files}); run "
                  "`upload` again for the next batch")
            return 0
        if c["failed"]:
            return 1
        remaining = sum(v["files"] for k, v in
                        repo.status_counts(source_id).items()
                        if k not in ("verified", "missing"))
        if remaining == 0:
            print("all files verified — run `verify --full` before sign-off")
        return 0

    if cmd == "verify":
        src = repo.conn.execute("SELECT id FROM sources WHERE root=?",
                                (source_root,)).fetchone()
        if not src:
            print("nothing scanned for this source yet", file=sys.stderr)
            return 2
        source_id = src["id"]
        counters = verify_files(repo, settings, source_id)
        print(f"HEAD-verified: {counters}")
        problems = []
        if args.full:
            full_counters, problems = reconcile_full(
                repo, settings, source_id, args.sample_hash, source_root)
            print(f"full reconcile: {full_counters}")
            with open(args.out, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["issue", "key"])
                writer.writerows(problems)
            if problems:
                print(f"exceptions written to {args.out}", file=sys.stderr)
        bad = counters["mismatch"] + counters["missing"] + len(problems)
        return 1 if bad else 0

    return 2


def _job_counts(uploader):
    c = uploader.counters
    return {"files_verified": c["verified"], "files_failed": c["failed"],
            "bytes_uploaded": c["bytes"]}


if __name__ == "__main__":
    sys.exit(main())
