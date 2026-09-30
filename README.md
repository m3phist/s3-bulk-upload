# External drive → S3 migration

Resumable, journaled, copy-only migration of an external hard drive into S3,
preserving the drive's file hierarchy 1:1 as object keys. Implements
`s3_external_drive_migration_proposal.md`.

- `upload.py` — bounded-batch uploader with SQLite journal, collision
  protection, checksum verification, dry-run, single-instance lock.
- `reconcile.py` — independent source↔destination manifest comparison
  (does not trust the journal).
- `.env` — the five `STORAGE_*` settings plus optional `SOURCE_DIR`/`S3_PREFIX`
  defaults. Gitignored; template in `.env.example`.
- `journal.sqlite3` — upload journal (WAL mode). Lives here, **not on the
  drive**. Gitignored. Back it up occasionally (`cp journal.sqlite3 ~/...`).

Everything runs from the bundled venv: `./.venv/bin/python`.
(Re-create it with `python3 -m venv .venv && ./.venv/bin/pip install boto3`.)

## Guarantees and scope

Copy-only: the source is never modified, destination objects are never
deleted. "Intact" means **regular-file bytes + exact relative paths**. Empty
directories, symlinks (reported, not followed), permissions, timestamps,
xattrs, and resource forks are *not* preserved — per the proposal, out of
scope unless separately requested. macOS junk (`.DS_Store`, `._*`,
`.Spotlight-V100`, …) is excluded on both sides.

## Journal states

`discovered → uploading → uploaded → verified`, or `failed` (with error text,
retried on the next run). A file is skipped on later runs only when it is
`verified` **and** its size + mtime are unchanged. Unfinished
`uploading`/`uploaded` entries are re-processed. The journal records the
migration ID, source root, bucket, and prefix; running against a different
source/bucket/prefix aborts unless `--force-source` (or use a new `--journal`).

## Phase 1 — pilot

```bash
cd ~/redsquare/dch/dch-scripts/s3-bulk-upload
# 1. plug in drive; set SOURCE_DIR (and a dedicated S3_PREFIX) in .env
./.venv/bin/python upload.py --dry-run                  # mapping + size estimate
./.venv/bin/python upload.py --batch-files 10           # 10-file pilot
./.venv/bin/python upload.py --report                   # inspect journal
aws s3 ls s3://<bucket>/<prefix>/ --recursive | head    # eyeball keys
```

Spot-check nested paths, Unicode names, spaces, zero-byte files, and one
file ≥ 64 MiB (multipart).

## Phase 2 — controlled migration

```bash
caffeinate -i ./.venv/bin/python upload.py --batch-files 0   # run to completion
# or repeated bounded batches (default 100 newly verified files per run):
caffeinate -i ./.venv/bin/python upload.py
```

- Interrupt anytime (Ctrl-C, network drop, drive unplug) — re-running resumes.
  File-level resume: an interrupted large file restarts from byte 0.
- A second concurrent instance against the same journal is refused (flock).
- Systemic failures (bad credentials, missing bucket, drive disconnected)
  stop the run; per-file failures are journaled and retried next run.
- Batch = *newly verified files*, not attempts or bytes (proposal §5).

Defaults: 4 workers, 64 MiB multipart threshold/parts,
`--verify checksum` — local SHA-256 journaled for every file; single-part
uploads compared against S3's stored SHA-256; multipart uploads are
part-level SHA-256-validated by S3 in transit (composite checksum recorded).
`--verify readback` additionally downloads and re-hashes every object
(strongest, costs egress). `--verify size` is sizes only.

Collision policy (proposal §6): if a destination key already exists and this
journal didn't create it, the file is marked `failed` — never silently
overwritten. Use a dedicated `S3_PREFIX`, or `--overwrite` after approval.

## Phase 3 — reconciliation & sign-off

Freeze writes to the drive, then:

```bash
./.venv/bin/python upload.py --batch-files 0        # final sweep: must end clean
./.venv/bin/python reconcile.py --manifest manifest-final.csv --sample-hash 25
```

`reconcile.py` walks the source and lists the bucket independently, compares
keys + sizes both directions (missing / size-mismatch / unexpected), optionally
read-back-hashes a random sample, writes `reconcile-exceptions.csv`, and exits
non-zero on any discrepancy. Sign off only on exit 0; keep the drive until a
restore test passes.

## Recovery

| Problem | Action |
|---|---|
| Run interrupted | Just re-run — journal resumes |
| Drive letter/mount changed | Re-mount at the same path, or `--force-source` after checking it's the same drive |
| Journal lost/corrupt | Restore your backup copy, or start a fresh journal — `verified` state rebuilds naturally (existing identical objects will report as collisions; verify with `reconcile.py`, then `--overwrite` only for true mismatches) |
| Stale multipart parts | `aws s3api list-multipart-uploads --bucket <b>`; abort old ones, or add a lifecycle rule aborting incomplete uploads after 7 days |
| Credentials expired | Fix `.env`, re-run |
