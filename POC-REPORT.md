# POC report — dch-scripts → s3://dch-migration/poc/dch-scripts/

**Date:** 2026-09-30 · **Tool:** s3migrate 0.1.0 · **Run from:** macOS (darwin), Python 3.13, boto3 1.43
**Source:** `~/redsquare/dch/dch-scripts` · **Registry:** `registry-poc.sqlite3`
**Bucket:** `dch-migration` (**ap-southeast-1** — note: the bucket was created in
ap-southeast-1, not ap-southeast-5; `.env` was corrected to match)
**Excludes:** `.venv .git __pycache__ .pytest_cache .env *.sqlite3* *.lock s3migrate-*.log *.csv`

All numbers below are read from the registry and live bucket, not estimated.

## Result: PASS

| Check | Result |
|---|---|
| Files verified | **38 / 38** uploadable files (7,131,606 bytes) |
| Failed uploads outstanding | 0 |
| Collisions | 0 |
| HEAD re-verification | 38 checked, 38 ok, 0 mismatch, 0 missing |
| Full listing reconcile (registry ↔ bucket, both directions) | 38 = 38, 0 problems, exceptions CSV empty |
| Sampled read-back SHA-256 | 10 / 10 match |
| `verify --full --sample-hash 10` exit code | 0 |

## Discovery (scan)

43 files initially (6.8 MiB, 8 dirs), 0 scan errors, 0 symlinks, 0 empty dirs.
Top extensions: 15 `py`, 8 `md`, 5 no-extension, 4 `html`, 3 `sh`, 2 `txt`,
2 `sql`, 1 `zip` (6.4 MiB — the largest object). Dry-run manifest with every
destination key: `poc-manifest.csv` (43 rows, spot-checked: nested paths,
dotfile `.clone-db.log`, zero-byte file, exact case preserved).

## Upload jobs (from `upload_jobs`)

| Job | What | Outcome |
|---|---|---|
| 1 | `--batch-files 15` pilot | completed: exactly **15** newly verified (6,769,760 B) — batch gate held |
| 2 | remainder, workers=1 | completed: 23 verified; **1 failed correctly** — a log file that grew mid-upload was refused (“source changed since scan”), demonstrating the stability guard |
| 3 | `resume` | completed: **0 uploads** — all verified files skipped, attempt counts unchanged (max 1 per file) |
| 4 | final sweep after exclude fix | completed: 1 verified (`.clone-db.log`, revived from `missing`) |

## Interruption & recovery (dedicated demo, 800 MiB scratch source)

A hard `SIGTERM` mid-multipart-transfer (crash-equivalent) left the registry
showing exactly the truth: the in-flight file `uploading`, untouched files
`discovered`, the job row `running`, exit 143. `resume` then completed
everything: the killed file re-uploaded from byte 0 (**2** attempts recorded),
the others kept **1** attempt, final reconcile 4 = 4 with 0 problems. The
orphaned multipart parts from the kill were listed and aborted
(`list-multipart-uploads` / `abort-multipart-upload`) — for the 1 TB run, add
the lifecycle rule that auto-aborts incomplete multipart uploads after 7 days.
Demo prefixes were deleted afterwards; only `poc/dch-scripts/` remains.

## Registry consistency

- 39 `verified`-outcome attempt rows for 38 verified files (one file verified,
  changed on disk, and re-verified) — interrupted ≠ verified is preserved.
- 6 rows retired as `missing` (5 stale `.pytest_cache` entries registered
  before that exclude existed, 1 demo artifact). This exposed a real gap fixed
  during the POC: **scan now retires rows that vanish from or become excluded
  from the source** instead of leaving them uploadable (covered by a new test).
- Exploration queries work as intended, e.g.
  `python -m s3migrate list-files --db registry-poc.sqlite3 --ext md --status verified`
  and `--name clone --json` return relpath, size, SHA-256, status, and the
  `s3://` URI per row; the same data is reachable via
  `SELECT * FROM registry` in SQLite or `make pg-load` into Postgres.

## Exceptions

None outstanding. `poc-exceptions.csv` is empty (header only). The one
transient failure (job 2's growing log file) was intentional behaviour and
resolved by the final sweep after correcting the exclude list.

## Go/no-go notes for the 1 TB run

1. Bucket/credentials/region verified working end to end (after two fixes
   worth remembering: the IAM policy update, and the region mismatch).
2. Observed throughput to ap-southeast-1 during the demo: ~65 MiB/s on this
   office line (800 MiB in ~12 s) — extrapolate the drive run from its own
   pilot, since the drive will run on a different machine/network (Windows PC).
3. On the Windows PC: follow README “Windows” setup; the registry created
   there is portable (POSIX relpaths) and never stores credentials.
4. Add the incomplete-multipart lifecycle rule to `dch-migration` before the
   big run.
