#!/usr/bin/env python3
"""prove_framework_invariants.py — the §6d primitives fire, and the rules exist.

A guard nobody has watched fail is indistinguishable from a guard that cannot
fail (#16). This proves the two new helpers behave on every branch, that the
colour-blind judge closes a class the old filter provably missed, and that the
three numbered invariants other repos may cite by anchor are still in
TRIFECTA-PATTERN.md — a doc rule cannot fire, but it can silently vanish in a
rewrite, which is the same failure wearing prose.

Run: python3 prove_framework_invariants.py   (exit 0 only when every scenario
behaves; any deviation exits 1 and says which claim broke)
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import checks as C

HERE = Path(__file__).resolve().parent
FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


def iso_hours_ago(h):
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat(
        timespec="seconds")


# ── moving() — three failure sentences for three different claims ───────────

r = C.moving("m", lambda: iso_hours_ago(1), 12, what="thing")
scenario("moving: 1h-old stamp against a 12h bar is ok",
         r["ok"] and "1.0h old" in r["detail"], r["detail"])

r = C.moving("m", lambda: iso_hours_ago(13), 12, what="thing")
scenario("moving: 13h against 12h is CRITICAL stale",
         (not r["ok"]) and r["severity"] == C.CRITICAL and "stale" in r["detail"],
         f"severity={r['severity']} {r['detail']}")

r = C.moving("m", lambda: None, 12, what="thing")
scenario("moving: a stampless output is CRITICAL — we read it and it cannot testify",
         (not r["ok"]) and r["severity"] == C.CRITICAL
         and "no timestamp" in r["detail"], f"severity={r['severity']} {r['detail']}")


def _raises():
    raise OSError("connection dropped")


r = C.moving("m", _raises, 12, what="thing")
scenario("moving: a reader that raises is WARN — blind is not broken (#45/#51)",
         (not r["ok"]) and r["severity"] == C.WARN
         and "cannot read" in r["detail"], f"severity={r['severity']} {r['detail']}")

r = C.moving("m", lambda: "not-a-date", 12, what="thing")
scenario("moving: an unparseable stamp fails rather than passing vacuously",
         not r["ok"], r["detail"])

# ── require_success() — positive evidence, colour-blind about failure ───────

v = C.require_success(["success", "failure", "failure"])
scenario("require_success: one explicit success is success", v == "success", v)

v = C.require_success(["cancelled"] * 3)
scenario("require_success: cancelled 3/3 is no-success (the static-api case)",
         v == "no-success", v)

v = C.require_success(["skipped"] * 3)
scenario("require_success: skipped 3/3 is no-success — a colour nobody enumerated",
         v == "no-success", v)

v = C.require_success(["timed_out"] * 3)
scenario("require_success: timed_out 3/3 is no-success", v == "no-success", v)

v = C.require_success(["failure", None, None])
scenario("require_success: one decided run is UNDECIDED, not health and not defect",
         v is C.UNDECIDED, repr(v))

v = C.require_success(None)
scenario("require_success: a failed read is UNREADABLE, never a verdict",
         v is C.UNREADABLE, repr(v))

# ── the closed-class demonstration ──────────────────────────────────────────
# The candidate filter this commit replaced, quoted faithfully from the
# pre-fix workflows_never_succeeding (see git history of checks.py):
#
#     candidates = {n: v for n, v in per.items()
#                   if v["total"] >= 3 and v["ok"] == 0 and v["cancelled"] > 0}
#
# Fed an all-skipped history, it finds nothing — ok == 0, cancelled == 0 —
# while require_success() calls the same history no-success. That difference
# IS the fixed defect, demonstrated rather than asserted.

old_per = {"Static API": {"ok": 0, "total": 3, "cancelled": 0}}
old_candidates = {n: v for n, v in old_per.items()
                  if v["total"] >= 3 and v["ok"] == 0 and v["cancelled"] > 0}
new_verdict = C.require_success(["skipped"] * 3)
scenario("closed class: the OLD cancelled>0 filter misses all-skipped; the new judge does not",
         old_candidates == {} and new_verdict == "no-success",
         f"old filter found {old_candidates!r}; require_success says {new_verdict!r}")

# ── the §6d anchors exist, and their absence is detectable ──────────────────

ANCHORS = ("R1 — A receipt is not evidence",
           "R2 — Ran is not worked",
           "R3 — Require known-good")
doc = (HERE / "TRIFECTA-PATTERN.md").read_text(encoding="utf-8")
missing = [a for a in ANCHORS if a not in doc]
scenario("doc: all three §6d anchors present in TRIFECTA-PATTERN.md",
         not missing, f"missing={missing!r}" if missing else "all anchors found")

# Break leg: delete R2's heading in a throwaway copy and confirm the same
# comparison notices — proving the assertion above can actually fail.
with tempfile.TemporaryDirectory() as td:
    broken = doc.replace("R2 — Ran is not worked", "")
    p = Path(td) / "TRIFECTA-PATTERN.md"
    p.write_text(broken, encoding="utf-8")
    still_missing = [a for a in ANCHORS if a not in p.read_text(encoding="utf-8")]
    scenario("doc break-leg: deleting R2's heading in a copy is detected",
             still_missing == ["R2 — Ran is not worked"], f"detected={still_missing!r}")

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
