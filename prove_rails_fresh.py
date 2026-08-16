#!/usr/bin/env python3
"""prove_rails_fresh.py — the scaffolding registry fires on age, on drift in
both directions, and never reads blind as green.

Issue #6: a constraint written against a weaker system is a liability once
the system improves, and nothing in the loop notices the day it flips.
rails_report judges a hand-maintained registry of dated rails against the
upstream repo it describes. This proves every verdict class over synthetic
registries and a fake fetcher — no network — including the two edges that
would rot silently: a never-reviewed rail is judged from `written` (null is
legal, not a free pass), and a fetch failure is counted as unverifiable
rather than quietly skipped.

Run: python3 prove_rails_fresh.py   (exit 0 only on all-behaved)
"""

import sys
from datetime import datetime, timedelta, timezone

import checks as C

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


def iso_months_ago(m):
    return (datetime.now(timezone.utc)
            - timedelta(days=m * 30.4375)).strftime("%Y-%m-%d")


def rail(rid, written_months_ago=1, last_reviewed=None, path="scripts/a.py",
         anchor="def rail_a", ledger=None, guarding="weak-model slop"):
    return {"id": rid, "written": iso_months_ago(written_months_ago),
            "last_reviewed": last_reviewed, "review_note": "",
            "guarding_against": guarding,
            "source": {"path": path, "must_contain": anchor},
            "rejection_ledger": ledger}


def registry(*rails_):
    return {"schema": "rapp-scaffolding/1.0", "review_after_months": 6,
            "rails": list(rails_)}


# The fake upstream: every scenario serves the quality config too, because
# the ban-list cross-check reads it and an absent config is (correctly) an
# unverifiable finding, which would contaminate unrelated scenarios.
def fetcher(files, config='{"banned_phrases": ["x y"]}'):
    served = dict(files)
    served.setdefault(C.BAN_LIST_CONFIG, config)

    def fetch(path):
        if path not in served:
            raise OSError(f"fake fetch: no such path {path}")
        return served[path]
    return fetch


CLAIMING = rail("banned_phrase_lists", 1, path="scripts/qg.py",
                anchor="banned_phrases")

# (1) Control: fresh rails, anchors present -> ok, and the unmeasured gap
# stays VISIBLE in the green detail rather than only appearing on red days.
r = C.rails_report(
    registry(rail("rail_a", 2), CLAIMING),
    fetcher({"scripts/a.py": "def rail_a(): pass",
             "scripts/qg.py": "banned_phrases = []"}))
scenario("(1) fresh + matching control is ok, unmeasured count in the detail",
         r["ok"] and "2 rails with no rejection ledger (unmeasured)" in r["detail"],
         r["detail"])

# (2) A 7-months-unreviewed rail (last_reviewed null -> judged from written)
# fires BY NAME with its age and what it was guarding against.
r = C.rails_report(
    registry(rail("grounded_refs", 7, guarding="hallucinated file paths"),
             CLAIMING),
    fetcher({"scripts/a.py": "def rail_a(): pass",
             "scripts/qg.py": "banned_phrases = []"}))
scenario("(2) 7-months-unreviewed rail fires at WARN naming rail, age, target",
         (not r["ok"]) and r["severity"] == C.WARN
         and "grounded_refs" in r["detail"]
         and "unreviewed for" in r["detail"] and "months (bar 6)" in r["detail"]
         and "hallucinated file paths" in r["detail"],
         f"severity={r['severity']} {r['detail']}")

# (3) The registry itself unreadable -> warn. Blind is never green (#45).
r = C.rails_report(None, fetcher({}))
scenario("(3) absent registry -> warn 'unreadable', never green",
         (not r["ok"]) and r["severity"] == C.WARN
         and "unreadable" in r["detail"],
         f"severity={r['severity']} {r['detail']}")

# (4) Drift, direction one: the anchor VANISHED upstream. Either the rail
# was stripped without the registry hearing about it, or it moved and the
# row is stale — both are findings, not calms.
r = C.rails_report(
    registry(rail("rail_a", 2), CLAIMING),
    fetcher({"scripts/a.py": "the function was refactored away",
             "scripts/qg.py": "banned_phrases = []"}))
scenario("(4) vanished must_contain fires 'no longer found upstream'",
         (not r["ok"]) and "rail_a" in r["detail"]
         and "no longer found upstream" in r["detail"],
         r["detail"])

# (5) Drift, direction two: the fetch RAISED. Counted as unverifiable and
# failed at warn — an unreadable source file must never read as verified.
r = C.rails_report(
    registry(rail("rail_a", 2), CLAIMING),
    fetcher({"scripts/qg.py": "banned_phrases = []"}))  # scripts/a.py raises
scenario("(5) fetch raising -> 'unverifiable' in the detail, warn not ok",
         (not r["ok"]) and r["severity"] == C.WARN
         and "unverifiable: 1" in r["detail"] and "rail_a" in r["detail"],
         f"severity={r['severity']} {r['detail']}")

# (6) A review BUMP clears the age red: written 8 months ago, reviewed last
# month -> ok. Clearing the red is a dated human decision, which is the
# check's entire theory of action.
r = C.rails_report(
    registry(rail("rail_a", 8, last_reviewed=iso_months_ago(1)), CLAIMING),
    fetcher({"scripts/a.py": "def rail_a(): pass",
             "scripts/qg.py": "banned_phrases = []"}))
scenario("(6) old rail with a recent last_reviewed bump is ok",
         r["ok"], r["detail"])

# (7) Never-reviewed is judged from written, not treated specially: the
# same null last_reviewed passes at 2 months and fires at 8.
fresh = C.rails_report(
    registry(rail("young", 2), CLAIMING),
    fetcher({"scripts/a.py": "def rail_a(): pass",
             "scripts/qg.py": "banned_phrases = []"}))
stale = C.rails_report(
    registry(rail("young", 8), CLAIMING),
    fetcher({"scripts/a.py": "def rail_a(): pass",
             "scripts/qg.py": "banned_phrases = []"}))
scenario("(7) null last_reviewed: 2-month rail ok, 8-month rail fires",
         fresh["ok"] and (not stale["ok"]) and "young" in stale["detail"],
         f"2mo ok={fresh['ok']}; 8mo {stale['detail'][:90]}")

# (8) The one machine-readable cross-check: a served ban list no registry
# row claims is a rail filtering output with no row dating it.
r = C.rails_report(
    registry(rail("rail_a", 2)),   # no claiming row for banned_phrases
    fetcher({"scripts/a.py": "def rail_a(): pass"}))
scenario("(8) served ban list with no claiming rail row fires",
         (not r["ok"]) and "banned_phrases" in r["detail"]
         and "no claiming rail" in r["detail"], r["detail"])

# (9) An empty rails list is an audit that has not happened, never
# "no rails exist" — the vacuous-green shape (#45) wearing a schema.
r = C.rails_report({"schema": "rapp-scaffolding/1.0", "rails": []},
                   fetcher({}))
scenario("(9) empty rails list -> warn 'audit has not happened', not green",
         (not r["ok"]) and r["severity"] == C.WARN
         and "audit has not happened" in r["detail"],
         f"severity={r['severity']} {r['detail']}")

# (10) The COMMITTED registry parses and every row carries the fields the
# report judges — the synthetic worlds above prove the logic, this proves
# the artifact we actually shipped is judgeable by it.
import json
doc = json.loads(C.SCAFFOLDING_REGISTRY.read_text(encoding="utf-8"))
rows = doc.get("rails") or []
well_formed = rows and all(
    row.get("id") and row.get("written")
    and (row.get("source") or {}).get("path")
    and (row.get("source") or {}).get("must_contain")
    and row.get("guarding_against")
    for row in rows)
scenario("(10) committed scaffolding/rappterbook.json is judgeable "
         f"({len(rows)} rails)", bool(well_formed),
         ", ".join(str(row.get("id")) for row in rows))

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
