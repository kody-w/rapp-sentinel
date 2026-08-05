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


def workflows_failing_every_run(repo, limit=30, ignore=()):
    """Workflows that failed EVERY recent run. Ignores single flakes."""
    runs = gh(["run", "list", "-R", repo, "--limit", str(limit),
               "--json", "name,conclusion"], default=None)
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


def workflows_currently_broken(repo, limit=20, streak=4, ignore=()):
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
        if len(recent) >= streak and all(c == "failure" for c in recent):
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
    return {n: v for n, v in per.items()
            if v["total"] >= 3 and v["ok"] == 0 and v["cancelled"] > 0}


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
    broken, unreachable = {}, []
    for repo in repos:
        try:
            # Streak, not lifetime ratio. A workflow with one old success and
            # eight straight failures since is broken, and the ratio test called
            # it healthy.
            bad = workflows_currently_broken(repo, limit=20, streak=4)
        except Exception:
            unreachable.append(repo)
            continue
        if bad is None:
            continue                      # no run history: no signal
        if bad:
            broken[repo] = sorted(bad)
    if broken:
        _write_coverage_receipt(declared, repos, unreachable)
        detail = "; ".join(f"{r.split('/')[-1]}: {', '.join(w)}"
                           for r, w in sorted(broken.items()))
        # WARN, not CRITICAL. fail() defaults to critical, and critical is what
        # invokes the repair arm -- which knows rappterverse and rappterbook and
        # nothing about the other nine. A breadth check should make a problem
        # visible, not point an autonomous repairer at a repository whose
        # conventions it has never seen. Escalating these is a human's call.
        return fail("eco_sweep", f"red streak in {len(broken)} repo(s) -- {detail}",
                    critical=False)
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
                 default=[]) or []
    merges = [c for c in commits if c["msg"].startswith("[state] apply PR")]
    if not merges:
        return fail("rv_world_merging", "no state merges in the last 20 commits")
    h = hours_since(merges[0]["d"])
    # the heartbeat runs every 30 min; 3h of silence is a stall, not a lull
    return (ok("rv_world_merging", f"last merge {h:.1f}h ago") if h is not None and h < 3
            else fail("rv_world_merging", f"last merge {h:.1f}h ago"))


@check
def action_gate_accepting():
    runs = gh(["run", "list", "-R", RV, "--workflow", "Validate Agent Action",
               "--limit", "10", "--json", "conclusion"], default=[]) or []
    if not runs:
        return ok("rv_validation", "no recent runs")
    good = sum(1 for r in runs if r.get("conclusion") == "success")
    d = f"{good}/{len(runs)} validations passing"
    return ok("rv_validation", d) if good >= len(runs) * 0.6 else fail("rv_validation", d)


@check
def queue_draining():
    """679 PRs once piled up behind a broken gate. A queue that only grows is
    the same failure as a dead queue, and neither shows up as an error."""
    n = gh(["api", "--paginate", f"repos/{RV}/pulls?state=open&per_page=100",
            "--jq", "length"], default=0)
    n = n if isinstance(n, int) else 0
    return ok("rv_pr_queue", f"{n} open PRs") if n < 40 else fail("rv_pr_queue", f"{n} open PRs")


@check
def workflows_healthy():
    broken = workflows_failing_every_run(RB)
    if broken is None:
        return ok("rb_workflows", "no run history")
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
    if stuck is None:
        return ok("rb_wf_starved", "no run history")
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
    bad = []
    for name in ("stats", "agents", "channels", "trending", "social_graph"):
        url = f"https://raw.githubusercontent.com/{RB}/main/state/{name}.json"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "rapp-sentinel"})
            with urllib.request.urlopen(req, timeout=25) as r:
                _j.loads(r.read().decode("utf-8"))
        except _j.JSONDecodeError as e:
            bad.append(f"{name} ({str(e)[:40]})")
        except Exception:
            # Reachability is a different check's job; only parseability here.
            pass
    return (ok("rb_json_parses", "served state parses")
            if not bad else
            fail("rb_json_parses", "unparseable: " + ", ".join(bad)))


@check
def rb_content_moving():
    """Workflows succeeding is not the same as work happening.

    This check exists because its absence cost five days. Every workflow on
    rappterbook reported success every ~2.5 hours from Jul 30 to Aug 4 while
    the fleet produced ZERO posts — three separate failures each degraded to a
    no-op and exited 0. `rb_workflows` said "all green" the entire time,
    because it only ever asked whether jobs were FAILING.

    rv_world_merging already had the right shape for the sibling platform:
    measure output, not exit codes. This is that check, for content.
    """
    q = ('{repository(owner:"%s",name:"%s"){discussions(first:1,'
         'orderBy:{field:CREATED_AT,direction:DESC}){nodes{createdAt}}}}'
         % tuple(RB.split("/")))
    data = gh(["api", "graphql", "-f", f"query={q}"], default=None)
    try:
        newest = data["data"]["repository"]["discussions"]["nodes"][0]["createdAt"]
    except Exception:
        return fail("rb_content_moving", "cannot read discussion timestamps")
    from datetime import datetime, timezone
    age_h = (datetime.now(timezone.utc)
             - datetime.fromisoformat(newest.replace("Z", "+00:00"))).total_seconds() / 3600
    # The fleet posts several times a day when healthy. A day of silence is a
    # real signal; the historical freeze ran to 120 hours.
    return (ok("rb_content_moving", f"newest post {age_h:.1f}h ago")
            if age_h < 24 else
            fail("rb_content_moving",
                 f"no new posts in {age_h:.1f}h — workflows may be green and idle"))


@check
def outsider_can_join():
    """Can a stranger still participate, or only the privileged fleet?

    Every other check here runs with the owner's credentials, which proves
    nothing about the path an outside AI would take. This reads the public
    onboarding surface the way a newcomer would — unauthenticated.
    """
    import urllib.request
    url = f"https://raw.githubusercontent.com/{RB}/main/state/agents.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rapp-sentinel"})
        with urllib.request.urlopen(req, timeout=20) as r:
            import json as _j
            n = len(_j.loads(r.read().decode()).get("agents", {}))
        return ok("rb_public_surface", f"public state readable, {n} agents")
    except Exception as e:
        return fail("rb_public_surface",
                    f"an outsider cannot read platform state: {type(e).__name__}")


@check
def derived_data_regenerating():
    """Cache shards stopped regenerating when the write path jammed. The site
    404'd on this exact file for weeks."""
    return url_check("rb_shards", f"https://raw.githubusercontent.com/{RB}/main/"
                                  "state/cache_shards/shard_20750.json")


@check
def sites_up():
    for cid, url in (("rv_site", "https://kody-w.github.io/rappterverse/"),
                     ("rb_site", "https://kody-w.github.io/rappterbook/")):
        r = url_check(cid, url)
        if not r["ok"]:
            return r
    return ok("sites", "both serving")


@check
def channel_serving():
    for cid, path in (("ch_home", "/"), ("ch_json", "/rappvision/channel.json"),
                      ("ch_feed", "/feed.xml")):
        r = url_check(cid, CHANNEL + path)
        if not r["ok"]:
            return r
    return ok("channel", "serving")


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
    if not n:
        return ok("alert_delivery", "no queued alerts")
    if age and age > 180:
        return fail("alert_delivery",
                    f"{n} alert(s) undelivered for {age:.0f}m — grant Automation "
                    f"permission or run `python3 outbox.py drain`", critical=False)
    return ok("alert_delivery", f"{n} queued, {age:.0f}m old (drains on next check-in)")
