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

    # ── a reviewed exception must be narrow ─────────────────────────────────
    # The first allowlist exempted a whole FILE, so reviewing one string in a
    # marketing page would also have blinded the scanner to a matter number
    # landing in that same page tomorrow. Exceptions pin the pattern.
    A = [{"repo": "rb", "pattern": "A Person"}]
    scenario("a pattern-scoped exception applies to its own pattern",
             ipscan._allowed(A, "rb", "docs/deck.html", "A Person"), "allowed")
    scenario("...and does NOT blind the file to every other pattern",
             not ipscan._allowed(A, "rb", "docs/deck.html", "SOME-MATTER-NO"),
             "still caught")
    scenario("...nor apply to a different repo",
             not ipscan._allowed(A, "other", "docs/deck.html", "A Person"),
             "still caught")

    # ── a big repo gets a second, longer chance ─────────────────────────────
    # The first estate run wrote off the two LARGEST repos at the short
    # timeout. Honest ("unscanned"), but it left the biggest surfaces
    # unchecked, which is the wrong place to have a gap.
    attempts = []

    def slow_then_ok(cmd, **kw):
        if cmd[:2] == ["git", "clone"]:
            attempts.append(kw.get("timeout"))
            if len(attempts) == 1:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kw.get("timeout"))
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, **kw)

    work2 = TMP / "work2"
    work2.mkdir(exist_ok=True)
    real_run = subprocess.run
    subprocess.run = slow_then_ok
    try:
        hits, err = ipscan.scan_repo("o", "huge", [], [], work2, slow=900)
    finally:
        subprocess.run = real_run
    scenario("a clone that times out is retried once with a longer budget",
             err == "" and attempts == [ipscan.MAX_CLONE_SECONDS, 900],
             f"attempts={attempts} err={err!r}")

    # ── a failed clone must not leave its bytes behind ──────────────────────
    # Found on the first full estate run: a clone killed at the timeout left
    # 195MB of partial checkout in the work dir, and every later failure
    # would have stacked on it. A scan that fills the disk is a scan that
    # stops running.
    work = TMP / "work"
    work.mkdir(exist_ok=True)
    real_run = subprocess.run

    def fake_clone(cmd, **kw):
        if cmd[:2] == ["git", "clone"]:
            dest = Path(cmd[-1])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "partial.bin").write_text("x" * 4096, encoding="utf-8")
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)
        return real_run(cmd, **kw)

    subprocess.run = fake_clone
    try:
        hits, err = ipscan.scan_repo("o", "big-repo", ["PAT"], [], work)
    finally:
        subprocess.run = real_run
    scenario("a clone killed at the timeout leaves nothing behind",
             hits == [] and "exceeded" in err
             and not (work / "big-repo").exists(),
             f"err={err!r} leftover={(work / 'big-repo').exists()}")

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
