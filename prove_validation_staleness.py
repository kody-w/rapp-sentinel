"""Reproduction for #52 — a ratio over a dead window is not a live reading.

The 2026-08-06 GitHub outage stopped the gate for 4.6h. The check reported
"5/10 validations passing" -- which reads as "rejecting half the work" when
the truth was "not running at all". Two different problems, one message.
"""
import sys
from datetime import datetime, timedelta, timezone
import checks

R_gh, R_aw = checks.gh, checks.active_workflows
CASES = []


def ago(h):
    return (datetime.now(timezone.utc) - timedelta(hours=h)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def runs(n_success, n_fail, age_h, in_flight=0):
    out = [{"conclusion": "success", "createdAt": ago(age_h + i * 0.01)}
           for i in range(n_success)]
    out += [{"conclusion": "failure", "createdAt": ago(age_h + i * 0.01)}
            for i in range(n_fail)]
    out += [{"conclusion": "", "createdAt": ago(age_h)} for _ in range(in_flight)]
    return out


def case(name, want_ok, rows, want_detail=None, workflows=("Validate Agent Action",)):
    checks.gh = lambda a, default=None: rows
    checks.active_workflows = lambda r: list(workflows)
    try:
        got = checks.action_gate_accepting()
    finally:
        checks.gh, checks.active_workflows = R_gh, R_aw
    good = got["ok"] is want_ok
    if want_detail and want_detail not in got["detail"]:
        good = False
    CASES.append((good, name, want_ok, got["ok"], got["detail"][:48]))


case("fresh window, 10/10 passing", True, runs(10, 0, 0.2), "validations passing")
case("fresh window, 5/10 passing (below 0.6 bar)", False, runs(5, 5, 0.2),
     "validations passing")
case("fresh window, exactly 6/10 (at the bar)", True, runs(6, 4, 0.2))
case("STALE window, 10/10 success -- must NOT read as healthy", False,
     runs(10, 0, 26.0), "has not run in")
case("STALE window, 5/10 -- says stopped, not rejecting", False,
     runs(5, 5, 4.6), "has not run in")
case("just inside window (2.9h)", False, runs(5, 5, 2.9), "validations passing")
case("all runs in flight, none concluded", False, runs(0, 0, 0.1, in_flight=3),
     "in flight")
case("empty history, gate active (#43 behavior preserved)", False, [],
     "active but has zero runs")
case("empty history, gate absent (#43 behavior preserved)", True, [],
     "not present", workflows=("Other",))
case("runs carry no timestamps", False,
     [{"conclusion": "success"}, {"conclusion": "success"}], "no timestamps")

bad = 0
for good, name, want, got, detail in CASES:
    bad += not good
    print(f"  [{'ok' if good else 'XX'}] {name}\n"
          f"        expected ok={want}  got ok={got}  {detail}")
print(f"\n{len(CASES)-bad}/{len(CASES)} scenarios behaved as specified")
sys.exit(1 if bad else 0)
