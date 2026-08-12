"""Reproduction: a run history we could not READ is not a repo with no runs.

Live, 2026-08-12T04:29Z: rb_wf_starved reported "30 active workflows produced
no runs at all" at CRITICAL and woke the repair arm. rappterbook had ~40,000
runs at that moment, and the tick 15 minutes either side was clean. gh() returns
its default on ANY failure -- non-zero exit, timeout, unparseable stdout -- and
`if not runs: return None` collapsed that into the same value as a genuinely
empty repository, which the caller then stated as a fact ABOUT THE REPOSITORY.
The same false page fired on 2026-08-10.

The distinction this locks in:

    read failed        -> warn, "cannot read run history"   (nothing to repair)
    read said "empty"  -> critical, workflows exist but are dead   (#43 stands)

#43 is the reason the absent case must KEEP paging, so every case is exercised
in both directions. A fix that silenced absence would pass a lazier test.
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
    # A verdict can carry the right severity and still tell a lie. The whole
    # incident was a true-shaped sentence asserting something false about the
    # repository, so assert on the sentence too.
    if forbid and forbid in got["detail"]:
        good = False
    CASES.append((good, name, want_ok, want_critical, got["ok"], is_crit,
                  got["detail"][:52]))


def gh_returning(run_list):
    """Patch gh so only `run list` is stubbed; everything else stays real."""
    def stub(args, default=None):
        if args[:2] == ["run", "list"]:
            return default if run_list is None else run_list
        return _REAL_GH(args, default=default)
    checks.gh = stub


ACTIVE = ["static-api", "Compute Trending", "Deploy Pages"]
GREEN = [{"name": "static-api", "conclusion": "success"}] * 5

# ── the incident: the read failed, the repo is fine ─────────────────────────
case("rb_wf_starved: run list UNREADABLE -> warn, not a page", False, False,
     lambda: (gh_returning(None),
              setattr(checks, "active_workflows", lambda r: ACTIVE)),
     checks.rb_workflows_never_succeed,
     forbid="produced no runs at all")
case("rb_workflows: run list UNREADABLE -> warn, not a page", False, False,
     lambda: (gh_returning(None),
              setattr(checks, "active_workflows", lambda r: ACTIVE)),
     checks.workflows_healthy,
     forbid="produced no runs at all")

# ── #43 must survive: observed absence still pages ──────────────────────────
case("rb_wf_starved: workflows exist, ZERO runs -> still CRITICAL", False, True,
     lambda: (gh_returning([]),
              setattr(checks, "active_workflows", lambda r: ACTIVE)),
     checks.rb_workflows_never_succeed)
case("rb_workflows: workflows exist, ZERO runs -> still CRITICAL", False, True,
     lambda: (gh_returning([]),
              setattr(checks, "active_workflows", lambda r: ACTIVE)),
     checks.workflows_healthy)

# ── the other #43 arms, unchanged ───────────────────────────────────────────
case("rb_wf_starved: no workflows defined -> ok", True, False,
     lambda: (gh_returning([]),
              setattr(checks, "active_workflows", lambda r: [])),
     checks.rb_workflows_never_succeed)
case("rb_wf_starved: workflow list unreadable -> warn", False, False,
     lambda: (gh_returning([]),
              setattr(checks, "active_workflows", lambda r: None)),
     checks.rb_workflows_never_succeed)
case("rb_workflows: no workflows defined -> ok", True, False,
     lambda: (gh_returning([]),
              setattr(checks, "active_workflows", lambda r: [])),
     checks.workflows_healthy)
case("rb_workflows: workflow list unreadable -> warn", False, False,
     lambda: (gh_returning([]),
              setattr(checks, "active_workflows", lambda r: None)),
     checks.workflows_healthy)

# ── controls: a real, healthy window still reads green ──────────────────────
case("rb_wf_starved: CONTROL healthy runs -> ok", True, False,
     lambda: gh_returning(GREEN), checks.rb_workflows_never_succeed)
case("rb_workflows: CONTROL healthy runs -> ok", True, False,
     lambda: gh_returning(GREEN), checks.workflows_healthy)

# ── a real defect must still page through the new branch ───────────────────
case("rb_workflows: CONTROL 100% failing -> CRITICAL", False, True,
     lambda: gh_returning([{"name": "Agent Heartbeat",
                            "conclusion": "failure"}] * 4),
     checks.workflows_healthy)

bad = 0
for good, name, w_ok, w_crit, g_ok, g_crit, detail in CASES:
    bad += not good
    print(f"  [{'ok' if good else 'XX'}] {name}\n"
          f"        expected ok={w_ok} critical={w_crit}  "
          f"got ok={g_ok} critical={g_crit}  {detail}")
print(f"\n{len(CASES)-bad}/{len(CASES)} scenarios behaved as specified")
sys.exit(1 if bad else 0)
