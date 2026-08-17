#!/usr/bin/env python3
"""Bridge Sentinel's durable outbox to an authorized Aqua-session watcher."""

import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import outbox


CLAIMS = outbox.STATE / "watcher-claims"
ATTEMPTS = outbox.STATE / "outbox-attempts.json"
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (60, 300, 1800)


def _claim_path(raw_line):
    return CLAIMS / hashlib.sha256(raw_line.encode("utf-8")).hexdigest()


def _validated_claim(raw):
    path = Path(raw).resolve()
    if path.parent != CLAIMS.resolve() or not path.is_dir():
        raise ValueError("invalid claim path")
    return path


def _digest(raw_line):
    return hashlib.sha256(raw_line.encode("utf-8")).hexdigest()


def _load_attempts_unlocked():
    try:
        data = json.loads(ATTEMPTS.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        raise RuntimeError(f"attempt ledger unreadable: {exc}") from exc


def _save_attempts_unlocked(data):
    ATTEMPTS.parent.mkdir(parents=True, exist_ok=True)
    tmp = ATTEMPTS.with_name(
        f"{ATTEMPTS.name}.tmp.{os.getpid()}.{outbox.time.time_ns()}")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, ATTEMPTS)
    finally:
        tmp.unlink(missing_ok=True)


def _append_durable(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _claim_head(path):
    expected = (path / "hash").read_text(encoding="utf-8")
    lines = outbox._queue_lines_unlocked()
    if not lines:
        raise RuntimeError("outbox changed before claim completion")
    actual = _digest(lines[0])
    if actual != expected:
        raise RuntimeError("outbox head changed before claim completion")
    return expected, lines, json.loads(lines[0])


def claim():
    with outbox._locked(outbox.LOCK):
        lines = outbox._queue_lines_unlocked()
        if not lines:
            return 3
        raw_line = lines[0]
        message = json.loads(raw_line)
        if message.get("attachments"):
            return 4
        digest = _digest(raw_line)
        attempts = _load_attempts_unlocked()
        retry = attempts.get(digest) or {}
        if retry.get("next_attempt"):
            try:
                gate = datetime.fromisoformat(retry["next_attempt"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError("attempt ledger has invalid timestamp") from exc
            if datetime.now(timezone.utc) < gate:
                return 5

        path = _claim_path(raw_line)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, value in (
            ("text", message["text"]),
            ("to", message["to"]),
            ("hash", digest),
        ):
            target = path / name
            target.write_text(str(value), encoding="utf-8")
            os.chmod(target, 0o600)

    print(path)
    return 0


def acknowledge(raw, reason=""):
    path = _validated_claim(raw)
    with outbox._locked(outbox.LOCK):
        expected, lines, message = _claim_head(path)
        outbox._append_sent(message)
        outbox._rewrite_queue_unlocked(lines[1:])
        attempts = _load_attempts_unlocked()
        attempts.pop(expected, None)
        _save_attempts_unlocked(attempts)
        kept = len(lines) - 1

    outbox._cleanup_attachments(message)
    outbox._record_drain(1, kept, reason[:240])
    shutil.rmtree(path)
    return 0


def uncertain(raw, reason):
    """Preserve an attempted send whose delivery could not be verified."""
    path = _validated_claim(raw)
    with outbox._locked(outbox.LOCK):
        expected, lines, message = _claim_head(path)
        _append_durable(outbox.UNVERIFIED, {
            **message,
            "attempted_at": outbox.now(),
            "reason": reason[:240],
        })
        outbox._rewrite_queue_unlocked(lines[1:])
        attempts = _load_attempts_unlocked()
        attempts.pop(expected, None)
        _save_attempts_unlocked(attempts)
        kept = len(lines) - 1

    outbox._record_drain(
        0, kept, f"delivery unverified: {reason[:200]}")
    shutil.rmtree(path)
    return 0


def fail(raw, reason):
    path = _validated_claim(raw)
    with outbox._locked(outbox.LOCK):
        expected, lines, message = _claim_head(path)
        attempts = _load_attempts_unlocked()
        prior = attempts.get(expected) or {}
        count = int(prior.get("count") or 0) + 1
        dead = count >= MAX_ATTEMPTS
        if dead:
            _append_durable(outbox.DEAD_LETTER, {
                **message,
                "failed_at": outbox.now(),
                "attempts": count,
                "reason": reason[:240],
            })
            outbox._rewrite_queue_unlocked(lines[1:])
            attempts.pop(expected, None)
            kept = len(lines) - 1
        else:
            delay = BACKOFF_SECONDS[min(count - 1, len(BACKOFF_SECONDS) - 1)]
            attempts[expected] = {
                "count": count,
                "last_failure": outbox.now(),
                "next_attempt": (
                    datetime.now(timezone.utc) + timedelta(seconds=delay)
                ).isoformat(timespec="seconds"),
                "reason": reason[:240],
            }
            kept = len(lines)
        _save_attempts_unlocked(attempts)

    outcome = "dead-lettered" if dead else f"retry {count}/{MAX_ATTEMPTS}"
    outbox._record_drain(0, kept, f"{outcome}: {reason[:200]}")
    shutil.rmtree(path)
    return 0


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "claim":
        return claim()
    if command == "ack" and len(sys.argv) >= 3:
        return acknowledge(sys.argv[2], " ".join(sys.argv[3:]))
    if command == "uncertain" and len(sys.argv) >= 4:
        return uncertain(sys.argv[2], " ".join(sys.argv[3:]))
    if command == "fail" and len(sys.argv) >= 4:
        return fail(sys.argv[2], " ".join(sys.argv[3:]))
    print("usage: watcher_outbox.py claim|ack CLAIM|uncertain CLAIM REASON|"
          "fail CLAIM REASON",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
