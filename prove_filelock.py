#!/usr/bin/env python3
"""prove_filelock.py — the lock must actually exclude, on whatever machine this runs.

The sentinel's queue is appended to by a tick, a drain, and sometimes an evolve worker at once.
If the lock silently does nothing, two writers interleave and a message is lost or doubled — the
exact failure the outbox exists to prevent. `fcntl` does not exist on Windows, so the lock now
goes through filelock.py; these legs prove the guarantee survived the move, and they prove it by
running a REAL second process, not by trusting the API.
"""
import os, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import filelock

fails = []
def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + (("  — " + str(detail)) if detail and not cond else ""))
    if not cond: fails.append(label)

print("prove: the file lock excludes (%s)" % filelock.platform_note())
tmp = Path(tempfile.mkdtemp())
lockfile = tmp / "q.lock"

# 1. a second PROCESS cannot take the lock while we hold it
holder = subprocess.Popen(
    [sys.executable, "-c",
     "import sys,time;sys.path.insert(0,%r);import filelock;"
     "fh=open(%r,'a+');filelock.lock_exclusive(fh);print('HELD',flush=True);time.sleep(3);filelock.unlock(fh)"
     % (str(Path(__file__).resolve().parent), str(lockfile))],
    stdout=subprocess.PIPE, text=True)
assert holder.stdout.readline().strip() == "HELD"
fh = open(lockfile, "a+")
check("1. a second process is refused while the lock is held", filelock.lock_nb(fh) is False)
holder.wait(timeout=15)
check("2. and gets it once the holder lets go", filelock.lock_nb(fh) is True)
filelock.unlock(fh); fh.close()

# 3. raw file descriptors work too (evolve_worker locks an fd, not a file object)
fd = os.open(str(tmp / "fd.lock"), os.O_CREAT | os.O_RDWR, 0o600)
check("3. a raw fd can be locked", filelock.lock_nb(fd) is True)
filelock.unlock(fd); os.close(fd)

# 4. the context manager releases even when the body raises
try:
    with filelock.locked(tmp / "ctx.lock"):
        raise RuntimeError("boom")
except RuntimeError:
    pass
fh = open(tmp / "ctx.lock", "a+")
check("4. the lock is released when the body raises", filelock.lock_nb(fh) is True)
filelock.unlock(fh); fh.close()

# 5. the outbox still queues and reads under the new lock
os.environ["SENTINEL_HOME"] = str(tmp / "home")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import outbox
outbox.enqueue("a message that must survive the lock change", "+15555550123")
check("5. the outbox queues through the shim", len(outbox._pending()) == 1, outbox._pending())


# ── where state lives when the machine is not a Mac ──────────────────────────
import paths
mac = str(Path.home() / "Library" / "Application Support" / "rapp-sentinel")
if sys.platform == "darwin":
    check("6. macOS app-support path is unchanged (the ledger key must not move)",
          str(paths.app_support()) == mac, paths.app_support())
else:
    check("6. off macOS, state does NOT go to a macOS-shaped path",
          "Library/Application Support" not in str(paths.app_support()), paths.app_support())
check("7. app_support joins parts", str(paths.app_support("baselines")).endswith("baselines"))
import baseline
if os.name == "nt":
    check("8. the world-writable guard is skipped where the filesystem has no mode bits",
          baseline.world_writable_ancestor(Path(tempfile.mkdtemp())) is None)
else:
    check("8. the world-writable guard still catches /tmp on POSIX",
          baseline.world_writable_ancestor(Path("/tmp")) is not None)
print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall proved (lock + platform paths)")
sys.exit(1 if fails else 0)
