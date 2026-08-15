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

Everything below `# ── your checks ──` is an example: the two GitHub-native
platforms this pattern was built for. Delete it and write your own.
"""

import json
import pathlib as _pathlib
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


def gh(args, default=None):
    """Run `gh` and parse JSON. Returns `default` on any failure."""
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

    gh returns runs newest-first; this relies on that ordering.
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
    out = {}
    for name, concl in per.items():
        recent = concl[:streak]
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
        d = per.setdefault(r["name"], {"ok": 0, "total": 0, "cancelled": 0})
        d["total"] += 1
        if r.get("conclusion") == "success":
            d["ok"] += 1
        elif r.get("conclusion") == "cancelled":
            d["cancelled"] += 1
    candidates = {n: v for n, v in per.items()
                  if v["total"] >= 3 and v["ok"] == 0 and v["cancelled"] > 0}
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

HOME = _pathlib.Path(__file__).resolve().parent

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
                target = thin if (isinstance(meta, dict)
                                  and meta.get("insufficient_window")) else confirmed
                target.setdefault(r, []).append(name)
        parts = []
        if confirmed:
            parts.append("red streak in %d repo(s) -- " % len(confirmed) + "; ".join(
                f"{r.split('/')[-1]}: {', '.join(sorted(w))}"
                for r, w in sorted(confirmed.items())))
        if thin:
            parts.append("too few runs to judge -- " + "; ".join(
                f"{r.split('/')[-1]}: {', '.join(sorted(w))}"
                for r, w in sorted(thin.items())))
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


@check
def world_is_meaningfully_active():
    """Commits are not life when one bot repeats one action forever."""
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
    detail = ", ".join(f"{n} ({v['cancelled']}/{v['total']} cancelled)"
                       for n, v in sorted(stuck.items()))
    return fail("rb_wf_starved", "never succeeded: " + detail)


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
    bad, parsed = [], 0
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
        except Exception:
            # Reachability is a different check's job; only parseability here.
            pass
    if bad:
        return fail("rb_json_parses", "unparseable: " + ", ".join(bad))
    if parsed == 0:
        # Every fetch failed, so nothing was examined and "served state parses"
        # was a claim about the empty set -- true and worthless (#45).
        return fail("rb_json_parses",
                    f"read none of {len(names)} state files - cannot judge "
                    f"parseability", critical=False)
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


@check
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
            note = f"public state readable, {n} agents"
            if i:
                note += f" (recovered after {i + 1} attempts)"
            return ok("rb_public_surface", note)
        except Exception as e:
            # "URLError" on its own cannot separate a deleted file from a
            # dropped connection, and that string is the entire diagnosis the
            # repair arm is woken with. Carry the reason.
            last = f"{type(e).__name__}: {e}"
    return fail("rb_public_surface",
                f"an outsider cannot read platform state "
                f"after {attempts} attempts: {last}")


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


@check
def derived_data_regenerating():
    """Cache shards stopped regenerating when the write path jammed. The site
    404'd on this exact file for weeks."""
    return url_check("rb_shards", f"https://raw.githubusercontent.com/{RB}/main/"
                                  "state/cache_shards/shard_20750.json")


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
        # read is the blind-green mistake (#45).
        return fail("gh_status", f"cannot read GitHub status ({type(e).__name__})",
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


@check
def channel_serving():
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
                    f"{n} alert(s) undelivered for {age:.0f}m — grant Automation "
                    f"permission or run `python3 outbox.py drain`", critical=False)
    return ok("alert_delivery", f"{n} queued, {age:.0f}m old (drains on next check-in)")
