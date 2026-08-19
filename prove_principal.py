#!/usr/bin/env python3
"""Reproduction for principal.py — the rubric must be right about what it can see.

WAR STORY. First round of visits (2026-08-18): the principal found a storykeeper whose
tick had been dead for 7 hours while its own record verified clean, an estate sentinel
with ten alerts rotting behind an unreadable chat.db, and an empty classroom on the
battlestation. None of those were lies by the sentinels — they were the sentinels' OWN
blind spots, visible only from the back of the room. This proof pins the rubric so a
future edit cannot quietly grade absence as attendance.

Legs (evaluate() on fixtures; no ssh, no model):
  1. CONTROL   — fresh tick, chains ok, no fails, budgets fine → A, no penalty notes.
  2. ABSENT    — last_run older than 3× interval → attendance 0, grade drops.
  3. FROZEN    — last_run identical to the previous visit >2 intervals ago → attendance 0.
  4. BROKEN    — a chain_ok False or a truncated anchor → record 0.
  5. STANDING  — the same check red now and ≥6h ago → job penalised; a NEW red less so.
  6. LIAR      — status ok while failed non-empty → honesty 0.
  7. EMPTY     — no direction/verdict → 'empty classroom' note, F.
  8. VISITS    — overdue classrooms are chosen first; nobody escapes within the window.
"""
import json
from datetime import datetime, timedelta, timezone

import principal as P

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
ROOM = {"slug": "t", "name": "Teacher", "interval_s": 900}


def snap(age_min=5, status="degraded", failed=(), critical=(), chain_ok=True, truncated=False, queued=0, direction=True):
    at = (NOW - timedelta(minutes=age_min)).isoformat(timespec="seconds")
    return {
        "home": "/x", "last_run": {"at": at, "status": status, "failed": list(failed)},
        "last_verdict": {"status": status, "failed": list(failed), "critical": list(critical),
                         "checks": [{"id": "a", "ok": True}, {"id": "b", "ok": "b" not in failed}]},
        "roll_call": {"brainstem": {"chain_ok": chain_ok, "frames": 3, "age_minutes": 5, "alive": True}},
        "anchors": {"brainstem": {"truncated": truncated}},
        "direction": {"cares_about": ["x"]} if direction else None,
        "config": {"level": 1, "notify": True, "daily_escalation_budget": 8},
        "escalations": {}, "outbox_last_drain": {"at": (NOW - timedelta(hours=8)).isoformat(), "sent": 0, "kept": queued, "why": "x"} if queued else None,
        "outbox_queued": queued, "log_tail": [],
    }


def expect(c, m):
    print(("  ok   " if c else "  FAIL ") + m)
    if not c:
        raise SystemExit(1)


print("1. CONTROL")
o = P.evaluate(ROOM, snap(), None, NOW); expect(o["grade"] == "A" and o["score"] == 100, "A/100: %s" % o["notes"])
print("2. ABSENT")
o = P.evaluate(ROOM, snap(age_min=90), None, NOW); expect(o["points"]["attendance"] == 0 and o["grade"] != "A", "attendance 0: %s" % o["notes"][0])
print("3. FROZEN")
prev = {"utc": (NOW - timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "evidence": {"last_run_at": (NOW - timedelta(minutes=5)).isoformat(timespec="seconds"), "failed": []}}
o = P.evaluate(ROOM, snap(age_min=5), prev, NOW); expect(o["points"]["attendance"] == 0 and any("frozen" in n for n in o["notes"]), "frozen last_run → 0")
print("4. BROKEN")
o = P.evaluate(ROOM, snap(chain_ok=False), None, NOW); expect(o["points"]["record"] == 0, "broken chain → record 0")
o = P.evaluate(ROOM, snap(truncated=True), None, NOW); expect(o["points"]["record"] == 0, "truncation → record 0")
print("5. STANDING RED")
prev = {"utc": (NOW - timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%S.000Z"), "evidence": {"last_run_at": "x", "failed": ["b"]}}
o_stand = P.evaluate(ROOM, snap(failed=["b"]), prev, NOW)
o_new = P.evaluate(ROOM, snap(failed=["b"]), None, NOW)
expect(o_stand["points"]["job"] < o_new["points"]["job"] and any("standing red" in n for n in o_stand["notes"]),
       "standing %d < new %d" % (o_stand["points"]["job"], o_new["points"]["job"]))
print("6. LIAR")
o = P.evaluate(ROOM, snap(status="ok", failed=["b"]), None, NOW); expect(o["points"]["honesty"] == 0, "ok-with-failures → honesty 0")
print("7. EMPTY")
o = P.evaluate(ROOM, {"home": "/nowhere"}, None, NOW); expect(o["grade"] == "F" and any("empty classroom" in n for n in o["notes"]), "empty → F")
print("8. VISITS")
rooms = [{"slug": s} for s in "abcd"]
card = {"classrooms": {"a": {"latest": {"utc": NOW.strftime("%Y-%m-%dT%H:%M:%S.000Z")}},
                       "b": {"latest": {"utc": (NOW - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}}}}
picks = P.choose_visits(rooms, card, {"visits_per_tick": 2, "visit_everyone_within_hours": 24}, NOW, seed=1)
expect("b" in [p["slug"] for p in picks] and "a" not in [p["slug"] for p in picks][:1], "overdue first: %s" % [p["slug"] for p in picks])
print("all legs held")
