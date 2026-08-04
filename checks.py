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


# ══════════════════════════════════════════════════════════════════════════
# ── your checks ── everything below here is an example. Replace it.
# ══════════════════════════════════════════════════════════════════════════

RV = "kody-w/rappterverse"
RB = "kody-w/rappterbook"
CHANNEL = "https://kody-w.github.io/rappvision-field-notes"


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
