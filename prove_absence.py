"""Reproduction for #43 — absence is not health.

Before this fix all three checks returned ok on empty run history, so a
deleted, disabled, or trigger-broken workflow stayed green forever. The
mutation batches never caught it because they only broke things by making
them FAIL, never by making them ABSENT.

Each check is exercised across all four states.
"""
import sys
import checks

R = dict(wfer=checks.workflows_failing_every_run,
         wns=checks.workflows_never_succeeding,
         aw=checks.active_workflows, gh=checks.gh)


def restore():
    checks.workflows_failing_every_run = R["wfer"]
    checks.workflows_never_succeeding = R["wns"]
    checks.active_workflows = R["aw"]
    checks.gh = R["gh"]


def run(fn):
    try:
        return fn()
    finally:
        restore()


CASES = []


def case(name, want_ok, setup, fn):
    setup()
    got = run(fn)
    ok = got["ok"] is want_ok
    CASES.append((ok, name, want_ok, got["ok"], got["detail"][:46]))


# ── rb_workflows ────────────────────────────────────────────────────────────
def _no_hist():
    checks.workflows_failing_every_run = lambda *a, **k: None


case("rb_workflows: workflows exist, ZERO runs", False,
     lambda: (_no_hist(), setattr(checks, "active_workflows",
              lambda r: ["Compute Trending", "Deploy Pages"])),
     checks.workflows_healthy)
case("rb_workflows: no workflows defined", True,
     lambda: (_no_hist(), setattr(checks, "active_workflows", lambda r: [])),
     checks.workflows_healthy)
case("rb_workflows: workflow list unreadable", False,
     lambda: (_no_hist(), setattr(checks, "active_workflows", lambda r: None)),
     checks.workflows_healthy)
case("rb_workflows: CONTROL runs exist, none broken", True,
     lambda: setattr(checks, "workflows_failing_every_run", lambda *a, **k: {}),
     checks.workflows_healthy)

# ── rb_wf_starved ───────────────────────────────────────────────────────────
def _no_hist2():
    checks.workflows_never_succeeding = lambda *a, **k: None


case("rb_wf_starved: workflows exist, ZERO runs", False,
     lambda: (_no_hist2(), setattr(checks, "active_workflows",
              lambda r: ["static-api"])),
     checks.rb_workflows_never_succeed)
case("rb_wf_starved: no workflows defined", True,
     lambda: (_no_hist2(), setattr(checks, "active_workflows", lambda r: [])),
     checks.rb_workflows_never_succeed)
case("rb_wf_starved: CONTROL runs exist, all healthy", True,
     lambda: setattr(checks, "workflows_never_succeeding", lambda *a, **k: {}),
     checks.rb_workflows_never_succeed)

# ── rv_validation ───────────────────────────────────────────────────────────
case("rv_validation: gate ACTIVE with zero runs", False,
     lambda: (setattr(checks, "gh", lambda a, default=None: []),
              setattr(checks, "active_workflows",
                      lambda r: ["Validate Agent Action", "Other"])),
     checks.action_gate_accepting)
case("rv_validation: gate workflow not present", True,
     lambda: (setattr(checks, "gh", lambda a, default=None: []),
              setattr(checks, "active_workflows", lambda r: ["Other"])),
     checks.action_gate_accepting)
case("rv_validation: CONTROL runs exist, passing", True,
     lambda: setattr(checks, "gh",
                     lambda a, default=None: [{"conclusion": "success"}] * 10),
     checks.action_gate_accepting)

bad = 0
for good, name, want, got, detail in CASES:
    bad += not good
    print(f"  [{'ok' if good else 'XX'}] {name}\n"
          f"        expected ok={want}  got ok={got}  {detail}")
print(f"\n{len(CASES)-bad}/{len(CASES)} scenarios behaved as specified")
sys.exit(1 if bad else 0)
