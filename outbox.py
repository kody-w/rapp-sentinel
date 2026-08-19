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
import sqlite3
import time
import subprocess
import sys
import zipfile
import fcntl
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from paths import HOME

STATE = HOME / "state"
STATE.mkdir(exist_ok=True)
QUEUE = STATE / "outbox.jsonl"
SENT = STATE / "outbox-sent.jsonl"
UNVERIFIED = STATE / "outbox-unverified.jsonl"
DEAD_LETTER = STATE / "outbox-dead-letter.jsonl"
REPORTS = STATE / "reports"
LOCK = STATE / "outbox.lock"
DRAIN_LOCK = STATE / "outbox-drain.lock"

SEND_TIMEOUT = 25   # a hang is the failure mode; fail fast and leave it queued

APPLESCRIPT = '''
on run argv
  set messageText to item 1 of argv
  set recipientHandle to item 2 of argv
  tell application "Messages"
    set svc to 1st account whose service type = iMessage
    set recipient to participant recipientHandle of svc
    if messageText is not "" then
      send messageText to recipient
    end if
  end tell
  repeat with itemIndex from 3 to count of argv
    set attachmentPath to item itemIndex of argv
    set attachmentFile to (POSIX file attachmentPath) as alias
    tell application "Messages"
      set svc to 1st account whose service type = iMessage
      set recipient to participant recipientHandle of svc
      send attachmentFile to recipient
    end tell
  end repeat
end run
'''


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _prepare_attachment(raw):
    """Wrap blocked HTML file types in a ZIP that preserves the static file."""
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        return path
    if path.suffix.lower() not in (".html", ".htm"):
        return path
    archive = path.with_suffix(path.suffix + ".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.write(path, arcname=path.name)
    path.unlink()
    return archive


@contextmanager
def _locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _queue_lines_unlocked():
    if not QUEUE.exists():
        return []
    return [line for line in QUEUE.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _rewrite_lines_unlocked(path, lines):
    """Atomically rewrite one JSONL ledger while its caller holds the lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(line + "\n" for line in lines)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _rewrite_queue_unlocked(lines):
    """Atomic rewrite so appenders never lose their writes."""
    _rewrite_lines_unlocked(QUEUE, lines)


def enqueue(text, to, attachments=None):
    """Never blocks, never raises. A queued message is a kept message."""
    paths = [str(_prepare_attachment(p)) for p in (attachments or [])]
    line = json.dumps({
        "at": now(), "to": to, "text": text, "attachments": paths
    }, ensure_ascii=False)
    with _locked(LOCK):
        with open(QUEUE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _pending():
    with _locked(LOCK):
        return [json.loads(line) for line in _queue_lines_unlocked()]


CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"


def _delivered_count(to):
    """Rows Messages has actually written for this handle, or None if unknown.

    None means "cannot tell" and is deliberately different from 0. A caller must
    not treat an unreadable database as proof of non-delivery.
    """
    if not CHAT_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True, timeout=5)
        try:
            # is_sent=1 AND error=0, not COUNT(*). Messages writes a row for a
            # send that FAILED too: an unroutable handle yields
            # is_sent=0, is_delivered=0, error=22, while a real send yields
            # is_sent=1, is_delivered=1, error=0. Counting bare rows would call
            # the failure a delivery, which is the same mistake as trusting the
            # exit code, one layer in.
            row = con.execute(
                "SELECT COUNT(*) FROM message m JOIN handle h ON m.handle_id = h.ROWID"
                " WHERE h.id = ? AND m.is_from_me = 1 AND m.is_sent = 1"
                " AND m.error = 0", (to,)).fetchone()
        finally:
            con.close()
        return int(row[0]) if row else None
    except Exception:
        return None


def _delivered_attachment_count(to, transfer_name):
    """Delivered outgoing attachments with this filename for the recipient."""
    if not CHAT_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM attachment a"
                " JOIN message_attachment_join j ON j.attachment_id = a.ROWID"
                " JOIN message m ON m.ROWID = j.message_id"
                " JOIN handle h ON m.handle_id = h.ROWID"
                " WHERE h.id = ? AND m.is_from_me = 1 AND m.is_sent = 1"
                " AND m.error = 0 AND (a.transfer_name = ? OR a.filename LIKE ?)",
                (to, transfer_name, f"%/{transfer_name}"),
            ).fetchone()
        finally:
            con.close()
        return int(row[0]) if row else None
    except Exception:
        return None


def _send(text, to, attachments=None):
    """Send, then prove it landed.

    osascript exiting 0 is not delivery. Measured against an unroutable handle:
    returncode 0, empty stderr, and chat.db never gained a row. drain() then
    counts that as sent, appends it to outbox-sent.jsonl, and drops it from the
    queue -- so a falsely successful send destroys the only copy of an alert.

    This is the rule the overnight instructions already impose on every
    hand-sent message: "a send that reports success but does not increment the
    count did not happen." The unattended path is where it matters more.

    Messages writes the row, not osascript, so the count is polled briefly
    rather than read once. An unreadable chat.db yields an explicitly
    unverifiable failure; a process exit code must never destroy the only copy
    of an alert.
    """
    attachment_paths = [Path(p) for p in (attachments or [])]
    missing = [p.name for p in attachment_paths if not p.is_file()]
    if missing:
        return False, "attachment missing: " + ", ".join(missing)

    before = _delivered_count(to)
    attachment_before = {
        p.name: _delivered_attachment_count(to, p.name)
        for p in attachment_paths
    }
    try:
        p = subprocess.run(
            ["osascript", "-", text, to, *[str(path) for path in attachment_paths]],
            input=APPLESCRIPT, capture_output=True, text=True, timeout=SEND_TIMEOUT)
    except subprocess.TimeoutExpired:
        # the launchd signature: blocked on a permission prompt nobody can see
        return False, "osascript timed out (no Automation permission in this context)"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    if p.returncode != 0:
        return False, p.stderr.strip()[:160]
    if before is None or any(v is None for v in attachment_before.values()):
        # osascript already SENT this message. Reporting a failure here put it back
        # in the queue, and the next drain sent it again - the operator got every
        # alert twice while the log said "unverifiable" (the Principal's first
        # finding, 2026-08-18: 10 such copies queued on the estate sentinel). An
        # unreadable chat.db means we cannot PROVE delivery, not that it did not
        # happen; a send we cannot verify is recorded as sent-unverified exactly
        # once, never repeated. (A refused/unroutable handle still fails above on
        # returncode; the falsely-successful-send case this docstring guards is the
        # one where chat.db IS readable and gains no row - that path is unchanged.)
        return True, "sent unverified: chat.db unreadable in this context (no Full Disk Access)"

    for _ in range(40):                      # attachments can take longer to index
        text_landed = not text or (_delivered_count(to) or 0) > before
        files_landed = all(
            (_delivered_attachment_count(to, name) or 0) > count
            for name, count in attachment_before.items()
        )
        if text_landed and files_landed:
            return True, ""
        time.sleep(0.25)
    return False, (f"osascript exited 0 but chat.db did not record the complete "
                   f"text/attachment delivery for {to} within 10s - staying queued")


def _cleanup_attachments(message):
    """Delete only generated report snapshots after confirmed delivery."""
    root = REPORTS.resolve()
    for raw in message.get("attachments", []):
        try:
            path = Path(raw).resolve()
            if root in path.parents:
                path.unlink(missing_ok=True)
        except Exception:
            pass


def _append_sent(message):
    with open(SENT, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({**message, "sent_at": now()}, ensure_ascii=False) + "\n")


def drain(limit=20):
    """Send what is queued. Stops at the first failure — if this context cannot
    send one message it cannot send any, and retrying just burns time."""
    with _locked(DRAIN_LOCK):
        with _locked(LOCK):
            snapshot_lines = _queue_lines_unlocked()
        if not snapshot_lines:
            return 0, 0, "empty"
        pending = [json.loads(line) for line in snapshot_lines]

        sent, why = 0, ""
        sent_messages = []
        for m in pending:
            if sent >= limit:
                break
            ok, err = _send(m["text"], m["to"], m.get("attachments"))
            if not ok:
                why = err
                break
            if err:                      # sent, but unverified - say so in the ledger and the drain record
                m = dict(m, unverified=err)
                why = err
            try:
                _append_sent(m)
            except OSError as e:
                why = f"sent ledger write failed: {type(e).__name__}: {e}"
                break
            sent += 1
            sent_messages.append(m)

        cleanup_after_commit = []
        with _locked(LOCK):
            current_lines = _queue_lines_unlocked()
            if current_lines[:len(snapshot_lines)] == snapshot_lines:
                # Keep unsent snapshot entries plus anything appended in parallel.
                kept_lines = (snapshot_lines[sent:]
                              + current_lines[len(snapshot_lines):])
                _rewrite_queue_unlocked(kept_lines)
                cleanup_after_commit = list(sent_messages)
            else:
                # Another writer changed queue shape unexpectedly; preserve
                # current queue to avoid dropping anything.
                kept_lines = current_lines
                _rewrite_queue_unlocked(kept_lines)

        for message in cleanup_after_commit:
            _cleanup_attachments(message)

        _record_drain(sent, len(kept_lines), why)
        return sent, len(kept_lines), why


LAST_DRAIN = QUEUE.parent / "outbox-last-drain.json"


def _record_drain(sent, kept, why):
    """Keep the reason the alert path failed.

    drain() has always known which failure it hit and returned it to whoever
    called it, so the reason died with the process. alert_delivery then saw a
    queue depth and an age, and stayed quiet for 180 minutes before saying
    anything -- while the very first failed drain already knew.

    The two failures need opposite responses and are indistinguishable by depth:
    an osascript timeout is a TCC/launchd problem that will send fine from an
    interactive context, while "exited 0 but chat.db recorded no SENT message"
    means the message was genuinely rejected and will fail the same way
    everywhere.
    """
    try:
        LAST_DRAIN.parent.mkdir(parents=True, exist_ok=True)
        LAST_DRAIN.write_text(json.dumps({
            "at": now(), "sent": sent, "kept": kept, "why": why or "",
        }, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception:
        pass          # bookkeeping must never break a drain


def last_drain():
    try:
        return json.loads(LAST_DRAIN.read_text(encoding="utf-8"))
    except Exception:
        return None


def status():
    pending = _pending()
    last = last_drain()
    def count_lines(path):
        try:
            return sum(1 for line in path.read_text(encoding="utf-8").splitlines()
                       if line.strip())
        except Exception:
            return 0

    missing = sum(
        1 for message in pending for raw in message.get("attachments", [])
        if not Path(raw).is_file()
    )
    base = {
        "last_drain": last,
        "missing_attachments": missing,
        "unverified": count_lines(UNVERIFIED),
        "dead_letter": count_lines(DEAD_LETTER),
    }
    if not pending:
        return {**base, "pending": 0, "oldest_minutes": None}
    oldest = min(datetime.fromisoformat(m["at"]) for m in pending)
    age = (datetime.now(timezone.utc) - oldest).total_seconds() / 60
    return {**base, "pending": len(pending), "oldest_minutes": round(age, 1)}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "drain":
        sent, kept, why = drain()
        print(f"sent={sent} still_queued={kept}" + (f" reason={why}" if why else ""))
        sys.exit(0 if kept == 0 else 1)
    elif cmd == "enqueue":
        enqueue(sys.argv[2], sys.argv[3], sys.argv[4:])
        print("queued")
    else:
        print(json.dumps(status(), indent=2))
