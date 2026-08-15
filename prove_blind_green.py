"""Reproduction for #45 — a dead instrument must never read as green.

Before this fix, three checks reported healthy while measuring nothing:

    eco_sweep       'N additional repositories swept, none on a red streak'  (swept 0)
    rv_pr_queue     '0 open PRs'                                            (API dead)
    rb_json_parses  'served state parses'                                   (read 0 files)

Each is paired with a healthy-world control, because the point is not to make
these checks pessimistic -- it is to make "I cannot see" distinct from "all clear".
"""
import sys, urllib.request, urllib.error
from datetime import datetime, timezone
import checks

R = dict(gh=checks.gh, wcb=checks.workflows_currently_broken,
         dr=checks.declared_repos, open=urllib.request.urlopen,
         wcr=checks._write_coverage_receipt)
CASES = []


def restore():
    checks.gh, checks.workflows_currently_broken = R["gh"], R["wcb"]
    checks.declared_repos, urllib.request.urlopen = R["dr"], R["open"]
    checks._write_coverage_receipt = R["wcr"]


def case(name, want_ok, setup, fn, want_detail=None):
    setup()
    try:
        got = fn()
    except Exception as e:
        got = {"ok": None, "detail": f"raised {type(e).__name__}"}
    finally:
        restore()
    behaved = got["ok"] is want_ok
    if want_detail:
        behaved = behaved and want_detail in got["detail"]
    CASES.append((behaved, name, want_ok, got["ok"], got["detail"][:72]))


def sweep_env(bad_for):
    """3 declared repos beyond the deeply-checked pair."""
    checks.declared_repos = lambda: ["kody-w/rapp-1", "kody-w/rapp-moment",
                                     "kody-w/rapp-egg-hub"]
    checks.workflows_currently_broken = bad_for
    checks._write_coverage_receipt = lambda *a, **k: None


# ── eco_sweep ───────────────────────────────────────────────────────────────
case("eco_sweep: instrument dead, every repo returns None", False,
     lambda: sweep_env(lambda repo, **k: None), checks.ecosystem_not_silently_broken)
case("eco_sweep: CONTROL all repos have history, none red", True,
     lambda: sweep_env(lambda repo, **k: {}), checks.ecosystem_not_silently_broken)
case("eco_sweep: CONTROL one repo lacks history, rest clean", True,
     lambda: sweep_env(lambda repo, **k: None if repo.endswith("rapp-1") else {}),
     checks.ecosystem_not_silently_broken)
case("eco_sweep: a genuine red streak still fails", False,
     lambda: sweep_env(lambda repo, **k:
                       {"Build": {"streak": 9}} if repo.endswith("rapp-1") else {}),
     checks.ecosystem_not_silently_broken)
case("eco_sweep: thin history reports the measured failures", False,
     lambda: sweep_env(lambda repo, **k:
                       {"Release": {"streak": 2, "of": 2, "failed": 2,
                                    "insufficient_window": True}}
                       if repo.endswith("rapp-1") else {}),
     checks.ecosystem_not_silently_broken,
     want_detail="Release (2/2 observed runs failed)")

# ── rv_pr_queue ─────────────────────────────────────────────────────────────
def pr_queue(size):
    created_at = datetime.now(timezone.utc).isoformat()
    return [
        {"number": number, "title": f"PR {number}", "createdAt": created_at}
        for number in range(1, size + 1)
    ]


case("rv_pr_queue: API dead (gh returns default)", False,
     lambda: setattr(checks, "gh", lambda a, default=None: default),
     checks.queue_draining)
case("rv_pr_queue: CONTROL genuinely empty queue", True,
     lambda: setattr(checks, "gh", lambda a, default=None: []), checks.queue_draining)
case("rv_pr_queue: CONTROL the 679 pile-up still fails", False,
     lambda: setattr(checks, "gh", lambda a, default=None: pr_queue(679)),
     checks.queue_draining)

# ── rb_json_parses ──────────────────────────────────────────────────────────
def net_dead():
    def boom(req, timeout=None):
        raise urllib.error.URLError("network down")
    urllib.request.urlopen = boom


import io
case("rb_json_parses: every fetch fails, nothing examined", False,
     net_dead, checks.rb_served_json_parses)
case("rb_json_parses: CONTROL all five parse", True,
     lambda: setattr(urllib.request, "urlopen",
                     lambda req, timeout=None: io.BytesIO(b'{"a":1}')),
     checks.rb_served_json_parses)
case("rb_json_parses: CONTROL conflict markers still fail", False,
     lambda: setattr(urllib.request, "urlopen",
                     lambda req, timeout=None: io.BytesIO(b'<<<<<<< HEAD')),
     checks.rb_served_json_parses)

bad = 0
for good, name, want, got, detail in CASES:
    bad += not good
    print(f"  [{'ok' if good else 'XX'}] {name}\n"
          f"        expected ok={want}  got ok={got}  {detail}")
print(f"\n{len(CASES)-bad}/{len(CASES)} scenarios behaved as specified")
sys.exit(1 if bad else 0)
