#!/usr/bin/env python3
"""Bridge Sentinel's durable outbox to an authorized Aqua-session watcher."""

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import outbox


CLAIMS = outbox.STATE / "watcher-claims"


def _claim_path(raw_line):
    return CLAIMS / hashlib.sha256(raw_line.encode("utf-8")).hexdigest()


def _validated_claim(raw):
    path = Path(raw).resolve()
    if path.parent != CLAIMS.resolve() or not path.is_dir():
        raise ValueError("invalid claim path")
    return path


def claim():
    with outbox._locked(outbox.LOCK):
        lines = outbox._queue_lines_unlocked()
        if not lines:
            return 3
        raw_line = lines[0]
        message = json.loads(raw_line)
        if message.get("attachments"):
            return 4

        path = _claim_path(raw_line)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, value in (
            ("text", message["text"]),
            ("to", message["to"]),
            ("hash", hashlib.sha256(raw_line.encode("utf-8")).hexdigest()),
        ):
            target = path / name
            target.write_text(str(value), encoding="utf-8")
            os.chmod(target, 0o600)

    print(path)
    return 0


def acknowledge(raw, reason=""):
    path = _validated_claim(raw)
    expected = (path / "hash").read_text(encoding="utf-8")
    with outbox._locked(outbox.LOCK):
        lines = outbox._queue_lines_unlocked()
        if not lines:
            raise RuntimeError("outbox changed before acknowledgement")
        actual = hashlib.sha256(lines[0].encode("utf-8")).hexdigest()
        if actual != expected:
            raise RuntimeError("outbox head changed before acknowledgement")
        message = json.loads(lines[0])
        outbox._append_sent(message)
        outbox._rewrite_queue_unlocked(lines[1:])
        kept = len(lines) - 1

    outbox._cleanup_attachments(message)
    outbox._record_drain(1, kept, reason[:240])
    shutil.rmtree(path)
    return 0


def fail(raw, reason):
    path = _validated_claim(raw)
    with outbox._locked(outbox.LOCK):
        kept = len(outbox._queue_lines_unlocked())
    outbox._record_drain(0, kept, reason[:240])
    shutil.rmtree(path)
    return 0


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "claim":
        return claim()
    if command == "ack" and len(sys.argv) >= 3:
        return acknowledge(sys.argv[2], " ".join(sys.argv[3:]))
    if command == "fail" and len(sys.argv) >= 4:
        return fail(sys.argv[2], " ".join(sys.argv[3:]))
    print("usage: watcher_outbox.py claim|ack CLAIM|fail CLAIM REASON",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
