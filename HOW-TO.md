# How to use s3migrate

Practical recipes. The README covers concepts and reference; this is the
"what do I type" guide. Every command below is shown in both forms:

- **macOS/Linux:** `./.venv/bin/python -m s3migrate …` (or the `make` shortcuts)
- **Windows (PowerShell):** `.\.venv\Scripts\python -m s3migrate …`

One-time setup is in README §1 (venv + dependencies + `.env`).

---

## 1. Using an external drive as the source

**macOS** — the drive mounts under `/Volumes/<name>`:

```bash
ls /Volumes                        # find the exact mount name
# .env:  SOURCE_DIR=/Volumes/MyDrive
./.venv/bin/python -m s3migrate dry-run
./.venv/bin/python -m s3migrate upload --batch-files 10     # pilot
caffeinate -i ./.venv/bin/python -m s3migrate upload --batch-files 0
```

**Windows** — the drive gets a letter (check File Explorer or `Get-Volume`):

```powershell
# .env:  SOURCE_DIR=E:\        (a subfolder like E:\photos works too)
.\.venv\Scripts\python -m s3migrate dry-run
.\.venv\Scripts\python -m s3migrate upload --batch-files 10   # pilot
.\.venv\Scripts\python -m s3migrate upload --batch-files 0
```

Rules that matter for drives:

- **Keep the registry OFF the drive.** The default (`registry.sqlite3` in
  this folder, on the internal disk) is correct. If the drive disconnects
  mid-write you lose nothing but the in-flight file.
- **Keep the machine awake**: macOS `caffeinate -i …`; Windows
  Settings → Power → never sleep on AC (or
  `powercfg /change standby-timeout-ac 0`). Use a powered/rear USB port.
- **Freeze writes to the drive** for the migration window. Files that
  change mid-upload are detected and refused, but a quiet drive finishes
  in one pass instead of chasing its tail.
- If the drive later mounts under a **different letter/name**, just point
  `--source` (or `SOURCE_DIR`) at the new mount — file identity is the
  relative path, so nothing re-uploads as long as the content is the same.

## 2. Using a local folder as the source

Identical — the source is just a directory:

```bash
./.venv/bin/python -m s3migrate upload \
    --source ~/Projects/archive --prefix archive-2026 --batch-files 0
```

```powershell
.\.venv\Scripts\python -m s3migrate upload `
    --source C:\Users\me\Projects\archive --prefix archive-2026 --batch-files 0
```

Give each distinct source its **own `--db` and its own `--prefix`** (see §3).
`--exclude` keeps noise out: `--exclude .git --exclude node_modules
--exclude '*.tmp'` (patterns match any folder/file name or relative path).

## 3. Parallel runs — what's supported

**Within one run: yes, parallelism is built in.** `--max-workers N`
(default 4) uploads N files concurrently, and each large file additionally
sends up to 4 multipart chunks in parallel. For a big migration, tune
workers first — an external spinning drive often does best at 4–8; going
higher mostly adds seek thrash.

**Two processes on the SAME registry: refused, by design.** The second
process exits immediately with code 3 ("another instance holds the lock").
This is what makes crash/resume bookkeeping trustworthy.

**Two processes on DIFFERENT migrations: fine.** Each needs its own
`--db`, its own source, and its own `--prefix`:

```bash
# terminal 1
python -m s3migrate upload --db reg-drive-a.sqlite3 --source /Volumes/DriveA --prefix drive-a --batch-files 0
# terminal 2
python -m s3migrate upload --db reg-drive-b.sqlite3 --source /Volumes/DriveB --prefix drive-b --batch-files 0
```

**Don't** point two registries at the same source+prefix to "go faster" —
each registry treats the other's uploads as foreign objects and reports
collisions instead of overwriting them (safe, but all you get is noise).
To go faster on one drive, raise `--max-workers`; the bottleneck is almost
always the drive or the uplink, not the process count.

## 4. If the computer crashes (or sleeps, or the drive unplugs)

Nothing needs rescuing. Every state change is committed to SQLite the
moment it happens, so after a crash the registry is an honest snapshot:

| State in registry | Meaning after a crash |
|---|---|
| `verified` | Fully uploaded **and** checksum-verified — will never be re-sent |
| `uploading` / `uploaded` | Was in flight — will be re-processed from scratch |
| `discovered` / `pending` / `changed` / `failed` | Not done — will be (re)tried |
| job row stuck at `running` | Cosmetic leftover of the crash; harmless |

What to know:

- A large file killed mid-transfer **restarts from byte 0** (resume is
  file-level, not chunk-level). With 64 MiB multipart that's the only
  wasted work.
- The killed multipart upload leaves invisible orphaned parts in S3 that
  still cost storage. Set the bucket lifecycle rule once —
  *abort incomplete multipart uploads after 7 days* — or clean manually:
  `aws s3api list-multipart-uploads --bucket <b>` then
  `abort-multipart-upload`.
- If the crash was really a disconnect, the run stops itself with
  "source root disappeared (drive disconnected?)" rather than marking
  thousands of files failed.
- Worst case the registry file itself is lost: start a fresh one and
  re-run. Already-uploaded objects show up as collisions (they weren't
  made by the new registry) — run `verify --full` to confirm they match
  the source, then re-run upload with `--overwrite` only for true
  mismatches. Cheaper: just back the registry up now and then.

## 5. How to resume

```bash
./.venv/bin/python -m s3migrate resume --batch-files 0        # macOS/Linux
```
```powershell
.\.venv\Scripts\python -m s3migrate resume --batch-files 0    # Windows
```

That's the whole procedure. `resume` is literally the same command as
`upload` — the registry makes *every* run a resume: it rescans (fast, just
directory stats), skips everything `verified`-and-unchanged, and processes
the rest. Run it after a crash, after Ctrl-C, after a batch limit, after a
week away, after the drive moved to another computer — same command.

Check where you stand any time:

```bash
python -m s3migrate status          # counts per state + recent failures
python -m s3migrate list-files --status failed
```

`status` and `list-files` are read-only — safe while an upload is running.

**Resuming on a different machine / OS** (e.g. scanned on the Mac, real run
on the Windows PC): copy this folder *including the registry file and its
`-wal`/`-shm` sidecars*, plug in the drive, set `SOURCE_DIR` to the new
mount (`E:\`), and `resume`. The registry stores portable `/`-separated
paths, so nothing verified re-uploads. Expect one interactive prompt-free
rescan first; the drive's own content is the identity, not the mount point.

Done when: `upload --batch-files 0` ends with **"all files verified"**, then

```bash
python -m s3migrate verify --full --sample-hash 25
```

exits `0` — that's the independent bucket-listing reconcile plus 25 random
read-back hash checks. Keep the drive until that passes and a restore test
(download a few files, open them) succeeds.
