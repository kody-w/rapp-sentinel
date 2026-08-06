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
    out = []
    for state, n in pairs:
        out += [{"conclusion": state}] * n
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
     lambda: feed([{"conclusion": None}] * 8 + [{"conclusion": "success"}] * 2),
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
case("CONTROL just under the 0.6 bar fails", False,
     lambda: feed(concl(("success", 5), ("failure", 5))),
     checks.action_gate_accepting)

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

bad = 0
for good, name, want, got, sev, detail in CASES:
    bad += not good
    print(f"  [{'ok' if good else 'XX'}] {name}\n"
          f"        expected ok={want}  got ok={got} ({sev})  {detail}")
print(f"\n{len(CASES)-bad}/{len(CASES)} scenarios behaved as specified")
sys.exit(1 if bad else 0)
