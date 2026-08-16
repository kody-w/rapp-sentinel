#!/usr/bin/env python3
"""prove_test_baseline.py — baselines are dated, environment-stamped and
SET-based, and the two phantom-failure sources are refused, observed firing.

The decisive scenario is the 7 == 7 lie: a baseline of {a..g} against a
current of {a..f, x} — the counts match at seven and the platform has both
a real regression (x) and a silent fix (g). A count-based comparison calls
that "matches the baseline"; the check must name both sets.

The live leg builds a real git fixture with a three-test suite, records,
flips a test, compares, and watches the exact newly_failing id surface —
then breaks the environment both measured ways (shallow clone,
world-writable parent) and watches record REFUSE rather than write a
plausible wrong number.

Run: python3 prove_test_baseline.py   (exit 0 only on all-behaved)
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import baseline as B
import checks as C

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


def iso_ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds")


# ── synthetic leg: the check judges receipts, sets not counts ───────────────

TMP = Path(tempfile.mkdtemp(prefix="prove-baseline-"))
real = dict(bp=B.baseline_path, cp=B.compare_path, enr=B.enrollment)
B.baseline_path = lambda slug: TMP / f"test_baseline-{slug}.json"
B.compare_path = lambda slug: TMP / f"test_compare-{slug}.json"
B.enrollment = lambda: {"demo": {"repo": "x", "test_cmd": ["true"]}}


def write_receipts(base=None, comp=None):
    for p in (B.baseline_path("demo"), B.compare_path("demo")):
        if p.exists():
            p.unlink()
    if base is not None:
        B.baseline_path("demo").write_text(json.dumps(base), encoding="utf-8")
    if comp is not None:
        B.compare_path("demo").write_text(json.dumps(comp), encoding="utf-8")


BASE7 = {"schema": B.SCHEMA, "commit": "abc123", "utc": iso_ago(30),
         "clone": "full", "cwd_class": "non-world-writable", "os": sys.platform,
         "failures": ["t.a", "t.b", "t.c", "t.d", "t.e", "t.f", "t.g"],
         "collected": 70}

try:
    # THE decisive scenario: counts equal at 7, sets differ.
    write_receipts(BASE7, {"utc": iso_ago(1), "scheduled": False,
                           "newly_failing": ["t.x"], "newly_passing": ["t.g"]})
    r = C.test_baseline_honest()
    scenario("7==7 lie: equal counts, different sets -> FAILS naming both directions",
             (not r["ok"]) and "t.x" in r["detail"] and "t.g" in r["detail"]
             and "newly failing" in r["detail"],
             r["detail"][:140])

    # Identical sets: ok, with the baseline's age printed even when green.
    write_receipts(BASE7, {"utc": iso_ago(2), "scheduled": False,
                           "newly_failing": [], "newly_passing": []})
    r = C.test_baseline_honest()
    scenario("identical sets -> ok, age printed even when green",
             r["ok"] and "d old" in r["detail"] and "0 newly failing" in r["detail"],
             r["detail"][:140])

    # Environment mismatch: the comparison REFUSED, and the check says so.
    write_receipts(BASE7, {"utc": iso_ago(1), "scheduled": False,
                           "refused": "environment mismatch on cwd_class "
                                      "('non-world-writable' vs 'world-writable')"})
    r = C.test_baseline_honest()
    scenario("cross-environment compare -> 'refusing set comparison' surfaces",
             (not r["ok"]) and "refusing set comparison" in r["detail"],
             r["detail"][:140])

    # Undated / setless baseline is the defect itself.
    write_receipts({"schema": B.SCHEMA, "commit": "abc"}, None)
    r = C.test_baseline_honest()
    scenario("undated baseline -> fails as #13's own defect",
             (not r["ok"]) and "undated" in r["detail"], r["detail"][:120])

    # Enrolled, never recorded: a warn that says what to run.
    write_receipts(None, None)
    r = C.test_baseline_honest()
    scenario("enrolled-never-recorded -> warn naming the record command",
             (not r["ok"]) and r["severity"] == C.WARN
             and "baseline.py record" in r["detail"], r["detail"][:120])

    # Never compared (no opt-in): green with the fact stated — an install
    # that never opted into the schedule is not held at degraded.
    write_receipts(BASE7, None)
    r = C.test_baseline_honest()
    scenario("baseline recorded, never compared -> ok, opt-in stated",
             r["ok"] and "never compared" in r["detail"], r["detail"][:140])

    # A SCHEDULED comparison that stopped is a dead instrument (#45)...
    write_receipts(BASE7, {"utc": iso_ago(60), "scheduled": True,
                           "newly_failing": [], "newly_passing": []})
    r = C.test_baseline_honest()
    scenario("scheduled compare silent 60h -> warn 'dead instrument'",
             (not r["ok"]) and "stopped" in r["detail"], r["detail"][:140])

    # ...but a MANUAL comparison never age-warns.
    write_receipts(BASE7, {"utc": iso_ago(60), "scheduled": False,
                           "newly_failing": [], "newly_passing": []})
    r = C.test_baseline_honest()
    scenario("manual compare 60h old -> still ok (no opt-in, no age demand)",
             r["ok"], r["detail"][:140])
finally:
    B.baseline_path, B.compare_path, B.enrollment = real["bp"], real["cp"], real["enr"]

# ── live leg: a real git fixture, recorded, regressed, refused ──────────────

WORK = Path(tempfile.mkdtemp(prefix="prove-baseline-live-"))
FIX = WORK / "fixture"
FIX.mkdir()
(FIX / "test_demo.py").write_text(
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_a(self): pass\n"
    "    def test_b(self): pass\n"
    "    def test_known_bad(self): self.fail('inherited')\n",
    encoding="utf-8")
subprocess.run(["git", "init", "-q", str(FIX)], check=True)
subprocess.run(["git", "-C", str(FIX), "add", "-A"], check=True)
subprocess.run(["git", "-C", str(FIX), "-c", "user.email=p@p", "-c",
                "user.name=p", "commit", "-qm", "fixture"], check=True)

STATE = WORK / "state"
STATE.mkdir()
B.CLONES, real_clones = WORK / "clones", B.CLONES
B.baseline_path = lambda slug: STATE / f"test_baseline-{slug}.json"
B.compare_path = lambda slug: STATE / f"test_compare-{slug}.json"
B.enrollment = lambda: {"fixture": {
    "repo": str(FIX),
    "test_cmd": [sys.executable, "-m", "unittest", "test_demo"]}}

try:
    rc = B.record("fixture")
    doc = json.loads(B.baseline_path("fixture").read_text(encoding="utf-8"))
    scenario("live: record captures the NAMED inherited failure with env stamp",
             rc == 0 and doc["failures"] == ["test_demo.T.test_known_bad"]
             and doc["clone"] == "full" and doc["collected"] == 3,
             f"failures={doc['failures']} clone={doc['clone']}")

    # Regress the fixture: flip test_b, fix known_bad — count stays 1.
    (FIX / "test_demo.py").write_text(
        "import unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_a(self): pass\n"
        "    def test_b(self): self.fail('regression')\n"
        "    def test_known_bad(self): pass\n",
        encoding="utf-8")
    subprocess.run(["git", "-C", str(FIX), "-c", "user.email=p@p", "-c",
                    "user.name=p", "commit", "-aqm", "regress"], check=True)
    B.compare("fixture")
    comp = json.loads(B.compare_path("fixture").read_text(encoding="utf-8"))
    scenario("live: compare names the regression AND the silent fix, count unchanged",
             comp["newly_failing"] == ["test_demo.T.test_b"]
             and comp["newly_passing"] == ["test_demo.T.test_known_bad"],
             f"newly_failing={comp['newly_failing']} newly_passing={comp['newly_passing']}")

    # Refusal 1: a world-writable clone parent is the /tmp trap.
    B.CLONES = WORK / "trap"
    B.CLONES.mkdir()
    os.chmod(B.CLONES, 0o777 | stat.S_ISVTX)
    refused = None
    try:
        B._measure("fixture", B.enrollment()["fixture"])
    except B.RefusedMeasurement as e:
        refused = str(e)
    scenario("live: world-writable clone parent -> REFUSED, observed firing",
             refused is not None and "world-writable" in refused,
             (refused or "no refusal")[:120])
    B.CLONES = WORK / "clones"

    # Refusal 2: nonzero exit with zero parsed ids (a crashing suite).
    B.enrollment = lambda: {"fixture": {
        "repo": str(FIX),
        "test_cmd": [sys.executable, "-c", "import sys; sys.exit(2)"]}}
    refused = None
    try:
        B._measure("fixture", B.enrollment()["fixture"])
    except B.RefusedMeasurement as e:
        refused = str(e)
    scenario("live: exit-2-with-no-parsed-set -> REFUSED ('a count without a set')",
             refused is not None and "count without a set" in refused,
             (refused or "no refusal")[:120])
finally:
    B.CLONES = real_clones
    B.baseline_path, B.compare_path, B.enrollment = real["bp"], real["cp"], real["enr"]

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
