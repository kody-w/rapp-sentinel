#!/usr/bin/env python3
"""prove_publication_hygiene.py — the leak check fires, and the scanner
refuses to become the leak.

Two properties worth proving, because both were learned the expensive way:

  1. A denylisted string live in a public repo must PAGE. It is the one
     finding whose cost grows every hour it stays up, and unlike an upstream
     outage there is always an action available.
  2. The denylist must never ship. A committed list of the exact strings you
     are hiding is a more efficient disclosure than the leak it prevents, so
     ipscan REFUSES a git-tracked denylist rather than using it — the same
     refuse-rather-than-pretend posture neighborhood.say() takes.

Plus the boring-but-load-bearing ones: unconfigured is not a failure (a
permanent warn would silently disable the evolve arm), and an unscanned repo
is named rather than rounded away.

Run: python3 prove_publication_hygiene.py   (exit 0 only on all-behaved)
"""

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import checks as C
import ipscan

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


def iso_ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds")


TMP = Path(tempfile.mkdtemp(prefix="prove-iphygiene-"))
real = dict(receipt=ipscan.RECEIPT, deny=ipscan.DENYLIST)
ipscan.RECEIPT = TMP / "ipscan.json"
ipscan.DENYLIST = TMP / "publication-denylist.json"


def receipt(**kw):
    base = {"schema": "rapp-ipscan/1.0", "utc": iso_ago(2), "owner": "o",
            "configured": True, "pattern_count": 3, "scanned": 194,
            "unscanned": [], "findings": []}
    base.update(kw)
    ipscan.RECEIPT.write_text(json.dumps(base), encoding="utf-8")


try:
    # ── the finding that matters ────────────────────────────────────────────
    receipt(findings=[{"repo": "public-thing",
                       "files": [{"file": "docs/SPEC.md", "patterns_matched": 1},
                                 {"file": "board.json", "patterns_matched": 2}]}])
    r = C.publication_hygiene()
    scenario("denylisted content live in a public repo -> CRITICAL, naming repo and files",
             (not r["ok"]) and r["severity"] == C.CRITICAL
             and "public-thing" in r["detail"] and "docs/SPEC.md" in r["detail"],
             f"severity={r['severity']} {r['detail'][:110]}")

    # ── clean, and the honest variants ──────────────────────────────────────
    receipt()
    r = C.publication_hygiene()
    scenario("clean scan -> ok with positive counts, never a bare 'fine'",
             r["ok"] and "194 public repos clean" in r["detail"], r["detail"])

    receipt(unscanned=[{"repo": "huge-thing", "reason": "clone exceeded 120s"}])
    r = C.publication_hygiene()
    scenario("an unscanned repo is NAMED, not rounded away (R3)",
             (not r["ok"]) and r["severity"] == C.WARN
             and "UNSCANNED" in r["detail"] and "huge-thing" in r["detail"],
             r["detail"][:110])

    receipt(utc=iso_ago(24 * 9))
    r = C.publication_hygiene()
    scenario("a 9-day-old scan warns - the estate has moved since",
             (not r["ok"]) and r["severity"] == C.WARN and "last scan" in r["detail"],
             r["detail"][:110])

    receipt(utc=None)
    r = C.publication_hygiene()
    scenario("an undated receipt is a finding, not a crash",
             (not r["ok"]) and "undated" in r["detail"], r["detail"][:90])

    # ── unconfigured must not hold the organism degraded forever ────────────
    ipscan.RECEIPT.unlink()
    r = C.publication_hygiene()
    scenario("no denylist, no receipt -> ok and says so (opt-in, not a standing red)",
             r["ok"] and "no denylist" in r["detail"].lower()
             or r["ok"] and "not configured" in r["detail"].lower()
             or r["ok"] and "no publication denylist" in r["detail"],
             r["detail"])

    ipscan.DENYLIST.write_text(json.dumps({"patterns": ["SOME-MARKER"]}),
                               encoding="utf-8")
    r = C.publication_hygiene()
    scenario("denylist configured but never scanned -> warn naming the command",
             (not r["ok"]) and "never scanned" in r["detail"]
             and "ipscan.py scan" in r["detail"], r["detail"][:110])

    # ── the scanner refuses to become the leak ──────────────────────────────
    # A denylist tracked by git is the disclosure it exists to prevent, so
    # load_denylist() must refuse it. Simulated by pointing the tracked-check
    # at README.md, which is unambiguously tracked (the harness first
    # pointed at ITSELF and fell through to a parse error, because a
    # brand-new file is not tracked yet - a fixture that proves the
    # wrong branch proves nothing).
    ipscan.DENYLIST = Path(__file__).resolve().parent / "README.md"   # tracked
    pats, allow, err = ipscan.load_denylist()
    scenario("a git-TRACKED denylist is REFUSED, not used",
             pats == [] and "TRACKED BY GIT" in err, err[:100])
    r = C.publication_hygiene()
    scenario("the check surfaces that refusal instead of reading green",
             (not r["ok"]) and "unusable" in r["detail"], r["detail"][:100])

    # ── and the real repo must not be shipping a denylist today ─────────────
    tracked = subprocess.run(
        ["git", "ls-files", "sensitive/"], capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parent), timeout=30).stdout.strip()
    ignored = "sensitive/" in Path(
        Path(__file__).resolve().parent / ".gitignore").read_text(encoding="utf-8")
    scenario("this repository tracks no sensitive/ files and gitignores the path",
             tracked == "" and ignored,
             f"tracked={tracked!r} gitignored={ignored}")
finally:
    ipscan.RECEIPT, ipscan.DENYLIST = real["receipt"], real["deny"]

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
