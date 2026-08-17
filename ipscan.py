#!/usr/bin/env python3
"""ipscan.py — does anything you meant to keep private appear in a public repo?

Some strings are fine on your laptop and expensive in public: an internal
matter number, a path into a private repo, an unannounced deadline, a
person's name in a config file. Nothing notices when one of them rides along
in a commit, because every individual commit looks reasonable.

This is the scanner for that. You give it a list of strings that must never
be published; it enumerates an owner's PUBLIC repositories, shallow-clones
each one, and reports every match. A tick-side check (`ip_hygiene` in
checks.py) reads the receipt this writes, so the expensive part runs on a
schedule and the cheap part runs every tick — the same split baseline.py
uses.

    python3 ipscan.py scan [owner]     clone + grep every public repo
    python3 ipscan.py status           summarize the last scan

THE PATTERN FILE NEVER SHIPS. It lives at sensitive/publication-denylist.json
(gitignored, and this module refuses to run if it finds one tracked by git).
A list of the exact strings you are hiding is itself the leak — publishing
the denylist would be a more efficient disclosure than the leak it prevents.
There is no default list and no example values in this repository.

    {
      "patterns": ["INTERNAL-ONLY-MARKER", "some-private/path"],
      "allow": [{"repo": "docs-site", "pattern": "...", "why": "reviewed"}]
    }

Findings are written to state/ipscan.json as counts and file paths WITHOUT
the matched text: a receipt that quotes the secret defeats itself.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from paths import HOME, CODE

DENYLIST = HOME / "sensitive" / "publication-denylist.json"
RECEIPT = HOME / "state" / "ipscan.json"
DEFAULT_OWNER = "kody-w"

# First attempt is short so 400 small repos stay fast. A repo that times out
# gets ONE retry with a much longer budget rather than being written off: the
# estate's two largest repos (multi-GB) both failed at 120s and landed in
# `unscanned`, which is honest but leaves exactly the biggest surfaces
# unchecked. A separate aggregation proved 600s is enough for them.
MAX_CLONE_SECONDS = int(os.environ.get("IPSCAN_CLONE_TIMEOUT", "120"))
SLOW_CLONE_SECONDS = int(os.environ.get("IPSCAN_SLOW_TIMEOUT", "900"))


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tracked_by_git(path):
    """Is `path` tracked in the code repo? If so, refuse — see module docstring."""
    try:
        rel = Path(path).resolve().relative_to(CODE.resolve())
    except Exception:
        return False
    r = subprocess.run(["git", "-C", str(CODE), "ls-files", "--error-unmatch", str(rel)],
                       capture_output=True, text=True, timeout=30)
    return r.returncode == 0


def load_denylist():
    """(patterns, allow, error). No file is not an error — it is 'unconfigured'."""
    if not DENYLIST.exists():
        return [], [], ""
    if _tracked_by_git(DENYLIST):
        return [], [], ("denylist is TRACKED BY GIT - refusing to use it. A "
                        "committed list of the strings you are hiding is a "
                        "better disclosure than the leak it prevents.")
    try:
        doc = json.loads(DENYLIST.read_text(encoding="utf-8"))
    except Exception as e:
        return [], [], f"denylist unreadable: {type(e).__name__}: {e}"
    pats = [p for p in (doc.get("patterns") or []) if isinstance(p, str) and p.strip()]
    return pats, (doc.get("allow") or []), ""


def public_repos(owner):
    r = subprocess.run(
        ["gh", "repo", "list", owner, "--limit", "1000", "--json",
         "name,visibility,isArchived"],
        capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"gh repo list failed: {(r.stderr or '')[:120]}")
    return sorted(x["name"] for x in json.loads(r.stdout)
                  if x["visibility"] == "PUBLIC" and not x["isArchived"])


def _allowed(allow, repo, path, pattern):
    """Is this (repo, path, pattern) a reviewed exception?

    An entry may pin `repo`, `path` and `pattern` (each optional, "*" or
    absent means any). Pinning the PATTERN is the point: an exception that
    exempts a whole file from every pattern is how a reviewed decision about
    one string quietly becomes a blind spot for the next one. Reviewing "the
    CEO is named on the company's own marketing page" must not also stop the
    scanner noticing a matter number in that same file tomorrow.
    """
    for a in allow:
        if a.get("repo") not in (repo, "*", None):
            continue
        if a.get("path") not in (path, "*", None):
            continue
        if a.get("pattern") not in (pattern, "*", None):
            continue
        return True
    return False


def _clone(owner, repo, dest, budget):
    return subprocess.run(
        ["git", "clone", "-q", "--depth", "1", "--single-branch",
         f"https://github.com/{owner}/{repo}.git", str(dest)],
        capture_output=True, text=True, timeout=budget)


def scan_repo(owner, repo, patterns, allow, workdir, slow=SLOW_CLONE_SECONDS):
    """(hits, error). hits = [{file, count}] — never the matched text."""
    dest = workdir / repo
    try:
        try:
            r = _clone(owner, repo, dest, MAX_CLONE_SECONDS)
        except subprocess.TimeoutExpired:
            # Big repo, not a broken one. Give it the long budget once — the
            # largest repos are exactly the ones worth not skipping.
            shutil.rmtree(dest, ignore_errors=True)
            print(f"      {repo}: slow, retrying with {slow}s", flush=True)
            r = _clone(owner, repo, dest, slow)
        if r.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            return [], f"clone failed: {(r.stderr or '').strip()[:80]}"
    except subprocess.TimeoutExpired:
        # A clone killed at the timeout leaves whatever it had already
        # fetched on disk. Measured on the first full estate run: one
        # timed-out clone of a large repo left 195MB behind, and every
        # subsequent failure would have stacked on top of it for the rest of
        # the scan. The failure paths have to clean up after themselves or a
        # long scan becomes a disk-filler.
        shutil.rmtree(dest, ignore_errors=True)
        return [], f"clone exceeded {slow}s even on the slow retry"
    except Exception as e:
        shutil.rmtree(dest, ignore_errors=True)
        return [], f"{type(e).__name__}: {e}"

    hits = {}
    try:
        for pat in patterns:
            g = subprocess.run(
                ["grep", "-rlF", "--exclude-dir=.git", "-i", pat, str(dest)],
                capture_output=True, text=True, timeout=180)
            for line in (g.stdout or "").splitlines():
                rel = str(Path(line).relative_to(dest))
                if _allowed(allow, repo, rel, pat):
                    continue
                hits[rel] = hits.get(rel, 0) + 1
    except Exception as e:
        return [], f"grep failed: {type(e).__name__}: {e}"
    finally:
        shutil.rmtree(dest, ignore_errors=True)
    return [{"file": f, "patterns_matched": n} for f, n in sorted(hits.items())], ""


def scan(owner=DEFAULT_OWNER):
    patterns, allow, err = load_denylist()
    if err:
        print(f"REFUSED: {err}")
        return 3
    if not patterns:
        print(f"no denylist at {DENYLIST} - nothing to scan for.\n"
              f"Create it (gitignored) with a 'patterns' list to enable.")
        RECEIPT.parent.mkdir(exist_ok=True)
        RECEIPT.write_text(json.dumps({
            "schema": "rapp-ipscan/1.0", "utc": utc_now(), "owner": owner,
            "configured": False, "scanned": 0, "unscanned": [], "findings": [],
        }, indent=2) + "\n", encoding="utf-8")
        return 0

    repos = public_repos(owner)
    print(f"scanning {len(repos)} public {owner} repos for "
          f"{len(patterns)} pattern(s)…")
    findings, unscanned, scanned = [], [], 0
    work = Path(tempfile.mkdtemp(prefix="ipscan-"))
    try:
        for i, repo in enumerate(repos, 1):
            hits, error = scan_repo(owner, repo, patterns, allow, work)
            if error:
                unscanned.append({"repo": repo, "reason": error})
                print(f"  [{i}/{len(repos)}] {repo}: UNSCANNED ({error})")
                continue
            scanned += 1
            if hits:
                findings.append({"repo": repo, "files": hits})
                print(f"  [{i}/{len(repos)}] {repo}: {len(hits)} file(s) MATCH")
            elif i % 25 == 0 or i == len(repos):
                # A clean repo is silent, so a long clean scan printed nothing
                # at all for twenty-five minutes and looked wedged - which is
                # how a useful scan gets killed by the operator watching it.
                # A heartbeat every 25 repos costs nothing and says it is alive.
                print(f"  [{i}/{len(repos)}] scanning… "
                      f"{len(findings)} match(es), {len(unscanned)} unscanned",
                      flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    RECEIPT.parent.mkdir(exist_ok=True)
    RECEIPT.write_text(json.dumps({
        "schema": "rapp-ipscan/1.0",
        "utc": utc_now(),
        "owner": owner,
        "configured": True,
        "pattern_count": len(patterns),      # the count, never the patterns
        "scanned": scanned,
        "unscanned": unscanned,
        "findings": findings,
    }, indent=2) + "\n", encoding="utf-8")
    print(f"\n{scanned} scanned, {len(unscanned)} unscanned, "
          f"{len(findings)} repo(s) with matches -> {RECEIPT}")
    return 1 if findings else 0


def status():
    try:
        doc = json.loads(RECEIPT.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"no readable receipt: {type(e).__name__}")
        return 2
    print(json.dumps({k: v for k, v in doc.items() if k != "findings"}, indent=2))
    for f in doc.get("findings") or []:
        print(f"  {f['repo']}: " + ", ".join(x["file"] for x in f["files"][:6]))
    return 0


def main(argv):
    if argv[:1] == ["scan"]:
        return scan(argv[1] if len(argv) > 1 else DEFAULT_OWNER)
    if argv[:1] == ["status"]:
        return status()
    print(__doc__.strip().splitlines()[0])
    print("usage: ipscan.py scan [owner] | status")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
