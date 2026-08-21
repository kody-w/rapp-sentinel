#!/usr/bin/env python3
"""Verify uncertain iMessage sends against Messages' delivered-message ledger."""

import argparse
import filelock
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import outbox


CHAT_DB = outbox.CHAT_DB
APPLE_EPOCH_OFFSET = 978307200
MATCH_WINDOW_SECONDS = 15
VERIFY_LOCK = outbox.STATE / "outbox-verify.lock"
PID_FILE = outbox.STATE / "outbox-verify.pid"


def decode_attributed_body(value):
    """Extract NSString text from Messages' archived attributedBody blob."""
    if not isinstance(value, (bytes, bytearray)) or not value:
        return None
    body = bytes(value)
    marker = b"NSString"
    offset = body.find(marker)
    if offset < 0:
        return None
    offset += len(marker)
    plus = body.find(b"+", offset)
    if plus < 0 or plus + 1 >= len(body):
        return None
    offset = plus + 1
    prefix = body[offset]
    offset += 1
    if prefix == 0x81:
        if offset >= len(body):
            return None
        length = body[offset]
        offset += 1
    elif prefix == 0x82:
        if offset + 2 > len(body):
            return None
        length = int.from_bytes(body[offset:offset + 2], "little")
        offset += 2
    elif prefix == 0x83:
        if offset + 3 > len(body):
            return None
        length = int.from_bytes(body[offset:offset + 3], "little")
        offset += 3
    else:
        length = prefix
    if offset + length > len(body):
        return None
    try:
        return body[offset:offset + length].decode("utf-8")
    except UnicodeDecodeError:
        return None


def _unix_from_apple(raw):
    value = int(raw)
    seconds = value / 1_000_000_000 if value > 10_000_000_000 else value
    return seconds + APPLE_EPOCH_OFFSET


def _attempted_timestamp(message):
    raw = message.get("attempted_at") or message.get("sent_at") or message.get("at")
    if not isinstance(raw, str):
        raise ValueError("uncertain message has no attempted timestamp")
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()


def _candidates(connection, message):
    attempted = _attempted_timestamp(message)
    window = MATCH_WINDOW_SECONDS * 1_000_000_000
    apple = int((attempted - APPLE_EPOCH_OFFSET) * 1_000_000_000)
    rows = connection.execute(
        """
        SELECT m.ROWID, m.date, m.text, m.attributedBody
        FROM message m
        JOIN handle h ON m.handle_id = h.ROWID
        WHERE h.id = ?
          AND m.is_from_me = 1
          AND m.is_sent = 1
          AND m.is_delivered = 1
          AND m.error = 0
          AND m.date BETWEEN ? AND ?
        ORDER BY ABS(m.date - ?), m.ROWID
        """,
        (message["to"], apple - window, apple + window, apple),
    ).fetchall()
    expected = message.get("text") or ""
    matches = []
    for row_id, apple_date, plain, attributed in rows:
        plain_matches = isinstance(plain, str) and plain == expected
        archive_matches = (
            isinstance(attributed, (bytes, bytearray))
            and expected.encode("utf-8") in bytes(attributed)
        )
        decoded_matches = decode_attributed_body(attributed) == expected
        if plain_matches or archive_matches or decoded_matches:
            matches.append((row_id, abs(_unix_from_apple(apple_date) - attempted)))
    return matches


def verify():
    """Move uniquely matched uncertain sends to the verified sent ledger."""
    with outbox._locked(outbox.LOCK):
        try:
            snapshot = [
                line for line in outbox.UNVERIFIED.read_text(
                    encoding="utf-8").splitlines() if line.strip()
            ]
        except FileNotFoundError:
            snapshot = []
    if not snapshot:
        return 0, 0

    try:
        connection = sqlite3.connect(f"file:{Path(CHAT_DB)}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise RuntimeError(f"Messages delivery ledger unreadable: {exc}") from exc

    verified = {}
    used_row_ids = set()
    try:
        for raw_line in snapshot:
            message = json.loads(raw_line)
            matches = [
                match for match in _candidates(connection, message)
                if match[0] not in used_row_ids
            ]
            if len(matches) == 1:
                row_id, delta = matches[0]
                used_row_ids.add(row_id)
                verified[raw_line] = {
                    "message_rowid": row_id,
                    "delta_seconds": round(delta, 3),
                }
    finally:
        connection.close()

    with outbox._locked(outbox.LOCK):
        current = [
            line for line in outbox.UNVERIFIED.read_text(
                encoding="utf-8").splitlines() if line.strip()
        ]
        kept = []
        committed = 0
        for raw_line in current:
            evidence = verified.get(raw_line)
            if evidence is None:
                kept.append(raw_line)
                continue
            message = json.loads(raw_line)
            outbox._append_sent({
                **message,
                "verified_at": outbox.now(),
                "delivery_evidence": {
                    "source": "Messages/chat.db",
                    **evidence,
                },
            })
            committed += 1
        outbox._rewrite_lines_unlocked(outbox.UNVERIFIED, kept)

    outbox._record_drain(
        committed, len(kept),
        f"verified {committed} uncertain send(s) against Messages delivery rows")
    return committed, len(kept)


def verify_once():
    try:
        verified, remaining = verify()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"verified={verified} still_unverified={remaining}")
    return 0


def run_loop(interval):
    VERIFY_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(VERIFY_LOCK, "a+", encoding="utf-8") as lock:
        try:
            if not filelock.lock_nb(lock):
                raise BlockingIOError("another verify holds the lock")
        except BlockingIOError:
            print("outbox verifier already running", file=sys.stderr)
            return 0
        PID_FILE.write_text(str(os.getpid()) + "\n", encoding="utf-8")
        try:
            while True:
                try:
                    verified, remaining = verify()
                    if verified or remaining:
                        print(
                            f"{outbox.now()} verified={verified} "
                            f"still_unverified={remaining}",
                            flush=True,
                        )
                except Exception as exc:
                    print(f"{outbox.now()} {exc}", file=sys.stderr, flush=True)
                time.sleep(interval)
        finally:
            PID_FILE.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true",
                        help="continuously reconcile retained sends")
    parser.add_argument("--interval", type=int, default=60,
                        help="loop interval in seconds (minimum 10)")
    args = parser.parse_args(argv)
    if args.loop:
        return run_loop(max(10, args.interval))
    return verify_once()


if __name__ == "__main__":
    sys.exit(main())
