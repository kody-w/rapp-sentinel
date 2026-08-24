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

  enqueue()  durably accepts ordinary alerts; dedupe-keyed alerts fail closed
             while corrupt terminal evidence is unresolved.
  drain()    actually sends. Only works from a context that HAS permission —
             an interactive terminal, or a session where the user has granted
             Automation access to the sending binary.

Undelivered messages accumulate visibly rather than evaporating, and the
dashboard reports the backlog, so a delivery outage announces itself instead of
looking like a quiet night.

  python3 outbox.py drain     # send everything queued
  python3 outbox.py status    # how many are stuck, and how old
  python3 outbox.py ack-recovery ID REASON
  python3 outbox.py resolve-quarantine ID REASON
  python3 outbox.py resolve-unknown ID delivered|not-delivered REASON
"""

import base64
import hashlib
import json
import secrets
import sqlite3
import time
import subprocess
import sys
import zipfile
import filelock
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
EXPIRED = STATE / "outbox-expired.jsonl"
UNKNOWN = STATE / "outbox-unknown.jsonl"
UNKNOWN_RESOLVED = STATE / "outbox-unknown-resolved.jsonl"
INFLIGHT = STATE / "outbox-inflight.json"
QUARANTINE = STATE / "outbox-terminal-quarantine.jsonl"
QUARANTINE_RESOLVED = STATE / "outbox-terminal-quarantine-resolved.jsonl"
STRICT_RECOVERY = STATE / "outbox-strict-ledger-recovery.json"
REPORTS = STATE / "reports"
LOCK = STATE / "outbox.lock"
DRAIN_LOCK = STATE / "outbox-drain.lock"

SEND_TIMEOUT = 25   # a hang is the failure mode; fail fast and leave it queued
INFLIGHT_STALE_SECONDS = 120
QUARANTINE_SCHEMA = "rapp-outbox-terminal-quarantine/1.0"
QUARANTINE_RESOLUTION_SCHEMA = (
    "rapp-outbox-terminal-quarantine-resolution/1.0")
UNKNOWN_RESOLUTION_SCHEMA = "rapp-outbox-unknown-resolution/1.0"
STRICT_RECOVERY_SCHEMA = "rapp-outbox-strict-ledger-recovery/1.0"
STRICT_RECOVERY_INCIDENT_SCHEMA = (
    "rapp-outbox-strict-ledger-recovery-incident/1.0")
UNKNOWN_RESOLUTIONS = frozenset({"delivered", "not-delivered"})
ENTRY_ID_HEX_CHARS = 32


class TerminalLedgerError(RuntimeError):
    """A terminal outbox ledger cannot be safely interpreted."""


class DedupeAmbiguityError(TerminalLedgerError):
    """Corrupt sent evidence prevents safe dedupe-keyed effects."""


class TornTerminalLedger(TerminalLedgerError):
    """Only the final JSONL record is incomplete, so it can be quarantined."""

    def __init__(self, path, valid_lines, raw_line):
        self.path = Path(path)
        self.valid_lines = list(valid_lines)
        self.raw_line = raw_line
        super().__init__(
            f"{self.path.name} has a torn trailing terminal record")

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
        filelock.lock_exclusive(fh)
        try:
            yield
        finally:
            filelock.unlock(fh)


def _queue_lines_unlocked():
    if not QUEUE.exists():
        return []
    return [line for line in QUEUE.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _terminal_ledgers():
    # Recover UNKNOWN first: every other tail is resolved by appending an
    # UNKNOWN record, so a damaged UNKNOWN tail must never become an interior
    # corruption when a second ledger is recovered.
    return (UNKNOWN, SENT, UNVERIFIED, DEAD_LETTER, EXPIRED)


def _line_digest(raw_line):
    return hashlib.sha256(raw_line.encode("utf-8")).hexdigest()


def _queue_entry_identity(message, queue_sha256):
    """Return a persisted entry ID, or a stable identity for a legacy row."""
    entry_id = message.get("entry_id")
    if entry_id is None:
        return f"legacy:{queue_sha256}"
    if not re_full_hex(entry_id, ENTRY_ID_HEX_CHARS):
        raise TerminalLedgerError(
            "outbox queue entry_id must be 32 lowercase hexadecimal characters")
    return entry_id


def _fsync_directory(path):
    descriptor = os.open(str(Path(path)), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _append_durable(path, payload):
    """Append one terminal record only after its bytes reach durable storage."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _scan_terminal_records_unlocked(path, shape_error=None):
    """Return valid records/lines and every malformed physical JSONL line."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return [], [], []
    except OSError as exc:
        raise TerminalLedgerError(
            f"{path.name} is unreadable: {type(exc).__name__}: {exc}") from exc
    if not raw:
        return [], [], []
    source_sha256 = hashlib.sha256(raw).hexdigest()
    lines = raw.splitlines(keepends=True)
    records = []
    valid_lines = []
    corruptions = []
    for index, raw_line in enumerate(lines):
        last = index + 1 == len(lines)
        try:
            text = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            corruptions.append({
                "path": path,
                "line_number": index + 1,
                "raw_line": raw_line,
                "reason": "invalid UTF-8",
                "tail": last,
                "source_sha256": source_sha256,
            })
            continue
        text = text.rstrip("\r\n")
        if not text.strip():
            corruptions.append({
                "path": path,
                "line_number": index + 1,
                "raw_line": raw_line,
                "reason": "blank terminal record",
                "tail": last,
                "source_sha256": source_sha256,
            })
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            corruptions.append({
                "path": path,
                "line_number": index + 1,
                "raw_line": raw_line,
                "reason": "malformed JSON",
                "tail": last,
                "source_sha256": source_sha256,
            })
            continue
        if not isinstance(value, dict):
            corruptions.append({
                "path": path,
                "line_number": index + 1,
                "raw_line": raw_line,
                "reason": "JSON value is not an object",
                "tail": last,
                "source_sha256": source_sha256,
            })
            continue
        invalid = shape_error(value) if shape_error is not None else ""
        if invalid:
            corruptions.append({
                "path": path,
                "line_number": index + 1,
                "raw_line": raw_line,
                "reason": invalid,
                "tail": last,
                "source_sha256": source_sha256,
            })
            continue
        records.append(value)
        valid_lines.append(text)
    return records, valid_lines, corruptions


def _quarantine_shape_error(record):
    if (record.get("schema") != QUARANTINE_SCHEMA
            or not isinstance(record.get("id"), str)
            or not isinstance(record.get("evidence_sha256"), str)
            or not isinstance(record.get("raw_base64"), str)):
        return "invalid terminal quarantine record shape"
    return ""


def _quarantine_resolution_shape_error(record):
    if (record.get("schema") != QUARANTINE_RESOLUTION_SCHEMA
            or not isinstance(record.get("incident_id"), str)
            or not isinstance(record.get("resolved_at"), str)
            or not isinstance(record.get("reason"), str)):
        return "invalid terminal quarantine resolution record shape"
    return ""


def _unknown_resolution_shape_error(record):
    if (record.get("schema") != UNKNOWN_RESOLUTION_SCHEMA
            or not re_full_hex(record.get("unknown_id"), 64)
            or record.get("resolution") not in UNKNOWN_RESOLUTIONS
            or not isinstance(record.get("resolved_at"), str)
            or not isinstance(record.get("reason"), str)):
        return "invalid UNKNOWN delivery resolution record shape"
    return ""


def _strict_ledger_specs():
    return (
        (QUARANTINE, "terminal quarantine", _quarantine_shape_error),
        (
            QUARANTINE_RESOLVED,
            "terminal quarantine resolutions",
            _quarantine_resolution_shape_error,
        ),
        (
            UNKNOWN_RESOLVED,
            "UNKNOWN delivery resolutions",
            _unknown_resolution_shape_error,
        ),
    )


def _strict_recovery_records_unlocked():
    try:
        raw = Path(STRICT_RECOVERY).read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise TerminalLedgerError(
            f"{Path(STRICT_RECOVERY).name} is unreadable: "
            f"{type(exc).__name__}: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TerminalLedgerError(
            f"{Path(STRICT_RECOVERY).name} is invalid despite atomic writes"
        ) from exc
    if (not isinstance(value, dict)
            or value.get("schema") != STRICT_RECOVERY_SCHEMA
            or set(value) != {"schema", "incidents"}
            or not isinstance(value.get("incidents"), list)):
        raise TerminalLedgerError(
            f"{Path(STRICT_RECOVERY).name} has an invalid document shape")
    required = {
        "schema", "id", "ledger", "line", "tail", "reason",
        "raw_sha256", "raw_base64", "evidence_sha256", "source_sha256",
        "recovered_at", "acknowledged_at", "acknowledgement_reason",
        "dedupe_ambiguous",
    }
    records = []
    for record in value["incidents"]:
        if (not isinstance(record, dict)
                or set(record) != required
                or record.get("schema") != STRICT_RECOVERY_INCIDENT_SCHEMA
                or not re_full_hex(record.get("id"), 64)
                or not isinstance(record.get("ledger"), str)
                or isinstance(record.get("line"), bool)
                or not isinstance(record.get("line"), int)
                or record["line"] < 1
                or not isinstance(record.get("tail"), bool)
                or not isinstance(record.get("reason"), str)
                or not re_full_hex(record.get("raw_sha256"), 64)
                or not isinstance(record.get("raw_base64"), str)
                or not re_full_hex(record.get("evidence_sha256"), 64)
                or not re_full_hex(record.get("source_sha256"), 64)
                or not isinstance(record.get("recovered_at"), str)
                or not isinstance(record.get("acknowledged_at"), str)
                or not isinstance(record.get("acknowledgement_reason"), str)
                or record.get("dedupe_ambiguous") is not True):
            raise TerminalLedgerError(
                f"{Path(STRICT_RECOVERY).name} has an invalid incident")
        try:
            preserved = base64.b64decode(record["raw_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise TerminalLedgerError(
                f"{Path(STRICT_RECOVERY).name} has invalid raw evidence"
            ) from exc
        if hashlib.sha256(preserved).hexdigest() != record["raw_sha256"]:
            raise TerminalLedgerError(
                f"{Path(STRICT_RECOVERY).name} raw evidence digest differs")
        records.append(record)
    return records


def _write_strict_recovery_records_unlocked(records):
    payload = {
        "schema": STRICT_RECOVERY_SCHEMA,
        "incidents": records,
    }
    _durable_replace(
        STRICT_RECOVERY,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def _preserve_strict_corruptions_unlocked(path, corruptions):
    """Persist raw strict-ledger failures before the source ledger is repaired."""
    records = _strict_recovery_records_unlocked()
    changed = False
    preserved = []
    for issue in corruptions:
        raw_line = issue["raw_line"]
        evidence_sha = hashlib.sha256(
            Path(path).name.encode("utf-8")
            + b"\0" + issue["source_sha256"].encode("ascii")
            + b"\0" + str(issue["line_number"]).encode("ascii")
            + b"\0" + raw_line
        ).hexdigest()
        match = next(
            (
                record for record in records
                if record["evidence_sha256"] == evidence_sha
                and not record["acknowledged_at"]
            ),
            None,
        )
        if match is None:
            occurrence = sum(
                1 for record in records
                if record["evidence_sha256"] == evidence_sha)
            incident_id = hashlib.sha256(
                f"{evidence_sha}:{occurrence}".encode("ascii")
            ).hexdigest()
            match = {
                "schema": STRICT_RECOVERY_INCIDENT_SCHEMA,
                "id": incident_id,
                "ledger": Path(path).name,
                "line": issue["line_number"],
                "tail": bool(issue["tail"]),
                "reason": issue["reason"],
                "raw_sha256": hashlib.sha256(raw_line).hexdigest(),
                "raw_base64": base64.b64encode(raw_line).decode("ascii"),
                "evidence_sha256": evidence_sha,
                "source_sha256": issue["source_sha256"],
                "recovered_at": now(),
                "acknowledged_at": "",
                "acknowledgement_reason": "",
                "dedupe_ambiguous": True,
            }
            records.append(match)
            changed = True
        preserved.append(match)
    if changed:
        _write_strict_recovery_records_unlocked(records)
    return preserved


def _recover_strict_ledgers_unlocked():
    """Preserve raw invalid strict records, then atomically retain valid rows."""
    for path, _label, shape_error in _strict_ledger_specs():
        _records, valid_lines, corruptions = _scan_terminal_records_unlocked(
            path, shape_error=shape_error)
        if not corruptions:
            continue
        _preserve_strict_corruptions_unlocked(path, corruptions)
        _rewrite_lines_unlocked(path, valid_lines)


def _unresolved_strict_recovery_unlocked():
    return [
        record for record in _strict_recovery_records_unlocked()
        if not record["acknowledged_at"]
    ]


def _raise_for_strict_recovery_unlocked(action):
    unresolved = _unresolved_strict_recovery_unlocked()
    if unresolved:
        raise DedupeAmbiguityError(
            f"{len(unresolved)} unacknowledged strict-ledger recovery "
            f"incident(s) block {action}; inspect {STRICT_RECOVERY} and "
            "acknowledge the preserved raw evidence")


def acknowledge_strict_recovery(incident_id, reason):
    """Acknowledge preserved strict-ledger bytes without resolving effects."""
    if not re_full_hex(incident_id, 64):
        raise ValueError(
            "recovery incident_id must be a lowercase 64-character SHA-256")
    if (not isinstance(reason, str) or not reason.strip()
            or len(reason.strip()) > 240):
        raise ValueError(
            "recovery acknowledgement reason must contain 1-240 characters")
    with _locked(LOCK):
        _recover_terminal_tails_unlocked()
        records = _strict_recovery_records_unlocked()
        incident = next(
            (record for record in records if record["id"] == incident_id),
            None,
        )
        if incident is None:
            raise ValueError("unknown strict-ledger recovery incident")
        if incident["acknowledged_at"]:
            return False
        incident["acknowledged_at"] = now()
        incident["acknowledgement_reason"] = reason.strip()
        _write_strict_recovery_records_unlocked(records)
    return True


def _read_terminal_records_unlocked(path):
    """Read a repaired terminal ledger; corruption must be handled under lock."""
    records, valid_lines, corruptions = _scan_terminal_records_unlocked(path)
    if corruptions:
        issue = corruptions[0]
        if issue["tail"]:
            raise TornTerminalLedger(
                issue["path"], valid_lines, issue["raw_line"])
        raise TerminalLedgerError(
            f"{issue['path'].name} has {issue['reason']} at terminal "
            f"record {issue['line_number']}")
    return records


def _read_strict_jsonl_unlocked(path, label):
    """Read controller-owned evidence after lock-held raw-byte recovery."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise TerminalLedgerError(
            f"{path.name} is unreadable: {type(exc).__name__}: {exc}") from exc
    records = []
    for index, raw_line in enumerate(raw.splitlines()):
        if not raw_line.strip():
            raise TerminalLedgerError(
                f"{label} has a blank record at line {index + 1}")
        try:
            value = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TerminalLedgerError(
                f"{label} has an invalid record at line {index + 1}") from exc
        if not isinstance(value, dict):
            raise TerminalLedgerError(
                f"{label} record {index + 1} is not an object")
        records.append(value)
    return records


def _quarantine_records_unlocked():
    records = _read_strict_jsonl_unlocked(
        QUARANTINE, "terminal quarantine")
    for record in records:
        if _quarantine_shape_error(record):
            raise TerminalLedgerError(
                f"{QUARANTINE.name} has an invalid record shape")
    return records


def _quarantine_resolutions_unlocked():
    records = _read_strict_jsonl_unlocked(
        QUARANTINE_RESOLVED, "terminal quarantine resolutions")
    for record in records:
        if _quarantine_resolution_shape_error(record):
            raise TerminalLedgerError(
                f"{QUARANTINE_RESOLVED.name} has an invalid record shape")
    return records


def _unknown_records_unlocked():
    records, valid_lines, corruptions = _scan_terminal_records_unlocked(UNKNOWN)
    if corruptions:
        issue = corruptions[0]
        if issue["tail"]:
            raise TornTerminalLedger(
                issue["path"], valid_lines, issue["raw_line"])
        raise TerminalLedgerError(
            f"{UNKNOWN.name} has {issue['reason']} at terminal "
            f"record {issue['line_number']}")
    occurrences = {}
    identified = []
    for record, raw_line in zip(records, valid_lines):
        unknown_id = record.get("unknown_id")
        if unknown_id is not None:
            if not re_full_hex(unknown_id, 64):
                raise TerminalLedgerError(
                    f"{UNKNOWN.name} has an invalid unknown_id")
        else:
            digest = _line_digest(raw_line)
            occurrence = occurrences.get(digest, 0)
            occurrences[digest] = occurrence + 1
            unknown_id = hashlib.sha256(
                f"legacy-unknown:{digest}:{occurrence}".encode("ascii")
            ).hexdigest()
        identified.append({"id": unknown_id, "record": record})
    return identified


def _unknown_resolutions_unlocked():
    records = _read_strict_jsonl_unlocked(
        UNKNOWN_RESOLVED, "UNKNOWN delivery resolutions")
    for record in records:
        if _unknown_resolution_shape_error(record):
            raise TerminalLedgerError(
                f"{UNKNOWN_RESOLVED.name} has an invalid record shape")
    return records


def _unresolved_unknown_unlocked():
    resolved = {
        record["unknown_id"]
        for record in _unknown_resolutions_unlocked()
    }
    return [
        item for item in _unknown_records_unlocked()
        if item["id"] not in resolved
    ]


def _append_unknown_unlocked(payload):
    record = {
        **payload,
        "unknown_id": secrets.token_hex(32),
    }
    _append_durable(UNKNOWN, record)
    return record


def _unresolved_quarantine_unlocked():
    resolved = {
        record["incident_id"]
        for record in _quarantine_resolutions_unlocked()
    }
    return [
        record for record in _quarantine_records_unlocked()
        if record["id"] not in resolved
    ]


def _quarantine_terminal_issue_unlocked(issue):
    raw_line = issue["raw_line"]
    evidence_sha = hashlib.sha256(
        issue["path"].name.encode("utf-8")
        + b"\0" + str(issue["line_number"]).encode("ascii")
        + b"\0" + raw_line
    ).hexdigest()
    existing = _quarantine_records_unlocked()
    resolved = {
        record["incident_id"]
        for record in _quarantine_resolutions_unlocked()
    }
    unresolved_match = next(
        (record for record in existing
         if record.get("evidence_sha256") == evidence_sha
         and record["id"] not in resolved),
        None,
    )
    if unresolved_match is not None:
        return unresolved_match
    occurrence = sum(
        1 for record in existing
        if record.get("evidence_sha256") == evidence_sha)
    incident_id = hashlib.sha256(
        f"{evidence_sha}:{occurrence}".encode("ascii")).hexdigest()
    record = {
        "schema": QUARANTINE_SCHEMA,
        "id": incident_id,
        "ledger": issue["path"].name,
        "line": issue["line_number"],
        "tail": bool(issue["tail"]),
        "reason": issue["reason"],
        "raw_sha256": hashlib.sha256(raw_line).hexdigest(),
        "raw_base64": base64.b64encode(raw_line).decode("ascii"),
        "evidence_sha256": evidence_sha,
        "quarantined_at": now(),
        "dedupe_ambiguous": True,
    }
    _append_durable(QUARANTINE, record)
    return record


def resolve_terminal_quarantine(incident_id, reason):
    """Explicitly acknowledge one corruption incident without erasing evidence."""
    if (not isinstance(incident_id, str)
            or not re_full_hex(incident_id, 64)):
        raise ValueError("incident_id must be a lowercase 64-character SHA-256")
    if (not isinstance(reason, str) or not reason.strip()
            or len(reason.strip()) > 240):
        raise ValueError("resolution reason must contain 1-240 characters")
    with _locked(LOCK):
        _recover_terminal_tails_unlocked()
        _raise_for_strict_recovery_unlocked(
            "terminal-quarantine resolution")
        incidents = {
            record["id"]: record for record in _quarantine_records_unlocked()
        }
        if incident_id not in incidents:
            raise ValueError("unknown terminal quarantine incident")
        resolved = {
            record["incident_id"]
            for record in _quarantine_resolutions_unlocked()
        }
        if incident_id in resolved:
            return False
        _append_durable(QUARANTINE_RESOLVED, {
            "schema": QUARANTINE_RESOLUTION_SCHEMA,
            "incident_id": incident_id,
            "resolved_at": now(),
            "reason": reason.strip(),
        })
    return True


def resolve_unknown_delivery(unknown_id, resolution, reason):
    """Acknowledge one UNKNOWN result without changing its raw evidence."""
    if not re_full_hex(unknown_id, 64):
        raise ValueError("unknown_id must be a lowercase 64-character identifier")
    if resolution not in UNKNOWN_RESOLUTIONS:
        raise ValueError("resolution must be 'delivered' or 'not-delivered'")
    if (not isinstance(reason, str) or not reason.strip()
            or len(reason.strip()) > 240):
        raise ValueError("resolution reason must contain 1-240 characters")
    with _locked(LOCK):
        _recover_terminal_tails_unlocked()
        _raise_for_strict_recovery_unlocked("UNKNOWN delivery resolution")
        evidence = {
            item["id"]: item["record"]
            for item in _unknown_records_unlocked()
        }
        if unknown_id not in evidence:
            raise ValueError("unknown UNKNOWN delivery evidence identifier")
        resolved = {
            record["unknown_id"]
            for record in _unknown_resolutions_unlocked()
        }
        if unknown_id in resolved:
            return False
        _append_durable(UNKNOWN_RESOLVED, {
            "schema": UNKNOWN_RESOLUTION_SCHEMA,
            "unknown_id": unknown_id,
            "resolution": resolution,
            "resolved_at": now(),
            "reason": reason.strip(),
        })
    return True


def re_full_hex(value, size):
    return (
        isinstance(value, str)
        and len(value) == size
        and all(char in "0123456789abcdef" for char in value)
    )


def _durable_replace(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _load_inflight_unlocked():
    try:
        value = json.loads(INFLIGHT.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TerminalLedgerError(
            f"{INFLIGHT.name} is unreadable: {type(exc).__name__}: {exc}") from exc
    required = {"raw_line", "queue_sha256", "started_at", "reason"}
    if (not isinstance(value, dict)
            or not required.issubset(value)
            or not set(value).issubset(required | {"entry_id"})
            or not isinstance(value["raw_line"], str)
            or not isinstance(value["queue_sha256"], str)
            or not isinstance(value["started_at"], str)
            or not isinstance(value["reason"], str)
            or value["queue_sha256"] != _line_digest(value["raw_line"])):
        raise TerminalLedgerError(f"{INFLIGHT.name} has an invalid shape")
    try:
        message = json.loads(value["raw_line"])
    except json.JSONDecodeError as exc:
        raise TerminalLedgerError(
            f"{INFLIGHT.name} contains malformed queue JSON") from exc
    if not isinstance(message, dict):
        raise TerminalLedgerError(
            f"{INFLIGHT.name} queue record is not an object")
    entry_id = _queue_entry_identity(message, value["queue_sha256"])
    if "entry_id" in value and value["entry_id"] != entry_id:
        raise TerminalLedgerError(
            f"{INFLIGHT.name} entry_id does not identify its queue record")
    value["entry_id"] = entry_id
    return value


def _inflight_age_seconds(inflight):
    try:
        started = datetime.fromisoformat(
            inflight["started_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise TerminalLedgerError(
            f"{INFLIGHT.name} has an invalid started_at timestamp") from exc
    if started.tzinfo is None:
        raise TerminalLedgerError(
            f"{INFLIGHT.name} started_at must include a timezone")
    age = (datetime.now(timezone.utc) - started).total_seconds()
    if age < -60:
        raise TerminalLedgerError(
            f"{INFLIGHT.name} started_at is in the future")
    return max(0.0, age)


def _write_inflight_unlocked(raw_line, reason):
    if _load_inflight_unlocked() is not None:
        raise TerminalLedgerError(
            "a prior outbox send is still ambiguous and must be recovered first")
    INFLIGHT.parent.mkdir(parents=True, exist_ok=True)
    try:
        message = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        raise TerminalLedgerError(
            "cannot write send intent for malformed queue JSON") from exc
    if not isinstance(message, dict):
        raise TerminalLedgerError(
            "cannot write send intent for a non-object queue record")
    queue_sha256 = _line_digest(raw_line)
    payload = json.dumps({
        "raw_line": raw_line,
        "queue_sha256": queue_sha256,
        "entry_id": _queue_entry_identity(message, queue_sha256),
        "started_at": now(),
        "reason": str(reason)[:240],
    }, ensure_ascii=False) + "\n"
    # This marker is written before the side effect. A tear here is fail-closed
    # (no send has happened yet), while a durable marker prevents a later
    # killed sender from ever automatically repeating that side effect.
    with open(INFLIGHT, "x", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(INFLIGHT.parent)


def _clear_inflight_unlocked():
    try:
        INFLIGHT.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(INFLIGHT.parent)


def _unknown_terminal_record(message, raw_line, reason, **extra):
    queue_sha256 = _line_digest(raw_line)
    return {
        **message,
        "entry_id": _queue_entry_identity(message, queue_sha256),
        "queue_sha256": queue_sha256,
        "unknown_at": now(),
        "reason": str(reason)[:240],
        **extra,
    }


def _recover_inflight_unlocked(force=False):
    """Terminalize a process that died after intent but before durable outcome."""
    inflight = _load_inflight_unlocked()
    if inflight is None:
        return False
    if (not force
            and _inflight_age_seconds(inflight) < INFLIGHT_STALE_SECONDS):
        return False
    lines = _queue_lines_unlocked()
    try:
        index = next(
            index for index, raw_line in enumerate(lines)
            if raw_line == inflight["raw_line"]
            and _line_digest(raw_line) == inflight["queue_sha256"]
        )
    except StopIteration:
        # The queue may already have been removed after a durable SENT record.
        # Keep a local, inspectable fact that this effect was ambiguous rather
        # than inventing a success-shaped resend decision.
        _append_unknown_unlocked({
            "entry_id": inflight["entry_id"],
            "queue_sha256": inflight["queue_sha256"],
            "unknown_at": now(),
            "reason": inflight["reason"],
            "queue_record_missing": True,
        })
        _clear_inflight_unlocked()
        return True
    try:
        message = json.loads(lines[index])
    except json.JSONDecodeError as exc:
        raise TerminalLedgerError(
            "ambiguous in-flight queue record is malformed") from exc
    if not isinstance(message, dict):
        raise TerminalLedgerError(
            "ambiguous in-flight queue record is not an object")
    _append_unknown_unlocked(
        _unknown_terminal_record(
            message, lines[index], inflight["reason"],
            send_started_at=inflight["started_at"],
        )
    )
    _rewrite_queue_unlocked(lines[:index] + lines[index + 1:])
    _clear_inflight_unlocked()
    return True


def _quarantine_queued_dedupe_unlocked(incidents):
    """Move keyed queue entries to UNKNOWN; plain alerts remain deliverable."""
    lines = _queue_lines_unlocked()
    if not lines:
        return 0
    kept = []
    moved = 0
    incident_ids = [record["id"] for record in incidents]
    for raw_line in lines:
        try:
            message = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise TerminalLedgerError(
                "outbox queue contains malformed JSON") from exc
        if not isinstance(message, dict):
            raise TerminalLedgerError(
                "outbox queue contains a non-object record")
        if not message.get("dedupe_key"):
            kept.append(raw_line)
            continue
        if not _terminal_evidence_seen_unlocked(message, raw_line):
            _append_unknown_unlocked(
                _unknown_terminal_record(
                    message,
                    raw_line,
                    "dedupe-keyed delivery is ambiguous after terminal "
                    "ledger corruption",
                    terminal_quarantine_ids=incident_ids[:20],
                    terminal_quarantine_count=len(incident_ids),
                )
            )
        moved += 1
    if moved:
        _rewrite_queue_unlocked(kept)
    return moved


def _recover_terminal_tails_unlocked():
    """Repair malformed terminal JSONL while retaining raw durable evidence.

    An append to a terminal ledger happens after the external send and before
    queue removal. A malformed final record therefore also terminalizes one
    queue head as UNKNOWN. Interior corruption cannot be tied to one message:
    every invalid raw line is quarantined, every valid record is kept, and all
    queued dedupe-keyed messages become UNKNOWN rather than being resent.
    """
    _recover_strict_ledgers_unlocked()
    while True:
        found = None
        for path in _terminal_ledgers():
            _, valid_lines, corruptions = _scan_terminal_records_unlocked(path)
            if corruptions:
                found = (path, valid_lines, corruptions)
                break
        if found is None:
            break
        path, valid_lines, corruptions = found
        tail = next(
            (issue for issue in corruptions if issue["tail"]), None)
        if (tail is not None
                and _load_inflight_unlocked() is None
                and _queue_lines_unlocked()):
            raw_line = _queue_lines_unlocked()[0]
            _write_inflight_unlocked(
                raw_line,
                f"malformed terminal tail {path.name}: "
                f"sha256={hashlib.sha256(tail['raw_line']).hexdigest()[:24]}",
            )
        quarantined = [
            _quarantine_terminal_issue_unlocked(issue)
            for issue in corruptions
        ]
        _rewrite_lines_unlocked(path, valid_lines)
        for issue, incident in zip(corruptions, quarantined):
            _append_unknown_unlocked({
                "unknown_at": now(),
                "reason": (
                    f"quarantined {issue['reason']} from {path.name} "
                    f"record {issue['line_number']}"),
                "terminal_quarantine_id": incident["id"],
                "terminal_ledger": path.name,
                "terminal_record": issue["line_number"],
            })
        if tail is not None and _load_inflight_unlocked() is not None:
            _recover_inflight_unlocked(force=True)

    unresolved = _unresolved_quarantine_unlocked()
    strict_recovery = _unresolved_strict_recovery_unlocked()
    if unresolved or strict_recovery:
        _quarantine_queued_dedupe_unlocked(
            [*unresolved, *strict_recovery])


def _dedupe_seen_in_unlocked(dedupe_key, paths):
    for path in paths:
        records = (
            _read_terminal_records_unlocked(path)
            if Path(path) in _terminal_ledgers()
            else [
                json.loads(line) for line in Path(path).read_text(
                    encoding="utf-8").splitlines() if line.strip()
            ] if Path(path).exists() else []
        )
        for record in records:
            if record.get("dedupe_key") == dedupe_key:
                return True
    return False


def _dedupe_seen_unlocked(dedupe_key):
    """Check every durable queue/terminal ledger while the outbox lock is held."""
    _recover_terminal_tails_unlocked()
    unresolved = _unresolved_quarantine_unlocked()
    strict_recovery = _unresolved_strict_recovery_unlocked()
    if unresolved or strict_recovery:
        raise DedupeAmbiguityError(
            f"{len(unresolved)} unresolved terminal-ledger corruption and "
            f"{len(strict_recovery)} unacknowledged strict-ledger recovery "
            "incident(s) block dedupe-keyed enqueue/send")
    return _dedupe_seen_in_unlocked(
        dedupe_key, (QUEUE, * _terminal_ledgers()))


def _dedupe_terminal_seen_unlocked(dedupe_key):
    _recover_terminal_tails_unlocked()
    unresolved = _unresolved_quarantine_unlocked()
    strict_recovery = _unresolved_strict_recovery_unlocked()
    if unresolved or strict_recovery:
        raise DedupeAmbiguityError(
            f"{len(unresolved)} unresolved terminal-ledger corruption and "
            f"{len(strict_recovery)} unacknowledged strict-ledger recovery "
            "incident(s) block dedupe-keyed enqueue/send")
    return _dedupe_seen_in_unlocked(
        dedupe_key, _terminal_ledgers())


def _rewrite_lines_unlocked(path, lines):
    """Atomically rewrite one JSONL ledger while its caller holds the lock."""
    data = "".join(line + "\n" for line in lines)
    _durable_replace(path, data)


def _rewrite_queue_unlocked(lines):
    """Atomic rewrite so appenders never lose their writes."""
    _rewrite_lines_unlocked(QUEUE, lines)


def enqueue(text, to, attachments=None, dedupe_key=None):
    """Durably queue once; keyed effects stop on unresolved terminal ambiguity."""
    if dedupe_key is not None:
        if (not isinstance(dedupe_key, str) or not dedupe_key.strip()
                or len(dedupe_key) > 240):
            raise ValueError("dedupe_key must contain 1-240 characters")
        dedupe_key = dedupe_key.strip()
    paths = [str(_prepare_attachment(p)) for p in (attachments or [])]
    with _locked(LOCK):
        _recover_terminal_tails_unlocked()
        _recover_inflight_unlocked()
        if dedupe_key and _dedupe_seen_unlocked(dedupe_key):
            return False
        message = {
            "entry_id": secrets.token_hex(16),
            "at": now(), "to": to, "text": text, "attachments": paths,
        }
        if dedupe_key:
            message["dedupe_key"] = dedupe_key
        line = json.dumps(message, ensure_ascii=False)
        with open(QUEUE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_directory(QUEUE.parent)
    return True


def _pending():
    with _locked(LOCK):
        _recover_terminal_tails_unlocked()
        _recover_inflight_unlocked()
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


def _append_sent(message, queue_sha256=None):
    payload = {**message, "sent_at": now()}
    if queue_sha256 is not None:
        payload["entry_id"] = _queue_entry_identity(message, queue_sha256)
        payload["queue_sha256"] = queue_sha256
    _append_durable(SENT, payload)


def _terminal_evidence_seen_unlocked(message, raw_line):
    """A durable terminal row for these exact queued bytes forbids a resend."""
    dedupe_key = message.get("dedupe_key")
    digest = _line_digest(raw_line)
    for path in _terminal_ledgers():
        for record in _read_terminal_records_unlocked(path):
            if record.get("queue_sha256") == digest:
                return True
            if dedupe_key and record.get("dedupe_key") == dedupe_key:
                return True
    return False


def drain(limit=20):
    """Send what is queued. Stops at the first failure — if this context cannot
    send one message it cannot send any, and retrying just burns time."""
    with _locked(DRAIN_LOCK):
        with _locked(LOCK):
            _recover_terminal_tails_unlocked()
            _recover_inflight_unlocked(force=True)
            snapshot_lines = _queue_lines_unlocked()
        if not snapshot_lines:
            return 0, 0, "empty"
        pending = [(line, json.loads(line)) for line in snapshot_lines]

        sent, why = 0, ""
        sent_messages = []
        for raw_line, m in pending:
            if sent >= limit:
                break
            with _locked(LOCK):
                _recover_terminal_tails_unlocked()
                _recover_inflight_unlocked(force=True)
                already_terminal = _terminal_evidence_seen_unlocked(
                    m, raw_line)
                if (m.get("dedupe_key")
                        and (
                            _unresolved_quarantine_unlocked()
                            or _unresolved_strict_recovery_unlocked()
                        )):
                    raise DedupeAmbiguityError(
                        "terminal or strict-ledger recovery evidence blocks "
                        "this dedupe-keyed send")
                if not already_terminal:
                    _write_inflight_unlocked(
                        raw_line, "send started before terminal outcome")
            if already_terminal:
                sent += 1
                sent_messages.append(m)
                continue
            ok, err = _send(m["text"], m["to"], m.get("attachments"))
            if not ok:
                with _locked(LOCK):
                    try:
                        _clear_inflight_unlocked()
                    except OSError:
                        # An uncleared intent becomes a durable UNKNOWN on the
                        # next pass, which is safer than sending this alert again.
                        pass
                why = err
                break
            if err:                      # sent, but unverified - say so in the ledger and the drain record
                m = dict(m, unverified=err)
                why = err
            try:
                with _locked(LOCK):
                    _append_sent(m, queue_sha256=_line_digest(raw_line))
                    _clear_inflight_unlocked()
            except OSError as e:
                why = f"sent ledger write failed: {type(e).__name__}: {e}"
                break
            sent += 1
            sent_messages.append(m)

        cleanup_after_commit = []
        with _locked(LOCK):
            _recover_terminal_tails_unlocked()
            _recover_inflight_unlocked(force=True)
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
    with _locked(LOCK):
        _recover_terminal_tails_unlocked()
        pending = [json.loads(line) for line in _queue_lines_unlocked()]
        terminal_counts = {
            path: len(_read_terminal_records_unlocked(path))
            for path in _terminal_ledgers()
        }
        unknown_records = _unknown_records_unlocked()
        unresolved_unknown = _unresolved_unknown_unlocked()
        quarantine = _quarantine_records_unlocked()
        unresolved = _unresolved_quarantine_unlocked()
        strict_recovery = _strict_recovery_records_unlocked()
        unresolved_recovery = [
            record for record in strict_recovery
            if not record["acknowledged_at"]
        ]
        inflight_record = _load_inflight_unlocked()
        inflight_age = (
            _inflight_age_seconds(inflight_record) / 60
            if inflight_record is not None else None)
    last = last_drain()

    missing = sum(
        1 for message in pending for raw in message.get("attachments", [])
        if not Path(raw).is_file()
    )
    base = {
        "last_drain": last,
        "missing_attachments": missing,
        "unverified": terminal_counts[UNVERIFIED],
        "dead_letter": terminal_counts[DEAD_LETTER],
        "expired": terminal_counts[EXPIRED],
        "unknown": len(unresolved_unknown),
        "unknown_total": len(unknown_records),
        "unknown_resolved": len(unknown_records) - len(unresolved_unknown),
        "unknown_unresolved_ids": [
            item["id"] for item in unresolved_unknown],
        "quarantine": len(quarantine),
        "dedupe_ambiguity": len(unresolved),
        "dedupe_ambiguity_ids": [
            record["id"] for record in unresolved],
        "strict_recovery": len(unresolved_recovery),
        "strict_recovery_total": len(strict_recovery),
        "strict_recovery_acknowledged": (
            len(strict_recovery) - len(unresolved_recovery)),
        "strict_recovery_ids": [
            record["id"] for record in unresolved_recovery],
        "strict_recovery_evidence_file": str(STRICT_RECOVERY),
        "dedupe_blocked": bool(unresolved or unresolved_recovery),
        "inflight": inflight_record is not None,
        "inflight_started_at": (
            inflight_record["started_at"]
            if inflight_record is not None else None),
        "inflight_minutes": (
            round(inflight_age, 1)
            if inflight_age is not None else None),
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
    elif cmd == "ack-recovery" and len(sys.argv) >= 4:
        changed = acknowledge_strict_recovery(
            sys.argv[2], " ".join(sys.argv[3:]))
        print("acknowledged" if changed else "already acknowledged")
    elif cmd == "resolve-quarantine" and len(sys.argv) >= 4:
        changed = resolve_terminal_quarantine(
            sys.argv[2], " ".join(sys.argv[3:]))
        print("resolved" if changed else "already resolved")
    elif cmd == "resolve-unknown" and len(sys.argv) >= 5:
        changed = resolve_unknown_delivery(
            sys.argv[2], sys.argv[3], " ".join(sys.argv[4:]))
        print("resolved" if changed else "already resolved")
    else:
        print(json.dumps(status(), indent=2))
