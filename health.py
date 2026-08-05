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


def _listening_pid(port):
    """The process LISTENing on `port`, or None.

    `-sTCP:LISTEN` is not optional. Without it lsof matches any socket on the
    port, including CLIENTS — on the machine this was written against it
    returned a browser that merely had a connection open, and called it the
    daemon.
    """
    try:
        r = subprocess.run(["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
                           capture_output=True, text=True, timeout=10)
        pids = [p for p in (r.stdout or "").split() if p.isdigit()]
        return pids[0] if pids else None
    except Exception:
        return None


def _launchd_attributed_pid(labels):
    """The PID launchd itself claims for one of `labels`, or None.

    ASK LAUNCHD. Do not infer supervision from PPID 1.

    The first version of this patch did exactly that, and it was wrong: a
    process whose parent has exited is reparented to launchd and is
    indistinguishable from a supervised one by PPID alone. On the machine this
    was written against, an orphaned daemon left over from a hand-start held the
    port all evening while `launchctl list com.openrappter.gateway` showed no
    PID at all and LastExitStatus 256 — the job had never run, and nothing would
    have restarted the thing serving traffic. PPID said 1 the whole time.
    """
    for label in labels:
        try:
            r = subprocess.run(["launchctl", "list", label],
                               capture_output=True, text=True, timeout=10)
            for line in (r.stdout or "").splitlines():
                if '"PID"' in line:
                    digits = "".join(ch for ch in line if ch.isdigit())
                    if digits:
                        return digits
        except Exception:
            continue
    return None


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

    # Which launchd label the openrappter daemon carries is an INSTALLATION
    # detail, not a fact about the software. This machine's job is
    # `com.openrappter.gateway`; the hardcoded string below was
    # `com.openrappter.daemon`, so the check reported "not loaded" for a daemon
    # that was running, supervised, and answering — the precise false negative
    # this loop exists to prevent, in the loop's own code, on its third audit.
    #
    # Two lessons already learned in this function, applied here:
    #   - a label is not evidence (brainstem's 14 "alive" attestations)
    #   - loaded is not running (the pid check four lines down)
    # A third follows from both: ABSENT IS NOT ABSENT. Before declaring the
    # daemon missing, look for the thing itself — a process holding the gateway
    # port whose parent is launchd is supervised, whatever the job is called.
    LABELS = ("com.openrappter.daemon", "com.openrappter.gateway")
    try:
        r = subprocess.run(["launchctl", "list"], capture_output=True,
                           text=True, timeout=10)
        pid = None
        loaded = False
        for line in (r.stdout or "").splitlines():
            if any(lbl in line for lbl in LABELS):
                first = line.split("\t")[0].strip()
                loaded = True
                if first.isdigit():
                    pid = int(first)
                    break  # a running job wins over a merely-loaded one
    except Exception:
        loaded, pid = False, None

    if loaded and pid:
        out.append(C.ok("w_openrappter", f"daemon running (pid {pid})"))
    else:
        # No positive PID from the registry — but the registry's opinion is not
        # the last word, and on this machine it is demonstrably wrong: the
        # gateway job lists a PID of `-` while a node process holding :18790
        # runs with PPID 1. Both remaining branches therefore ask the same
        # question, because the failure they distinguish is different:
        #   supervised    → healthy, launchctl simply is not attributing it
        #   unsupervised  → running, but nothing will restart it when it dies
        #   absent        → genuinely not there
        listening = _listening_pid(18790)
        attributed = _launchd_attributed_pid(LABELS)
        if listening and attributed and listening == attributed:
            out.append(C.ok("w_openrappter", f"daemon running under launchd (pid {listening})"))
        elif listening:
            out.append(C.fail("w_openrappter",
                              f"pid {listening} is serving :18790 but launchd does not claim "
                              "it — ORPHAN, nothing will restart it when it dies",
                              critical=False))
        elif loaded:
            out.append(C.fail("w_openrappter", "daemon loaded but NOT running (no pid)",
                              critical=False))
        else:
            out.append(C.fail("w_openrappter", "launchd daemon not loaded", critical=False))

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
    ran = {c.get("id") for c in results if c.get("id")} | {"w_checks_complete"}
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
            results.append(r if isinstance(r, dict) else
                           C.fail(fn.__name__, "check returned a non-result", critical=False))
        except Exception as e:
            # a check that throws is a broken check, not a broken platform
            results.append(C.fail(fn.__name__,
                                  f"check raised {type(e).__name__}: {e}", critical=False))
    results += probe_watchers()
    # Last, so it can see every id the run actually produced.
    results.append(check_completeness(results))

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
