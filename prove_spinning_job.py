#!/usr/bin/env python3
"""prove_spinning_job.py — a loaded job that only ever exits nonzero is
reported as the failure it is, and thresholds are real, not decorative.

#23's second, larger problem: com.openrappter.gateway loaded with runs = 27,
last exit 1, state = not running — starting, losing a runtime lock another
process held, and dying, forever. It is `loaded`, so every naive predicate
called it healthy, while the job that actually owned the port was invisible
to the queries being made. A job that can never come up is not a daemon, it
is a spinning wheel, and nothing was reporting it.

w_openrappter_spin audits every candidate label (known set + config's
watchers.openrappter.labels + openrappter-substring discovery in both domain
listings), measures each via targeted `launchctl print`, and fires at WARN —
a stale duplicate does not stop the served gateway, and waking the repair arm
to unload someone's launchd job is disproportionate.

Run: python3 prove_spinning_job.py   (exit 0 only on all-behaved)
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
GUI = H._gui_domain()


def print_stats(state, runs, last_exit):
    exit_txt = "(never exited)" if last_exit is None else str(last_exit)
    return (f"job = {{\n\tstate = {state}\n\truns = {runs}\n"
            f"\tlast exit code = {exit_txt}\n}}\n")


def make_run(table):
    def _run(cmd, timeout=15):
        if cmd[:2] == ["launchctl", "list"]:
            return table.get("list", "")
        if cmd[:2] == ["launchctl", "print"]:
            if cmd[2] == "system":
                return table.get("system", "")
            return table.get(("print", cmd[2]), "")
        raise AssertionError(f"unexpected command: {cmd}")
    return _run


real_run = H._run

# (a) The live case from #23: a gateway label discovered in the gui listing,
# loaded, 27 starts, exit 1 every time, never running.
H._run = make_run({
    "list": GUI_HEADER + "\n-\t1\tcom.openrappter.gateway",
    "system": "\tservices = {\n\t}\n",
    ("print", GUI + "/com.openrappter.gateway"):
        print_stats("not running", 27, 1),
})
r = H._spinning_openrappter_jobs()
scenario("(a) the #23 gateway: runs=27, exit 1, never running -> FIRES at WARN naming all three",
         (not r["ok"]) and r["severity"] == H.C.WARN
         and "com.openrappter.gateway" in r["detail"]
         and "runs=27" in r["detail"] and "last exit 1" in r["detail"],
         f"severity={r['severity']} {r['detail']}")

# (b) Control — this machine's healthy daemon: loaded and running.
H._run = make_run({
    "list": GUI_HEADER + "\n5556\t0\tcom.openrappter.daemon",
    "system": "\tservices = {\n\t}\n",
    ("print", GUI + "/com.openrappter.daemon"):
        print_stats("running", 1, None),
})
r = H._spinning_openrappter_jobs()
scenario("(b) control: a running daemon audits clean",
         r["ok"] and "none spinning" in r["detail"], r["detail"])

# (c) Boundary: two failed starts is a job being retried, not a wheel —
# the runs>=3 threshold is real, not decorative.
H._run = make_run({
    "list": GUI_HEADER + "\n-\t1\tcom.openrappter.gateway",
    "system": "\tservices = {\n\t}\n",
    ("print", GUI + "/com.openrappter.gateway"):
        print_stats("not running", 2, 1),
})
r = H._spinning_openrappter_jobs()
scenario("(c) boundary: runs=2 exit 1 does not fire", r["ok"], r["detail"])

# (d) Unreadable listings: blind is never green (#45).
H._run = make_run({"list": "", "system": ""})
r = H._spinning_openrappter_jobs()
scenario("(d) neither listing readable -> fail 'cannot audit', never ok",
         (not r["ok"]) and "cannot audit" in r["detail"], r["detail"])

# (e) Listings readable, no openrappter job loaded anywhere: an honest ok
# that says what was (not) found.
H._run = make_run({
    "list": GUI_HEADER + "\n-\t0\tcom.apple.something",
    "system": "\tservices = {\n\t\t       0      - \tcom.apple.rpmuxd\n\t}\n",
})
r = H._spinning_openrappter_jobs()
scenario("(e) no jobs loaded -> ok 'no openrappter launchd jobs loaded'",
         r["ok"] and "no openrappter launchd jobs loaded" in r["detail"],
         r["detail"])

# (f) A spinning job that exits 0 is a completion loop, not a crash loop —
# only nonzero exits fire.
H._run = make_run({
    "list": GUI_HEADER + "\n-\t0\tcom.openrappter.morning-decisions",
    "system": "\tservices = {\n\t}\n",
    ("print", GUI + "/com.openrappter.morning-decisions"):
        print_stats("not running", 40, 0),
})
r = H._spinning_openrappter_jobs()
scenario("(f) a scheduled job exiting 0 forty times is not a wheel",
         r["ok"], r["detail"])

H._run = real_run

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
