"""Cross-platform single-instance lock (fcntl on POSIX, msvcrt on Windows)."""

import os

if os.name == "nt":
    import msvcrt

    def _try_lock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
else:
    import fcntl

    def _try_lock(fh):
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)


def acquire(path):
    """Return an open, locked handle (keep it alive), or None if held."""
    fh = open(path, "a+", encoding="utf-8")
    try:
        _try_lock(fh)
    except OSError:
        fh.close()
        return None
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
    except OSError:
        pass  # PID note is best-effort; the lock itself is what matters
    return fh
