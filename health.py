#!/usr/bin/env python3
"""health.py — run every registered check, plus the watcher self-checks.

Costs nothing but API calls: no model is invoked here. The sentinel only
escalates when this reports something actually broken, which is what makes
running every 15 minutes forever affordable.

Domain checks live in checks.py — that is the file you edit. This file is the
runner and should rarely need changing.

Exit code is always 0; the verdict is the payload, not the status.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import checks as C
from paths import CODE, HOME


def _run(cmd, timeout=15):
    """stdout of `cmd`, or "" on ANY failure — the single subprocess seam.

    Every launchd interrogation below routes through here, so a prove harness
    can feed the parsers canned transcripts captured from a real machine, and
    a wedged launchctl cannot stall the tick past the timeout. "" means the
    command did not answer; parsers must treat a missing format marker as
    unreadable, never as an empty-but-healthy world (#45).
    """
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout or ""
    except Exception:
        return ""


def _brainstem_answers_turns(url="http://localhost:7071/chat", timeout=30):
    """Liveness for a thing whose job is answering: make it answer.

    The defaults ARE today's live values; probe_watchers passes the merged
    config through (issue #1 ask 2), so an install without a `watchers`
    block behaves byte-identically to the hardcoded probe this replaced.
    """
    import urllib.request, urllib.error
    body = json.dumps({"user_input": "reply with the single word: ok"}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        if isinstance(data.get("response"), str) and data["response"].strip():
            return C.ok("w_brainstem", f'answered a turn ({data["response"].strip()[:20]})')
        return C.fail("w_brainstem", "responded but produced no answer", critical=False)
    except urllib.error.HTTPError as e:
        return C.fail("w_brainstem", f"/chat HTTP {e.code}", critical=False)
    except Exception as e:
        return C.fail("w_brainstem",
                      f"/chat unreachable: {type(e).__name__}: {str(e)[:60]}",
                      critical=False)


# Which labels this daemon might carry. A hint, not a requirement: the verdict
# below is decided by which job claims the PID holding the port, so an
# installation using some other label still gets the right answer as long as it
# is listed here or the port check falls through honestly.
_OPENRAPPTER_LABELS = (
    "com.openrappter.rapptertwo",
    "com.openrappter.gateway",
    "com.openrappter.daemon",
)

# The estate-probe targets, carrying EXACTLY the literals that were hardcoded
# before issue #1 ask 2: a live config without a `watchers` key — every
# install today — merges to precisely these values, so the molt changes
# nothing. config.example.json documents the override shape.
WATCHER_DEFAULTS = {
    "brainstem": {
        "enabled": True,
        "url": "http://localhost:7071/chat",
        "timeout": 30,
    },
    "openrappter": {
        "enabled": True,
        "port": 18790,
        "labels": list(_OPENRAPPTER_LABELS),
    },
}


def _watcher_config():
    """WATCHER_DEFAULTS merged with config.json's `watchers` block.

    Tolerant on purpose (growth path): a missing config, an unparseable
    config, or a `watchers` value of the wrong shape all mean "the
    defaults" — the live organism's config predates this key and must keep
    behaving byte-identically. The merge is per-watcher and shallow
    ({**defaults, **user}), so a partial override such as
    {"brainstem": {"url": ...}} keeps every other default.

    This is the ONE read path for watcher targets. The spin scan's
    candidate labels (_openrappter_candidate_labels) come through here too,
    reading the same watchers.openrappter.labels key — never a second read
    of the same fact.
    """
    merged = {name: dict(defaults)
              for name, defaults in WATCHER_DEFAULTS.items()}
    try:
        cfg = json.loads((HOME / "config.json").read_text(encoding="utf-8"))
        user = cfg.get("watchers")
        if isinstance(user, dict):
            for name, defaults in WATCHER_DEFAULTS.items():
                override = user.get(name)
                if isinstance(override, dict):
                    merged[name] = {**defaults, **override}
    except Exception:
        pass    # a live config without the key is the default, not an error
    return merged


def _listening_pid(port):
    """The PID LISTENING on `port`, or None.

    `lsof -ti :PORT` alone is wrong: it matches any socket on that port,
    including CLIENTS. Asked about a live gateway it has returned a browser that
    merely had the dashboard open.
    """
    for tok in _run(["lsof", "-ti", f":{port}", "-sTCP:LISTEN"], timeout=10).split():
        if tok.isdigit():
            return tok
    return None


def _gui_domain():
    try:
        return f"gui/{os.getuid()}"
    except Exception:
        return "gui/501"


def _enumerate_claimants(pid):
    """Labels claiming `pid` in each launchd domain, plus per-domain readability.

    Returns (claimants, gui_readable, system_readable) where claimants is a
    list of (domain, label). Readability is judged by each listing's own
    format marker — `launchctl list`'s header row, `launchctl print system`'s
    services block — because "" from a failed command and a healthy-but-empty
    answer must never be the same value (#45).
    """
    claimants = []
    gui = _run(["launchctl", "list"])
    gui_lines = gui.splitlines()
    gui_ok = bool(gui_lines) and gui_lines[0].split()[:1] == ["PID"]
    if gui_ok:
        for line in gui_lines[1:]:
            parts = line.split()
            if len(parts) >= 3 and parts[0] == str(pid):
                claimants.append(("gui", parts[2]))
    sys_out = _run(["launchctl", "print", "system"], timeout=15)
    sys_ok = "services = {" in sys_out
    if sys_ok:
        in_services = False
        for line in sys_out.splitlines():
            s = line.strip()
            if s.startswith("services = {"):
                in_services = True
                continue
            if in_services:
                if s == "}":
                    break
                parts = s.split()
                if len(parts) >= 3 and parts[0] == str(pid):
                    claimants.append(("system", parts[-1]))
    return claimants, gui_ok, sys_ok


def _confirm_owner(target, pid):
    """POSITIVE confirmation (R3): the targeted job is running AND holds `pid`.

    A label appearing in a listing beside the right pid is a candidate, not a
    verdict — `launchctl print <target>` is the claim from launchd's own
    mouth, and both `state = running` and the pid must match.
    """
    txt = _run(["launchctl", "print", target])
    if not txt:
        return False
    lines = [l.strip() for l in txt.splitlines()]
    return "state = running" in lines and f"pid = {pid}" in lines


def _launchd_owner(pid, labels=None):
    """Which launchd job, in ANY domain, claims `pid` — or None.

    ASK LAUNCHD, and ask BOTH DOMAINS. `launchctl list` enumerates the USER
    domain only, so it cannot see a system LaunchDaemon. Measured on the machine
    this was reported from:

        launchctl list com.openrappter.daemon      -> NOT FOUND
        launchctl list com.openrappter.gateway     -> found, and NOT running
        launchctl list com.openrappter.rapptertwo  -> NOT FOUND

        launchctl print system/com.openrappter.rapptertwo
            path  = /Library/LaunchDaemons/com.openrappter.rapptertwo.plist
            state = running        pid = 70231
        lsof -ti :18790 -sTCP:LISTEN  ->  70231

    Do not infer supervision from PPID 1 — an orphan is reparented to launchd
    and is indistinguishable that way — nor from XPC_SERVICE_NAME, which is
    inherited from whatever spawned the process rather than proof of the job
    that did.

    ENUMERATE, do not guess labels. The previous version probed three
    hardcoded labels, so a job under any fourth label was invisible and the
    check could reach a defensible verdict by a route that could never name
    the supervisor — which is the thing a sentinel is for (#23). Both domain
    listings are walked for the pid; every candidate is then positively
    confirmed with a targeted print (state = running AND pid match). The
    hardcoded labels remain only as fallback targeted probes against listing
    format drift.

    Three-state on purpose: (owner, "supervised") when a confirmed job claims
    the pid; (None, "unsupervised") only when BOTH enumerations were readable
    and no job claims it — positively determined absence; (None, "unverified")
    when an enumeration could not be read — we could not look, which is not
    the same claim (#45), and it must never be stored indistinguishably from
    either truth.
    """
    if not pid:
        return None, "unverified"
    claimants, gui_ok, sys_ok = _enumerate_claimants(pid)
    for domain, label in claimants:
        target = ("system/" + label if domain == "system"
                  else _gui_domain() + "/" + label)
        if _confirm_owner(target, pid):
            return target, "supervised"
    for label in (labels or _OPENRAPPTER_LABELS):
        for target in ("system/" + label, _gui_domain() + "/" + label):
            if _confirm_owner(target, pid):
                return target, "supervised"
    if gui_ok and sys_ok:
        return None, "unsupervised"
    return None, "unverified"


def _openrappter_supervision(port=18790, labels=None):
    """Is the openrappter daemon running, and does launchd own it?

    Defaults are today's live values; probe_watchers passes the merged
    config through (issue #1 ask 2). `labels` feeds only the targeted
    fallback probes in _launchd_owner — enumeration of both domains stays
    the primary route, so an unlisted label is still found.

    The previous predicate searched `launchctl list` for one hardcoded label and
    called its absence "launchd daemon not loaded". On the machine this was
    reported from that is a false alarm: the daemon is running, supervised by a
    system LaunchDaemon the query cannot see, and the sentinel reported it as
    down.

    That is the defect this file already fixed for the brainstem four functions
    up — 14 "alive" attestations that had never touched /chat — repeated one
    function down. A label is not evidence. What serves the port is.
    """
    pid = _listening_pid(port)
    if not pid:
        return C.fail("w_openrappter", f"nothing is LISTENING on :{port}",
                      critical=False)

    owner, verdict = _launchd_owner(pid, labels=labels)
    # The additive `supervision` field is the three-state answer a consumer
    # can branch on. "unverified" was previously storable as indistinguishable
    # from true; now the word travels with the verdict (#23).
    if owner:
        r = C.ok("w_openrappter", f"{owner} owns pid {pid}")
        r["supervision"] = "supervised-by:" + owner
        return r
    if verdict == "unsupervised":
        # Both domains enumerated, no claimant: positively determined, and
        # still ok — a serving unsupervised daemon is a finding for a human,
        # not an outage (the mutation ledger records serving-but-unclaimed-ok
        # as deliberate; the false alarm this check replaced paged on it).
        r = C.ok("w_openrappter",
                 f"serving :{port} (pid {pid}); both launchd domains "
                 "enumerated, no job claims it — unsupervised")
        r["supervision"] = "unsupervised"
        return r
    r = C.ok("w_openrappter",
             f"serving :{port} (pid {pid}); a launchd domain could not be "
             "read — supervision UNVERIFIED, not disproved")
    r["supervision"] = "unverified"
    return r


def _openrappter_candidate_labels():
    """Labels worth auditing for the spin scan: the known set, the merged
    config's watchers.openrappter.labels (the SAME key probe_watchers'
    targets come from, read through the SAME _watcher_config() path —
    never a second read of the same fact), and any openrappter-substring
    label visible in either domain listing (discovery scoping; each
    discovered job is then individually measured, never judged from the
    listing row)."""
    labels = set(_OPENRAPPTER_LABELS)
    labels.update(
        x for x in (_watcher_config()["openrappter"].get("labels") or [])
        if isinstance(x, str))
    listings_readable = False
    gui = _run(["launchctl", "list"])
    gui_lines = gui.splitlines()
    if gui_lines and gui_lines[0].split()[:1] == ["PID"]:
        listings_readable = True
        for line in gui_lines[1:]:
            parts = line.split()
            if len(parts) >= 3 and "openrappter" in parts[2]:
                labels.add(parts[2])
    sys_out = _run(["launchctl", "print", "system"], timeout=15)
    if "services = {" in sys_out:
        listings_readable = True
        for line in sys_out.splitlines():
            parts = line.strip().split()
            if len(parts) >= 3 and "openrappter" in parts[-1]:
                labels.add(parts[-1])
    return labels, listings_readable


def _job_stats(target):
    """state / runs / last exit of one targeted job, or None if not loaded."""
    txt = _run(["launchctl", "print", target])
    if not txt:
        return None
    stats = {"state": None, "runs": 0, "last_exit": None}
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("state = ") and stats["state"] is None:
            stats["state"] = s[len("state = "):]
        elif s.startswith("runs = "):
            try:
                stats["runs"] = int(s[len("runs = "):])
            except ValueError:
                pass
        elif s.startswith("last exit code = "):
            val = s[len("last exit code = "):]
            try:
                stats["last_exit"] = int(val)
            except ValueError:
                stats["last_exit"] = None      # "(never exited)"
    return stats


def _spinning_openrappter_jobs():
    """A loaded job that starts, exits nonzero, and repeats is a spinning
    wheel, not a daemon — and every naive predicate calls it healthy (#23).

    Found live: com.openrappter.gateway loaded with runs = 27, last exit 1,
    state = not running, losing a runtime lock another process held, while
    the actual gateway served the port under a label no query looked at.
    Nothing reported it, because "loaded" was the whole test.

    Warn deliberately: a stale duplicate does not stop the served gateway,
    and waking the repair arm to unload someone's launchd job is
    disproportionate. Escape hatch if this flaps: require the same verdict
    on two consecutive ticks — deliberately deferred until observed.
    """
    labels, readable = _openrappter_candidate_labels()
    if not readable:
        return C.fail("w_openrappter_spin",
                      "cannot audit launchd jobs: neither domain listing "
                      "was readable", critical=False)
    spinning, audited = [], 0
    for label in sorted(labels):
        for target in (_gui_domain() + "/" + label, "system/" + label):
            stats = _job_stats(target)
            if stats is None:
                continue
            audited += 1
            if stats["state"] == "running":
                break
            if (stats["runs"] >= 3 and isinstance(stats["last_exit"], int)
                    and stats["last_exit"] != 0):
                spinning.append(f"{label} (runs={stats['runs']}, "
                                f"last exit {stats['last_exit']})")
            break          # one domain answered for this label; do not double-count
    if spinning:
        return C.fail("w_openrappter_spin",
                      "spinning launchd job(s): " + "; ".join(spinning),
                      critical=False)
    if audited == 0:
        return C.ok("w_openrappter_spin", "no openrappter launchd jobs loaded")
    return C.ok("w_openrappter_spin",
                f"{audited} job(s) audited, none spinning")


def _git(*args, timeout=25):
    """(returncode, stdout) for a git command against the CODE checkout.

    Separate from _sh because classification here turns on EXIT STATUS —
    `merge-base --is-ancestor` says everything in its return code and nothing
    on stdout. A raise (git absent, wedged, slow remote) becomes (None, "")
    so the caller reports blind rather than inventing a verdict (#45).
    """
    try:
        r = subprocess.run(("git", "-C", str(CODE)) + args,
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip()
    except Exception:
        return None, ""


def _deployed_code_is_current():
    """Is the code that is RUNNING the code that was merged? (R2 turned on
    the watcher itself.)

    Measured incident, 2026-08-17: rb_shards paged critical with a false red.
    The repair was diagnosed correctly, merged to origin/main as #99, and
    verified there — and the page kept firing for four more hours, because
    run.sh is `cd $(dirname $0); python3 sentinel.py` and pulls nothing. The
    live checkout sat eight commits behind origin/main, so the running process
    executed the pre-fix file. A second repair attempt then re-derived the
    same correct diagnosis against an instance that could never show it,
    which is the loop this check exists to break: a fix that lands on main
    but never reaches the process is indistinguishable, from inside, from a
    fix that did not work.

    health.py already names this deploy model out loud — "code arrives by git
    pull between ticks" — and nothing anywhere asserted that it had. Every
    other w_ check watches a peer; this one watches the delivery of our own
    repairs, which is the one outage the rest of the suite structurally
    cannot see, because a stale instance runs stale checks.

    Read-only about the working tree BY CONSTRUCTION: ls-remote and rev-parse
    touch no ref, no index and no file, so a checkout full of uncommitted work
    is never at risk from being watched. That is also why this only reports —
    pulling is a human's call, and a repair arm that silently upgraded the
    code it is running would be deploying unreviewed changes to the watcher
    mid-tick.

    Warn, not critical, and only for BEHIND. Ahead or diverged is a person
    mid-development, not a stranded repair, and paging on it every tick on
    the maintainer's own box is how a channel gets ignored (the eco_sweep
    reasoning). Unknowable states report warn rather than green, because
    "I could not tell whether my repairs are arriving" is not health.
    """
    rc, running = _git("rev-parse", "HEAD")
    if rc != 0 or not running:
        return C.fail("w_sentinel_current",
                      "cannot read running commit - is CODE a git checkout? "
                      "unable to tell whether merged repairs have arrived",
                      critical=False)
    rc, out = _git("ls-remote", "origin", "refs/heads/main")
    published = out.split()[0] if rc == 0 and out.split() else ""
    if not published:
        return C.fail("w_sentinel_current",
                      f"cannot read origin/main (running {running[:7]}) - "
                      "unable to tell whether merged repairs have arrived",
                      critical=False)
    if running == published:
        return C.ok("w_sentinel_current",
                    f"running merged origin/main {running[:7]}")
    # Objects for a commit merged on the server are not local until something
    # fetches them. Fetch into FETCH_HEAD only: no branch, no remote-tracking
    # ref, no index and no working-tree byte moves, so this cannot disturb
    # uncommitted work.
    _git("fetch", "--quiet", "origin", "main", timeout=60)
    rc, _ = _git("cat-file", "-e", published + "^{commit}")
    if rc != 0:
        return C.fail("w_sentinel_current",
                      f"running {running[:7]}, origin/main is {published[:7]} "
                      "- differs, and the published commit could not be "
                      "fetched to classify it", critical=False)
    rc, _ = _git("merge-base", "--is-ancestor", running, published)
    if rc != 0:
        return C.ok("w_sentinel_current",
                    f"running {running[:7]} is not behind origin/main "
                    f"{published[:7]} (ahead or diverged - local work, "
                    "not a stranded repair)")
    rc, behind = _git("rev-list", "--count", f"{running}..{published}")
    n = behind if rc == 0 and behind else "?"
    return C.fail("w_sentinel_current",
                  f"running {running[:7]}, {n} commit(s) behind origin/main "
                  f"{published[:7]} - merged repairs have not reached this "
                  "instance; deploy with: git -C "
                  f"{CODE} pull --ff-only origin main", critical=False)


def probe_watchers():
    """The watchers watching the watchmen.

    A watcher that dies quietly is worse than no watcher, because everything
    downstream keeps reporting green off a record that stopped moving. So each
    peer is checked here, and the sentinel checks its own freshness too — a
    stalled loop cannot notice that it stalled, so it leaves a timestamp behind
    for the next run, and for the other two, to judge.

    Returns (results, disabled_ids). Estate-probe targets come from
    config.json's `watchers` block via _watcher_config() (issue #1 ask 2);
    an absent block merges to exactly the values that used to be hardcoded
    here, so the live organism molts with zero behavior change. A watcher
    with enabled:false is not probed — and is not silently green either:
    its id rides back in disabled_ids so check_completeness can declare the
    omission in every verdict instead of it looking like a deleted check.
    """
    out, disabled = [], []
    cfg = _watcher_config()

    # Exercise the turn endpoint, not the front page. A GET on / returns 200
    # from a brainstem that can no longer answer a single turn — which is the
    # exact "green while frozen" failure this whole loop exists to catch, and
    # it was sitting in the loop's own liveness check. The brainstem neighbor
    # found it by reading its own chain: 14 "alive" attestations, none of which
    # had ever touched /chat.
    bs = cfg["brainstem"]
    if bs.get("enabled"):
        out.append(_brainstem_answers_turns(url=bs["url"],
                                            timeout=bs["timeout"]))
    else:
        disabled.append("w_brainstem")

    orp = cfg["openrappter"]
    if orp.get("enabled"):
        out.append(_openrappter_supervision(port=orp["port"],
                                            labels=orp.get("labels")))
    else:
        disabled.append("w_openrappter")

    # Deliberately independent of the port probe's enabled flag: a spinning
    # launchd job is a defect worth seeing even on a machine where nothing
    # should be serving the port. Disabling w_openrappter does NOT disable
    # this — they are separate declarations.
    out.append(_spinning_openrappter_jobs())

    # The in-tree anchor file cannot testify about its own truncation. Compare
    # it to the high-water mark kept outside the repository.
    try:
        import neighborhood as NB
        led = NB.check_external_ledger()
        if led["status"] == "disputed":
            out.append(C.fail("w_anchor_ledger", led["detail"], critical=True))
        elif led["status"] == "unreadable":
            out.append(C.fail("w_anchor_ledger",
                              f"external ledger unreadable: {led['detail']}"))
        else:
            out.append(C.ok("w_anchor_ledger", led["detail"]))
    except Exception as e:
        out.append(C.fail("w_anchor_ledger",
                          f"ledger check raised {type(e).__name__}: {e}"))

    beat = HOME / "state" / "last_run.json"
    age_m = None
    if beat.exists():
        try:
            prev = json.loads(beat.read_text(encoding="utf-8"))
            age_m = (datetime.now(timezone.utc) - datetime.fromisoformat(
                prev["at"].replace("Z", "+00:00"))).total_seconds() / 60
        except Exception:
            age_m = None
    out.append(C.ok("w_sentinel_fresh", "first run" if age_m is None
                    else f"last tick {age_m:.0f}m ago")
               if age_m is None or age_m < 90
               else C.fail("w_sentinel_fresh", f"last tick {age_m:.0f}m ago", critical=False))

    # A fresh tick of stale code still reports stale answers: last_run.json
    # moving proves the loop is alive, never that it is the loop we merged.
    out.append(_deployed_code_is_current())
    return out, disabled


def check_outsider_coverage(results):
    """Issue #5 ask 1, enforced per tick: an outsider view is a DISCIPLINE,
    not one lucky check.

    Every platform named in the manifest's `outsider_platforms` must have at
    least one result this tick that RAN from the outsider vantage —
    vantage == "outsider", a tag checks.outsider_check stamps only after
    proving no gh() call flowed through the execution. A check that ran and
    FAILED still covers its platform: the discipline is that someone stood
    outside and tried the door; the door being stuck is that check's own
    report, not a coverage gap. A result whose vantage was replaced with
    "credentialed" (it touched gh()) does NOT count — a violation is
    precisely the absence of the outsider vantage.

    Warn on gaps, not critical: the repair arm cannot write an
    unauthenticated check; a human can. And a manifest without the key —
    mid-deploy on the live organism, where code arrives by git pull between
    ticks and may run one tick ahead of its manifest — is "coverage
    unknown" at warn, never a raise and never green (#45).
    """
    # CODE, not HOME: the manifest is a property of checks.py and lives with
    # the code — same reasoning as the two manifest reads below (#1).
    manifest = CODE / "required_checks.json"
    try:
        doc = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as e:
        return C.fail("w_outsider_coverage",
                      f"required_checks.json unreadable: {type(e).__name__} "
                      "- coverage unknown", critical=False)
    platforms = doc.get("outsider_platforms")
    if not isinstance(platforms, list) or not platforms:
        return C.fail("w_outsider_coverage",
                      "outsider_platforms missing - coverage unknown",
                      critical=False)
    covered = {r.get("platform") for r in results
               if isinstance(r, dict) and r.get("vantage") == "outsider"}
    uncovered = sorted(str(p) for p in platforms if p not in covered)
    if uncovered:
        return C.fail("w_outsider_coverage",
                      "no unauthenticated check ran for: "
                      + ", ".join(uncovered), critical=False)
    return C.ok("w_outsider_coverage",
                f"{len(platforms)} platform(s) each watched from the "
                "outsider vantage this tick")


def check_freshness_pairing():
    """R2 enforced mechanically: a domain with run-status checks but no
    output-freshness check is reported every tick, not rediscovered per
    outage.

    The five-day rappterbook silence (#3) happened because run-status
    coverage existed and output coverage did not, and nothing made that gap
    visible at authoring time. The tick IS the authoring feedback loop here,
    so the manifest now classifies every required id (the `kinds` map in
    required_checks.json) and this check compares coverage per domain.

    An accepted gap must be a DECISION with a diff, not a silence: the
    `unpaired_accepted` map in the manifest names each domain deliberately
    left unpaired and why. It exists because a permanently-red check would
    hold the verdict at degraded forever — which silently disables the
    level-3 evolve arm (sentinel only evolves while healthy) — and because a
    red nobody can act on trains people to scroll past the channel. The
    finding stays recorded (CHECK-AUDIT.md, eco_sweep row); any FUTURE
    domain that gains run-status coverage without freshness coverage fires
    here at warn (the repair arm cannot write a freshness check; a human
    can).
    """
    # CODE, not HOME: the required set and its kinds map are a property of
    # checks.py, so they live with the code — same reasoning as
    # check_completeness below (SENTINEL_HOME split, #1).
    manifest = CODE / "required_checks.json"
    try:
        doc = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as e:
        return C.fail("w_freshness_paired",
                      f"required_checks.json unreadable: {type(e).__name__} - "
                      "pairing unknown", critical=False)
    kinds = doc.get("kinds")
    if not isinstance(kinds, dict) or not kinds:
        return C.fail("w_freshness_paired",
                      "kinds map missing from required_checks.json - "
                      "pairing unknown", critical=False)
    accepted = doc.get("unpaired_accepted") or {}
    domains = {}
    for cid, meta in kinds.items():
        if not isinstance(meta, dict):
            continue
        domains.setdefault(meta.get("domain"), set()).add(meta.get("kind"))
    unpaired = sorted(
        d for d, ks in domains.items()
        if d and "run-status" in ks and "output-freshness" not in ks
        and d not in accepted)
    required = set(doc.get("required") or [])
    runner_ids = {"w_checks_complete", "w_freshness_paired"}
    unclassified = sorted(required - set(kinds) - runner_ids)
    suffix = ""
    if unclassified:
        suffix = f"; {len(unclassified)} unclassified: " + ", ".join(unclassified)
    if unpaired:
        return C.fail("w_freshness_paired",
                      "run-status without output-freshness (R2): "
                      + ", ".join(unpaired) + suffix, critical=False)
    note = f"{len(domains)} domain(s) classified, all paired or accepted"
    if accepted:
        note += f" ({len(accepted)} accepted-unpaired: " + \
                ", ".join(sorted(accepted)) + ")"
    return C.ok("w_freshness_paired", note + suffix)


def check_completeness(results, disabled=()):
    """The registry enumerates; this REQUIRES.

    all_checks() returns whatever decorators happened to run. That makes a
    check impossible to notice the absence of: delete one @check line and the
    verdict still reads "healthy - all checks passing", because a check that
    never ran cannot report that it did not run.

    This is not hypothetical. Removing the decorator from
    rb_workflows_never_succeed silently dropped `rb_wf_starved` - the check
    written specifically because its absence had already cost five days - and
    nothing in the output changed except a count nobody compares.

    So the expected ids are committed to required_checks.json and compared
    here. Extra ids are reported, never failed: a new check should not be able
    to break the loop before someone lists it.

    Honest limit: this check cannot detect its own removal. That is what an
    external watcher is for, and rapp-overwatch does exactly this comparison
    from outside.

    `disabled` (default () — existing callers unaffected) carries ids whose
    probes config.json switched off (issue #1 ask 2). Only ids BOTH in the
    manifest and explicitly disabled are subtracted, and every subtraction
    is appended to the passing detail — the omission is a standing
    declaration in every verdict, never a silent shrinking of the required
    set. A disabled id the manifest does not know is reported as a probable
    config typo rather than quietly ignored.
    """
    # Include our own id: this check is running, so it ran. Computing `ran`
    # purely from results-so-far made the completeness check report ITSELF
    # missing the moment it was listed as required - the exact blind spot it
    # was written to close, reproduced inside the fix for it.
    # A duplicate id would let a check stop running while this stayed green: the
    # verdict would carry two entries, `ran` is a set, and the id would still be
    # present -- supplied by the other function. There are none today, which is
    # exactly when it is cheap to make impossible.
    seen = {}
    for c in results:
        cid = c.get("id")
        if not cid:
            continue
        who = c.get("produced_by", "?")
        if cid in seen and seen[cid] != who:
            return C.fail("w_checks_complete",
                          f"duplicate check id {cid!r} emitted by {seen[cid]} and {who}",
                          critical=True)
        seen[cid] = who
    ran = set(seen) | {"w_checks_complete"}
    # CODE, not HOME: the required set is a property of checks.py — every
    # instance running this code owes the same checks, so the manifest rides
    # with the code, not with an instance's state directory.
    manifest = CODE / "required_checks.json"
    if not manifest.exists():
        return C.fail("w_checks_complete", "required_checks.json is missing",
                      critical=True)
    try:
        required = set(json.loads(manifest.read_text(encoding="utf-8"))["required"])
    except Exception as e:
        return C.fail("w_checks_complete",
                      f"required_checks.json unreadable: {type(e).__name__}: {e}",
                      critical=True)

    honored = sorted(set(disabled) & required)
    typos = sorted(set(disabled) - required)
    suffix = ""
    if honored:
        suffix += (f"; {len(honored)} disabled by config: "
                   + ", ".join(honored))
    if typos:
        suffix += ("; disabled id(s) not in the manifest (config typo?): "
                   + ", ".join(typos))
    missing = sorted(required - set(honored) - ran)
    if missing:
        return C.fail("w_checks_complete",
                      f"{len(missing)} required check(s) did not run: "
                      + ", ".join(missing) + suffix, critical=True)
    unlisted = sorted(ran - required)
    detail = f"all {len(required) - len(honored)} required checks ran" + suffix
    if unlisted:
        detail += f"; {len(unlisted)} unlisted: {', '.join(unlisted)}"
    return C.ok("w_checks_complete", detail)


def main():
    results = []
    for fn in C.all_checks():
        try:
            r = fn()
            r = r if isinstance(r, dict) else C.fail(
                fn.__name__, "check returned a non-result", critical=False)
        except Exception as e:
            # a check that throws is a broken check, not a broken platform
            r = C.fail(fn.__name__, f"check raised {type(e).__name__}: {e}", critical=False)
        # Nine of twelve ids do not match their function name -- rb_wf_starved
        # comes from rb_workflows_never_succeed, rv_pr_queue from queue_draining.
        # That is why the missing-check defect in #15 was invisible: nothing in
        # the output connected the two names. The runner already holds fn, so it
        # can say so without checks.py knowing anything about it.
        r.setdefault("produced_by", fn.__name__)
        results.append(r)
    # The watcher probes are built inline rather than through the registry, so
    # they carry no function of their own. Naming their producer anyway keeps
    # every line in the verdict traceable, and gives the duplicate-id guard
    # below something real to compare.
    watcher_results, disabled_by_config = probe_watchers()
    for r in watcher_results:
        r.setdefault("produced_by", "probe_watchers")
        results.append(r)
    # After every vantage-tagged result exists, before completeness — so the
    # coverage check is itself covered by the required-ids comparison.
    coverage = check_outsider_coverage(results)
    coverage.setdefault("produced_by", "check_outsider_coverage")
    results.append(coverage)
    # Before completeness, so the pairing check is itself covered by the
    # required-ids comparison.
    pairing = check_freshness_pairing()
    pairing.setdefault("produced_by", "check_freshness_pairing")
    results.append(pairing)
    # Last, so it can see every id the run actually produced — and the ids
    # config.json deliberately switched off, so a disabled probe is a
    # declaration in the verdict rather than a phantom deletion.
    completeness = check_completeness(results, disabled=disabled_by_config)
    completeness.setdefault("produced_by", "check_completeness")
    results.append(completeness)

    failed = [c for c in results if not c["ok"]]
    crit = [c for c in failed if c["severity"] == C.CRITICAL]

    print(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "critical" if crit else ("degraded" if failed else "healthy"),
        "checks": results,
        "failed": [c["id"] for c in failed],
        "critical": [c["id"] for c in crit],
        "summary": "; ".join(f"{c['id']}: {c['detail']}" for c in failed)
                   or "all checks passing",
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
