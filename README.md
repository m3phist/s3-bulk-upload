# s3migrate — external drive → S3, with a queryable registry

Resumable, journaled, **copy-only** migration of an external hard drive into
S3, preserving the drive's hierarchy 1:1 as object keys, backed by a SQLite
metadata registry that downstream tools (or an AI bot) can query for every
file's S3 reference. Runs on **macOS, Linux, and Windows**.

```
s3migrate/            the package (python -m s3migrate <command>)
  config.py           .env + settings; credentials never persisted or logged
  db.py               SQLite connection + plain-SQL migrations
  migrations/         numbered schema files (0001_initial.sql, ...)
  repository.py       ALL SQL lives here — swap for psycopg to go Postgres
  scanner.py          streaming discovery (no in-memory tree)
  s3io.py             client factory, checksum semantics
  uploader.py         bounded-batch multipart uploads + verification
  verifier.py         HEAD re-verification and full reconciliation
  cli.py, locking.py  subcommands, cross-platform single-instance lock
tests/                pytest unit + moto-mocked S3 integration suite
```

## Scope of "intact"

Regular-file **bytes + exact relative paths, filenames and case**. S3 keys are
always `/`-separated regardless of the scanning OS. Represented explicitly:

- **Empty directories** — recorded in the registry (`directories.is_empty`);
  not materialised in S3 by default, or as zero-byte `path/` marker objects
  with `--dir-markers`.
- **Symlinks** — recorded in `special_entries` with their target; never
  followed, never uploaded (S3 has no symlink concept).
- Permissions/xattrs/timestamps beyond mtime: not preserved (out of scope).
- macOS junk (`.DS_Store`, `._*`, `.Spotlight-V100`…) and Windows junk
  (`$RECYCLE.BIN`, `System Volume Information`, `Thumbs.db`…) are excluded.

Copy-only: the source is never modified; destination objects are never
deleted, and a destination key this registry did not create fails the file
("collision") instead of being overwritten — use a dedicated `S3_PREFIX`, or
`--overwrite` after approval.

## Setup

**macOS / Linux**

```bash
cd dch-scripts/s3-bulk-upload
make install                 # python3 -m venv .venv + deps
cp .env.example .env         # fill in the STORAGE_* values
make test
```

**Windows** (PowerShell; needs Python 3.11+ from python.org)

```powershell
cd dch-scripts\s3-bulk-upload
py -m venv .venv
.\.venv\Scripts\pip install -r requirements-dev.txt
copy .env.example .env       # fill in; SOURCE_DIR=E:\ style paths
.\.venv\Scripts\python -m pytest tests -q
```

On Windows there is no `make`; call the CLI directly with
`.\.venv\Scripts\python -m s3migrate <command>` wherever the docs say
`make <target>`. Everything below is OS-neutral: the registry stores
`/`-separated relative paths, so a registry begun on one OS resumes on
another as long as the drive mounts with the same content.

## CLI

```
python -m s3migrate scan       # discover into the registry; prints counts + extensions
python -m s3migrate dry-run    # scan + full key-mapping manifest CSV; uploads nothing
python -m s3migrate upload     # scan, then upload a batch (default 100 newly verified)
python -m s3migrate resume     # alias of upload — every run resumes
python -m s3migrate status     # per-status counts, failures, symlinks/specials
python -m s3migrate verify     # HEAD-re-verify; --full lists the bucket and reconciles
python -m s3migrate list-files # query: --name --path --ext --status --min-size --json
```

Common flags: `--env-file`, `--db`, `--source`, `--prefix`, `--exclude`
(repeatable fnmatch on relative path or basename). Upload flags:
`--batch-files N` (0 = unlimited), `--max-workers`, `--multipart-threshold`,
`--multipart-chunk` (MiB), `--verify size|checksum|readback`, `--overwrite`,
`--dir-markers`, `--no-scan`.

Exit codes: `0` ok · `1` finished with failures/discrepancies · `2` systemic
(auth, bucket, missing drive, config) · `3` lock held by another instance.

## Batches, resume, verification

- **Batch** = N *newly verified* files per run (proposal semantics), gated so
  a batch of N never completes more than N. Failures don't consume budget and
  are retried on the next run.
- **Resume**: every state transition is committed to SQLite
  (`discovered → uploading → uploaded → verified`, plus `failed` and
  `changed`). Kill the process at any point; `resume` re-checks anything not
  `verified`. An interrupted multipart upload restarts that file from byte 0
  (file-level resume). Add an S3 lifecycle rule aborting incomplete multipart
  uploads after ~7 days.
- **Changed sources**: a rescan re-marks files whose size/mtime moved as
  `changed` (verification cleared, re-uploaded); files are also stat-checked
  immediately before and after their upload.
- **Verification** (`--verify checksum`, default): local SHA-256 recorded for
  every file; single-part uploads compared against S3's stored whole-object
  SHA-256; multipart parts are SHA-256-validated by S3 in transit and the
  composite checksum recorded — multipart ETags/composites are **never**
  treated as whole-file hashes. `readback` re-downloads and re-hashes
  everything (strongest, costs egress). `verify --full --sample-hash N` does
  an independent listing comparison plus sampled read-back hashing.

## The registry (for the exploration bot)

SQLite file (default `registry.sqlite3` — keep it **off** the external drive;
back it up occasionally). Tables: `sources`, `directories`, `files`,
`special_entries`, `upload_jobs`, `upload_attempts` (see
`s3migrate/migrations/0001_initial.sql`). The `registry` view is the stable
query surface:

```sql
SELECT s3_uri, size FROM registry WHERE extension = 'pdf' AND status = 'verified';
SELECT * FROM registry WHERE relpath LIKE 'Clients/%' ORDER BY size DESC LIMIT 20;
```

Programmatic access: `Repository(path).list_files(...)` or
`python -m s3migrate list-files --ext pdf --json`. No credentials are ever
stored in the registry, so it is safe to hand to other tools.

**PostgreSQL**: `repository.py` is the single SQL boundary — port it to
psycopg when multiple workers/services need concurrent access. Until then
`make pg-load PG_DSN=...` mirrors the registry view into a `s3_registry`
table for SQL access from the DCH stack.

## dch-scripts proof of concept

```bash
make poc-scan        # counts, bytes, extension breakdown
make poc-dry-run     # poc-manifest.csv with every destination key
make poc-upload ARGS='--batch-files 0'
make poc-verify      # full reconcile + sampled read-back hashes
make poc-clean       # remove the poc/ prefix + local poc files
```

The POC uses `--db registry-poc.sqlite3` and prefix `poc/dch-scripts/` so it
never mixes with the real drive migration.

## The real 1 TB run

1. Plug in the drive; set `SOURCE_DIR` (`/Volumes/Drive` or `E:\`) and a
   dedicated `S3_PREFIX` in `.env`. Freeze writes to the drive.
2. `python -m s3migrate dry-run` — sanity-check counts and keys.
3. Pilot: `python -m s3migrate upload --batch-files 10`, inspect `status`.
4. Full run: `upload --batch-files 0`. Keep the machine awake:
   macOS `caffeinate -i ...` (the `make upload` target does this);
   Windows: Settings → Power → never sleep on AC (or
   `powercfg /change standby-timeout-ac 0`), and use a powered USB port.
5. Interruptions are safe — `resume` continues; verified files are never
   re-sent. Watch progress with `status`.
6. Sign-off: `upload --batch-files 0` ends clean, then
   `verify --full --sample-hash 25` exits 0. Keep the drive until a restore
   test passes.

Throughput reality check: 1 TB ≈ 22 h at a sustained 100 Mbps; extrapolate
from the pilot's verified-bytes-per-hour, not the line rate.
