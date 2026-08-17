#!/usr/bin/env python3
"""checks.py — the only file most people need to edit.

A check is a small function that answers ONE question about the thing you are
keeping alive, cheaply and without a model. It returns:

    ok(id, detail)                  passing
    fail(id, detail, critical=True) failing — critical wakes the repair arm

Rules that make the difference between a loop that works and one that cries wolf:

  * SPECIFIC. "the world merged in the last 3 hours" is a check.
    "the system is healthy" is a mood.
  * CHEAP. No model. If deciding whether something is broken needs a model,
    the check is not sharp enough yet.
  * ACTIONABLE. A failure should imply a next step. `rb_workflows: 100%
    failing: Agent Heartbeat` implies one. `something seems off` does not.
  * HONEST SEVERITY. critical=True spends money and takes actions. Use it for
    "the thing is not doing its job", not for "a page is slow".

Three invariants, numbered so they can be cited in review (the incidents that
paid for them are in TRIFECTA-PATTERN.md §6d):

  * R1 A RECEIPT IS NOT EVIDENCE. Never pass on an exit code, an HTTP 200, a
    workflow conclusion, or an intermediary's "✅ APPLIED". Evidence is the
    observable end state: the published file, the row, the pid LISTENING,
    the answered turn.
  * R2 RAN IS NOT WORKED. Every watched domain carries at least one check
    that measures OUTPUT movement — a timestamp on the thing produced, not
    the status of the process producing it. moving() is the cheap default.
  * R3 REQUIRE KNOWN-GOOD, NEVER ENUMERATE KNOWN-BAD. ok() must be gated on
    a positive assertion; the absence of a named bad state is not health,
    because a state nobody thought to name passes silently.
    require_success() is the cheap default for run histories.

Check ids are load-bearing in state files and are never renamed, so an id
does not have to match its function's name (`rb_wf_starved` lives in
`rb_workflows_never_succeed`). The `produced_by` field health.py stamps on
every verdict line is how you map a line back to its function without
grepping.

Everything below `# ── your checks ──` is an example: the two GitHub-native
platforms this pattern was built for. Delete it and write your own.

One carve-out (issue #1 ask 2): the estate-specific watcher probes — the
brainstem /chat POST and the openrappter port/labels — live in health.py's
probe_watchers and are retargeted through config.json's `watchers` block
(see config.example.json), not by editing this file.
"""

import functools
import json
import subprocess
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone

TIMEOUT = 25
CRITICAL, WARN = "critical", "warn"

_REGISTRY = []


def check(fn):
    """Decorator. Any function decorated with @check runs every tick."""
    _REGISTRY.append(fn)
    return fn


def outsider_check(platform):
    """Register like @check AND enforce the outsider vantage (issue #5).

    A check wearing this decorator claims to see `platform` the way a
    stranger would — no owner credentials. The claim is ENFORCED, not
    asserted (R3): _GH_CALLS is snapshotted around the check, and if the
    execution routed through gh() — the one credentialed helper in this
    file — the check's own result is REPLACED with a critical failure,
    because a verdict produced with the owner's token proves nothing about
    the front door. The runner is single-threaded, so a plain module
    counter is a sound witness.

    The result is tagged additively: platform=<platform> always, and
    vantage="outsider" only when the execution stayed unprivileged. A
    violation is tagged vantage="credentialed" instead, so
    w_outsider_coverage (health.py) cannot count a violation as coverage —
    the vantage tag is earned, never declared.

    Honest limit: the counter proves no owner credentials flowed through
    the one credentialed helper (gh). A check that shells out to `gh`
    directly, or reads a token file itself, is invisible to it — that is a
    code-review property, the same class of limit as w_checks_complete's
    inability to detect its own removal.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            before = _GH_CALLS
            r = fn(*args, **kwargs)
            if not isinstance(r, dict):
                return r      # the runner already reports non-results
            if _GH_CALLS != before:
                r = fail(r.get("id") or fn.__name__,
                         "claimed outsider vantage but used owner "
                         "credentials (gh)")
                r["vantage"] = "credentialed"
            else:
                r["vantage"] = "outsider"
            r["platform"] = platform
            return r
        _REGISTRY.append(wrapper)
        return wrapper
    return deco


def all_checks():
    return list(_REGISTRY)


# ── helpers you can use in your checks ──────────────────────────────────────

def ok(cid, detail=""):
    return {"id": cid, "ok": True, "severity": WARN, "detail": detail}


def fail(cid, detail="", critical=True):
    return {"id": cid, "ok": False,
            "severity": CRITICAL if critical else WARN, "detail": detail}


def http_status(url):
    req = urllib.request.Request(url, method="GET",
                                 headers={"User-Agent": "sentinel"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


# How many times gh() has been entered this process. The runner is
# single-threaded, so outsider_check() can snapshot this around a check to
# prove the execution never touched the owner's credentials.
_GH_CALLS = 0


def gh(args, default=None):
    """Run `gh` and parse JSON. Returns `default` on any failure.

    The ONE credentialed helper in this file: everything gh does rides the
    owner's token. _GH_CALLS is incremented on entry, before any outcome,
    so even a failed or timed-out gh call counts as having reached for the
    owner's credentials.
    """
    global _GH_CALLS
    _GH_CALLS += 1
    try:
        r = subprocess.run(["gh"] + args, capture_output=True,
                           text=True, timeout=TIMEOUT)
        if r.returncode != 0:
            return default
        return json.loads(r.stdout) if r.stdout.strip() else default
    except Exception:
        return default


def hours_since(iso):
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - d).total_seconds() / 3600
    except Exception:
        return None


def url_check(cid, url, critical=False):
    s = http_status(url)
    return ok(cid, url) if s == 200 else fail(cid, f"HTTP {s} — {url}", critical)


class _Sentinel:
    def __init__(self, name):
        self._name = name

    def __repr__(self):
        return self._name


# "I could not read the run history" is not "this repo has never run anything".
# gh() returns its default on ANY failure -- non-zero exit, timeout, unparseable
# stdout -- so `if not runs: return None` made a blind instrument and a genuinely
# empty repository the same value, and the callers below turned that value into a
# claim ABOUT THE REPOSITORY: "30 active workflows produced no runs at all",
# critical, which pages the repair arm. rappterbook has 40,000 runs; the sentence
# was false every time a read merely failed, and it fired on 2026-08-10 and
# 2026-08-12 with no runs missing either time.
#
# Same error the rest of this file has already been taught four times: #45 (a
# dead PR API read as an empty queue), prove_blind_green (a dead instrument must
# never read as green), #51 (do not spend repair budget on outages), #58 (a URL
# outage is not a missing check). Absence still pages (#43) -- but only absence
# we actually observed.
UNREADABLE = _Sentinel("UNREADABLE")

# Evidence THIN, not evidence ABSENT-BY-FAILURE. UNREADABLE means the read
# itself failed (we are blind); UNDECIDED means the read worked and what came
# back is too little to judge (we can see, there is just not enough there).
# The two demand different sentences — "cannot read run history" versus "too
# few runs to judge" — and prove_undecided.py exists because conflating the
# second with health hid a 5-of-5 failing workflow behind a thin window.
UNDECIDED = _Sentinel("UNDECIDED")


def moving(cid, timestamp_fn, max_age_h, what="output"):
    """Output-freshness verdict — positive evidence that work HAPPENED (R2).

    `timestamp_fn` returns an ISO stamp of the newest OBSERVED OUTPUT (never a
    run receipt), or None when the output carries no stamp, or raises when it
    could not be read. The three cases get three different sentences, because
    they are three different claims (#45/#51):

      reader raised -> warn      blind, not broken; no repair budget is spent
                                 on a read we could not make
      stamp absent  -> critical  we READ the output and it cannot testify to
                                 its own freshness — that is a defect in the
                                 output, not in our instrument
      stamp stale   -> critical  the outage this helper exists for
    """
    try:
        stamp = timestamp_fn()
    except Exception as e:
        return fail(cid, f"cannot read {what} timestamp "
                         f"({type(e).__name__}: {str(e)[:60]})", critical=False)
    if stamp is None:
        return fail(cid, f"{what} carries no timestamp - cannot claim movement")
    age = hours_since(stamp)
    if age is None:
        return fail(cid, f"{what} timestamp unreadable: {str(stamp)[:40]!r}")
    if age >= max_age_h:
        return fail(cid, f"{what} stale {age:.1f}h (bar {max_age_h}h)")
    return ok(cid, f"{what} {age:.1f}h old")


def require_success(conclusions, min_runs=3):
    """Judge run history by POSITIVE evidence: an explicit success (R3).

    Colour-blind about failure. cancelled, skipped, timed_out, action_required
    and failure are all equally not-success, so a colour nobody enumerated
    cannot pass — the shape that made static-api's cancelled-3/3 invisible to
    `fail == total`, and would have made an all-skipped workflow invisible to
    `cancelled > 0`.

    Returns one of:
      "success"    at least one explicit conclusion == "success"
      "no-success" >= min_runs explicit conclusions, none of them a success
      UNDECIDED    fewer than min_runs explicit conclusions (thin evidence —
                   a prompt to look, never a defect and never health)
      UNREADABLE   conclusions is None (the read failed; the caller inherited
                   gh()'s default and must say "blind", not "fine")

    min_runs=3 mirrors the existing total >= 3 bar in this file. Callers that
    restate it silently own it (#27).
    """
    if conclusions is None:
        return UNREADABLE
    decided = [c for c in conclusions if c]   # queued/in-flight = non-verdicts
    if any(c == "success" for c in decided):
        return "success"
    return "no-success" if len(decided) >= min_runs else UNDECIDED


def workflows_failing_every_run(repo, limit=30, ignore=()):
    """Workflows that failed EVERY recent run. Ignores single flakes."""
    runs = gh(["run", "list", "-R", repo, "--limit", str(limit),
               "--json", "name,conclusion"], default=None)
    if runs is None:
        return UNREADABLE
    if not runs:
        return None
    per = {}
    for r in runs:
        if r["name"] in ignore:
            continue
        # Queued/in-flight runs carry no conclusion and are non-verdicts.
        # Counting them in `total` let one in-flight run hide five straight
        # failures from the fail == total test — the seam between this
        # check and rb_wf_starved, which deliberately hands failures-only
        # histories back to this one (found by the 2026-08-16 review sweep).
        if not r.get("conclusion"):
            continue
        per.setdefault(r["name"], {"fail": 0, "total": 0})
        per[r["name"]]["total"] += 1
        if r.get("conclusion") == "failure":
            per[r["name"]]["fail"] += 1
    return {n: v for n, v in per.items()
            if v["total"] >= 2 and v["fail"] == v["total"]}


def active_workflows(repo):
    """Names of workflows that EXIST and are active on `repo`.

    Absence of run history is ambiguous on its own. A repo with no workflows
    has nothing to report; a repo whose workflows exist but have stopped
    running is stalled. Nothing else in this file could tell those apart, so
    three checks reported "no run history" as healthy and would have stayed
    green forever if a workflow were deleted, disabled, or had its trigger
    broken (#43).

    Returns None when the workflow list itself cannot be read, so callers can
    say "unknown" instead of quietly assuming health.
    """
    wf = gh(["api", f"repos/{repo}/actions/workflows", "--jq",
             '[.workflows[] | select(.state=="active") | .name]'], default=None)
    return wf if isinstance(wf, list) else None


def workflows_currently_broken(repo, limit=100, streak=4, ignore=()):
    """Workflows whose most recent `streak` runs ALL failed.

    `workflows_failing_every_run` requires fail == total, so a single old
    success makes a workflow invisible no matter how long it has been red since.
    Found by listing the watched repositories by hand: rapp-spine's "Verify
    Spine" was 12/15 failed with the eight most recent all failing, and the
    sweep called the repository clean.

    "Broken since forever" and "broken since Tuesday" are the same outage. What
    separates a real failure from a flake is not the lifetime ratio, it is
    whether it is failing NOW and has been for a while -- a streak on the newest
    runs, not a count over all of them.

    gh returns runs newest-first; this relies on that ordering. Active
    workflows absent from the shared repository window are probed separately:
    otherwise a busy repo can push a low-frequency failed Release workflow
    completely out of view and turn red into green.
    """
    runs = gh(["run", "list", "-R", repo, "--limit", str(limit),
               "--json", "name,conclusion"], default=None)
    if not runs:
        return None
    per = {}
    for r in runs:
        if r["name"] in ignore:
            continue
        per.setdefault(r["name"], []).append(r.get("conclusion"))

    active = active_workflows(repo)
    if active:
        for name in active:
            if name in ignore or name in per:
                continue
            history = gh([
                "run", "list", "-R", repo,
                "--workflow", name,
                "--limit", str(streak),
                "--json", "conclusion",
            ], default=None)
            if not history:
                continue
            conclusions = [
                row.get("conclusion") for row in history
                if row.get("conclusion")
            ]
            leading_failures = 0
            for conclusion in conclusions:
                if conclusion != "failure":
                    break
                leading_failures += 1
            if leading_failures:
                per[name] = conclusions

    out = {}
    for name, concl in per.items():
        recent = concl[:streak]
        sparse = active and name in active and name not in {
            r["name"] for r in runs if r["name"] not in ignore
        }
        if sparse and recent and recent[0] == "failure":
            leading_failures = 0
            for conclusion in recent:
                if conclusion != "failure":
                    break
                leading_failures += 1
            out[name] = {
                "streak": leading_failures,
                "of": len(recent),
                "failed": sum(1 for c in recent if c == "failure"),
                "insufficient_window": True,
                "sparse": True,
            }
            continue
        if len(recent) < streak:
            # Too few runs inside the window to judge. That is NOT "fine", and
            # calling it fine is how openrappter's Ring workflow went from
            # correctly flagged to reported clean while still failing 5 of 5:
            # three merges pushed its runs out of a 20-run window shared with
            # every other workflow. The busier the repository, the smaller this
            # window is per workflow, so silence here scales with activity.
            if concl and all(c == "failure" for c in concl):
                out[name] = {"streak": len(concl), "of": len(concl),
                             "failed": len(concl), "insufficient_window": True}
            continue
        if all(c == "failure" for c in recent):
            out[name] = {"streak": len(recent), "of": len(concl),
                         "failed": sum(1 for c in concl if c == "failure")}
    return out


def workflows_never_succeeding(repo, limit=40, ignore=()):
    """Workflows with recent runs but not one success.

    `workflows_failing_every_run` only counts conclusion == "failure", so a
    workflow that is CANCELLED every time has fail == 0, fails the
    fail == total test, and is invisible. Verified live: rappterbook's
    static-api workflow was cancelled 3/3 — it had never once completed — and
    the sentinel reported all green.

    Cancellation is not benign. It usually means a concurrency group is
    starving the job, which is exactly "the work never happens" wearing a
    colour that is neither red nor green.

    Colour-blind on purpose (R3). The first version's candidate filter was
    `cancelled > 0` — the enumerate-known-bad shape sitting inside the very
    check written against it. A workflow skipped or timed out on every run
    has ok == 0 and cancelled == 0, and was invisible. So candidacy is now
    decided by require_success(): no explicit success across enough decided
    runs, whatever colours the non-successes wear.
    """
    runs = gh(["run", "list", "-R", repo, "--limit", str(limit),
               "--json", "name,conclusion"], default=None)
    if runs is None:
        return UNREADABLE
    if not runs:
        return None
    per = {}
    for r in runs:
        if r["name"] in ignore:
            continue
        per.setdefault(r["name"], []).append(r.get("conclusion"))
    candidates = {}
    for name, concl in per.items():
        if require_success(concl) != "no-success":
            continue
        decided = [c for c in concl if c]
        # Failing every run is workflows_failing_every_run's signal, already
        # paged as rb_workflows' "100% failing". Reporting it here too would
        # raise two alarms for one outage (prove_starvation_confirm holds
        # this contract). This check owns the colours that are neither red
        # nor green: cancelled, skipped, timed_out, action_required.
        if all(c == "failure" for c in decided):
            continue
        candidates[name] = {
            "total": len(decided),
            "colours": dict(Counter(decided)),
        }
    if not candidates:
        return {}
    # Confirm each candidate against a wider lookback FOR THAT WORKFLOW.
    #
    # The narrow window is counted in repo-wide runs, not time. On rappterbook
    # 40 runs span ~7.7h and are shared by 23 workflows, so 22 of them get 3 or
    # fewer runs -- meaning the `total >= 3` bar is cleared at exactly the
    # point the sample becomes a single thin time-slice. After the 2026-08-06
    # Actions outage this reported "never succeeded: Overseer (cloud fallback)"
    # on 3 outage-window runs, while that workflow had 5 successes just outside
    # it. The busier the repo, the narrower the window in wall-clock time and
    # the thinner the per-workflow evidence (#56).
    #
    # A workflow recovering from an incident is not a starved workflow. The
    # original case this check was written for -- static-api cancelled 3/3,
    # never once completed -- still fires, because a wider lookback finds no
    # success there either.
    confirmed = {}
    for name, v in candidates.items():
        wide = gh(["run", "list", "-R", repo, "--workflow", name,
                   "--limit", "30", "--json", "conclusion"], default=None)
        if wide is None:
            # Cannot widen: keep the candidate rather than silently clearing it.
            v["unconfirmed"] = True
            confirmed[name] = v
            continue
        if not any(r.get("conclusion") == "success" for r in wide):
            confirmed[name] = v
    return confirmed


# ══════════════════════════════════════════════════════════════════════════
# ── your checks ── everything below here is an example. Replace it.
# ══════════════════════════════════════════════════════════════════════════

# Instance state (direction.json, state/coverage.json) keys off HOME so one
# checkout can serve many SENTINEL_HOME instances. The checks themselves are
# code and ride with the checkout.
from paths import HOME

RV = "kody-w/rappterverse"
RB = "kody-w/rappterbook"

# The repositories with bespoke checks. Everything else declared in
# direction.json gets the generic sweep below, which is weaker but is the
# difference between "watched shallowly" and "not watched at all".
DEEPLY_CHECKED = {RV, RB}
CHANNEL = "https://kody-w.github.io/rappvision-field-notes"


def public_json(repo, path, attempts=2):
    """Read public state with a short retry; return (document, error)."""
    import time
    last = ""
    url = f"https://raw.githubusercontent.com/{repo}/main/{path}"
    for attempt in range(attempts):
        if attempt:
            time.sleep(attempt)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "rapp-sentinel"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8")), ""
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
    return None, last


def real_posts_analyzed(meta):
    """Real discussion count, excluding explicitly synthetic sidecars."""
    return int(
        meta.get("real_posts_analyzed",
                 meta.get("total_posts_analyzed", 0)) or 0)


def declared_repos():
    """What direction.json says this sentinel cares about.

    Read at runtime on purpose. Before this existed, `cares_about` was consumed
    by NO code and `watch_repos` only by the dashboard, so the declared scope
    and the real scope could drift apart indefinitely and did: the list named
    two repositories while the ecosystem had grown past twenty. Adding a name
    now adds a check, which is the only way a declaration means anything.
    """
    d = json.loads((HOME / "direction.json").read_text(encoding="utf-8"))
    return [r for r in (d.get("cares_about") or []) if "/" in r]


def _write_coverage_receipt(declared, swept, unreachable):
    """Record which repositories were actually examined this tick.

    An outside watcher needs to verify coverage, and its first attempt did it by
    grepping this file for repository literals. That worked only while the list
    was hardcoded -- the moment the sweep started reading cares_about at
    RUNTIME, which is the better design, every swept repository became invisible
    to the grep and the guard reported nine false positives.

    A guard that cries wolf is worse than no guard, because it teaches you to
    scroll past it. So coverage is now evidenced by behaviour instead of by
    source text: this is a receipt for what was looked at, written by the code
    that looked.
    """
    try:
        (HOME / "state").mkdir(exist_ok=True)
        (HOME / "state" / "coverage.json").write_text(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "declared": sorted(declared),
            "deep": sorted(DEEPLY_CHECKED),
            "swept": sorted(swept),
            "unreachable": sorted(unreachable),
            "examined": sorted(set(swept) | (set(declared) & DEEPLY_CHECKED)),
        }, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass          # a receipt is evidence, never a reason to fail a check


@check
def ecosystem_not_silently_broken():
    """A generic sweep over every declared repository.

    Deliberately shallow: it asks only whether a repository's workflows fail
    EVERY time, which is the signal that already caught a real outage on
    rappterbook. Depth per repository does not scale; noticing that a
    load-bearing repository has been red for a week does.

    Repositories with no workflow history are not a finding. Absence of runs is
    absence of evidence, and failing on it would train everyone to ignore this.
    """
    try:
        declared = declared_repos()
    except Exception as e:
        # The first version wrapped this in a bare except returning [], so an
        # undefined name made the sweep report "no additional repositories
        # declared" -- a green light for a check that could not run. Caught by
        # importing it. A declaration we cannot read is a finding, not a calm.
        return fail("eco_sweep",
                    f"cannot read cares_about: {type(e).__name__}: {e}",
                    critical=False)
    repos = [r for r in declared if r not in DEEPLY_CHECKED]
    if not repos:
        return ok("eco_sweep", "no additional repositories declared")
    broken, unreachable, no_history = {}, [], []
    for repo in repos:
        try:
            # Streak, not lifetime ratio. A workflow with one old success and
            # eight straight failures since is broken, and the ratio test called
            # it healthy.
            #
            # No explicit limit: the default is the reviewed one. #25 raised it
            # to 100 and this call site kept passing 20, so the fix never took
            # effect in production and the blindness it was written for was
            # still live. A caller that re-states a default silently owns it.
            bad = workflows_currently_broken(repo, streak=4)
        except Exception:
            unreachable.append(repo)
            continue
        if bad is None:
            # One repo without run history is absence of evidence, as the
            # docstring says. EVERY repo without it is a dead instrument
            # wearing the same clothes, and the old code could not tell them
            # apart -- it reported "N repositories swept, none on a red
            # streak" after sweeping nothing (#45).
            no_history.append(repo)
            continue
        if bad:
            broken[repo] = bad
    # The receipt is the only artifact proving coverage, so write it whether
    # or not anything is broken. It used to be written only inside `if broken`,
    # which skipped it precisely when coverage was zero.
    _write_coverage_receipt(declared, repos, unreachable)
    if len(repos) > 1 and len(no_history) == len(repos):
        return fail("eco_sweep",
                    f"swept {len(repos)} repos and every one returned no run "
                    f"history - instrument likely dead, not an all-clear",
                    critical=False)
    if broken:
        # "failed 20 of 22" and "I saw 2 runs and both failed" are different
        # claims. The first is a defect; the second is a prompt to look. Merging
        # them is what let an under-sampled healthy workflow (rapp-map's spine)
        # read as a third broken one.
        confirmed, thin = {}, {}
        for r, wfs in broken.items():
            for name, meta in wfs.items():
                if isinstance(meta, dict) and meta.get("insufficient_window"):
                    thin.setdefault(r, []).append((name, meta))
                else:
                    confirmed.setdefault(r, []).append(name)
        parts = []
        if confirmed:
            parts.append("red streak in %d repo(s) -- " % len(confirmed) + "; ".join(
                f"{r.split('/')[-1]}: {', '.join(sorted(w))}"
                for r, w in sorted(confirmed.items())))
        if thin:
            parts.append("thin failing history -- " + "; ".join(
                f"{r.split('/')[-1]}: " + ", ".join(
                    (
                        f"{name} (newest {meta.get('streak', '?')} failed; "
                        f"{meta.get('of', '?')} runs inspected)"
                        if meta.get("sparse")
                        else
                        f"{name} ({meta.get('failed', meta.get('streak', '?'))}/"
                        f"{meta.get('of', '?')} observed runs failed)"
                    )
                    for name, meta in sorted(workflows)
                )
                for r, workflows in sorted(thin.items())))
        detail = " | ".join(parts)
        # WARN, not CRITICAL. fail() defaults to critical, and critical is what
        # invokes the repair arm -- which knows rappterverse and rappterbook and
        # nothing about the other nine. A breadth check should make a problem
        # visible, not point an autonomous repairer at a repository whose
        # conventions it has never seen. Escalating these is a human's call.
        return fail("eco_sweep", detail, critical=False)
    _write_coverage_receipt(declared, repos, unreachable)
    note = f"{len(repos)} additional repositories swept, none on a red streak"
    if unreachable:
        note += f" ({len(unreachable)} unreachable: " + \
                ", ".join(r.split("/")[-1] for r in unreachable) + ")"
    return ok("eco_sweep", note)


@check
def world_still_merging():
    """The one that matters. This platform sat frozen for 19 days while every
    surface metric stayed green — 168ms first paint, zero errors, and no state
    merged since July 13th. 'The site returns 200' would never have caught it."""
    commits = gh(["api", f"repos/{RV}/commits?per_page=20", "--jq",
                  '[.[] | {msg:(.commit.message|split("\\n")[0]), d:.commit.committer.date}]'],
                 default=None)
    # A history we could not READ is not a world that stopped. `default=[]` made
    # a failed read and a frozen platform the same value, and the line below
    # states that value as a fact about the platform -- the one sentence this
    # sentinel exists to say. On 2026-08-13T18:30Z it said it while PR #6365 was
    # nine minutes old and all twenty commits in the window were merges; the
    # same tick could not read rappterverse's workflow list either. Warn, so a
    # blind instrument never reads green (prove_blind_green) and never spends
    # repair budget on an outage (#51). Observed absence still pages, below.
    if commits is None:
        return fail("rv_world_merging", "cannot read commit history",
                    critical=False)
    merges = [c for c in commits if c["msg"].startswith("[state] apply PR")]
    if not merges:
        return fail("rv_world_merging", "no state merges in the last 20 commits")
    h = hours_since(merges[0]["d"])
    # the heartbeat runs every 30 min; 3h of silence is a stall, not a lull
    return (ok("rv_world_merging", f"last merge {h:.1f}h ago") if h is not None and h < 3
            else fail("rv_world_merging", f"last merge {h:.1f}h ago"))


@outsider_check("rappterverse")
def world_is_meaningfully_active():
    """Commits are not life when one bot repeats one action forever.

    Outsider vantage: every read here is public_json — unauthenticated raw
    fetches of served state, the same bytes a stranger's crawler would get.
    The decorator holds that honest: any gh() call would replace this
    check's verdict with a credential-violation failure.
    """
    actions_doc, actions_error = public_json(RV, "state/actions.json")
    chat_doc, chat_error = public_json(RV, "state/chat.json")
    agents_doc, agents_error = public_json(RV, "state/agents.json")
    unreadable = [
        name for name, doc in (
            ("actions", actions_doc), ("chat", chat_doc), ("agents", agents_doc))
        if doc is None
    ]
    if unreadable:
        errors = "; ".join(
            error for error in (actions_error, chat_error, agents_error) if error)
        return fail("rv_meaningful_activity",
                    f"cannot read {', '.join(unreadable)} state: {errors[:160]}",
                    critical=False)

    actions = actions_doc.get("actions", [])
    messages = chat_doc.get("messages", [])
    recent = actions[-50:]
    actors = Counter(str(row.get("agentId") or "") for row in recent)
    types = Counter(str(row.get("type") or "") for row in recent)
    signatures = Counter(
        (str(row.get("agentId") or ""), str(row.get("type") or ""),
         json.dumps(row.get("data") or {}, sort_keys=True))
        for row in recent
    )
    findings = []
    actor_count = len([actor for actor in actors if actor])
    type_count = len([kind for kind in types if kind])
    if len(recent) >= 20 and actor_count < 3:
        findings.append(
            f"only {actor_count} actor(s) in the last {len(recent)} actions")
    if recent and signatures:
        signature, count = signatures.most_common(1)[0]
        if len(recent) >= 20 and count / len(recent) >= 0.9:
            findings.append(
                f"{count}/{len(recent)} actions repeat "
                f"{signature[0]} {signature[1]}")

    chat_age = hours_since((chat_doc.get("_meta") or {}).get("lastUpdate"))
    agent_age = hours_since((agents_doc.get("_meta") or {}).get("lastUpdate"))
    if chat_age is None:
        findings.append("chat has no readable lastUpdate")
    elif chat_age >= 24:
        findings.append(f"chat stale {chat_age:.1f}h")
    if agent_age is None:
        findings.append("agent state has no readable lastUpdate")
    elif agent_age >= 24:
        findings.append(f"agent state stale {agent_age:.1f}h")
    if not messages:
        findings.append("chat has no messages")

    if findings:
        return fail("rv_meaningful_activity", "; ".join(findings))
    return ok(
        "rv_meaningful_activity",
        f"{actor_count} actors, {type_count} action types in last {len(recent)}; "
        f"chat {chat_age:.1f}h old; agents {agent_age:.1f}h old",
    )


# Consecutive newest failures before the gate counts as rejecting work. The
# gate runs on roughly a 30min cadence, so 4 in a row is ~2h of refusing
# everything -- the same order of patience as the 3h staleness bar below.
GATE_STREAK = 4


@check
def action_gate_accepting():
    runs = gh(["run", "list", "-R", RV, "--workflow", "Validate Agent Action",
               "--limit", "10", "--json", "conclusion,createdAt"], default=None)
    # Same conflation as rv_world_merging above: an unreadable run list fell
    # through to the zero-runs branch and, whenever the workflow list happened
    # to read fine, paged "gate workflow is active but has zero runs". On
    # 2026-08-13 both reads failed together so it landed on the honest wording
    # below; had only this one failed it would have been a false critical.
    if runs is None:
        return fail("rv_validation", "cannot read gate run history",
                    critical=False)
    if not runs:
        # "no recent runs" was reported as ok while the id claims the gate is
        # ACCEPTING. A gate with no runs is stopped. Distinguish deleted from
        # stalled before deciding (#43).
        names = active_workflows(RV)
        if names is None:
            return fail("rv_validation", "no runs and workflow list unreadable",
                        critical=False)
        if "Validate Agent Action" in names:
            return fail("rv_validation", "gate workflow is active but has zero runs")
        return ok("rv_validation", "gate workflow not present")
    # A queued or in-progress run has not validated anything yet: gh reports it
    # with an empty conclusion. Dividing by every run in the window puts those
    # non-verdicts in the denominator, so "the gate is busy" reads as "the gate
    # is rejecting work". That is what paged the repair arm during the
    # 2026-08-06 Actions outage -- 5 successes, 3 runs killed at "Set up job"
    # before the validator existed, and 2 still queued -- reported as
    # "5/10 validations passing" with a critical severity aimed at a repository
    # that had done nothing wrong.
    #
    # Every other helper here already judges explicit conclusions only
    # (workflows_currently_broken, workflows_never_succeeding); this was the one
    # place that divided by runs it had no verdict for.
    decided = [r for r in runs if r.get("conclusion")]
    if not decided:
        # Absence of a verdict is not a rejection (#43), but it is not health
        # either. Non-critical: this is visible without pointing the repair arm
        # at a repository where there is nothing to repair.
        return fail("rv_validation",
                    f"{len(runs)} runs in flight, none concluded yet",
                    critical=False)
    # A ratio over a window that ENDED hours ago is not a live reading. During
    # the 2026-08-06 outage the gate ran every ~30min, stopped for 4.6h, and
    # this reported "5/10 validations passing" -- which reads as "rejecting
    # half the work" when the truth was "not running at all". Two different
    # problems. Without createdAt the check could not tell them apart, so a
    # gate stopped for days holding 10 stale successes would have reported
    # "10/10 validations passing" indefinitely, at full confidence (#52).
    #
    # #43 resolved an EMPTY window; this resolves a STALE one. Runs exist, so
    # that logic is never reached.
    newest = max((r.get("createdAt") or "" for r in decided), default="")
    age_h = hours_since(newest) if newest else None
    if age_h is None:
        return fail("rv_validation", "gate runs carry no timestamps",
                    critical=False)
    if age_h >= 3:
        # ~30min cadence; 3h is 6x headroom, the same reasoning as
        # rv_world_merging. Non-critical: a stopped gate during a GitHub
        # outage is not this repository's defect, and #50 already classifies
        # rv_validation as GITHUB_DEPENDENT so no repair budget is spent.
        return fail("rv_validation",
                    f"gate has not run in {age_h:.1f}h - stopped, not rejecting",
                    critical=False)
    good = sum(1 for r in decided if r.get("conclusion") == "success")
    d = f"{good}/{len(decided)} validations passing ({age_h:.1f}h old)"
    # A streak on the newest runs, not a ratio over the whole window. This is
    # the rule workflows_currently_broken already applies to every other
    # repository here: "what separates a real failure from a flake is not the
    # lifetime ratio, it is whether it is failing NOW".
    #
    # eco_sweep learned that in one direction -- a ratio let ONE old success
    # hide eight straight failures. This is the same error with the opposite
    # sign: a ratio lets five old failures hide five straight successes.
    #
    # Live, 2026-08-06: the gate failed 5 runs during the GitHub Actions outage
    # (15:41-17:48Z -- jobs dying at "Set up job", and run 31118968285 marked
    # failure while its own `validate` AND `validation-gate` jobs both
    # SUCCEEDED), then recovered and passed 5 in a row. Reported as
    # "5/10 validations passing" at CRITICAL, which woke the repair arm against
    # a gate that was at that moment accepting everything put in front of it.
    # The window was fresh, so #52's staleness guard could not catch it: the
    # runs were recent, the failures were simply over.
    #
    # gh returns runs newest-first, but this verdict now DEPENDS on that order,
    # so sort on the timestamps we already fetched rather than trusting it.
    newest_first = [r.get("conclusion") for r in
                    sorted(decided, key=lambda r: r.get("createdAt") or "",
                           reverse=True)]
    if (len(newest_first) >= GATE_STREAK
            and all(c == "failure" for c in newest_first[:GATE_STREAK])):
        return fail("rv_validation",
                    f"gate rejecting work: newest {GATE_STREAK} runs all "
                    f"failed ({d})")
    if good < len(decided) * 0.6:
        # Real failures in the window, but the gate is passing NOW. Visible,
        # not a page -- the same treatment eco_sweep and rb_rollup_coverage
        # give a finding a human should see but the repair arm cannot act on.
        # Self-clears as the burst ages out of the window.
        return fail("rv_validation",
                    f"{d} - failures are behind us, newest run "
                    f"{newest_first[0]}", critical=False)
    return ok("rv_validation", d)


@check
def queue_draining():
    """679 PRs once piled up behind a broken gate. A queue that only grows is
    the same failure as a dead queue, and neither shows up as an error."""
    prs = gh(["pr", "list", "-R", RV, "--state", "open", "--limit", "1000",
              "--json", "number,title,createdAt"], default=None)
    if not isinstance(prs, list):
        # default=0 made a dead API indistinguishable from an empty queue, and
        # an empty queue is the healthiest possible reading (#45).
        return fail("rv_pr_queue", "cannot read the PR queue", critical=False)
    n = len(prs)
    if not prs:
        return ok("rv_pr_queue", "empty")
    oldest = min(prs, key=lambda pr: pr.get("createdAt") or "")
    oldest_age = hours_since(oldest.get("createdAt"))
    titles = Counter(str(pr.get("title") or "") for pr in prs)
    repeated_title, repeated = titles.most_common(1)[0]
    findings = []
    if n >= 40:
        findings.append(f"{n} open PRs")
    if oldest_age is None:
        findings.append("oldest PR has no readable creation time")
    elif oldest_age >= 6:
        findings.append(
            f"oldest PR #{oldest['number']} has waited {oldest_age:.1f}h")
    if n >= 5 and repeated / n >= 0.8:
        findings.append(
            f"{repeated}/{n} queued PRs repeat {repeated_title[:80]}")
    if findings:
        return fail("rv_pr_queue", "; ".join(findings))
    return ok("rv_pr_queue", f"{n} open PRs, oldest {oldest_age:.1f}h")


@check
def workflows_healthy():
    broken = workflows_failing_every_run(RB)
    if broken is UNREADABLE:
        # Blind, not broken. Say so at warn: the repair arm cannot fix a read.
        return fail("rb_workflows", "cannot read run history", critical=False)
    if broken is None:
        # Empty history is not health. Ask whether anything is SUPPOSED to run.
        names = active_workflows(RB)
        if names is None:
            return fail("rb_workflows", "no run history and workflow list unreadable",
                        critical=False)
        if names:
            return fail("rb_workflows",
                        f"{len(names)} active workflows produced no runs at all")
        return ok("rb_workflows", "no workflows defined")
    return (ok("rb_workflows", "all green") if not broken
            else fail("rb_workflows", "100% failing: " + ", ".join(sorted(broken))))


@check
def rb_workflows_never_succeed():
    """A workflow that is always cancelled never fails, and never runs.

    Found the day it was written: the static-api workflow had been cancelled
    3/3 times, so it had produced nothing at all, while every failure-based
    check stayed green.
    """
    stuck = workflows_never_succeeding(RB)
    if stuck is UNREADABLE:
        return fail("rb_wf_starved", "cannot read run history", critical=False)
    if stuck is None:
        names = active_workflows(RB)
        if names is None:
            return fail("rb_wf_starved", "no run history and workflow list unreadable",
                        critical=False)
        if names:
            return fail("rb_wf_starved",
                        f"{len(names)} active workflows produced no runs at all")
        return ok("rb_wf_starved", "no workflows defined")
    if not stuck:
        return ok("rb_wf_starved", "every workflow has at least one success")

    def colour_note(v):
        colours = v.get("colours") or {}
        note = ", ".join(f"{colours[c]}/{v.get('total', '?')} {c}"
                         for c in sorted(colours))
        if v.get("unconfirmed"):
            note += ", wide lookback unreadable"
        return note

    # A workflow that is SKIPPED every run never ran the job at all — a
    # trigger or path-filter is deciding that, and a path-filtered workflow
    # can legitimately skip forever. That is a prompt to look, not a paged
    # outage, so all-skipped stays visible at WARN rather than pretending we
    # know which it is (refuse-rather-than-pretend). Any other colour mix —
    # cancelled, timed_out, action_required, failure — means work was asked
    # for and never completed, which is the starvation this check pages on.
    hard = {n: v for n, v in stuck.items()
            if set((v.get("colours") or {})) - {"skipped"}}
    soft = {n: v for n, v in stuck.items() if n not in hard}
    parts = []
    if hard:
        parts.append("never succeeded: " + ", ".join(
            f"{n} ({colour_note(v)})" for n, v in sorted(hard.items())))
    if soft:
        parts.append("never ran the job (all skipped - trigger or "
                     "path-filter suspect): " + ", ".join(
                         f"{n} ({colour_note(v)})" for n, v in sorted(soft.items())))
    return fail("rb_wf_starved", "; ".join(parts), critical=bool(hard))


@check
def rb_served_json_parses():
    """A served document that does not parse is worse than a missing one.

    state/social_graph.json sat on main with 590 unresolved git conflict
    markers - 1.69 MB, publicly served, CORS-open, unparseable by every
    consumer. Twelve files, 617 markers. No check looked at whether the
    bytes we publish are actually readable, only whether they were reachable,
    so all of it was invisible.
    """
    import json as _j
    import urllib.request
    bad, unread, parsed = [], [], 0
    names = ("stats", "agents", "channels", "trending", "social_graph")
    for name in names:
        url = f"https://raw.githubusercontent.com/{RB}/main/state/{name}.json"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "rapp-sentinel"})
            with urllib.request.urlopen(req, timeout=25) as r:
                _j.loads(r.read().decode("utf-8"))
            parsed += 1
        except _j.JSONDecodeError as e:
            bad.append(f"{name} ({str(e)[:40]})")
        except Exception as e:
            # Reachability is a different check's job — but a file we could
            # not read is a file we did NOT examine, and `except: pass` made
            # 4-unreachable-plus-1-parsed read as "served state parses (1/5)"
            # at full green. Partial blindness is not a passing grade (R3):
            # every unexamined name is carried into the verdict.
            unread.append(f"{name} ({type(e).__name__})")
    if bad:
        return fail("rb_json_parses", "unparseable: " + ", ".join(bad))
    if parsed == 0:
        # Every fetch failed, so nothing was examined and "served state parses"
        # was a claim about the empty set -- true and worthless (#45).
        return fail("rb_json_parses",
                    f"read none of {len(names)} state files - cannot judge "
                    f"parseability", critical=False)
    if unread:
        return fail("rb_json_parses",
                    f"parsed {parsed}/{len(names)}; could not read: "
                    + ", ".join(unread), critical=False)
    return ok("rb_json_parses", f"served state parses ({parsed}/{len(names)})")


@check
def rb_content_moving():
    """Workflows succeeding is not the same as work happening.

    This check exists because its absence cost five days. Every workflow on
    rappterbook reported success every ~2.5 hours from Jul 30 to Aug 4 while
    the fleet produced ZERO posts - three separate failures each degraded to a
    no-op and exited 0. `rb_workflows` said "all green" the entire time,
    because it only ever asked whether jobs were FAILING.

    It first measured GitHub Discussion recency. That proxy was invalidated on
    2026-08-06: discussions are now seeded by human engagement (upvote /
    downvote / comment), not by fleet output, so discussion silence became a
    CORRECT state. Keeping the old assertion would have failed the platform
    for behaving as designed - and the cheapest way to turn it green would
    have been to generate filler posts, which is exactly what the policy
    exists to prevent. A check satisfiable by degrading the product is worse
    than no check.

    So it now measures the layer that must keep moving regardless of human
    traffic: the roll-up that materializes state for the static site.
    `materialized_at` proves Compute Trending ran AND wrote output rather than
    merely exiting 0, and `total_posts_analyzed` is the content counter behind
    the site.

    Fetching that roll-up is not the same as judging it. One sample used to be
    enough to page: on 2026-08-14T14:29Z a single SSL handshake timeout against
    raw.githubusercontent.com reported "cannot read roll-up state" at CRITICAL
    and woke the repair arm, while `rb_rollup_coverage` read THE SAME URL
    successfully in THE SAME TICK and the roll-up was 0.6h old. Nothing had
    stopped moving; one TCP connection died.

    So the read is retried before it is believed, exactly as `outsider_can_join`
    already does against this host, and an exhausted read is warn -- it means we
    do not know whether content moved, which is not a repair the repair arm can
    perform (#45, #51, #58, #59, #60). A roll-up that is served but does not say
    when it was materialized is a different claim -- we read it and it is wrong
    -- and still pages. Bytes that parse are `rb_json_parses`' job, not this one.
    """
    import json as _j
    import time
    import urllib.request
    from datetime import datetime, timezone
    url = f"https://raw.githubusercontent.com/{RB}/main/state/trending.json"
    attempts, meta, last = 3, None, ""
    for i in range(attempts):
        if i:
            time.sleep(2 * i)
        try:
            req = urllib.request.Request(url,
                                         headers={"User-Agent": "rapp-sentinel"})
            # No explicit timeout: the module default is the reviewed one, and a
            # call site that re-states a default silently owns it (#27).
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                meta = _j.loads(r.read().decode("utf-8")).get("_meta", {})
            break
        except Exception as e:
            # "URLError" alone cannot separate a deleted roll-up from a dropped
            # connection, and that string is the whole diagnosis the repair arm
            # is woken with. Carry the reason.
            last = f"{type(e).__name__}: {e}"
    if meta is None:
        return fail("rb_content_moving",
                    f"cannot read roll-up state after {attempts} attempts: "
                    f"{last[:80]}", critical=False)
    try:
        stamp = meta.get("materialized_at") or meta["last_updated"]
        posts = real_posts_analyzed(meta)
    except Exception as e:
        return fail("rb_content_moving",
                    f"roll-up served without a materialization stamp "
                    f"({type(e).__name__}: {str(e)[:40]})")
    age_h = (datetime.now(timezone.utc)
             - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds() / 3600
    # Compute Trending runs every ~3-4h and each run commits. Observed worst
    # content gap over 24h was 7.6h; 12h leaves headroom without hiding a stall.
    if age_h >= 12:
        return fail("rb_content_moving",
                    f"roll-up stale {age_h:.1f}h - green workflows may be idle")
    if posts <= 0:
        return fail("rb_content_moving", "roll-up reports zero posts analyzed")
    return ok("rb_content_moving", f"roll-up {age_h:.1f}h old, {posts} posts")


@check
def rb_derived_state_tells_the_truth():
    """A fresh roll-up must not publish mutually impossible counters."""
    stats, stats_error = public_json(RB, "state/stats.json")
    trending, trending_error = public_json(RB, "state/trending.json")
    analytics, analytics_error = public_json(RB, "state/analytics.json")
    posted, posted_error = public_json(RB, "state/posted_log.json")
    unreadable = [
        name for name, doc in (
            ("stats", stats), ("trending", trending),
            ("analytics", analytics), ("posted_log", posted))
        if doc is None
    ]
    if unreadable:
        errors = "; ".join(
            error for error in (
                stats_error, trending_error, analytics_error, posted_error)
            if error)
        return fail("rb_derived_truth",
                    f"cannot read {', '.join(unreadable)}: {errors[:160]}",
                    critical=False)

    reported_posts = int(stats.get("total_posts") or 0)
    analyzed_posts = real_posts_analyzed(trending.get("_meta") or {})
    summary = analytics.get("summary") or {}
    total_comments = int(summary.get("total_comments") or 0)
    reply_rate = float(summary.get("reply_rate_pct") or 0)
    thread_depth = float(summary.get("avg_thread_depth") or 0)
    recent_posts = posted.get("posts") or []
    commented_posts = sum(
        1 for post in recent_posts if int(post.get("commentCount") or 0) > 0)

    findings = []
    if analyzed_posts and reported_posts < analyzed_posts * 0.9:
        findings.append(
            f"stats reports {reported_posts} posts while roll-up analyzed "
            f"{analyzed_posts}")
    if total_comments and commented_posts and reply_rate == 0:
        findings.append(
            f"analytics reports 0% reply rate despite {commented_posts} "
            f"commented retained posts")
    if total_comments and commented_posts:
        expected_depth = round(total_comments / commented_posts, 1)
        if abs(thread_depth - expected_depth) > max(1.0, expected_depth * 0.5):
            findings.append(
                f"analytics thread depth {thread_depth:.1f} disagrees with "
                f"{total_comments} comments across {commented_posts} threads")
    if findings:
        return fail("rb_derived_truth", "; ".join(findings))
    return ok(
        "rb_derived_truth",
        f"{reported_posts} reported / {analyzed_posts} analyzed posts; "
        f"reply rate {reply_rate:.1f}%; depth {thread_depth:.1f}",
    )


@outsider_check("rappterbook")
def outsider_can_join():
    """Can a stranger still participate, or only the privileged fleet?

    Every other check here runs with the owner's credentials, which proves
    nothing about the path an outside AI would take. This reads the public
    onboarding surface the way a newcomer would — unauthenticated.

    Confirms before escalating. One sample used to be enough to call the surface
    dead: a single dropped connection to raw.githubusercontent.com woke the
    repair arm at 19:12 on 2026-08-05, and the file was served 200 in 0.2s
    either side of it. critical=True spends money and takes actions, so it has
    to mean "outsiders cannot join", not "one TCP connection died". This is the
    same rule the workflow helpers already apply — judge a streak, not one
    sample — and this was the last check in the file still breaking it.
    """
    import time
    import urllib.request
    url = f"https://raw.githubusercontent.com/{RB}/main/state/agents.json"
    attempts, last = 3, ""
    for i in range(attempts):
        if i:
            time.sleep(2 * i)
        try:
            req = urllib.request.Request(url,
                                         headers={"User-Agent": "rapp-sentinel"})
            # No explicit timeout: the module default is the reviewed one, and a
            # call site that re-states a default silently owns it (#27). This one
            # said 20 while TIMEOUT said 25, on a payload now over 3MB.
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                import json as _j
                n = len(_j.loads(r.read().decode()).get("agents", {}))
            if n == 0:
                # Readable is not joinable (R3). A roster wiped to zero parses
                # cleanly and served 200, and the old code called that ok --
                # an empty world reading healthy. Warn, not critical: the
                # repair arm cannot restore a roster it did not delete, and
                # paging it at a platform-owner decision spends budget on
                # nothing (#50). Flip to critical only if observed behavior
                # earns it.
                return fail("rb_public_surface",
                            "public state readable but lists zero agents",
                            critical=False)
            note = f"public state readable, {n} agents"
            if i:
                note += f" (recovered after {i + 1} attempts)"
            return ok("rb_public_surface", note)
        except Exception as e:
            # "URLError" on its own cannot separate a deleted file from a
            # dropped connection, and that string is the entire diagnosis the
            # repair arm is woken with. Carry the reason.
            last = f"{type(e).__name__}: {e}"
    # Warn, not critical. Exhausting the retries means we do not KNOW whether
    # an outsider can join -- it is not evidence that they cannot. This branch
    # kept the default severity while the retry loop above was added, so the
    # check was taught to doubt one sample and then paged on the doubt anyway;
    # on 2026-08-17 a GitHub-side 503 fired it while diagnose.py read this same
    # URL 200 in 0.4s. It is also the same table rb_content_moving already
    # follows against this host: a failed fetch is warn, an observed bad read
    # still pages (#45, #51, #58, #59, #60). There is no repair the repair arm
    # can perform against GitHub's CDN.
    return fail("rb_public_surface",
                f"an outsider cannot read platform state "
                f"after {attempts} attempts: {last}", critical=False)


@check
def rb_rollup_covers_corpus():
    """A fresh timestamp over a frozen quantity is still an outage.

    rb_content_moving (#39) replaced a stale discussion-recency proxy with a
    roll-up freshness assertion, and shipped green while the roll-up had been
    frozen for 37 hours: `materialized_at` advanced on every run, so the
    freshness arm passed, and `total_posts_analyzed` sat at 11634 across ten
    consecutive materializations. Compute Trending reported success for all
    ten. See rappterbook#20887.

    Alarming on a frozen COUNT would repeat the mistake #39 fixed: under the
    engagement-triggered discussion policy, the corpus legitimately plateaus
    when nobody engages, so a growth-rate rule would fail the platform for
    behaving correctly.

    Coverage is policy-proof. When the platform is quiet, the analyzed count
    and the real corpus hold still TOGETHER and the ratio stays 1.0. The ratio
    only falls when the roll-up is computing the site's rankings from
    materially less than the platform contains. `real_posts_analyzed` is used
    when present so synthetic sidecar posts do not inflate corpus coverage.
    """
    import json as _j
    import urllib.request
    url = f"https://raw.githubusercontent.com/{RB}/main/state/trending.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rapp-sentinel"})
        with urllib.request.urlopen(req, timeout=25) as r:
            meta = _j.loads(r.read().decode("utf-8")).get("_meta", {})
        analyzed = real_posts_analyzed(meta)
    except Exception as e:
        return fail("rb_rollup_coverage", f"cannot read roll-up state ({str(e)[:40]})")
    q = ('{repository(owner:"%s",name:"%s")'
         '{discussions{totalCount}}}' % tuple(RB.split("/")))
    data = gh(["api", "graphql", "-f", f"query={q}"], default=None)
    try:
        actual = int(data["data"]["repository"]["discussions"]["totalCount"])
    except Exception:
        return fail("rb_rollup_coverage", "cannot read corpus size")
    if actual <= 0:
        return fail("rb_rollup_coverage", "corpus reports zero discussions")
    pct = 100.0 * analyzed / actual
    # Quiet platform => both numbers hold still together => pct stays ~100.
    # 90% absorbs materialization lag without hiding a structural shortfall.
    # Warn, not critical: the shortfall is real but the remedy (scrape scope
    # and API cost) is an owner decision, tracked in rappterbook#20887. Same
    # treatment eco_sweep gives a known red that is blocked on a human. A
    # nightly page nobody can act on trains people to ignore the channel.
    return (ok("rb_rollup_coverage", f"{pct:.1f}% ({analyzed}/{actual})")
            if pct >= 90 else
            fail("rb_rollup_coverage",
                 f"site computed from {pct:.1f}% of corpus ({analyzed}/{actual})",
                 critical=False))


_SHARD_INDEX_PATH = "state/cache_shards/index.json"


def _newest_shard(index):
    """The ACTIVE shard — highest range_start in the generator's own manifest.

    Shards are range-partitioned (250 discussion ids each). A shard goes
    permanently immutable the moment ids roll past its range_end, so only the
    newest one is still a write target. Raises rather than guessing when the
    manifest cannot be read as a shards map (#45).
    """
    shards = index.get("shards") if isinstance(index, dict) else None
    if not isinstance(shards, dict) or not shards:
        raise RuntimeError("shard index carries no shards map")
    numeric = [k for k in shards if str(k).lstrip("-").isdigit()]
    if not numeric:
        raise RuntimeError("shard index has no numeric ranges")
    key = max(numeric, key=int)
    row = shards[key]
    if not isinstance(row, dict):
        raise RuntimeError(f"shard entry {key} is not an object")
    return key, row


@check
def derived_data_regenerating():
    """Cache shards stopped regenerating when the write path jammed. The site
    404'd on this exact file for weeks.

    A bare 200-check was the full #11 triple in one line: reachable is not
    parseable is not current. The bytes are still required to parse and to
    claim a positive discussion count, and freshness is judged against the
    generator's MEASURED cadence: it commits every ~2-5h (worst observed gap
    4.9h across 2026-08-15/16), so 15h is three times the worst gap — the same
    headroom reasoning as rb_content_moving's 12h bar.

    This probe used to be pinned to shard_20750.json and took freshness from
    that one path's commit history. Shards are range-partitioned (250 ids
    each), so a shard goes PERMANENTLY immutable once discussion ids roll past
    its range_end — which happened at 2026-08-17T05:22Z when ids crossed
    21000. The generator never faltered: it kept writing and committing
    shard_21000.json every run. But the pinned path could not move again, so
    the check went critical-red at 16.1h and would have stayed red forever.
    That is exactly the "permanent false red" this docstring already warned
    about, arriving by range rollover rather than by a redesign, and the
    remedy it prescribed was a newest-shard-exists assertion rather than a
    silenced bar — which is what this now is.

    Freshness comes from index.json's `_meta.generated_at`, a stamp the
    generator writes INTO the bytes every run, so the verdict no longer
    depends on which shard happened to change, nor on commit history at all.
    The newest shard named by that index is then fetched, because an index
    naming a shard is not the site serving it — a 404 there is the original
    outage and stays critical.
    """
    import json as _j
    import urllib.request
    import urllib.error
    base = f"https://raw.githubusercontent.com/{RB}/main/state/cache_shards"

    def _get(url):
        req = urllib.request.Request(url, headers={"User-Agent": "rapp-sentinel"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read().decode("utf-8")

    try:
        body = _get(f"https://raw.githubusercontent.com/{RB}/main/{_SHARD_INDEX_PATH}")
    except Exception as e:
        # Blind, not broken: one dropped connection must not read as a jammed
        # write path (#45/#51).
        return fail("rb_shards",
                    f"cannot read shard index ({type(e).__name__}: {str(e)[:60]})",
                    critical=False)
    try:
        doc = _j.loads(body)
    except Exception as e:
        return fail("rb_shards", f"shard index unparseable ({str(e)[:60]})")
    try:
        key, row = _newest_shard(doc)
    except Exception as e:
        return fail("rb_shards", f"shard index unusable ({str(e)[:70]})")

    name = row.get("file") or f"shard_{int(key):05d}.json"
    try:
        sbody = _get(f"{base}/{name}")
    except urllib.error.HTTPError as e:
        # The index names a shard the site does not serve — the outage.
        return fail("rb_shards", f"newest shard {name} not served (HTTP {e.code})")
    except Exception as e:
        return fail("rb_shards",
                    f"cannot read newest shard {name} "
                    f"({type(e).__name__}: {str(e)[:50]})", critical=False)
    try:
        sdoc = _j.loads(sbody)
    except Exception as e:
        return fail("rb_shards", f"newest shard {name} unparseable ({str(e)[:50]})")
    count = (sdoc.get("_meta") or {}).get("count")
    if not isinstance(count, int) or count <= 0:
        return fail("rb_shards", f"newest shard {name} reports no discussions")

    return moving("rb_shards",
                  lambda: (doc.get("_meta") or {}).get("generated_at"), 15,
                  what=f"cache shards (newest {name}, {count} discussions)")


# ── issue #6: guardrails built for weaker models expire ─────────────────────

# The registry rides the CHECKOUT, not the instance: which rails exist is a
# property of the watched platform's code, the same reasoning that puts
# required_checks.json under CODE rather than HOME.
from paths import CODE as _CODE

SCAFFOLDING_REGISTRY = _CODE / "scaffolding" / "rappterbook.json"

# The one machine-readable cross-check rails_fresh can make from outside:
# rappterbook publicly serves this config, and quality_guardian.py regenerates
# its ban lists each cycle (verified live 2026-08-16: banned_phrases x64,
# banned_words x6). Any served list-valued key containing "banned" must have a
# registry row claiming it, or a new ban list is filtering output with no row
# dating it.
BAN_LIST_CONFIG = "state/quality_config.json"

_MONTH_DAYS = 30.4375


def months_since(day):
    """Months since an ISO date or datetime; None when unparseable."""
    try:
        d = datetime.fromisoformat(str(day).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).days / _MONTH_DAYS
    except Exception:
        return None


def rails_report(registry_doc, fetch):
    """Verdict over a scaffolding registry (pure - the fetch is injected).

    `registry_doc` is the parsed registry (None when it could not be read),
    `fetch(path)` returns the text of `path` as served from the watched
    repo's main branch, raising on failure. Injected so the harness needs
    no network and so the drift half is testable against synthetic worlds.

    Three claims, judged separately:
      * AGE - a rail older than review_after_months since
        max(written, last_reviewed) is surfaced BY NAME with what it was
        guarding against. last_reviewed=null legally means "never reviewed";
        age is then judged from written, so a never-reviewed rail cannot
        hide behind the null.
      * DRIFT, both directions - source.path must still contain
        must_contain. A vanished anchor is a finding ("rail no longer found
        upstream": either the rail was removed without this registry
        hearing about it, or it moved and the row is stale - both are the
        registry lying). An UNREADABLE fetch is counted as "unverifiable: N"
        and fails at warn - blind is never green (#45).
      * BAN-LIST CROSS-CHECK - every list-valued "banned*" key in the
        served quality config must have a claiming rail row, so a new ban
        list cannot start filtering output with no row dating it.
    """
    cid = "rails_fresh"
    if registry_doc is None:
        return fail(cid, "scaffolding registry unreadable - rail ages "
                         "unknown, which is the exact blindness #6 names",
                    critical=False)
    rails = registry_doc.get("rails")
    if not isinstance(rails, list) or not rails:
        return fail(cid, "scaffolding registry lists no rails - the audit "
                         "has not happened, not 'no rails exist'",
                    critical=False)
    try:
        bar = float(registry_doc.get("review_after_months") or 6)
    except Exception:
        bar = 6.0
    findings, unverifiable = [], []
    unmeasured, oldest = 0, 0.0
    for rail in rails:
        if not isinstance(rail, dict):
            findings.append("registry row is not an object")
            continue
        rid = str(rail.get("id") or "?")
        if not rail.get("rejection_ledger"):
            unmeasured += 1
        stamp = rail.get("last_reviewed") or rail.get("written")
        age = months_since(stamp)
        if age is None:
            findings.append(f"{rid} carries no parseable written/"
                            f"last_reviewed date - age unknown")
        else:
            oldest = max(oldest, age)
            if age >= bar:
                what = str(rail.get("guarding_against") or "?")[:90]
                findings.append(
                    f"{rid} unreviewed for {age:.1f} months (bar {bar:g}) - "
                    f"still guarding against: {what}")
        src = rail.get("source") or {}
        path, anchor = src.get("path"), src.get("must_contain")
        if not path or not anchor:
            findings.append(f"{rid} declares no verifiable source anchor")
            continue
        try:
            text = fetch(path)
        except Exception as e:
            unverifiable.append(f"{rid} ({type(e).__name__})")
            continue
        if anchor not in (text or ""):
            findings.append(f"{rid} rail no longer found upstream "
                            f"({path} lacks {anchor!r})")
    try:
        cfg = json.loads(fetch(BAN_LIST_CONFIG))
        for key in sorted(k for k, v in cfg.items()
                          if "banned" in k and isinstance(v, list) and v):
            claimed = any(
                key in str((r.get("source") or {}).get("must_contain") or "")
                or key in str(r.get("guarding_against") or "")
                for r in rails if isinstance(r, dict))
            if not claimed:
                findings.append(f"served ban list {key!r} "
                                f"({len(cfg[key])} entries) has no claiming "
                                f"rail row")
    except Exception as e:
        unverifiable.append(f"ban-list cross-check ({type(e).__name__})")
    ledger_note = (f"{unmeasured} rails with no rejection ledger (unmeasured)"
                   if unmeasured else "")
    if findings or unverifiable:
        parts = list(findings)
        if unverifiable:
            parts.append(f"unverifiable: {len(unverifiable)} "
                         f"({', '.join(unverifiable)})")
        if ledger_note:
            parts.append(ledger_note)
        return fail(cid, "; ".join(parts), critical=False)
    detail = (f"{len(rails)} rails verified upstream, oldest "
              f"{oldest:.1f} months against a {bar:g}-month bar")
    if ledger_note:
        detail += f"; {ledger_note}"
    return ok(cid, detail)


@check
def scaffolding_rails_fresh():
    """Quality rails expire silently as models improve; date the assumption.

    Issue #6's evidence: validate_grounded_references was entirely rational
    against weaker models, then its false-positive rate hit 100% of
    legitimate output and it caused a five-day total outage - and nothing in
    the loop noticed the day it flipped. scaffolding/rappterbook.json dates
    each rail (seeded from a read-only audit of the live repo, 2026-08-16;
    every path and anchor verified served) and this check surfaces, at warn,
    any rail unreviewed past the bar, any rail whose anchor has vanished
    upstream, and any served ban list with no claiming row.

    Warn ON PURPOSE. Clearing the red means a human bumping last_reviewed
    with a review_note in a commit - which IS the dated decision record #6
    asks for. Critical would page the repair arm at a judgment call it must
    not make (#6 ask 4: never strip a rail without evidence).

    Honest limits, stated rather than implied:
      * The registry is HAND-MAINTAINED CLAIMS. This check dates the
        assumption; it cannot verify that a claimed review actually
        happened, any more than a code comment can.
      * Full enumeration of UNREGISTERED rails is not automatable from
        outside - a new regex in content_engine.py is invisible until a
        human audits it into the registry. The one machine-readable
        cross-check that IS possible is made: rappterbook publicly serves
        state/quality_config.json whose ban lists are regenerated each
        cycle, so a ban list nobody claimed is caught. Everything else is
        the audit cadence this check exists to force.
    """
    try:
        doc = json.loads(SCAFFOLDING_REGISTRY.read_text(encoding="utf-8"))
    except Exception:
        doc = None
    cache = {}

    def fetch(path):
        # Memoized per tick: four rails share content_engine.py, and four
        # identical 25s-timeout fetches would quadruple both the cost and
        # the chance one dropped connection splits the verdict.
        if path not in cache:
            url = f"https://raw.githubusercontent.com/{RB}/main/{path}"
            req = urllib.request.Request(
                url, headers={"User-Agent": "rapp-sentinel"})
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    cache[path] = ("ok", r.read().decode("utf-8"))
            except Exception as e:
                cache[path] = ("err", e)
        kind, value = cache[path]
        if kind == "err":
            raise value
        return value

    return rails_report(doc, fetch)


REJECTION_LEDGER = "state/slop_cop_log.json"
REJECTION_WINDOW_H = 48
# n >= 5 and >= 90% flagged: "rejecting essentially everything" - the
# day-one catch for the July-30 outage shape, and the one shape worth
# paging. n >= 10 and >= 50%: visible, but whether the cop is miscalibrated
# or the input got worse is a human question - the same severity split
# rv_validation draws between a full-streak rejection and a bad ratio.
REJECTION_CRITICAL_MIN_N, REJECTION_CRITICAL_RATE = 5, 0.9
REJECTION_WARN_MIN_N, REJECTION_WARN_RATE = 10, 0.5


def rejection_report(doc, read_error=""):
    """Verdict over the served slop-cop ledger (pure - the read is injected).

    Live shape re-verified 2026-08-16 against
    https://raw.githubusercontent.com/kody-w/rappterbook/main/state/slop_cop_log.json:
    a ring of 500 `reviews` rows ({post_number, title, score, reason,
    flagged, timestamp}) plus `_meta` ({total_reviews, total_flags,
    last_run, last_run_reviewed, last_run_flagged, last_run_avg_score,
    materialized_at}). The window is judged by TIMESTAMP, never by row
    count: a 500-row ring spans ~10 weeks, and a rate over the whole ring
    would dilute a two-day 100%-rejection outage below every threshold.

    Verdicts, in the order they short-circuit:
      * ledger unreadable        -> warn ("unmeasured, not fine" - blind is
                                   never green, #45)
      * _meta.last_run >= 48h    -> warn: rejections UNMEASURED, not fine.
                                   A cop that stopped patrolling is not a
                                   clean street.
      * ran, but no review stamps
        inside 48h               -> warn: it ran and judged nothing.
      * n>=5, rate >= 90%        -> CRITICAL "rejecting essentially
                                   everything"
      * n>=10, rate >= 50%       -> warn - a human question
      * else                     -> ok with counts + freshness

    The min-sample gates are the difference between a signal and a slot
    machine: one flagged post on a quiet platform is 1/1 = 100% and must
    never page.
    """
    cid = "rb_rejection_rate"
    if doc is None:
        return fail(cid,
                    f"rejection ledger not readable - rejections unmeasured, "
                    f"not fine ({str(read_error)[:80]})", critical=False)
    meta = doc.get("_meta") or {}
    run_age = hours_since(str(meta.get("last_run") or ""))
    if run_age is None:
        return fail(cid, "rejection ledger carries no parseable "
                         "_meta.last_run - cannot claim rejections are "
                         "measured", critical=False)
    if run_age >= REJECTION_WINDOW_H:
        return fail(cid,
                    f"slop-cop last ran {run_age:.1f}h ago - rejections "
                    f"UNMEASURED for {run_age:.0f}h, not fine",
                    critical=False)
    window, stampless = [], 0
    for row in (doc.get("reviews") or []):
        if not isinstance(row, dict):
            continue
        age = hours_since(str(row.get("timestamp") or ""))
        if age is None:
            stampless += 1
        elif age < REJECTION_WINDOW_H:
            window.append(row)
    if not window:
        note = (f" ({stampless} rows carry no parseable timestamp)"
                if stampless else "")
        return fail(cid,
                    f"slop-cop ran {run_age:.1f}h ago but reviewed nothing "
                    f"in the last {REJECTION_WINDOW_H}h{note}",
                    critical=False)
    n = len(window)
    flagged = sum(1 for row in window if row.get("flagged"))
    rate = flagged / n
    d = (f"{flagged}/{n} flagged in the last {REJECTION_WINDOW_H}h "
         f"({rate:.0%}), cop ran {run_age:.1f}h ago")
    if n >= REJECTION_CRITICAL_MIN_N and rate >= REJECTION_CRITICAL_RATE:
        return fail(cid, f"slop-cop rejecting essentially everything: {d}")
    if n >= REJECTION_WARN_MIN_N and rate >= REJECTION_WARN_RATE:
        return fail(cid,
                    f"slop-cop flagging most recent output: {d} - "
                    f"miscalibrated rail or worse input, a human question "
                    f"either way", critical=False)
    return ok(cid, d)


@check
def rb_slop_cop_rejection_rate():
    """Measure rejections, not just failures (#6 ask 2).

    The July-30 outage would have been caught on day one by a "rejection
    rate hit 100%" check; this is that check for the one rail whose
    verdicts are PUBLIC. rejection_report holds the judgment; the read is
    injected here so the day another rail publishes a ledger, extending
    this is one wrapper, not a rewrite.

    The honest limit, stated plainly: this measures the ONE rail with a
    public ledger - slop_cop, which logs every review to
    state/slop_cop_log.json. The rail that actually caused July 30
    (validate_grounded_references) logs its rejections NOWHERE public;
    generation_outcome records them inside the run, and today only
    rb_content_moving catches the downstream symptom (output stops
    moving). The upstream ask that extends this check to the outage-shaped
    rail is rappterbook publishing state/validation_rejections.json - a
    per-rail rejected/accepted counter ring, same shape as the slop-cop
    log. Until it exists, this check is honest about measuring the
    measurable rather than pretending to measure everything.
    """
    doc, err = public_json(RB, REJECTION_LEDGER)
    return rejection_report(doc, err)


@check
def test_baseline_honest():
    """A baseline is a claim with a timestamp AND an environment (#13).

    'Matches the baseline' let a real regression through because the
    baseline was a number in someone's head from two hours ago — and the
    same commit measured 7, 10 or 0 failures depending on clone depth and
    directory permissions. baseline.py records the NAMED failing set with
    its environment stamp; this check reads those receipts every tick and
    reports set drift — newly_failing and newly_passing by name, never a
    count — plus the baseline's age, so nobody briefs an agent with a stale
    number again.

    Organism-safe by design: comparisons are heavy (full clone + suite) and
    OPT-IN via the com.rapp.test-baseline plist. A compare receipt only
    age-warns when it was written by the SCHEDULE (a scheduled instrument
    that stops is dead, #45); an install that never opted in is reported as
    exactly that, in the passing detail, not held at degraded forever —
    a permanent warn would silently disable the evolve arm.
    """
    import baseline as B
    try:
        enrolled = B.enrollment()
    except Exception as e:
        return fail("w_test_baseline",
                    f"enrollment unreadable: {type(e).__name__}", critical=False)
    if not enrolled:
        return ok("w_test_baseline", "no repos enrolled")
    critical_regressions = False
    try:
        cfg = json.loads((HOME / "config.json").read_text(encoding="utf-8"))
        critical_regressions = bool(cfg.get("baseline_regression_critical"))
    except Exception:
        pass
    problems, notes = [], []
    for slug in sorted(enrolled):
        try:
            base = json.loads(B.baseline_path(slug).read_text(encoding="utf-8"))
        except FileNotFoundError:
            problems.append(f"{slug}: no baseline recorded - run "
                            f"`python3 baseline.py record {slug}`")
            continue
        except Exception as e:
            problems.append(f"{slug}: baseline unreadable ({type(e).__name__})")
            continue
        age_h = hours_since(base.get("utc"))
        if age_h is None or not isinstance(base.get("failures"), list):
            problems.append(f"{slug}: baseline is undated or carries no "
                            f"failure set - the exact defect #13 names")
            continue
        stamp = f"{slug}: baseline {age_h / 24:.1f}d old, " \
                f"{len(base['failures'])} known-failing"
        try:
            comp = json.loads(B.compare_path(slug).read_text(encoding="utf-8"))
        except Exception:
            notes.append(stamp + ", never compared (opt-in schedule)")
            continue
        comp_age = hours_since(comp.get("utc"))
        if comp_age is None:
            # An undated compare receipt is the #13 defect wearing the fix's
            # own clothes — and formatting None crashed the whole check
            # (found by the 2026-08-16 review sweep).
            problems.append(f"{slug}: compare receipt is undated - "
                            f"cannot judge when it last ran")
            continue
        if comp.get("refused"):
            problems.append(f"{slug}: refusing set comparison - "
                            f"{comp['refused']}")
            continue
        newly_failing = comp.get("newly_failing") or []
        if newly_failing:
            problems.append(
                f"{slug}: newly failing vs baseline "
                f"{(base.get('commit') or '?')[:8]} ({base.get('utc')}): "
                + ", ".join(newly_failing)
                + (f"; newly passing: {', '.join(comp['newly_passing'])}"
                   if comp.get("newly_passing") else ""))
            continue
        if comp.get("scheduled") and comp_age is not None and comp_age >= 48:
            problems.append(f"{slug}: scheduled comparison stopped "
                            f"{comp_age:.0f}h ago - dead instrument, not health")
            continue
        note = stamp + f", 0 newly failing (compared {comp_age:.1f}h ago)"
        if comp.get("newly_passing"):
            note += f", newly passing: {', '.join(comp['newly_passing'])}"
        notes.append(note)
    if problems:
        return fail("w_test_baseline", "; ".join(problems + notes),
                    critical=critical_regressions and any(
                        "newly failing" in p for p in problems))
    return ok("w_test_baseline", "; ".join(notes))


@check
def publication_hygiene():
    """Did anything you meant to keep private end up in a public repo?

    Every individual commit looks reasonable, which is why this class of
    mistake is never caught by review: an internal matter number in a
    changelog, a path into a private repo in a test fixture, a person's name
    in a config file. Nothing in the estate noticed any of it until someone
    went looking on purpose, and by then it had been served publicly for
    months.

    ipscan.py does the expensive part on a schedule (clone every public repo,
    grep a LOCAL-ONLY denylist); this reads the receipt every tick. The
    denylist never ships and ipscan refuses to run against one that git
    tracks — a committed list of the strings you are hiding is a better
    disclosure than the leak it prevents. The receipt records file paths and
    counts, never the matched text, so this check can name where to look
    without republishing what it found.

    Unconfigured is not a failure: an install with no denylist says so in its
    passing detail rather than holding the verdict at degraded forever (the
    same opt-in shape as w_test_baseline, and for the same reason — a
    permanent warn silently disables the evolve arm).
    """
    import ipscan
    try:
        doc = json.loads(ipscan.RECEIPT.read_text(encoding="utf-8"))
    except FileNotFoundError:
        patterns, _, err = ipscan.load_denylist()
        if err:
            return fail("ip_hygiene", f"denylist unusable: {err[:120]}",
                        critical=False)
        if not patterns:
            return ok("ip_hygiene", "no publication denylist configured")
        return fail("ip_hygiene",
                    "denylist configured but never scanned - run "
                    "`python3 ipscan.py scan`", critical=False)
    except Exception as e:
        return fail("ip_hygiene",
                    f"scan receipt unreadable: {type(e).__name__}", critical=False)

    if not doc.get("configured"):
        return ok("ip_hygiene", "no publication denylist configured")
    age_h = hours_since(doc.get("utc"))
    findings = doc.get("findings") or []
    unscanned = doc.get("unscanned") or []
    if findings:
        # Critical: this is the one finding whose cost grows every hour it
        # stays up, and unlike an upstream outage there is always an action.
        where = "; ".join(
            f"{f['repo']}: " + ", ".join(x["file"] for x in f["files"][:3])
            for f in findings[:4])
        return fail("ip_hygiene",
                    f"denylisted content live in {len(findings)} public "
                    f"repo(s) - {where}")
    if age_h is None:
        return fail("ip_hygiene", "scan receipt is undated", critical=False)
    if age_h >= 24 * 8:
        return fail("ip_hygiene",
                    f"last scan {age_h / 24:.1f}d ago - the estate has moved "
                    f"since", critical=False)
    note = (f"{doc.get('scanned', 0)} public repos clean "
            f"({doc.get('pattern_count', 0)} patterns, {age_h / 24:.1f}d ago)")
    if unscanned:
        # Coverage gaps are stated, never rounded away (R3).
        return fail("ip_hygiene",
                    note + f"; {len(unscanned)} UNSCANNED: "
                    + ", ".join(u["repo"] for u in unscanned[:5]), critical=False)
    return ok("ip_hygiene", note)


@check
def github_status_operational():
    """Distinguish OUR outage from GitHub's.

    2026-08-06 18:10Z: the sentinel went critical with two true positives --
    rappterverse state had not merged in 3h and the validation gate was
    failing 5/10. Both real. Neither ours. GitHub Actions and Pages were in
    major outage, and the failed runs said so plainly:

        Failed to resolve action download info. Error: Service Unavailable
        ##[error]The HTTP request timed out after 00:01:40.

    Three unrelated workflows failed in the same window -- Validate Agent
    Action, PII Scan, Regression Tests -- which is not three coincidental
    bugs. The shape was visible and no check looked at it, so attribution
    required a human reading raw run logs.

    That distinction decides the response. "The gate is broken" is a defect
    to fix; "Actions is down" is a wait. The neighborhood has a repair arm
    that takes actions and spends money, and pointing it at an external
    outage aims it at a problem that does not exist here.

    Warn-level on purpose: this must never wake anyone, but it has to appear
    in the SAME report as the reds it explains.
    """
    import json as _j
    import urllib.request
    watched = ("Actions", "Pages", "API Requests", "Webhooks")
    url = "https://www.githubstatus.com/api/v2/components.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rapp-sentinel"})
        with urllib.request.urlopen(req, timeout=25) as r:
            comps = _j.loads(r.read().decode("utf-8")).get("components", [])
    except Exception as e:
        # Fail closed. Reporting "all operational" about a source we could not
        # read is the blind-green mistake (#45). Carry the reason: "HTTPError"
        # alone cannot separate a 503 (wait) from a 404 (fix the URL), and the
        # two demand opposite responses (#1, ask 5).
        return fail("gh_status",
                    f"cannot read GitHub status ({type(e).__name__}: "
                    f"{str(e)[:60]})",
                    critical=False)
    seen = {c.get("name"): c.get("status") for c in comps
            if c.get("name") in watched}
    if not seen:
        return fail("gh_status", "status page listed none of the watched components",
                    critical=False)
    bad = {n: s for n, s in seen.items() if s != "operational"}
    return (ok("gh_status", f"{len(seen)} components operational")
            if not bad else
            fail("gh_status",
                 "GitHub degraded: " + ", ".join(f"{n} {s}" for n, s in sorted(bad.items()))
                 + " - external, not ours",
                 critical=False))


def _fanout(cid, probes):
    """Report a multi-URL probe under ONE id: `cid`, on every path.

    These checks are registered in required_checks.json under the id they
    return when everything is up. Returning the per-URL result directly on
    failure meant the required id was absent EXACTLY when the platform was
    broken, so w_checks_complete -- whose critical means "a check was deleted"
    -- fired instead. A transient GitHub Pages 503 on one URL therefore
    escalated a warn into the one alarm reserved for the registry losing a
    check. Two unrelated failures, one signal, and the more serious one
    crying wolf.

    The per-URL id stays in the detail: which URL failed is the actionable
    part, and losing it to fix the id would trade one blind spot for another.
    """
    for sub, url in probes:
        r = url_check(sub, url)
        if not r["ok"]:
            # Carry the sub-probe's severity rather than assuming warn, so a
            # probe later marked critical cannot be silently downgraded here.
            return fail(cid, f"{sub} {r['detail']}",
                        critical=r["severity"] == CRITICAL)
    return None


@check
def sites_up():
    return _fanout("sites", (
        ("rv_site", "https://kody-w.github.io/rappterverse/"),
        ("rb_site", "https://kody-w.github.io/rappterbook/"),
    )) or ok("sites", "both serving")


@outsider_check("channel")
def channel_serving():
    # url_check is a plain unauthenticated GET — the outsider's read.
    return _fanout("channel", (
        ("ch_home", CHANNEL + "/"),
        ("ch_json", CHANNEL + "/rappvision/channel.json"),
        ("ch_feed", CHANNEL + "/feed.xml"),
    )) or ok("channel", "serving")


@check
def alerts_can_actually_reach_you():
    """A watchdog that cannot reach you is not watching anything.

    Delivery is the one dependency the rest of this system cannot compensate
    for: every other check could pass while the alert path is dead, and you
    would read silence as calm. So a backed-up outbox is a first-class health
    failure, not a footnote on a dashboard nobody opens at 3am.

    Warn-level on purpose — a stuck queue must not trigger an autonomous repair
    that then tries to tell you about itself through the same broken channel.
    """
    import sys, pathlib as _p
    sys.path.insert(0, str(_p.Path(__file__).resolve().parent))
    try:
        import outbox
        st = outbox.status()
    except Exception as e:
        return fail("alert_delivery", f"outbox unreadable: {e}", critical=False)
    n, age = st.get("pending", 0), st.get("oldest_minutes")
    missing = int(st.get("missing_attachments") or 0)
    last = st.get("last_drain") or {}
    why = (last.get("why") or "").strip()
    if missing:
        return fail("alert_delivery",
                    f"{missing} queued static report attachment(s) are missing",
                    critical=False)

    # A drain that failed already knows which failure it was. Waiting 180
    # minutes to mention it wastes the one piece of information that says what
    # to do about it: an osascript timeout is a TCC/launchd problem and will
    # send fine interactively, while "exited 0 but chat.db recorded no SENT
    # message" means the message was rejected and will fail the same way
    # everywhere. Still warn-level -- a stuck queue must not trigger a repair
    # that tries to report itself through the same broken channel.
    if n and why:
        return fail("alert_delivery",
                    f"{n} alert(s) queued; last drain failed: {why[:150]}",
                    critical=False)
    if not n:
        return ok("alert_delivery", "no queued alerts")
    if age and age > 180:
        return fail("alert_delivery",
                    f"{n} alert(s) undelivered for {age:.0f}m — persistent "
                    f"outbox drainer stalled; run `python3 outbox.py drain`",
                    critical=False)
    return ok("alert_delivery",
              f"{n} queued, {age:.0f}m old (persistent drain within 5 min)")


# ── the visitor's vantage (#12) ─────────────────────────────────────────────

# The served pages a visitor actually loads. Overridable module-level on
# purpose, same as the constants above: the prove harness points these at
# synthetic pages.
PAGE_FETCH_PAGES = (
    f"https://kody-w.github.io/{RV.split('/')[1]}/",
    f"https://kody-w.github.io/{RB.split('/')[1]}/",
    f"https://kody-w.github.io/{RB.split('/')[1]}/reddit.html",
    f"https://kody-w.github.io/{RB.split('/')[1]}/hackernews.html",
    f"https://kody-w.github.io/{RB.split('/')[1]}/explore.html",
    CHANNEL + "/",
)

# Hard cap on probes per tick. When it is hit the cap is NAMED in the
# detail — silent truncation would be a coverage claim the check no longer
# makes. Pollers and fetch/xhr targets sort first, so the cap sheds the
# passive tail (images), never the loud failures.
PAGE_PROBE_CAP = 40
PAGE_PROBE_WALL_S = 120     # wall-clock budget for the probe loop; see below

_KIND_RANK = {"fetch": 0, "xhr": 1, "script": 2, "link": 3, "json": 4,
              "img": 5}


def _short_name(url):
    from urllib.parse import urlparse as _up
    path = _up(url).path
    return path.rsplit("/", 1)[-1] or path or url


def _write_pagescan_receipt(pages, targets, failures):
    """Receipt for what the visitor's vantage observed this tick.

    Best-effort, never a reason to fail the check (mirrors
    _write_coverage_receipt). observed_by:"regex" states the vantage; a
    CDP-driven scanner can later write the same file with observed_by:"cdp"
    and every consumer — cadence_honest reads the targets — carries on.
    """
    try:
        (HOME / "state").mkdir(exist_ok=True)
        (HOME / "state" / "pagescan.json").write_text(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "observed_by": "regex",
            "pages": [{"url": p["url"], "status": p["status"],
                       "targets": len(p["targets"]),
                       "skipped": p["skipped"]} for p in pages],
            "targets": [{"url": t["url"], "kind": t["kind"],
                         "polling": t["polling"],
                         "interval_s": t["interval_s"],
                         "first_party": True,
                         "status": t.get("probe_status")} for t in targets],
            "failures": [t["url"] for t in failures],
        }, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass          # a receipt is evidence, never a reason to fail a check


@check
def served_pages_fetch_what_they_ask_for():
    """Does everything the served pages say they will request actually come
    back? Nothing asked this before #12, and the answer was six dangling
    fetches — three polling forever — and 550KB of authored content
    silently 404ing behind a state/-vs-docs/ path drift that no repo-side
    check could see, because each half was individually fine.

    STATED LIMIT: this vantage is regex extraction over served bytes
    (pagescan.py). It sees URL literals; a URL computed at runtime is
    counted as a blind spot, never guessed. A CDP-driven variant can slot
    in later by writing state/pagescan.json with observed_by:"cdp" — the
    receipt is the seam, this check's verdict logic does not change.

    EXPECTED TO FIRE against rappterbook's dead-fetch class — it was
    written to, and the first live scan (2026-08-16) found the #12 drift
    already fixed upstream (reddit.html's FEED_BASE now points at
    docs/feed) with the remaining dangling requests hiding behind computed
    URLs, counted as blind spots. While red it holds the install at
    degraded, which pauses the level-3 evolve arm. That is CORRECT
    behavior: evolve's precondition is "nothing is on fire", and 550KB of
    authored content silently 404ing on every visitor is a fire. The
    remedy is fixing the platform repo (or an owner-filed acceptance),
    never softening this check.

    critical=False throughout: the repair arm operates on this install; the
    fix for a dangling page request is a content change in the platform
    repo, which is not a repair it can perform (#50/#51 budget rule).
    """
    import pagescan
    pages = [pagescan.scan_page(u, pagescan.http_fetch)
             for u in PAGE_FETCH_PAGES]
    blind = sum(p["skipped"] for p in pages)
    unfetchable = [p for p in pages if not (200 <= p["status"] < 300)]

    total_extracted = sum(len(p["targets"]) for p in pages)
    if total_extracted == 0:
        _write_pagescan_receipt(pages, [], [])
        note = ""
        if unfetchable:
            note = ("; " + ", ".join(
                f"{_short_name(p['url']) or 'page'} HTTP {p['status']}"
                for p in unfetchable) + " unfetchable")
        return fail("page_fetch",
                    f"scanner extracted nothing across {len(pages)} pages "
                    f"— blind, not clean{note}", critical=False)

    merged, third_party = {}, set()
    for p in pages:
        for t in p["targets"]:
            if not t["first_party"]:
                third_party.add(t["url"])
                continue
            cur = merged.get(t["url"])
            if cur is None:
                cur = dict(t)
                merged[t["url"]] = cur
            elif t["polling"]:
                cur["polling"] = True
                if t["interval_s"] is not None and (
                        cur["interval_s"] is None
                        or t["interval_s"] < cur["interval_s"]):
                    cur["interval_s"] = t["interval_s"]

    ordered = sorted(merged.values(),
                     key=lambda t: (not t["polling"],
                                    _KIND_RANK.get(t["kind"], 9), t["url"]))
    capped = len(ordered) > PAGE_PROBE_CAP
    to_probe = ordered[:PAGE_PROBE_CAP]
    failures, probed = [], 0
    # A wall budget as well as a count cap. 40 probes at the 25s module
    # timeout is a 1000s worst case inside a health run whose own budget is
    # 600s — a slow network would turn the whole verdict into a timeout.
    # Stopping at the wall and SAYING so keeps the claim honest: probed
    # targets are judged, unprobed ones are named as unprobed, and no
    # silent truncation reads as coverage (2026-08-16 review sweep).
    import time as _t
    deadline = _t.monotonic() + PAGE_PROBE_WALL_S
    for t in to_probe:
        if _t.monotonic() > deadline:
            break
        t["probe_status"] = pagescan.probe(t["url"], pagescan.http_fetch)
        probed += 1
        if not (200 <= t["probe_status"] < 300):
            failures.append(t)
    _write_pagescan_receipt(pages, to_probe, failures)

    notes = []
    if probed < len(to_probe):
        notes.append(f"probe wall {PAGE_PROBE_WALL_S}s hit after "
                     f"{probed}/{len(to_probe)} targets — the rest unprobed, "
                     f"not fine")
    if capped:
        notes.append(f"probe cap {PAGE_PROBE_CAP} hit "
                     f"({len(ordered)} first-party targets — "
                     f"{len(ordered) - PAGE_PROBE_CAP} unprobed)")
    if unfetchable:
        notes.append(", ".join(
            f"page {_short_name(p['url']) or p['url']} HTTP {p['status']}"
            for p in unfetchable) + " — could not scan")

    if failures or unfetchable:
        # Pollers first — a 404 every 30s is a different outage from one at
        # load — then fetch/xhr/script, passive img last (`ordered` already
        # sorts that way and `failures` preserves it).
        parts = []
        for t in failures:
            status = t["probe_status"] or "unreachable"
            if t["polling"]:
                iv = (f"polled every {t['interval_s']:g}s"
                      if t["interval_s"] is not None
                      else "polled (interval not parseable)")
                parts.append(f"{_short_name(t['url'])} {status}, {iv}")
            else:
                parts.append(f"{_short_name(t['url'])} {status} ({t['kind']})")
        detail = (f"{len(failures)}/{len(to_probe)} first-party requests "
                  f"do not come back: " + "; ".join(parts[:6]))
        if len(parts) > 6:
            detail += f" (+{len(parts) - 6} more)"
        if not failures:
            detail = "every probed request came back"
        if notes:
            detail += "; " + "; ".join(notes)
        return fail("page_fetch", detail, critical=False)

    pollers = sum(1 for t in to_probe if t["polling"])
    detail = (f"{len(pages)} pages, {len(to_probe)} first-party targets all "
              f"2xx ({pollers} pollers), {len(third_party)} third-party "
              f"skipped, {blind} computed-URL blind spots")
    if notes:
        detail += "; " + "; ".join(notes)
    return ok("page_fetch", detail)


# Seed documents whose cadence claims matter most, UNIONed at runtime with
# the first-party .json targets the last page scan actually observed.
CADENCE_SEED_DOCS = (
    f"https://raw.githubusercontent.com/{RB}/main/state/trending.json",
    f"https://raw.githubusercontent.com/{RB}/main/state/synthetic_posts.json",
    f"https://raw.githubusercontent.com/{RV}/main/state/chat.json",
    CHANNEL + "/rappvision/channel.json",
)
CADENCE_DOC_CAP = 15
CADENCE_GRACE = 10          # fire only past 10x the declared cadence...
CADENCE_FLOOR_S = 1800      # ...and never inside 30 min, so cron jitter
                            # cannot page. The target is 2-minutes-claimed
                            # vs a-month-real, not 2-minutes vs 9.


def _fmt_span(seconds):
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


@check
def served_docs_keep_their_cadence_claims():
    """A served document whose declared refresh cadence contradicts its own
    newest timestamp is lying to every reader, and the contradiction is
    machine-checkable (#12): synthetic_posts.json claimed a 2-minute
    refresh while its last_updated sat a month old, and nothing compared
    the two halves of the same file.

    Grammar-or-nothing: declared_cadence() parses a closed grammar and
    returns None for anything else, so an unparsed claim is counted as
    silent, never guessed at. Firing needs BOTH halves readable — except a
    cadence-declared-with-no-readable-stamp, which fires as an
    unfalsifiable claim (a promise with no way to check it is the R1 shape:
    a receipt the document writes about itself).

    The bar is max(10x declared, 30min): 10x grace plus a floor, so cron
    jitter never pages — the target is a month of drift behind a 2-minute
    claim. Unreachable or unparseable documents are counted in the detail,
    not judged; parseability is rb_json_parses' beat. critical=False: the
    remedy is a content fix in the platform repo, not repair-arm budget.
    """
    import pagescan
    docs = list(CADENCE_SEED_DOCS)
    # Union with what the visitor's vantage last saw, if it is fresh enough
    # to trust (<24h). A stale or missing receipt silently narrows to the
    # seed set — the union is an enrichment, never a dependency.
    try:
        receipt = json.loads((HOME / "state" / "pagescan.json").read_text(
            encoding="utf-8"))
        age_h = hours_since(receipt.get("at"))
        if age_h is not None and age_h < 24:
            for t in receipt.get("targets") or []:
                u = t.get("url") or ""
                if (t.get("first_party")
                        and u.split("?")[0].endswith(".json")
                        and u not in docs):
                    docs.append(u)
    except Exception:
        pass
    docs = docs[:CADENCE_DOC_CAP]

    liars, unreadable = [], []
    read = declared = 0
    now = datetime.now(timezone.utc)
    # Same wall-budget discipline as page_fetch: 15 docs at the 25s module
    # timeout is a 375s worst case; stop at the wall and count the rest as
    # unread rather than letting a slow network eat the health run's budget.
    import time as _t
    deadline = _t.monotonic() + 90
    for u in docs:
        if _t.monotonic() > deadline:
            unreadable.append(f"{len(docs) - read - len(unreadable)} doc(s) "
                              f"unread - 90s wall budget hit")
            break
        status, body = pagescan.http_fetch(u)
        if not (200 <= status < 300) or not body:
            unreadable.append(f"{_short_name(u)} ({status or 'unreachable'})")
            continue
        try:
            doc = json.loads(body)
        except Exception:
            unreadable.append(f"{_short_name(u)} (unparseable)")
            continue
        read += 1
        cadence = pagescan.declared_cadence(doc)
        if cadence is None:
            continue
        declared += 1
        stamp = pagescan.newest_stamp(doc)
        if stamp is None:
            liars.append(f"{_short_name(u)} claims refresh every "
                         f"{_fmt_span(cadence)} but carries no readable "
                         f"stamp — unfalsifiable claim")
            continue
        age_s = (now - pagescan.parse_iso(stamp)).total_seconds()
        if age_s > max(CADENCE_GRACE * cadence, CADENCE_FLOOR_S):
            liars.append(f"{_short_name(u)} claims refresh every "
                         f"{_fmt_span(cadence)} but newest stamp is "
                         f"{_fmt_span(age_s)} old")
    if read == 0:
        return fail("cadence_honest",
                    f"read none of {len(docs)} served documents — blind, "
                    f"not clean (" + ", ".join(unreadable[:5]) + ")",
                    critical=False)
    suffix = ""
    if unreadable:
        suffix = (f" ({len(unreadable)} unreadable, counted not judged: "
                  + ", ".join(unreadable[:4]) + ")")
    if liars:
        return fail("cadence_honest", "; ".join(liars[:5]) + suffix,
                    critical=False)
    return ok("cadence_honest",
              f"{read}/{len(docs)} documents read, {declared} declare a "
              f"cadence and every stamp honors it, {read - declared} "
              f"declare none{suffix}")
# The evidence file the outsider smoke writes (participate.py) and the check
# below reads. A module global so a prove harness can point the read at a
# synthetic log without touching the instance's real record.
PARTICIPATION_LOG = HOME / "state" / "participation.jsonl"


def _smoke_interval_hours():
    """config.json's smoke_interval_hours, defaulting like sentinel.DEFAULTS.

    Tolerant on purpose (growth path): the live config predates the key."""
    try:
        cfg = json.loads((HOME / "config.json").read_text(encoding="utf-8"))
        return float(cfg.get("smoke_interval_hours", 72))
    except Exception:
        return 72.0


def _smoke_enabled():
    """Whether this instance is authorized to exercise public write paths."""
    try:
        cfg = json.loads((HOME / "config.json").read_text(encoding="utf-8"))
        return bool(cfg.get("smoke_enabled", True))
    except Exception:
        return True


@check
def outsider_smoke_exercised():
    """The front door stays EXERCISED, not just theoretically open (#5).

    rb_public_surface proves a stranger can READ; nothing read-only can
    prove a stranger can WRITE. sentinel.outsider_smoke runs participate.py
    against one platform per spend and participation.jsonl carries the
    verdicts — each row's ok earned by re-reading PUBLISHED state, never a
    receipt (R1, #2). This check is the read-only side of that loop: per
    platform participate.py names, the newest smoke row must be a
    smoke.landed with ok=True, younger than 2x the smoke interval plus 24h
    (one full missed rotation plus a day of slack).

    Absence pages (#43): a platform never smoked, or a missing log, is
    "outsider path never exercised" — warn, so a fresh clone is not
    critical on tick 1, and because the remedy (waiting for the budgeted
    smoke, or raising the level so it may write) is a human's call, not the
    repair arm's. An ok=True row that is not smoke.landed (a dry run, or a
    run that died between steps) never reached published-state verification
    and does not count as exercised — that would be a receipt.

    Known coupling, stated rather than hidden: the tick's smoke only runs
    while the verdict is HEALTHY, and this warn holds a fresh install at
    degraded — so a new instance pages here until a human primes the loop
    with one manual smoke. That is deliberate (#43: absence must page; a
    front door nobody has ever opened is not a green), and the detail
    carries the priming command so the page is actionable.
    """
    if not _smoke_enabled():
        return ok("w_outsider_smoke",
                  "disabled by config; this instance is read-only on watched platforms")
    import participate
    platforms = sorted(participate.PLATFORMS)
    try:
        lines = PARTICIPATION_LOG.read_text(encoding="utf-8").splitlines()
    except Exception:
        return fail("w_outsider_smoke",
                    "outsider path never exercised - no participation log; "
                    "prime by hand once: python3 participate.py smoke "
                    "--platform <name>", critical=False)
    newest = {}
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        if str(row.get("kind", "")).startswith("smoke."):
            newest[row.get("platform")] = row      # later lines overwrite
    bar = 2 * _smoke_interval_hours() + 24
    findings = []
    for p in platforms:
        row = newest.get(p)
        if row is None:
            findings.append(f"{p}: outsider path never exercised")
            continue
        if not row.get("ok"):
            findings.append(f"{p}: newest smoke failed - {row.get('kind')}: "
                            f"{str(row.get('detail'))[:80]}")
            continue
        if row.get("kind") != "smoke.landed":
            findings.append(f"{p}: newest smoke never reached published-state "
                            f"verification ({row.get('kind')})")
            continue
        age = hours_since(row.get("utc"))
        if age is None:
            findings.append(f"{p}: newest smoke row carries no readable "
                            f"timestamp")
        elif age >= bar:
            findings.append(f"{p}: last verified smoke {age:.1f}h ago "
                            f"(bar {bar:.0f}h)")
    if findings:
        detail = "; ".join(findings)
        if "never exercised" in detail:
            detail += ("; prime by hand once: python3 participate.py smoke "
                       "--platform <name>")
        return fail("w_outsider_smoke", detail, critical=False)
    return ok("w_outsider_smoke",
              f"{len(platforms)} platform(s) smoked from outside within "
              f"{bar:.0f}h")
