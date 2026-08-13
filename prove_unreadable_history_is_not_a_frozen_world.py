"""Reproduction: a commit history we could not READ is not a world that stopped.

Live, 2026-08-13T18:30:23Z: rv_world_merging reported "no state merges in the
last 20 commits" at CRITICAL and woke the repair arm. rappterverse had merged
`[state] apply PR #6365` at 18:20:55Z -- nine minutes earlier -- and every one
of the twenty most recent commits was a state merge. The same tick reported
rv_validation as "no runs and workflow list unreadable": two reads against the
same repository failed together, and only the one that had been taught the
difference said so. Re-running both checks by hand minutes later returned
"last merge 0.2h ago" and "10/10 validations passing" with nothing changed.

gh() returns its default on ANY failure -- non-zero exit, timeout, rate limit,
unparseable stdout -- so `default=[]` made a blind instrument and a genuinely
frozen platform the same value, and the caller stated that value as a fact
ABOUT THE PLATFORM: the one sentence this sentinel exists to say.

The distinction this locks in, for both checks that still had it:

    read failed        -> warn, "cannot read ..."         (nothing to repair)
    read said "empty"  -> critical, the world has stopped (the 19-day stall)

The 19-day freeze is the reason the absent case must KEEP paging, so every
case is exercised in both directions. A fix that silenced absence would pass a
lazier test and blind the check that matters most.

Same error the rest of this file has already been taught: #45 (a dead PR API
read as an empty queue), prove_blind_green (a dead instrument must never read
as green), #51, #58 (a URL outage is not a missing check), #59 (a run history
we could not read is not a repo with no runs).
"""
import sys

import checks

_REAL_GH = checks.gh
_REAL_AW = checks.active_workflows


def restore():
    checks.gh = _REAL_GH
    checks.active_workflows = _REAL_AW


CASES = []


def case(name, want_ok, want_critical, setup, fn, forbid=None):
    setup()
    try:
        got = fn()
    finally:
        restore()
    is_crit = got["severity"] == checks.CRITICAL and not got["ok"]
    good = got["ok"] is want_ok and is_crit is want_critical
    # A verdict can carry the right severity and still tell a lie. The incident
    # was a true-shaped sentence asserting something false about the platform,
    # so assert on the sentence too.
    if forbid and forbid in got["detail"]:
        good = False
    CASES.append((good, name, want_ok, want_critical, got["ok"], is_crit,
                  got["detail"][:52]))


def commits_returning(payload):
    """Patch gh so only the commits read is stubbed; everything else stays real."""
    def stub(args, default=None):
        if args[:1] == ["api"] and "/commits" in args[1]:
            return default if payload is None else payload
        return _REAL_GH(args, default=default)
    checks.gh = stub


def gate_runs_returning(payload):
    """Patch gh so only the gate's `run list` is stubbed."""
    def stub(args, default=None):
        if args[:2] == ["run", "list"]:
            return default if payload is None else payload
        return _REAL_GH(args, default=default)
    checks.gh = stub


def _iso(hours_ago):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc)
            - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def merges(hours_ago):
    return [{"msg": f"[state] apply PR #{6365 - i}", "d": _iso(hours_ago + i)}
            for i in range(20)]


NOISE = [{"msg": "chore: docs", "d": _iso(0.2)}] * 20
GATE_GREEN = [{"conclusion": "success", "createdAt": _iso(0.2)}] * 10

# ── the incident: the read failed, the world is fine ────────────────────────
case("rv_world_merging: commits UNREADABLE -> warn, not a page", False, False,
     lambda: commits_returning(None), checks.world_still_merging,
     forbid="no state merges in the last 20 commits")

# ── the 19-day stall must survive: observed absence still pages ─────────────
case("rv_world_merging: 20 commits, NONE a merge -> still CRITICAL", False, True,
     lambda: commits_returning(NOISE), checks.world_still_merging)
case("rv_world_merging: merging but STALE 5h -> still CRITICAL", False, True,
     lambda: commits_returning(merges(5)), checks.world_still_merging)

# ── control: a live, merging world still reads green ───────────────────────
case("rv_world_merging: CONTROL merged 12min ago -> ok", True, False,
     lambda: commits_returning(merges(0.2)), checks.world_still_merging)

# ── the same defect, same tick, in the gate check ──────────────────────────
case("rv_validation: run list UNREADABLE -> warn, not a page", False, False,
     lambda: (gate_runs_returning(None),
              setattr(checks, "active_workflows",
                      lambda r: ["Validate Agent Action"])),
     checks.action_gate_accepting,
     forbid="active but has zero runs")
case("rv_validation: gate active, ZERO runs observed -> still CRITICAL",
     False, True,
     lambda: (gate_runs_returning([]),
              setattr(checks, "active_workflows",
                      lambda r: ["Validate Agent Action"])),
     checks.action_gate_accepting)
case("rv_validation: gate absent, ZERO runs observed -> ok", True, False,
     lambda: (gate_runs_returning([]),
              setattr(checks, "active_workflows", lambda r: ["Deploy Pages"])),
     checks.action_gate_accepting)
case("rv_validation: CONTROL 10/10 passing -> ok", True, False,
     lambda: gate_runs_returning(GATE_GREEN), checks.action_gate_accepting)

bad = 0
for good, name, w_ok, w_crit, g_ok, g_crit, detail in CASES:
    bad += not good
    print(f"  [{'ok' if good else 'XX'}] {name}\n"
          f"        expected ok={w_ok} critical={w_crit}  "
          f"got ok={g_ok} critical={g_crit}  {detail}")
print(f"\n{len(CASES)-bad}/{len(CASES)} scenarios behaved as specified")
sys.exit(1 if bad else 0)
