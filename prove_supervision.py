#!/usr/bin/env python3
"""prove_supervision.py — the supervisor is NAMED by enumeration, and
"unverified" can no longer be stored indistinguishably from either truth.

#23's tail: the previous _launchd_owner probed three hardcoded labels, so a
job under any fourth label was invisible — the check reached a defensible
verdict ("serving, so healthy") by a route that could never name the
supervisor, which is the thing a sentinel is for. The rewrite enumerates
BOTH launchd domains for the pid that owns the LISTEN socket, positively
confirms every candidate with a targeted print (state = running AND pid
match), and answers three-state: supervised-by:<target> / unsupervised
(both listings readable, no claimant — positively determined) / unverified
(a listing unreadable — we could not look).

Every transcript below was captured from a real machine (this one, and the
machine #23 was reported from), so the parsers are proven against real
bytes, not invented formats. health._run is the single subprocess seam.

Run: python3 prove_supervision.py   (exit 0 only on all-behaved)
"""

import sys

import health as H

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


GUI_HEADER = "PID\tStatus\tLabel"

# Captured 2026-08-16 from this machine (launchctl list, gui domain).
GUI_LIST_DAEMON = "\n".join([
    GUI_HEADER,
    "-\t0\tcom.apple.SafariHistoryServiceAgent",
    "-\t0\tcom.openrappter.morning-decisions",
    "5556\t0\tcom.openrappter.daemon",
])

# Captured shape of `launchctl print system` (services block).
SYS_PRINT_RAPPTERTWO = """system information:
	services = {
		       0      - 	com.apple.rpmuxd
		   70231      - 	com.openrappter.rapptertwo
		     610      - 	com.apple.runningboardd
	}
	attractive services = {
	}
"""

SYS_PRINT_EMPTYISH = """system information:
	services = {
		       0      - 	com.apple.rpmuxd
	}
"""

PRINT_RUNNING = lambda pid: f"""com.openrappter.x = {{
	path = /Library/LaunchDaemons/x.plist
	state = running
	runs = 1
	pid = {pid}
	last exit code = (never exited)
}}
"""


def make_run(table):
    """A health._run double: route each command to a canned transcript."""
    def _run(cmd, timeout=15):
        if cmd[0] == "lsof":
            return table.get("lsof", "")
        if cmd[:2] == ["launchctl", "list"]:
            return table.get("list", "")
        if cmd[:2] == ["launchctl", "print"]:
            if cmd[2] == "system":
                return table.get("system", "")
            return table.get(("print", cmd[2]), "")
        raise AssertionError(f"unexpected command: {cmd}")
    return _run


real_run = H._run
GUI = H._gui_domain()

# (a) The #23 machine: gui listings cannot see the system LaunchDaemon; the
# system services block names the label whose pid owns the socket.
H._run = make_run({
    "lsof": "70231\n",
    "list": GUI_HEADER + "\n-\t0\tcom.something.else",
    "system": SYS_PRINT_RAPPTERTWO,
    ("print", "system/com.openrappter.rapptertwo"): PRINT_RUNNING(70231),
})
r = H._openrappter_supervision()
scenario("(a) system LaunchDaemon named: supervised-by:system/... on the #23 shape",
         r["ok"] and r.get("supervision") == "supervised-by:system/com.openrappter.rapptertwo"
         and "owns pid 70231" in r["detail"],
         f"supervision={r.get('supervision')} {r['detail']}")

# (b) Both listings readable, nothing claims the pid, targeted fallbacks
# answer nothing: positively-determined unsupervised — still ok, but said.
H._run = make_run({
    "lsof": "4242\n",
    "list": GUI_LIST_DAEMON,
    "system": SYS_PRINT_EMPTYISH,
})
r = H._openrappter_supervision()
scenario("(b) both domains enumerated, no claimant -> unsupervised, ok stays True",
         r["ok"] and r.get("supervision") == "unsupervised"
         and "unsupervised" in r["detail"],
         f"supervision={r.get('supervision')} {r['detail']}")

# (c) A garbled enumeration is a question we could not ask — unverified,
# never silently promoted to either truth.
H._run = make_run({"lsof": "4242\n", "list": "", "system": ""})
r = H._openrappter_supervision()
scenario("(c) unreadable enumerations -> unverified, never 'unsupervised'",
         r["ok"] and r.get("supervision") == "unverified"
         and "UNVERIFIED" in r["detail"],
         f"supervision={r.get('supervision')} {r['detail']}")

# (d) Nothing LISTENING is still the one hard failure.
H._run = make_run({"lsof": ""})
r = H._openrappter_supervision()
scenario("(d) nothing LISTENING on :18790 -> fail fires",
         (not r["ok"]) and "LISTENING" in r["detail"], r["detail"])

# (e) Control — this machine's live shape: gui row + targeted confirm.
H._run = make_run({
    "lsof": "5556\n",
    "list": GUI_LIST_DAEMON,
    "system": SYS_PRINT_EMPTYISH,
    ("print", GUI + "/com.openrappter.daemon"): PRINT_RUNNING(5556),
})
r = H._openrappter_supervision()
scenario("(e) live-machine control: gui daemon confirmed and NAMED",
         r["ok"] and r.get("supervision") == f"supervised-by:{GUI}/com.openrappter.daemon",
         f"supervision={r.get('supervision')} {r['detail']}")

# (f) A listing row alone is a candidate, not a verdict: the targeted print
# disagrees on pid, so confirmation must refuse (R1 — the row is a receipt).
H._run = make_run({
    "lsof": "5556\n",
    "list": GUI_LIST_DAEMON,
    "system": SYS_PRINT_EMPTYISH,
    ("print", GUI + "/com.openrappter.daemon"): PRINT_RUNNING(9999),
})
r = H._openrappter_supervision()
scenario("(f) listing row without pid-matching print is NOT supervised",
         r["ok"] and r.get("supervision") == "unsupervised",
         f"supervision={r.get('supervision')} {r['detail']}")

H._run = real_run

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
