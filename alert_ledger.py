#!/usr/bin/env python3
"""alert_ledger.py — every alert DECISION is a rapp/1 frame, not a side effect.

Why this exists (measured in the wild, 2026-08-25). The sentinel texted the same
findings for 69 hours straight while its repair arm wrote nothing, and most of the text
was the watcher's own blindness ("cannot read the PR queue"). Nobody could answer the
only questions that matter — *did this alert change anything? has this exact condition
been reported before? is the watcher itself the broken thing?* — because alerting left
no auditable record: a message went out, and that was the whole story.

So the decision becomes the record. Every time the sentinel considers paging a human it
appends ONE frame to an append-only chain (`state/alerts.jsonl`), whatever it decides:

    alert.paged        it woke a human, and why it was allowed to
    alert.suppressed   the same failing-check set was already reported (cooldown)
    alert.blind        every finding was the watcher failing to observe — recorded,
                       never paged; this is a defect in the WATCHER
    alert.resolved     a previously-alerting condition is now green

That chain answers, for free and forever: how many times did this condition fire, how
long did it stay open, how much noise did the gate absorb, and which of my "findings"
were actually my own blindness. It is the same discipline the estate applies to
everything else — the frame is the memory, and the reasoning that produced a decision
is recorded next to the decision.

rapp/1 compliant: frames are built and verified with the vendored reference
implementation (rapp.py); the chain re-verifies end to end before every append, so a
tampered or truncated ledger is refused rather than silently extended. Numbers ride as
strings (the canonical form forbids floats).
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

import rapp as R
from paths import HOME

LEDGER = HOME / "state" / "alerts.jsonl"
STREAM_PREFIX = "alerts:@kody-w/"


def _utc() -> str:
    n = datetime.datetime.now(datetime.timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"


def _stream(instance: str) -> str:
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in (instance or "sentinel").lower())
    return STREAM_PREFIX + slug


def load(instance: str = "sentinel") -> list[dict]:
    """Read the chain, verifying every frame. A broken chain raises — never pretend."""
    if not LEDGER.exists():
        return []
    frames = [json.loads(l) for l in LEDGER.read_text(errors="ignore").splitlines() if l.strip()]
    head = None
    for f in frames:
        ok, step, why = R.verify_frame(f, head=head, stream_id_of_record=f.get("stream_id"))
        if not ok:
            raise ValueError(f"alert ledger BROKEN at seq {f.get('seq')}: step {step}: {why}")
        head = f
    return frames


def record(kind: str, instance: str, fingerprint: str, text: str,
           reason: str = "", checks: list[str] | None = None) -> dict | None:
    """Append one decision frame. Never raises into the alert path — a ledger problem
    must not stop a real alert from going out; it degrades to returning None."""
    try:
        stream = _stream(instance)
        frames = load(instance)
        head = frames[-1] if frames else None
        payload = {
            "decision": kind,                    # paged | suppressed | blind | resolved
            "instance": instance,
            "fingerprint": fingerprint,          # the failing-check SET, not the prose
            "checks": sorted(checks or []),
            "reason": reason,
            "text_head": (text or "").strip().replace("\n", " ")[:220],
            "at": _utc(),
        }
        f = R.build_frame(f"alert.{kind}", stream, (head["seq"] + 1) if head else 0,
                          _utc(), payload, prev=(head["payload_hash"] if head else None))
        ok, step, why = R.verify_frame(f, head=head, stream_id_of_record=stream)
        if not ok:
            return None
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(LEDGER, "a") as fh:
            fh.write(json.dumps(f) + "\n")
        return f
    except Exception:
        return None


def history(fingerprint: str, instance: str = "sentinel") -> dict:
    """What this exact condition has done before — the question the wild run could not
    answer. Used to decide whether a repeat deserves a human at all."""
    try:
        frames = load(instance)
    except Exception:
        return {"seen": 0, "paged": 0, "suppressed": 0, "blind": 0, "first_at": None, "last_at": None}
    mine = [f["payload"] for f in frames if f["payload"].get("fingerprint") == fingerprint]
    return {
        "seen": len(mine),
        "paged": sum(1 for p in mine if p["decision"] == "paged"),
        "suppressed": sum(1 for p in mine if p["decision"] == "suppressed"),
        "blind": sum(1 for p in mine if p["decision"] == "blind"),
        "first_at": mine[0]["at"] if mine else None,
        "last_at": mine[-1]["at"] if mine else None,
    }


def digest(instance: str = "sentinel", hours: float = 24.0) -> dict:
    """What the gate absorbed — so silence is provable, not assumed."""
    try:
        frames = load(instance)
    except Exception as e:
        return {"error": str(e)[:120]}
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    out = {"paged": 0, "suppressed": 0, "blind": 0, "resolved": 0,
           "blind_checks": {}, "window_hours": str(hours)}
    for f in frames:
        p = f["payload"]
        try:
            at = datetime.datetime.strptime(p["at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=datetime.timezone.utc)
        except Exception:
            continue
        if at < cutoff:
            continue
        d = p["decision"]
        if d in out:
            out[d] += 1
        if d == "blind":
            for c in p.get("checks", []):
                out["blind_checks"][c] = out["blind_checks"].get(c, 0) + 1
    return out


if __name__ == "__main__":
    import sys
    inst = sys.argv[1] if len(sys.argv) > 1 else "sentinel"
    print(json.dumps(digest(inst), indent=2))
