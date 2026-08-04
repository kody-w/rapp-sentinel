#!/usr/bin/env python3
"""outbox.py — queue messages that a background job cannot send itself.

osascript driving Messages HANGS when launched from launchd: it blocks on a TCC
Automation prompt that cannot be displayed in a background context. Measured —
90 second timeout, no error, no message. Which means a watchdog wired to text
you would have gone completely silent overnight while reporting itself healthy.

That is the exact failure this whole system exists to catch, sitting in its own
notification path. The dangerous part is not the hang; it is that silence looks
identical to "nothing happened".

So sending is split in two:

  enqueue()  always succeeds, never blocks. Called by any background job.
  drain()    actually sends. Only works from a context that HAS permission —
             an interactive terminal, or a session where the user has granted
             Automation access to the sending binary.

Undelivered messages accumulate visibly rather than evaporating, and the
dashboard reports the backlog, so a delivery outage announces itself instead of
looking like a quiet night.

  python3 outbox.py drain     # send everything queued
  python3 outbox.py status    # how many are stuck, and how old
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(__file__).resolve().parent
STATE = HOME / "state"
STATE.mkdir(exist_ok=True)
QUEUE = STATE / "outbox.jsonl"
SENT = STATE / "outbox-sent.jsonl"

SEND_TIMEOUT = 25   # a hang is the failure mode; fail fast and leave it queued

APPLESCRIPT = '''
on run argv
  tell application "Messages"
    set svc to 1st account whose service type = iMessage
    send (item 1 of argv) to participant (item 2 of argv) of svc
  end tell
end run
'''


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def enqueue(text, to):
    """Never blocks, never raises. A queued message is a kept message."""
    with open(QUEUE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": now(), "to": to, "text": text},
                            ensure_ascii=False) + "\n")


def _pending():
    if not QUEUE.exists():
        return []
    return [json.loads(l) for l in QUEUE.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def _send(text, to):
    try:
        p = subprocess.run(["osascript", "-", text, to], input=APPLESCRIPT,
                           capture_output=True, text=True, timeout=SEND_TIMEOUT)
        return p.returncode == 0, p.stderr.strip()[:160]
    except subprocess.TimeoutExpired:
        # the launchd signature: blocked on a permission prompt nobody can see
        return False, "osascript timed out (no Automation permission in this context)"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def drain(limit=20):
    """Send what is queued. Stops at the first failure — if this context cannot
    send one message it cannot send any, and retrying just burns time."""
    pending = _pending()
    if not pending:
        return 0, 0, "empty"

    sent, why = 0, ""
    for i, m in enumerate(pending):
        if sent >= limit:
            break
        ok, err = _send(m["text"], m["to"])
        if not ok:
            why = err
            break
        sent += 1
        with open(SENT, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({**m, "sent_at": now()}, ensure_ascii=False) + "\n")

    kept = pending[sent:]
    QUEUE.write_text("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in kept),
                     encoding="utf-8")
    return sent, len(kept), why


def status():
    pending = _pending()
    if not pending:
        return {"pending": 0, "oldest_minutes": None}
    oldest = min(datetime.fromisoformat(m["at"]) for m in pending)
    age = (datetime.now(timezone.utc) - oldest).total_seconds() / 60
    return {"pending": len(pending), "oldest_minutes": round(age, 1)}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "drain":
        sent, kept, why = drain()
        print(f"sent={sent} still_queued={kept}" + (f" reason={why}" if why else ""))
        sys.exit(0 if kept == 0 else 1)
    elif cmd == "enqueue":
        enqueue(sys.argv[2], sys.argv[3])
        print("queued")
    else:
        print(json.dumps(status(), indent=2))
