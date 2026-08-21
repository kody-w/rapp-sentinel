"""filelock.py — one exclusive file lock that works on every machine in the neighbourhood.

The sentinel's queues and ledgers are appended to by more than one process (a tick, a drain, an
evolve worker), so every writer takes an exclusive lock first. That lock was `fcntl.flock`, which
exists on macOS and Linux and simply does not exist on Windows — so `import outbox` raised
ModuleNotFoundError and no sentinel could be hatched on a Windows box at all. A neighbourhood that
can only be joined by one operating system is smaller than it claims to be.

Same guarantee on both: an exclusive, process-wide lock held for the duration of the block, and
`lock_nb` that returns False instead of blocking when someone else holds it.

    with locked(path):          # blocks until it is ours
        ...
    if lock_nb(fh):             # False if another process holds it
        ...

Windows note: `msvcrt.locking` locks a byte RANGE from the current position, so the lock is taken
on byte 0 and every caller must agree on that — which they do, because they all come through here.
"""
import os
import time
from contextlib import contextmanager

try:                                   # macOS / Linux
    import fcntl
    _WINDOWS = False
except ImportError:                    # Windows
    import msvcrt
    fcntl = None
    _WINDOWS = True


def _fd(target):
    """Accept an open file object OR a raw file descriptor — both are used in this codebase."""
    return target if isinstance(target, int) else target.fileno()


def _seek0(target):
    if isinstance(target, int):
        os.lseek(target, 0, os.SEEK_SET)
    else:
        target.seek(0)


def lock_exclusive(fh, blocking=True, timeout=30.0):
    """Take an exclusive lock on an open file handle or fd. Returns True, or False if non-blocking and busy."""
    if not _WINDOWS:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(_fd(fh), flags)
            return True
        except (OSError, BlockingIOError):
            if blocking:
                raise
            return False
    deadline = time.time() + timeout
    while True:
        try:
            _seek0(fh)
            msvcrt.locking(_fd(fh), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            if not blocking:
                return False
            if time.time() >= deadline:
                raise OSError("could not take the lock within %.0fs" % timeout)
            time.sleep(0.05)


def unlock(fh):
    """Release a lock taken by lock_exclusive. Never raises — a failed unlock must not lose the work."""
    try:
        if not _WINDOWS:
            fcntl.flock(_fd(fh), fcntl.LOCK_UN)
        else:
            _seek0(fh)
            msvcrt.locking(_fd(fh), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def lock_nb(fh):
    """Try to take the lock without waiting. True if we got it."""
    return lock_exclusive(fh, blocking=False)


@contextmanager
def locked(path, mode="a+"):
    """Hold an exclusive lock on `path` for the block."""
    path = path if hasattr(path, "parent") else __import__("pathlib").Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8") as fh:
        lock_exclusive(fh)
        try:
            yield fh
        finally:
            unlock(fh)


def platform_note():
    return "msvcrt byte-range lock (Windows)" if _WINDOWS else "fcntl.flock (POSIX)"
