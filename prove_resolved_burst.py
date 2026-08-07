"""Reproduction for #54 — a ratio cannot tell a resolved burst from a live one.

rv_validation scored the gate as success / len(decided) over the last 10 runs.
A ratio has no opinion about WHEN the failures happened, so a burst that is
completely over still drags the number under the 0.6 bar and pages the repair
arm at CRITICAL.

Live failure, 2026-08-07 00:06Z. rappterverse's window held 5 failures
(15:41-17:48Z, during the same GitHub Actions outage #47 and #52 were written
for) followed by 5 straight successes, the newest 6 minutes old. Reported:

    rv_validation [critical] 5/10 validations passing (0.1h old)

Every part of that is defensible in isolation and the conclusion is still
wrong. The gate was not rejecting work. It had rejected nothing for six hours.

The failures were not even rejections. Run 31117029307's `validate` job died at
"Set up job" -- before the trusted validator was fetched -- and run 31118968285
is recorded as a failure while its OWN `validate` and `validation-gate` jobs
both concluded success; only an infrastructure sibling was cancelled. The gate
did not decide against anything. It was never asked.

#52 cannot catch this: it fires on a window that ENDED hours ago, and this
window is fresh. The runs are recent. The failures are simply behind us.

This is eco_sweep's lesson with the sign flipped. There a lifetime ratio let
one old success hide eight straight failures, and the fix was to judge the
newest runs as a streak (`workflows_currently_broken`). Here a lifetime ratio
lets five old failures hide five straight successes. Same error, opposite sign
-- which is the same relationship #47 has to #43.
"""
import sys
from datetime import datetime, timedelta, timezone

import checks

R_gh, R_aw = checks.gh, checks.active_workflows
CASES = []


def window(*pairs):
    """Newest-first runs, one minute apart so the window is always fresh.

    Staleness belongs to prove_validation_staleness.py; it must never be what a
    case here is measuring.
    """
    out = []
    for state, n in pairs:
        out += [{"conclusion": state} for _ in range(n)]
    stamp = datetime.now(timezone.utc)
    for i, r in enumerate(out):
        r["createdAt"] = (stamp - timedelta(minutes=i)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    return out


def case(name, want_ok, rows, want_severity=None, want_detail=None):
    checks.gh = lambda a, default=None: list(rows)
    checks.active_workflows = lambda r: ["Validate Agent Action"]
    try:
        got = checks.action_gate_accepting()
    finally:
        checks.gh, checks.active_workflows = R_gh, R_aw
    good = got["ok"] is want_ok
    if want_severity is not None:
        good = good and got["severity"] == want_severity
    if want_detail and want_detail not in got["detail"]:
        good = False
    CASES.append((good, name, want_ok, got["ok"], got["severity"],
                  got["detail"][:56]))


# ── the exact production window that woke the repair arm ────────────────────
# 5 newest green, 5 older red. Ratio says 50% and pages; the gate is passing.
case("THE BUG: 5 failures then 5 successes is not a rejecting gate",
     False, window(("success", 5), ("failure", 5)),
     want_severity=checks.WARN)

# It must not be CRITICAL, because CRITICAL is what spends money and takes
# actions against a repository that has done nothing wrong.
case("THE BUG: and it must never be CRITICAL while the newest runs pass",
     False, window(("success", 5), ("failure", 5)),
     want_detail="failures are behind us")

# ── a gate that is actually rejecting must still page, loudly ───────────────
case("CONTROL newest 4 all failed: rejecting NOW, pages critically",
     False, window(("failure", 4), ("success", 6)),
     want_severity=checks.CRITICAL, want_detail="rejecting work")
case("CONTROL a totally broken gate pages critically",
     False, window(("failure", 10)), want_severity=checks.CRITICAL)
case("CONTROL rejecting now, with queued runs padding the front",
     False, window(("", 3), ("failure", 5), ("success", 2)),
     want_severity=checks.CRITICAL, want_detail="rejecting work")

# ── recovery is recovery, at every depth ────────────────────────────────────
# One green run does not clear a streak of failures on its own, but it does
# mean the gate is not currently refusing everything. It stays visible as a
# warn until the burst ages out.
case("a single fresh success breaks the streak, drops to warn",
     False, window(("success", 1), ("failure", 9)),
     want_severity=checks.WARN)
case("deep recovery: 6 green over 4 red is simply healthy",
     True, window(("success", 6), ("failure", 4)))

# ── the streak must be a STREAK, not any 4 failures in the window ───────────
case("scattered failures never form a streak, so never page",
     False, window(("success", 1), ("failure", 1), ("success", 1),
                   ("failure", 1), ("success", 1), ("failure", 1),
                   ("success", 1), ("failure", 1)),
     want_severity=checks.WARN)

# ── ordering must come from timestamps, not list order ──────────────────────
# gh returns newest-first today. The verdict now depends on that, so it is
# derived from createdAt rather than trusted from the list.
_shuffled = window(("success", 5), ("failure", 5))
_shuffled = _shuffled[5:] + _shuffled[:5]      # oldest-first, same evidence
case("REGRESSION order comes from createdAt, not list position",
     False, _shuffled, want_severity=checks.WARN)

# ── a thin window is not a page ─────────────────────────────────────────────
case("too few runs to see a streak stays non-critical",
     False, window(("failure", 2)), want_severity=checks.WARN)

# ── everything earlier in this lineage must survive ─────────────────────────
case("REGRESSION #47 queued runs cannot swing the verdict",
     True, window(("", 2), ("failure", 3), ("success", 5)))
case("REGRESSION #43 gate active with zero runs still fails", False, [],
     want_detail="zero runs")
case("REGRESSION #52 a stale all-green window is not health", False,
     [{"conclusion": "success",
       "createdAt": (datetime.now(timezone.utc) - timedelta(hours=26)).strftime(
           "%Y-%m-%dT%H:%M:%SZ")} for _ in range(10)],
     want_detail="has not run in")
case("CONTROL all green is ok", True, window(("success", 10)))

bad = 0
for good, name, want, got, sev, detail in CASES:
    bad += not good
    print(f"  [{'ok' if good else 'XX'}] {name}\n"
          f"        expected ok={want}  got ok={got} ({sev})  {detail}")
print(f"\n{len(CASES)-bad}/{len(CASES)} scenarios behaved as specified")
sys.exit(1 if bad else 0)
