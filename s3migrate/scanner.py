"""Streaming source discovery: walks the tree without holding it in memory,
populating sources / directories / files / special_entries as it goes."""

import fnmatch
import logging
import os
import stat as statmod

log = logging.getLogger("s3migrate.scan")

JUNK_NAMES = {
    ".DS_Store", ".apdisk", ".VolumeIcon.icns",
    ".Trashes", ".Spotlight-V100", ".fseventsd", ".TemporaryItems",
    ".DocumentRevisions-V100", "$RECYCLE.BIN", "System Volume Information",
    "Thumbs.db", "desktop.ini",
}
JUNK_PREFIXES = ("._",)


def is_junk(name):
    return name in JUNK_NAMES or name.startswith(JUNK_PREFIXES)


def is_excluded(rel, name, patterns):
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p)
               for p in patterns)


def to_posix_rel(relpath):
    """Native relative path -> the portable '/'-separated form the registry
    stores. S3 keys and DB rows are identical whether the scan ran on
    macOS or Windows."""
    return relpath.replace(os.sep, "/")


def native_path(root, posix_rel):
    """Registry relpath -> absolute native filesystem path."""
    return os.path.join(root, *posix_rel.split("/"))


def rel_to_key(posix_rel, prefix):
    if ".." in posix_rel.split("/"):
        raise ValueError(f"path escapes source root: {posix_rel}")
    return f"{prefix}{posix_rel}"


def extension_of(filename):
    _, dot, ext = filename.rpartition(".")
    return ext.lower() if dot and _ else ""


def scan(repo, settings, self_paths=frozenset()):
    """Walk settings.source into the registry. Returns a counters dict."""
    root = os.path.realpath(settings.source)
    if not os.path.isdir(root):
        raise FileNotFoundError(
            f"source not found or not a directory: {settings.source} "
            "(is the drive mounted?)")
    try:
        next(iter(os.scandir(root)))
    except StopIteration:
        raise RuntimeError(
            f"source {root} is empty — refusing to treat a missing mount as "
            "an empty drive")

    source = repo.get_or_create_source(root, os.stat(root).st_dev)
    source_id = source["id"]
    counters = {"files": 0, "bytes": 0, "new": 0, "changed": 0, "dirs": 0,
                "empty_dirs": 0, "symlinks": 0, "errors": 0, "missing": 0}
    repo.begin_scan_tracking()

    root_dir_id = repo.upsert_directory(source_id, "", None, False)
    stack = [(root, "", root_dir_id)]
    while stack:
        current, current_rel, dir_id = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError as exc:
            counters["errors"] += 1
            repo.record_special(source_id, current_rel or ".", "error",
                                note=f"scandir: {exc}")
            log.error("scan error dir=%s err=%s", current_rel, exc)
            continue

        kept = 0
        for entry in entries:
            rel = to_posix_rel(os.path.relpath(entry.path, root))
            if is_junk(entry.name):
                continue
            if settings.excludes and is_excluded(rel, entry.name, settings.excludes):
                continue
            if os.path.abspath(entry.path) in self_paths:
                continue
            kept += 1
            try:
                if entry.is_symlink():
                    counters["symlinks"] += 1
                    try:
                        target = os.readlink(entry.path)
                    except OSError:
                        target = None
                    repo.record_special(source_id, rel, "symlink", target=target,
                                        note="not uploaded: S3 has no symlinks")
                    continue
                if entry.is_dir(follow_symlinks=False):
                    counters["dirs"] += 1
                    child_id = repo.upsert_directory(source_id, rel, dir_id, False)
                    stack.append((entry.path, rel, child_id))
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                counters["errors"] += 1
                repo.record_special(source_id, rel, "error", note=str(exc))
                log.error("scan error path=%s err=%s", rel, exc)
                continue
            if not statmod.S_ISREG(st.st_mode):
                repo.record_special(source_id, rel, "other",
                                    note=f"mode {oct(st.st_mode)}")
                counters["errors"] += 1
                continue

            key = rel_to_key(rel, settings.prefix)
            _, status, is_new = repo.upsert_file(
                source_id, dir_id, rel, entry.name, extension_of(entry.name),
                st.st_size, st.st_mtime_ns, settings.bucket, key,
                f"s3://{settings.bucket}/{key}")
            counters["files"] += 1
            counters["bytes"] += st.st_size
            if is_new:
                counters["new"] += 1
            elif status == "changed":
                counters["changed"] += 1

        if kept == 0 and current_rel:
            counters["empty_dirs"] += 1
            repo.mark_directory_empty(dir_id)

    counters["missing"] = repo.finish_scan_tracking(source_id)
    return source_id, counters
