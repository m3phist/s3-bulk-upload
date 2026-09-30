# s3migrate — external drive → S3, with a queryable registry

Resumable, **copy-only** migration of an external hard drive into S3. It
mirrors the drive's folder structure 1:1 as object keys and records every
file — path, size, SHA-256, status, and final `s3://` URI — in a SQLite
registry you (or a bot) can query later. It never deletes, renames, or
modifies anything, on the drive or in the bucket.

Runs on macOS, Linux, and **Windows** (the 1 TB run targets a Windows PC).

---

## 1. Getting started

**macOS / Linux**

```bash
cd dch-scripts/s3-bulk-upload
make install                  # creates .venv and installs dependencies
cp .env.example .env          # then fill it in (see below)
make test                     # 24 tests should pass
```

**Windows** (PowerShell, Python 3.11+ from python.org)

```powershell
cd dch-scripts\s3-bulk-upload
py -m venv .venv
.\.venv\Scripts\pip install -r requirements-dev.txt
copy .env.example .env        # then fill it in
.\.venv\Scripts\python -m pytest tests -q
```

There is no `make` on Windows — wherever this README says `make <target>`,
run the CLI directly: `.\.venv\Scripts\python -m s3migrate <command>`.

**The `.env` file** (never committed; also never copied into the registry):

```dotenv
STORAGE_HOST=s3.ap-southeast-1.amazonaws.com   # empty is fine for AWS
STORAGE_REGION=ap-southeast-1                  # must match the BUCKET's region
STORAGE_BUCKET_NAME=dch-migration
STORAGE_ACCESS_KEY=...
STORAGE_SECRET_KEY=...
SOURCE_DIR=E:\                                 # Windows drive, or /Volumes/MyDrive on macOS
S3_PREFIX=drive-backup                         # dedicated prefix — strongly recommended
```

Gotcha we already hit once: if the region doesn't match where the bucket
actually lives, every request fails. `aws s3api head-bucket --bucket <name>`
prints the true region.

## 2. Running the migration

```bash
python -m s3migrate dry-run     # 1) see exactly what would happen — uploads NOTHING
python -m s3migrate upload --batch-files 10    # 2) small pilot
python -m s3migrate status                     # 3) check the result
python -m s3migrate upload --batch-files 0     # 4) the full run
python -m s3migrate verify --full --sample-hash 25   # 5) final sign-off
```

(macOS shortcuts: `make dry-run`, `make upload ARGS='--batch-files 0'`,
`make status`, `make verify ARGS='--full --sample-hash 25'`. The `make
upload` target wraps the run in `caffeinate` so the Mac stays awake; on
Windows set Settings → Power → never sleep on AC, or
`powercfg /change standby-timeout-ac 0`.)

Every command takes `--env-file`, `--db` (registry path), `--source`,
`--prefix`, and repeatable `--exclude` patterns (matched against each
relative path or basename, e.g. `--exclude '*.tmp' --exclude .git`).
Upload also takes `--max-workers` (default 4), `--multipart-threshold` /
`--multipart-chunk` (MiB, default 64), `--verify size|checksum|readback`,
`--overwrite`, `--dir-markers`, `--no-scan`.

## 3. What to expect

**dry-run** prints a scan summary and writes `dry-run-manifest.csv` with the
exact destination key for every file:

```
scanned: 43 files (6.8 MiB) in 8 dirs; 43 new, 0 changed, 0 empty dirs,
         0 symlinks, 0 errors, 0 gone from source
top extensions:
          py:     15 files         0.1 MiB
          md:      8 files         0.1 MiB
  ...
  photos/2020/a.jpg  ->  s3://dch-migration/drive-backup/photos/2020/a.jpg
```

**upload** rescans first (cheap — it only stats files), uploads, verifies
each file, then prints where things stand:

```
    verified:        38 files           6.8 MiB
      failed:         0 files           0.0 MiB
     missing:         6 files           0.0 MiB
       total:        44 files           6.8 MiB
this run: 15 verified (6.5 MiB), 0 failed, 0 collisions
batch limit reached (15); run `upload` again for the next batch
```

Things that are *supposed* to happen and are not errors:

- **Interruptions are safe.** Ctrl-C, network drop, sleep, crash, drive
  unplugged — just run `upload` (or `resume`, same thing) again. Verified
  files are never re-sent; an interrupted file restarts from byte 0.
- **`failed` rows are retried automatically** on the next run. A file that
  changes while being read is refused and retried once it's stable.
- **`missing`** means a file the registry knew disappeared from the drive
  (or was newly excluded). It's kept for the audit trail, never uploaded.
- **Collisions stop, not overwrite**: if a destination key already exists
  and this registry didn't put it there, the file is marked failed. Use a
  dedicated `S3_PREFIX`; `--overwrite` only after deciding that's right.
- **Symlinks and empty dirs aren't uploaded** (S3 has no equivalent) —
  they're recorded in the registry; `--dir-markers` can materialise empty
  dirs as zero-byte `path/` objects if you want them visible.

Exit codes: `0` ok · `1` finished but failures/discrepancies remain ·
`2` systemic problem (credentials, bucket, missing drive) · `3` another
instance is already running against this registry.

For 1 TB, expect the wall clock to be set by your uplink: ~22 h at a
sustained 100 Mbps. Run the 10-file pilot, check `status`, and extrapolate
from verified-bytes-per-hour rather than the line rate.

## 4. How batches work

`--batch-files N` means: **stop after N files reach `verified` in this run**
— not N attempts, not N gigabytes. The submission gate counts
in-flight uploads against the budget, so a batch of 100 never completes more
than 100. `--batch-files 0` removes the limit and runs to completion.

Because file sizes are unknown, a batch may take seconds or hours. Failures
don't consume batch budget (they're retried next run), so repeated
`upload` invocations march through the drive batch by batch:

```bash
python -m s3migrate upload            # default batch: 100 newly verified
python -m s3migrate upload            # next 100
...                                   # until: "all files verified"
```

Each run is recorded in `upload_jobs` with its config and counts, and every
individual try lands in `upload_attempts` — so "interrupted" and "verified"
are never confused, and you can audit exactly what happened per file.

## 5. Checking the SQLite registry

The registry (default `registry.sqlite3`, or whatever `--db` you passed) is
a normal SQLite file. Keep it **off** the external drive; back it up
occasionally. The `-wal`/`-shm` sidecar files belong to it — keep them
together. No credentials are ever stored in it.

**Quickest look — the CLI:**

```bash
python -m s3migrate status                        # counts, failures, specials
python -m s3migrate list-files --ext pdf --status verified
python -m s3migrate list-files --name invoice --json
python -m s3migrate list-files --path 'Clients/' --min-size 1000000
```

**sqlite3 shell** (`make sql`, or `sqlite3 registry.sqlite3`):

```sql
SELECT * FROM registry LIMIT 10;                      -- the query surface
SELECT s3_uri, size FROM registry WHERE extension='pdf' AND status='verified';
SELECT status, COUNT(*), SUM(size) FROM files GROUP BY status;
SELECT relpath, error FROM files WHERE status='failed';
SELECT * FROM upload_jobs ORDER BY id DESC LIMIT 5;   -- run history
```

**TablePlus** (or any SQLite GUI): Create a new connection → **SQLite** →
pick the `.sqlite3` file → connect. Browse the `registry` view for
exploration; read while an upload runs is fine, but don't edit rows — the
uploader owns writes.

**Postgres**: `repository.py` is the single SQL boundary — port it to
psycopg when multiple services need concurrent access. Until then,
`make pg-load PG_DSN=postgres://...` mirrors the registry view into an
`s3_registry` table.

Tables behind the view: `sources` (drive identity), `directories`
(hierarchy, empty-dir flags), `files` (one row per file: path, size,
mtime, SHA-256, S3 reference, status), `special_entries` (symlinks etc.),
`upload_jobs`, `upload_attempts`. Schema: `s3migrate/migrations/0001_initial.sql`.
File statuses: `discovered → uploading → uploaded → verified`, plus
`failed` (retryable), `changed` (source modified → re-verify), `missing`
(gone from source).

## 6. What "intact" means here

Regular-file **bytes + exact relative paths, filenames, and case**. S3 keys
are always `/`-separated regardless of OS, and the registry stores portable
paths — a registry begun on one OS resumes on another. Not preserved:
permissions, xattrs, resource forks, timestamps beyond mtime. macOS junk
(`.DS_Store`, `._*`, …) and Windows junk (`$RECYCLE.BIN`, `Thumbs.db`, …)
is excluded automatically.

Verification (`--verify checksum`, the default): a local SHA-256 is recorded
for every file; single-part uploads are compared against S3's stored
whole-object SHA-256; multipart parts are SHA-256-validated in transit and
the composite checksum recorded — composite checksums and multipart ETags
are never treated as whole-file hashes. `--verify readback` re-downloads and
re-hashes everything (strongest, costs egress). `verify --full --sample-hash N`
independently lists the bucket, reconciles both directions, and read-back
hashes a random sample.

## 7. Project layout

```
s3migrate/            the package (python -m s3migrate <command>)
  config.py           .env + settings; credentials never persisted or logged
  db.py               SQLite connection + plain-SQL migrations
  migrations/         numbered schema files
  repository.py       ALL SQL lives here — swap for psycopg to go Postgres
  scanner.py          streaming discovery (no in-memory tree)
  s3io.py             client factory, checksum semantics
  uploader.py         bounded-batch multipart uploads + verification
  verifier.py         HEAD re-verification and full reconciliation
  cli.py, locking.py  subcommands, cross-platform single-instance lock
tests/                24 pytest unit + moto-mocked S3 integration tests
Makefile              mac/Linux convenience targets (`make help`)
```

Before the real 1 TB run: freeze writes to the drive, add an S3 lifecycle
rule aborting incomplete multipart uploads after ~7 days, and keep the drive
until `verify --full` exits 0 **and** a restore test passes.
