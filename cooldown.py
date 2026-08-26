"""cooldown.py — suppress repeat alarms for a condition that has not actually changed.

Why this exists (measured 2026-08-25): 485 texts sent, including the SAME
"RAPP Sentinel needs you" message 60 times, plus 21 "degraded -> critical" and 21
"critical -> degraded" — a condition flapping across a threshold, alarming on every
crossing, for findings that were already 62 hours old.

The sentinel is designed to text on STATE CHANGE, which is correct. The problem is that
"state" was the prose of the message, so a flap read as a change and a re-measured age
("62.1h" -> "62.4h") read as new news.

Alert identity here is **the set of failing check names**, not the wording. Two messages
about the same checks are the same alarm no matter which direction the threshold was
crossed or how the numbers drifted. A repeat inside the cooldown window is suppressed and
recorded, never silently dropped — the daily digest still reports it.
"""
import json
import os
import re
import time
from pathlib import Path

# HOME comes from paths.py — the one place it is derived. Hardcoding
# Path.home()/"rapp-sentinel" here meant the cooldown wrote to a directory that does
# not exist on any deployed instance (they run from ~/Documents/GitHub/rapp-sentinel),
# so the suppression state was never read back and EVERY alarm looked new. Measured in
# the wild 2026-08-25: the same finding re-sent for 69 hours straight.
from paths import HOME as _HOME

STATE = _HOME / "state" / "cooldown.jsonl"
SUPPRESSED = _HOME / "state" / "cooldown-suppressed.jsonl"

# Hours a given condition stays quiet after it has been reported once.
# Override with RAPP_ALERT_COOLDOWN_HOURS.
DEFAULT_HOURS = 6.0

# Check names look like rv_world_merging, eco_sweep, w_sentinel_current, ip_hygiene...
_CHECK = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
# Anything that is only a changing magnitude should not make an alarm look new.
_NUM = re.compile(r"\d+(?:\.\d+)?")


# Phrases that mean "the watcher could not observe", not "the watched thing is broken".
# Measured in the wild 2026-08-25: the MAJORITY of alert text was this class — "cannot
# read the PR queue", "cannot read run history", "cannot audit launchd jobs", "unable to
# tell whether", "/chat unreachable". Paging a human because the watcher is blind trains
# them to ignore the channel. Blindness is still RECORDED (it is a real defect in the
# watcher) — it just never pages, and it is surfaced in the digest as a watcher problem.
_BLINDNESS = (
    "cannot read", "cannot audit", "could not read", "unable to tell",
    "unable to read", "cannot reach", "unreachable", "URLError", "ConnectionResetError",
    "is not a git checkout", "no such file",
)


def is_self_blindness(text: str) -> bool:
    """True when EVERY finding in this alert is the watcher failing to observe."""
    low = (text or "").lower()
    if not any(b.lower() in low for b in _BLINDNESS):
        return False
    # If the alert also carries a finding that is NOT a blindness phrase, it still pages.
    lines = [l.strip() for l in low.replace(";", "\n").splitlines() if l.strip()]
    substantive = [l for l in lines
                   if not any(b.lower() in l for b in _BLINDNESS)
                   and any(c in l for c in ("stale", "ago", "waited", "failed", "starv",
                                            "reject", "behind", "missing", "down"))]
    return not substantive


def fingerprint(text: str) -> str:
    """Identity of an alarm = the sorted set of check names it names.

    Falls back to the numberless text when a message names no checks at all, so a
    crash report still dedupes instead of repeating every cycle.
    """
    checks = sorted(set(_CHECK.findall(text or "")))
    if checks:
        return "checks:" + ",".join(checks)
    return "text:" + _NUM.sub("#", (text or "").strip())[:200]


def _load():
    if not STATE.exists():
        return {}
    out = {}
    try:
        for line in STATE.read_text(errors="ignore").splitlines():
            if line.strip():
                d = json.loads(line)
                out[d["key"]] = d["at"]
    except Exception:
        return out
    return out


def _record(key, to, at):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"key": key, "to": to, "at": at}) + "\n")


def _note_suppressed(key, to, text):
    SUPPRESSED.parent.mkdir(parents=True, exist_ok=True)
    with open(SUPPRESSED, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "at": time.time(), "key": key, "to": to, "text": (text or "")[:400]
        }, ensure_ascii=False) + "\n")


def should_send(text, to, hours=None):
    """True if this condition has not been reported to `to` inside the window."""
    try:
        hours = float(os.environ.get("RAPP_ALERT_COOLDOWN_HOURS", hours or DEFAULT_HOURS))
    except (TypeError, ValueError):
        hours = DEFAULT_HOURS
    if hours <= 0:
        return True
    key = f"{to}|{fingerprint(text)}"
    last = _load().get(key)
    nowt = time.time()
    if last is not None and (nowt - last) < hours * 3600:
        _note_suppressed(key, to, text)
        return False
    _record(key, to, nowt)
    return True


def suppressed_since(seconds):
    """What the cooldown held back — so the daily digest can still report it."""
    if not SUPPRESSED.exists():
        return []
    cutoff = time.time() - seconds
    out = []
    for line in SUPPRESSED.read_text(errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("at", 0) >= cutoff:
            out.append(d)
    return out
