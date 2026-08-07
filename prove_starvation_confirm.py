"""Reproduction for #56 — a thin window is not evidence of starvation.

After the 2026-08-06 Actions outage, rb_wf_starved reported
"never succeeded: Overseer (cloud fallback) (1/3 cancelled)" on three
outage-window runs, while that workflow held 5 successes just outside the
window. 40 repo-wide runs span ~7.7h and are shared by 23 workflows, so
`total >= 3` is cleared at exactly the point the sample becomes one thin slice.

The check must still catch the case it was written for: static-api,
cancelled 3/3, never once completed.
"""
import sys
import checks

R_gh = checks.gh
CASES = []


def rig(narrow, wide_by_name):
    """narrow: repo-wide run list. wide_by_name: {workflow: [conclusions]}"""
    def fake(args, default=None):
        if "--workflow" in args:
            name = args[args.index("--workflow") + 1]
            got = wide_by_name.get(name)
            if got is None:
                return default
            return [{"conclusion": c} for c in got]
        return narrow
    checks.gh = fake


def runs(name, concl):
    return [{"name": name, "conclusion": c} for c in concl]


def case(label, want_starved, narrow, wide):
    rig(narrow, wide)
    try:
        got = checks.workflows_never_succeeding("kody-w/rappterbook")
    finally:
        checks.gh = R_gh
    starved = sorted((got or {}).keys())
    ok = starved == sorted(want_starved)
    CASES.append((ok, label, want_starved, starved))


OVERSEER = runs("Overseer (cloud fallback)", ["cancelled", "failure", "failure"])
STATIC = runs("static-api", ["cancelled", "cancelled", "cancelled"])

case("TODAY: outage residue, successes outside the window -> NOT starved",
     [], OVERSEER,
     {"Overseer (cloud fallback)": ["success", "success", "failure", "success"]})

case("THE ORIGINAL CASE: static-api never once completed -> starved",
     ["static-api"], STATIC,
     {"static-api": ["cancelled", "cancelled", "cancelled", "cancelled"]})

case("both present, only the genuinely starved one reported",
     ["static-api"], OVERSEER + STATIC,
     {"Overseer (cloud fallback)": ["success", "success"],
      "static-api": ["cancelled", "cancelled", "cancelled"]})

case("wider lookback unreadable -> keep the candidate, do not clear silently",
     ["Overseer (cloud fallback)"], OVERSEER, {})

case("healthy workflow is never a candidate", [],
     runs("Compute Trending", ["success", "success", "success"]),
     {"Compute Trending": ["success"]})

case("failures only, no cancellations -> not this check's signal", [],
     runs("Slop Cop", ["failure", "failure", "failure"]),
     {"Slop Cop": ["failure", "failure", "failure"]})

bad = 0
for ok, label, want, got in CASES:
    bad += not ok
    print(f"  [{'ok' if ok else 'XX'}] {label}\n        expected {want}  got {got}")
print(f"\n{len(CASES)-bad}/{len(CASES)} scenarios behaved as specified")
sys.exit(1 if bad else 0)
