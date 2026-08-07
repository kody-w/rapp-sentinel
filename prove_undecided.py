"""Reproduction for #47 — a run with no verdict is not a failed verdict.

rv_validation scored the gate as success / len(runs) over the last 10 runs.
`gh run list` returns queued and in-progress runs with an EMPTY conclusion, so
every run that had not finished yet landed in the denominator and counted
against the gate.

Live failure, 2026-08-06: GitHub declared a major Actions outage at 15:22Z.
rappterverse's window then held 5 successes, 3 runs killed at "Set up job"
before the validator was ever fetched, and 2 still sitting in the queue. The
check reported "5/10 validations passing" at CRITICAL severity and woke the
repair arm — for a repository whose code was fine and whose gate had rejected
precisely nothing. rv_world_merging (2.9h) and rv_pr_queue (11 open) were both
green throughout, which is the pair that actually owns "the platform stalled".

The failing arithmetic is the point: 5/10 = 50% fails the 0.6 bar, while the
same evidence read honestly is 5 of 8 CONCLUDED runs = 62.5% and passes. The
two queued runs voted without having seen anything.

This is the sibling of #43. There it was absence of runs being read as health;
here it is absence of a verdict being read as rejection. Same error, opposite
sign.
"""
import sys
from datetime import datetime, timedelta, timezone

import checks

R = dict(aw=checks.active_workflows, gh=checks.gh)


def restore():
    checks.active_workflows = R["aw"]
    checks.gh = R["gh"]


def run(fn):
    try:
        return fn()
    finally:
        restore()


CASES = []


def case(name, want_ok, setup, fn, want_severity=None):
    setup()
    got = run(fn)
    good = got["ok"] is want_ok
    if want_severity is not None:
        good = good and got["severity"] == want_severity
    CASES.append((good, name, want_ok, got["ok"], got["severity"],
                  str(got["detail"])[:52]))


def feed(runs):
    """Serve a fixed run list, and assert the gate workflow is present so the
    zero-runs branch from #43 is never what we are measuring."""
    checks.gh = lambda a, default=None: list(runs)
    checks.active_workflows = lambda r: ["Validate Agent Action"]


def concl(*pairs):
    """Build a newest-first run list.

    Every run carries a createdAt, descending from now. Without them this whole
    file quietly stopped testing #47: #53 added a staleness guard that returns
    "gate runs carry no timestamps" before any ratio is computed, and these
    fixtures only ever set `conclusion`. 7 of 11 cases went red and the other 4
    passed for the wrong reason -- they expected a failure and got one, from a
    branch they were not written to exercise. A fixture that cannot reach the
    code under test is a dead instrument, same as any other.
    """
    out = []
    for state, n in pairs:
        # A distinct dict per run. `[{...}] * n` aliases ONE dict n times, so
        # stamping createdAt below would give every run the same timestamp.
        out += [{"conclusion": state} for _ in range(n)]
    stamp = datetime.now(timezone.utc)
    for i, r in enumerate(out):
        # One minute apart, so a full 10-run window stays well inside the 3h
        # staleness bar #53 added. Staleness is prove_validation_staleness.py's
        # subject; here it must never be what a case is measuring, the same way
        # feed() pins the workflow present so #43's branch is never in play.
        r["createdAt"] = (stamp - timedelta(minutes=i)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    return out


# ── the exact production window that woke the repair arm ────────────────────
OUTAGE = concl(("", 2), ("failure", 3), ("success", 5))

case("THE BUG: 2 queued + 3 failed + 5 passed is not a rejecting gate", True,
     lambda: feed(OUTAGE), checks.action_gate_accepting)

# ── the non-verdicts must not be able to swing the verdict ──────────────────
case("queued runs alone cannot fail a gate that never failed", True,
     lambda: feed(concl(("", 8), ("success", 2))),
     checks.action_gate_accepting)
case("null conclusion is treated the same as empty string", True,
     lambda: feed(concl((None, 8), ("success", 2))),
     checks.action_gate_accepting)

# ── absence of any verdict is visible, but is not a page ────────────────────
case("every run still in flight: not ok, and NOT critical", False,
     lambda: feed(concl(("", 10))), checks.action_gate_accepting,
     want_severity=checks.WARN)

# ── a genuinely rejecting gate must still fail, loudly ──────────────────────
case("CONTROL a real rejecting gate still fails critically", False,
     lambda: feed(concl(("failure", 7), ("success", 3))),
     checks.action_gate_accepting, want_severity=checks.CRITICAL)
case("CONTROL rejections still fail when queued runs pad the window", False,
     lambda: feed(concl(("", 5), ("failure", 7), ("success", 3))),
     checks.action_gate_accepting, want_severity=checks.CRITICAL)
case("CONTROL all passing is ok", True,
     lambda: feed(concl(("success", 10))), checks.action_gate_accepting)
case("CONTROL exactly at the 0.6 bar is ok", True,
     lambda: feed(concl(("success", 6), ("failure", 4))),
     checks.action_gate_accepting)
case("CONTROL under the 0.6 bar still fails once the burst has cleared, "
     "but only as a warn", False,
     lambda: feed(concl(("success", 5), ("failure", 5))),
     checks.action_gate_accepting, want_severity=checks.WARN)

# ── #43 must not regress: absence of runs is still not health ───────────────
case("REGRESSION #43 gate active with zero runs still fails", False,
     lambda: (setattr(checks, "gh", lambda a, default=None: []),
              setattr(checks, "active_workflows",
                      lambda r: ["Validate Agent Action", "Other"])),
     checks.action_gate_accepting)
case("REGRESSION #43 gate workflow not present is ok", True,
     lambda: (setattr(checks, "gh", lambda a, default=None: []),
              setattr(checks, "active_workflows", lambda r: ["Other"])),
     checks.action_gate_accepting)

# ── the guard that would have caught this file dying ────────────────────────
# Every case above is written to exercise the verdict logic #47 is about. When
# #53 added the staleness guard, these fixtures stopped reaching that logic
# entirely -- and the file went on printing a score, 4 of whose passes were
# accidents. A suite that cannot reach the code it names is a dead instrument.
# Assert reachability explicitly so the next early return fails loudly here
# instead of quietly turning this file into decoration.
_dead = [n for _, n, _, _, _, d in CASES if "carry no timestamps" in d]
CASES.append((not _dead, "no case short-circuits before the verdict logic",
              True, not _dead, checks.WARN,
              "; ".join(_dead)[:52] if _dead else "all cases reached it"))

bad = 0
for good, name, want, got, sev, detail in CASES:
    bad += not good
    print(f"  [{'ok' if good else 'XX'}] {name}\n"
          f"        expected ok={want}  got ok={got} ({sev})  {detail}")
print(f"\n{len(CASES)-bad}/{len(CASES)} scenarios behaved as specified")
sys.exit(1 if bad else 0)
