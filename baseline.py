#!/usr/bin/env python3
"""baseline.py — dated, environment-stamped, set-based test baselines (#13).

"Matches the baseline" is the sentence that lets a real regression through.
The incident: four agents were briefed "4 known failures" as verified fact;
one measured 7, adjudication measured 8 — all three numbers correct WHEN
TAKEN, on a main that moved between them. The follow-up was worse: the same
commit produced 7, 10 and 0 failures depending on clone depth and directory
permissions — /tmp is world-writable and a shallow clone is missing objects,
and each produces phantom failures that look exactly like real ones.

So a baseline here is a claim with a timestamp AND an environment, and the
comparison is set-based, never count-based — 7 == 7 says nothing when they
are different sevens.

    python3 baseline.py record <slug>    measure and WRITE the baseline
    python3 baseline.py compare <slug>   re-measure and diff against it

record is the ONLY way the baseline moves, so drift surfaces as
newly_failing until a human re-records — the visible, dated decision the
issue asks for. compare never writes the baseline. Both refuse the two
measured phantom-failure sources: a shallow clone, and a world-writable
ancestor directory. And a run that exits nonzero while parsing ZERO failing
test ids is refused outright — a count without a set is the defect this
file exists to fix.

Heavy on purpose (full clone + full suite), so it is NOT run by the tick:
w_test_baseline in checks.py reads the receipts this writes. Schedule
compare daily with com.rapp.test-baseline.plist.template (opt-in).

Baselines live in gitignored state/ — per-install recording IS the finding:
committing one would republish an undated, unportable claim.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from paths import HOME

SCHEMA = "rapp-test-baseline/1.0"
CLONES = Path.home() / "Library" / "Application Support" / "rapp-sentinel" / "baselines"

# The landing enrollment is this repo alone — known-runnable, no deps to
# sort. Enrolling rappterbook/rappterverse is a config decision for whoever
# has sorted each repo's test dependencies; shipping them enrolled here
# would ship a red nobody can clear.
DEFAULT_ENROLLMENT = {
    "rapp-sentinel": {
        "repo": "https://github.com/kody-w/rapp-sentinel.git",
        "test_cmd": [sys.executable, "-m", "unittest",
                     "test_static_delivery", "test_ledger_coverage"],
    },
}


def enrollment():
    try:
        cfg = json.loads((HOME / "config.json").read_text(encoding="utf-8"))
        enrolled = cfg.get("baselines")
        if isinstance(enrolled, dict) and enrolled:
            return enrolled
    except Exception:
        pass
    return DEFAULT_ENROLLMENT


def baseline_path(slug):
    return HOME / "state" / f"test_baseline-{slug}.json"


def compare_path(slug):
    return HOME / "state" / f"test_compare-{slug}.json"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def world_writable_ancestor(path):
    """The first ancestor (or the path itself) writable by an untrusted
    principal, or None. /tmp is drwxrwxrwt — the documented phantom-failure
    trap: a test asserting its output parent is not world-writable refuses
    there, correctly, for reasons unrelated to the code under test."""
    p = Path(path).resolve()
    for candidate in [p] + list(p.parents):
        try:
            if candidate.stat().st_mode & 0o002:
                return candidate
        except Exception:
            continue
    return None


def parse_failing_ids(output):
    """The NAMED set of failing tests, plus how many ran.

    unittest reports 'FAIL: test_x (module.Class.test_x)' and
    'ERROR: test_y (...)'. The id recorded is 'module.Class.test_x' —
    the set is the claim; the count is derived, never primary.
    """
    ids = set()
    for m in re.finditer(r"^(?:FAIL|ERROR): \S+ \(([^)]+)\)", output, re.M):
        ids.add(m.group(1))
    ran = 0
    m = re.search(r"^Ran (\d+) tests?", output, re.M)
    if m:
        ran = int(m.group(1))
    return sorted(ids), ran


def _measure(slug, spec):
    """Full clone into the app-support dir, run the suite, return the
    environment-stamped measurement. Raises RefusedMeasurement on any of the
    three refusal conditions."""
    dest = CLONES / slug
    bad = world_writable_ancestor(CLONES if not dest.exists() else dest)
    if bad is not None:
        raise RefusedMeasurement(
            f"{bad} is world-writable - measuring there produces phantom "
            f"failures (the /tmp trap); refusing")
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["git", "clone", "--quiet", spec["repo"], str(dest)],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RefusedMeasurement(f"clone failed: {(r.stderr or '')[:120]}")
    shallow = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "--is-shallow-repository"],
        capture_output=True, text=True, timeout=60).stdout.strip()
    if shallow != "false":
        raise RefusedMeasurement(
            "clone is shallow - missing objects produce phantom failures; "
            "refusing")
    commit = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"],
                            capture_output=True, text=True,
                            timeout=60).stdout.strip()
    run = subprocess.run(spec["test_cmd"], cwd=str(dest), capture_output=True,
                         text=True, timeout=1800)
    output = (run.stdout or "") + (run.stderr or "")
    failures, collected = parse_failing_ids(output)
    if run.returncode != 0 and not failures:
        raise RefusedMeasurement(
            f"suite exited {run.returncode} but zero failing test ids were "
            f"parsed - a count without a set is the defect this file exists "
            f"to fix; refusing to record. Tail: {output[-200:]!r}")
    return {
        "schema": SCHEMA,
        "slug": slug,
        "commit": commit,
        "utc": utc_now(),
        "clone": "full",
        "cwd_class": "non-world-writable",
        "os": sys.platform,
        "python": sys.version.split()[0],
        "cmd": list(spec["test_cmd"]),
        "failures": failures,
        "collected": collected,
    }


class RefusedMeasurement(RuntimeError):
    pass


ENV_KEYS = ("clone", "cwd_class", "os")


def record(slug):
    spec = enrollment().get(slug)
    if not spec:
        print(f"{slug} is not enrolled")
        return 2
    doc = _measure(slug, spec)
    baseline_path(slug).parent.mkdir(exist_ok=True)
    baseline_path(slug).write_text(json.dumps(doc, indent=2) + "\n",
                                   encoding="utf-8")
    print(f"baseline recorded: {slug} @ {doc['commit'][:8]} {doc['utc']} - "
          f"{len(doc['failures'])} failing of {doc['collected']} "
          f"({', '.join(doc['failures']) or 'none'})")
    return 0


def compare(slug):
    """Re-measure and diff the NAMED sets. Never writes the baseline."""
    spec = enrollment().get(slug)
    if not spec:
        print(f"{slug} is not enrolled")
        return 2
    try:
        base = json.loads(baseline_path(slug).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"no readable baseline for {slug}: {type(e).__name__} - run "
              f"`python3 baseline.py record {slug}` first")
        return 2
    cur = _measure(slug, spec)
    receipt = {
        "schema": "rapp-test-compare/1.0",
        "slug": slug,
        "utc": cur["utc"],
        "baseline_utc": base.get("utc"),
        "baseline_commit": base.get("commit"),
        "current_commit": cur["commit"],
        "scheduled": bool(os.environ.get("BASELINE_SCHEDULED")),
    }
    mismatch = [k for k in ENV_KEYS if base.get(k) != cur.get(k)]
    if mismatch:
        # The 7-vs-10-vs-0 lesson: a cross-environment set comparison is the
        # 7 == 7 lie with extra steps. Refuse, visibly.
        receipt["refused"] = ("environment mismatch on " + ", ".join(
            f"{k} ({base.get(k)!r} vs {cur.get(k)!r})" for k in mismatch))
    else:
        base_set = set(base.get("failures") or [])
        cur_set = set(cur["failures"])
        receipt["newly_failing"] = sorted(cur_set - base_set)
        receipt["newly_passing"] = sorted(base_set - cur_set)
    compare_path(slug).parent.mkdir(exist_ok=True)
    compare_path(slug).write_text(json.dumps(receipt, indent=2) + "\n",
                                  encoding="utf-8")
    print(json.dumps(receipt, indent=2))
    return 0 if not receipt.get("newly_failing") else 1


def main(argv):
    if len(argv) == 2 and argv[0] in ("record", "compare"):
        try:
            return {"record": record, "compare": compare}[argv[0]](argv[1])
        except RefusedMeasurement as e:
            print(f"REFUSED: {e}")
            return 3
    print(__doc__.strip().splitlines()[0])
    print("usage: baseline.py record|compare <slug>   "
          f"(enrolled: {', '.join(sorted(enrollment()))})")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
