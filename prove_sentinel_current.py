#!/usr/bin/env python3
"""prove_sentinel_current.py — a repair that was merged but never deployed is
reported, and a fresh heartbeat is not allowed to stand in for fresh code.

The incident this check was built from (2026-08-17): rb_shards paged critical
with a false red. The diagnosis was right, the fix was merged to origin/main
as #99, and the page kept firing for four more hours — run.sh pulls nothing,
so the live checkout stayed eight commits behind and the running process kept
executing the pre-fix file. From inside the instance, a fix that never
arrived and a fix that did not work are the same picture, so a second repair
attempt re-derived the same correct diagnosis against an instance that could
never show it.

w_sentinel_current makes the difference observable. It fires at WARN and only
for BEHIND: pulling is a human's call, and ahead-or-diverged is someone
mid-development on the maintainer's own box, not a stranded repair.

Run: python3 prove_sentinel_current.py   (exit 0 only on all-behaved)
"""

import sys

import health as H

FAILURES = []

OLD = "052c9d11c9b5d8ecd770908ba579b29fee4fe521"   # what was running
NEW = "f089143a5ef7c814d18dcbc398ca69d75573ca1a"   # what was merged


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


def make_git(head=OLD, remote=NEW, have_remote=True, is_ancestor=True,
             behind="8", head_rc=0, remote_rc=0):
    """Stand in for the git plumbing. Exit STATUS is the whole signal for
    merge-base/cat-file, so the fake speaks in return codes too."""
    def _git(*args, timeout=25):
        a = list(args)
        if a[:1] == ["rev-parse"]:
            return (head_rc, head if head_rc == 0 else "")
        if a[:1] == ["ls-remote"]:
            return (remote_rc,
                    f"{remote}\trefs/heads/main" if remote_rc == 0 else "")
        if a[:1] == ["fetch"]:
            return (0, "")
        if a[:1] == ["cat-file"]:
            return (0 if have_remote else 128, "")
        if a[:1] == ["merge-base"]:
            return (0 if is_ancestor else 1, "")
        if a[:1] == ["rev-list"]:
            return (0, behind)
        raise AssertionError(f"unexpected git call: {a}")
    return _git


real_git = H._git

# ── the blindness this closes ───────────────────────────────────────────────
# The instance was ticking normally the whole time it was stale. The nearest
# existing self-watch, w_sentinel_fresh, judges last_run.json age against 90
# minutes — so a punctual tick of six-commit-old code reads as healthy.
age_m = 3.0                      # the live value: ticks were landing fine
scenario("BLINDNESS: a fresh tick of stale code passes the freshness watch — "
         "'ran' is not 'is the code we merged'",
         age_m < 90,
         f"w_sentinel_fresh logic: last tick {age_m:.0f}m ago -> ok, while "
         f"the running commit was {OLD[:7]} and main was {NEW[:7]}")

# ── (a) the incident itself ─────────────────────────────────────────────────
H._git = make_git()
r = H._deployed_code_is_current()
scenario("(a) THE INCIDENT: 8 commits behind -> WARN naming both commits, "
         "the distance, and the deploy command",
         (not r["ok"]) and r["severity"] == H.C.WARN
         and OLD[:7] in r["detail"] and NEW[:7] in r["detail"]
         and "8 commit(s) behind" in r["detail"]
         and "pull --ff-only" in r["detail"],
         f"severity={r['severity']} {r['detail']}")

# ── (b) control: the deploy happened ────────────────────────────────────────
H._git = make_git(head=NEW, remote=NEW)
r = H._deployed_code_is_current()
scenario("(b) control: running exactly origin/main -> ok",
         r["ok"] and NEW[:7] in r["detail"], r["detail"])

# ── (c) ahead / diverged is not a stranded repair ───────────────────────────
H._git = make_git(head=NEW, remote=OLD, is_ancestor=False)
r = H._deployed_code_is_current()
scenario("(c) ahead or diverged -> ok; local work must not page every tick "
         "on the maintainer's own box",
         r["ok"] and "not behind" in r["detail"], r["detail"])

# ── (d) not a git checkout: blind is never green (#45) ──────────────────────
H._git = make_git(head_rc=128)
r = H._deployed_code_is_current()
scenario("(d) running commit unreadable -> WARN 'cannot read running commit', "
         "never ok",
         (not r["ok"]) and r["severity"] == H.C.WARN
         and "cannot read running commit" in r["detail"], r["detail"])

# ── (e) offline: unreachable remote is not a current instance ───────────────
H._git = make_git(remote_rc=128)
r = H._deployed_code_is_current()
scenario("(e) origin unreadable -> WARN 'cannot read origin/main', never ok",
         (not r["ok"]) and r["severity"] == H.C.WARN
         and "cannot read origin/main" in r["detail"]
         and OLD[:7] in r["detail"], r["detail"])

# ── (f) differs but unclassifiable: still not green ─────────────────────────
H._git = make_git(have_remote=False)
r = H._deployed_code_is_current()
scenario("(f) published commit un-fetchable -> WARN 'could not be fetched to "
         "classify', never ok",
         (not r["ok"]) and r["severity"] == H.C.WARN
         and "could not be fetched to classify" in r["detail"], r["detail"])

# ── (g) the count is read, not assumed ──────────────────────────────────────
H._git = make_git(behind="1")
r = H._deployed_code_is_current()
scenario("(g) one commit behind still fires, with the measured distance",
         (not r["ok"]) and "1 commit(s) behind" in r["detail"], r["detail"])

# ── (h) the check may never mutate the working tree ─────────────────────────
seen = []


def recording_git(*args, timeout=25):
    seen.append(list(args))
    return make_git()(*args, timeout=timeout)


H._git = recording_git
H._deployed_code_is_current()
forbidden = {"pull", "merge", "reset", "checkout", "clean", "commit", "push"}
used = {a[0] for a in seen}
scenario("(h) watching the checkout cannot endanger uncommitted work: no "
         "tree-touching git verb is ever issued",
         not (used & forbidden),
         f"verbs issued: {sorted(used)}")

H._git = real_git

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
