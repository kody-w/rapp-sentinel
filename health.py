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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import checks as C

HOME = Path(__file__).resolve().parent


def _brainstem_answers_turns():
    """Liveness for a thing whose job is answering: make it answer."""
    import urllib.request, urllib.error
    body = json.dumps({"user_input": "reply with the single word: ok"}).encode()
    req = urllib.request.Request("http://localhost:7071/chat", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        if isinstance(data.get("response"), str) and data["response"].strip():
            return C.ok("w_brainstem", f'answered a turn ({data["response"].strip()[:20]})')
        return C.fail("w_brainstem", "responded but produced no answer", critical=False)
    except urllib.error.HTTPError as e:
        return C.fail("w_brainstem", f"/chat HTTP {e.code}", critical=False)
    except Exception as e:
        return C.fail("w_brainstem", f"/chat unreachable: {type(e).__name__}", critical=False)


# Which labels this daemon might carry. A hint, not a requirement: the verdict
# below is decided by which job claims the PID holding the port, so an
# installation using some other label still gets the right answer as long as it
# is listed here or the port check falls through honestly.
_OPENRAPPTER_LABELS = (
    "com.openrappter.rapptertwo",
    "com.openrappter.gateway",
    "com.openrappter.daemon",
)


def _listening_pid(port):
    """The PID LISTENING on `port`, or None.

    `lsof -ti :PORT` alone is wrong: it matches any socket on that port,
    including CLIENTS. Asked about a live gateway it has returned a browser that
    merely had the dashboard open.
    """
    try:
        r = subprocess.run(["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
                           capture_output=True, text=True, timeout=10)
        for tok in (r.stdout or "").split():
            if tok.isdigit():
                return tok
    except Exception:
        pass
    return None


def _launchd_owner(pid):
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
    """
    if not pid:
        return None
    try:
        uid = str(os.getuid())
    except Exception:
        uid = "501"
    for label in _OPENRAPPTER_LABELS:
        for target in ("system/" + label, "gui/" + uid + "/" + label):
            try:
                r = subprocess.run(["launchctl", "print", target],
                                   capture_output=True, text=True, timeout=10)
                if r.returncode != 0:
                    continue
                for line in (r.stdout or "").splitlines():
                    stripped = line.strip()
                    if stripped.startswith("pid = "):
                        claimed = stripped[6:].strip()
                        if claimed.isdigit() and claimed == str(pid):
                            return target
            except Exception:
                continue
    return None


def _openrappter_supervision():
    """Is the openrappter daemon running, and does launchd own it?

    The previous predicate searched `launchctl list` for one hardcoded label and
    called its absence "launchd daemon not loaded". On the machine this was
    reported from that is a false alarm: the daemon is running, supervised by a
    system LaunchDaemon the query cannot see, and the sentinel reported it as
    down.

    That is the defect this file already fixed for the brainstem four functions
    up — 14 "alive" attestations that had never touched /chat — repeated one
    function down. A label is not evidence. What serves the port is.
    """
    pid = _listening_pid(18790)
    if not pid:
        return C.fail("w_openrappter", "nothing is LISTENING on :18790",
                      critical=False)

    owner = _launchd_owner(pid)
    if owner:
        return C.ok("w_openrappter", f"{owner} owns pid {pid}")

    # Serving, but nothing claims it. Say that, and do not call it an orphan:
    # that claim has been made from PPID and from XPC_SERVICE_NAME and was wrong
    # both times. A watcher that guesses is worse than one that admits the gap.
    return C.ok("w_openrappter",
                f"serving :18790 (pid {pid}); no launchd job claims it — "
                "supervision UNVERIFIED, not disproved")


def probe_watchers():
    """The watchers watching the watchmen.

    A watcher that dies quietly is worse than no watcher, because everything
    downstream keeps reporting green off a record that stopped moving. So each
    peer is checked here, and the sentinel checks its own freshness too — a
    stalled loop cannot notice that it stalled, so it leaves a timestamp behind
    for the next run, and for the other two, to judge.
    """
    out = []
    # Exercise the turn endpoint, not the front page. A GET on / returns 200
    # from a brainstem that can no longer answer a single turn — which is the
    # exact "green while frozen" failure this whole loop exists to catch, and
    # it was sitting in the loop's own liveness check. The brainstem neighbor
    # found it by reading its own chain: 14 "alive" attestations, none of which
    # had ever touched /chat.
    out.append(_brainstem_answers_turns())

    out.append(_openrappter_supervision())

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
    return out


def check_completeness(results):
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
    manifest = HOME / "required_checks.json"
    if not manifest.exists():
        return C.fail("w_checks_complete", "required_checks.json is missing",
                      critical=True)
    try:
        required = set(json.loads(manifest.read_text(encoding="utf-8"))["required"])
    except Exception as e:
        return C.fail("w_checks_complete",
                      f"required_checks.json unreadable: {type(e).__name__}: {e}",
                      critical=True)

    missing = sorted(required - ran)
    if missing:
        return C.fail("w_checks_complete",
                      f"{len(missing)} required check(s) did not run: "
                      + ", ".join(missing), critical=True)
    unlisted = sorted(ran - required)
    detail = f"all {len(required)} required checks ran"
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
    for r in probe_watchers():
        r.setdefault("produced_by", "probe_watchers")
        results.append(r)
    # Last, so it can see every id the run actually produced.
    completeness = check_completeness(results)
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
