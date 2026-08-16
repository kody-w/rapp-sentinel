#!/usr/bin/env python3
"""prove_sweep_findings.py — the 2026-08-16 review sweep's confirmed defects,
each reproduced against the fixed code.

An adversarial review of the day's delta (PRs #73-#87) confirmed a set of
defects that had survived every per-PR gate — including one latch that was
live on the running organism within an hour of merging. Per the house rule,
each fix ships with the reproduction that made it fire; these are grouped
because they share one provenance and one commit.

Run: python3 prove_sweep_findings.py   (exit 0 only on all-behaved)
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import baseline as B
import checks as C
import sentinel as S

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


def iso_ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds")


# ── 1. within_budget: smoke and evolve rows never eat repair slots ──────────
hist = ([{"at": iso_ago(1), "mode": "smoke", "result": "x"}]
        + [{"at": iso_ago(2), "mode": "evolve", "result": "x"}] * 2
        + [{"at": iso_ago(3), "mode": "repair", "result": "x"}] * 3
        + [{"at": iso_ago(4), "key": "k", "result": "SKIPPED", "skipped": True}])
okb, used = S.within_budget(hist, {"daily_escalation_budget": 8})
scenario("within_budget counts only repair/diagnose rows (3), never smoke/evolve/skipped",
         okb and used == 3, f"used={used}")
# The pre-fix arithmetic, quoted: every non-skipped row counted.
old_used = len([h for h in hist if not h.get("skipped")])
scenario("the OLD counter would have charged 6 of 8 slots for 3 repairs",
         old_used == 6, f"old_used={old_used}")

# ── 2. the smoke latch: warn-degraded ticks still reach the smoke arm ───────
import inspect
src_main = inspect.getsource(S.main)
scenario("latch: the smoke arm is gated on 'no criticals', not on 'healthy'",
         'if not verdict["critical"]:' in src_main
         and "smoked = bool(outsider_smoke(cfg))" in src_main,
         "gate present" if "smoked" in src_main else "MISSING")
scenario("latch: evolve is deferred on a tick that already smoked (run.sh ceiling)",
         "evolve deferred" in src_main, "deferral present")

# ── 3. the rotation never wedges on a blocked platform ──────────────────────
src_smoke = inspect.getsource(S.outsider_smoke)
after_block = src_smoke.split("if not allowed:")[1].split("return")[0]
scenario("rotation: the issue_allowed-blocked branch advances the turn before returning",
         'turn["i"] += 1' in after_block, "advance present in blocked branch")

# ── 4. run_health: a timeout is a verdict, never a crash-page ────────────────
import subprocess as _sp
real_run = _sp.run
def _hang(*a, **k):
    raise _sp.TimeoutExpired(cmd="health.py", timeout=600)
_sp.run = _hang
try:
    v = S.run_health()
finally:
    _sp.run = real_run
scenario("run_health timeout -> degraded verdict with health_runtime, no exception",
         v["status"] == "degraded" and v["failed"] == ["health_runtime"]
         and v["critical"] == [], json.dumps(v["summary"])[:90])

# ── 5. a malformed attests_for shape refuses properly, and the publish is
#      isolated from the witnessing steps ───────────────────────────────────
import neighborhood as NB
TMP = Path(tempfile.mkdtemp(prefix="prove-sweep-"))
real_home = NB.HOME
NB.HOME = TMP
(TMP / "config.json").write_text(json.dumps({"attests_for": ["not", "a", "map"]}),
                                 encoding="utf-8")
try:
    shaped = False
    try:
        NB._attests_for()
    except ValueError as e:
        shaped = "must be an object" in str(e)
    except AttributeError:
        shaped = False
    scenario("list-shaped attests_for -> deliberate ValueError, not AttributeError",
             shaped, "ValueError with shape message" if shaped else "wrong exception")
finally:
    NB.HOME = real_home
scenario("publish is isolated: sentinel wraps publish_head+hook in its own try",
         "head publish refused/failed" in src_main, "inner try present")

# ── 6. w_test_baseline survives an undated compare receipt ──────────────────
real_bp, real_cp, real_enr = B.baseline_path, B.compare_path, B.enrollment
B.baseline_path = lambda slug: TMP / f"tb-{slug}.json"
B.compare_path = lambda slug: TMP / f"tc-{slug}.json"
B.enrollment = lambda: {"demo": {"repo": "x", "test_cmd": ["true"]}}
try:
    B.baseline_path("demo").write_text(json.dumps(
        {"schema": B.SCHEMA, "commit": "abc", "utc": iso_ago(24),
         "clone": "full", "cwd_class": "non-world-writable",
         "os": sys.platform, "failures": []}), encoding="utf-8")
    B.compare_path("demo").write_text(json.dumps(
        {"newly_failing": [], "newly_passing": []}), encoding="utf-8")
    r = C.test_baseline_honest()
    scenario("undated compare receipt -> named finding, not a TypeError crash",
             (not r["ok"]) and "undated" in r["detail"], r["detail"][:100])
finally:
    B.baseline_path, B.compare_path, B.enrollment = real_bp, real_cp, real_enr

# ── 7. parse_failing_ids reads BOTH unittest header formats ──────────────────
OUT_39 = "FAIL: test_bad (test_demo.T)\nERROR: test_worse (test_demo.T)\nRan 3 tests in 0.001s\n"
OUT_314 = "FAIL: test_bad (test_demo.T.test_bad)\nRan 3 tests in 0.001s\n"
ids39, n39 = B.parse_failing_ids(OUT_39)
ids314, _ = B.parse_failing_ids(OUT_314)
scenario("3.9 format (method outside parens): full ids, two tests stay two",
         ids39 == ["test_demo.T.test_bad", "test_demo.T.test_worse"] and n39 == 3,
         f"{ids39}")
scenario("3.11+ format: unchanged", ids314 == ["test_demo.T.test_bad"], f"{ids314}")
# The pre-fix regex, quoted: it kept only the parens, collapsing both
# 3.9-format failures into one classname — the count-without-a-set defect
# inside the tool built against it.
import re as _re
old_ids = sorted({m.group(1) for m in _re.finditer(
    r"^(?:FAIL|ERROR): \S+ \(([^)]+)\)", OUT_39, _re.M)})
scenario("the OLD regex collapsed two 3.9 failures into one classname",
         old_ids == ["test_demo.T"], f"{old_ids}")

# ── 8. workflows_failing_every_run: in-flight runs are non-verdicts ─────────
real_gh = C.gh
C.gh = lambda args, default=None: (
    [{"name": "Deploy", "conclusion": "failure"}] * 5
    + [{"name": "Deploy", "conclusion": None}])
try:
    broken = C.workflows_failing_every_run("x/y")
finally:
    C.gh = real_gh
scenario("5 failures + 1 in-flight run still reads as 100% failing",
         broken == {"Deploy": {"fail": 5, "total": 5}}, f"{broken}")

# ── 9. pagescan: one 503 sample and one dropped page connection are retried ─
import pagescan as PS
calls = {"n": 0}
def flaky_503(url, method="GET", headers=None):
    calls["n"] += 1
    return (503, "") if calls["n"] == 1 else (200, "")
scenario("probe retries a single 5xx before believing it",
         PS.probe("https://x/y.json", flaky_503) == 200, f"fetches={calls['n']}")
calls["n"] = 0
def flaky_drop(url, method="GET", headers=None):
    calls["n"] += 1
    return (0, "") if calls["n"] == 1 else (200, "<html></html>")
page = PS.scan_page("https://x/", flaky_drop)
scenario("scan_page retries a dropped page connection once",
         page["status"] == 200, f"status={page['status']} fetches={calls['n']}")

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
