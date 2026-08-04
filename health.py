#!/usr/bin/env python3
"""health.py — probe the RAPP platforms and emit a verdict as JSON.

Costs nothing but API calls: no model is invoked here. The sentinel only
escalates to Copilot when this reports something actually broken, which is
what makes running every 15 minutes forever affordable.

Exit code is always 0 — the verdict is the payload, not the status.
Python 3 stdlib only.
"""

import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(__file__).resolve().parent

TIMEOUT = 25

# A check is (id, ok, severity, detail). Severity decides whether the sentinel
# escalates: "critical" always does, "warn" only reports.
CRITICAL, WARN = "critical", "warn"


def gh_json(args, default=None):
    try:
        r = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=TIMEOUT)
        if r.returncode != 0:
            return default
        return json.loads(r.stdout) if r.stdout.strip() else default
    except Exception:
        return default


def http_status(url):
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "rapp-sentinel"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def check(cid, ok, severity, detail):
    return {"id": cid, "ok": bool(ok), "severity": severity, "detail": detail}


def broken_workflows(repo, limit=30):
    runs = gh_json(["run", "list", "-R", repo, "--limit", str(limit),
                    "--json", "name,conclusion"], default=None)
    if not runs:
        return None
    per = {}
    for r in runs:
        per.setdefault(r["name"], {"fail": 0, "total": 0})
        per[r["name"]]["total"] += 1
        if r.get("conclusion") == "failure":
            per[r["name"]]["fail"] += 1
    # "broken" = failed every run, and ran more than once (so a single
    # flake never wakes the sentinel)
    return {n: v for n, v in per.items() if v["total"] >= 2 and v["fail"] == v["total"]}


def probe_rappterverse():
    out = []
    repo = "kody-w/rappterverse"

    # 1. Is the world still merging? That is the whole point of the platform,
    #    and it is exactly what silently stopped for 19 days.
    commits = gh_json(["api", f"repos/{repo}/commits?per_page=20", "--jq",
                       "[.[] | {msg: .commit.message, date: .commit.committer.date}]"],
                      default=[]) or []
    merges = [c for c in commits if c["msg"].startswith("[state] apply PR")]
    stalled_h = None
    if merges:
        dt = datetime.fromisoformat(merges[0]["date"].replace("Z", "+00:00"))
        stalled_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    out.append(check(
        "rv_world_merging",
        stalled_h is not None and stalled_h < 3,   # heartbeat is every 30 min
        CRITICAL,
        f"last state merge {stalled_h:.1f}h ago" if stalled_h is not None
        else "no '[state] apply PR' commit in the last 20",
    ))

    # 2. Is the action gate accepting work?
    runs = gh_json(["run", "list", "-R", repo, "--workflow", "Validate Agent Action",
                    "--limit", "10", "--json", "conclusion"], default=[]) or []
    ok = sum(1 for r in runs if r.get("conclusion") == "success")
    out.append(check("rv_validation", (not runs) or ok >= len(runs) * 0.6, CRITICAL,
                     f"{ok}/{len(runs)} recent validations succeeded"))

    # 3. Is the queue draining, or backing up the way it did behind the gate?
    n = gh_json(["api", "--paginate", f"repos/{repo}/pulls?state=open&per_page=100",
                 "--jq", "length"], default=0)
    n = n if isinstance(n, int) else 0
    out.append(check("rv_pr_queue", n < 40, CRITICAL, f"{n} open PRs"))

    out.append(check("rv_site",
                     http_status("https://kody-w.github.io/rappterverse/") == 200,
                     WARN, "GitHub Pages"))
    return out


def probe_rappterbook():
    out = []
    repo = "kody-w/rappterbook"
    broken = broken_workflows(repo)
    if broken is None:
        out.append(check("rb_workflows", True, WARN, "no run history available"))
    else:
        out.append(check("rb_workflows", not broken, CRITICAL,
                         "all green" if not broken
                         else "100% failing: " + ", ".join(sorted(broken))))

    out.append(check("rb_site",
                     http_status("https://kody-w.github.io/rappterbook/") == 200,
                     WARN, "GitHub Pages"))

    # The shard the live site 404'd on while the write path was jammed. If this
    # disappears again, derived data has stopped regenerating.
    out.append(check(
        "rb_shards",
        http_status("https://raw.githubusercontent.com/kody-w/rappterbook/main/"
                    "state/cache_shards/shard_20750.json") == 200,
        WARN, "cache shard reachable"))
    return out


def probe_channel():
    base = "https://kody-w.github.io/rappvision-field-notes"
    urls = {
        "ch_home": f"{base}/",
        "ch_json": f"{base}/rappvision/channel.json",
        "ch_share": f"{base}/v/platform-relaunch-recap.html",
        "ch_feed": f"{base}/feed.xml",
    }
    return [check(k, http_status(u) == 200, WARN, u) for k, u in urls.items()]


def probe_watchers():
    """The watchers watching the watchmen.

    Three things are supposed to keep this ecosystem honest: the openrappter
    daemon, the brainstem, and Copilot driving repairs. A watcher that dies
    quietly is worse than no watcher, because everything downstream keeps
    reporting green off stale data. So each one is checked here, and the
    sentinel checks its own freshness too — a sentinel that stopped running
    cannot notice that it stopped running, so it leaves a timestamp behind
    for the next run (and for the brainstem) to judge.
    """
    out = []

    # 1. brainstem — local Flask service
    out.append(check("w_brainstem", http_status("http://localhost:7071/") == 200,
                     WARN, "brainstem :7071"))

    # 2. openrappter daemon — registered with launchd and actually loaded
    try:
        r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10)
        loaded = "com.openrappter.daemon" in (r.stdout or "")
    except Exception:
        loaded = False
    out.append(check("w_openrappter", loaded, WARN, "launchd com.openrappter.daemon"))

    # 3. the sentinel's own last run — staleness means the loop itself stalled
    beat = HOME / "state" / "last_run.json"
    age_m = None
    if beat.exists():
        try:
            prev = json.loads(beat.read_text(encoding="utf-8"))
            dt = datetime.fromisoformat(prev["at"].replace("Z", "+00:00"))
            age_m = (datetime.now(timezone.utc) - dt).total_seconds() / 60
        except Exception:
            age_m = None
    # first ever run has no previous beat; that is fine, not a failure
    out.append(check("w_sentinel_fresh", age_m is None or age_m < 90, WARN,
                     "first run" if age_m is None else f"last sentinel run {age_m:.0f}m ago"))
    return out


def main():
    checks = (probe_rappterverse() + probe_rappterbook()
              + probe_channel() + probe_watchers())
    failed = [c for c in checks if not c["ok"]]
    crit = [c for c in failed if c["severity"] == CRITICAL]

    print(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "critical" if crit else ("degraded" if failed else "healthy"),
        "checks": checks,
        "failed": [c["id"] for c in failed],
        "critical": [c["id"] for c in crit],
        "summary": "; ".join(f"{c['id']}: {c['detail']}" for c in failed)
                   or "all checks passing",
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
