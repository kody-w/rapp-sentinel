#!/usr/bin/env python3
"""prove_starved_colors.py — the three R3 gap fixes fire, and the old code's
blindness is reproduced rather than asserted.

Three checks each passed on the absence of a named bad state:

  rb_wf_starved      candidate filter `cancelled > 0` — all-skipped invisible
  rb_public_surface  a zero-agent roster parsed cleanly and read as ok
  rb_json_parses     `except: pass` — 4-of-5 unreachable read as ok "1/5"

Every scenario here breaks precisely the condition a check claims to detect
and watches the verdict, per the #38 method: one break, one run, one row.

Run: python3 prove_starved_colors.py   (exit 0 only on all-behaved)
"""

import io
import json
import sys
import urllib.request
from pathlib import Path

import checks as C

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


# ── rb_wf_starved: colour-blind starvation ──────────────────────────────────

def fake_gh(narrow, wide):
    """A gh() double: the repo-wide window, then per-workflow lookbacks."""
    def gh(args, default=None):
        if "--workflow" in args:
            name = args[args.index("--workflow") + 1]
            rows = wide.get(name)
            return [{"conclusion": c} for c in rows] if rows is not None else default
        return [{"name": n, "conclusion": c} for n, c in narrow]
    return gh


real_gh = C.gh

# skipped 3/3 — the colour the old filter missed. Wide lookback also skipped.
C.gh = fake_gh([("Static API", "skipped")] * 3, {"Static API": ["skipped"] * 5})
r = C.rb_workflows_never_succeed()
scenario("rb_wf_starved fires on skipped-3/3, at WARN (path-filter suspect, not paged)",
         (not r["ok"]) and r["severity"] == C.WARN
         and "never ran the job" in r["detail"] and "skipped" in r["detail"],
         f"severity={r['severity']} {r['detail']}")

# timed_out 3/3 — asked for and never completed: starvation, CRITICAL.
C.gh = fake_gh([("Deploy", "timed_out")] * 3, {"Deploy": ["timed_out"] * 5})
r = C.rb_workflows_never_succeed()
scenario("rb_wf_starved fires on timed_out-3/3 at CRITICAL",
         (not r["ok"]) and r["severity"] == C.CRITICAL
         and "never succeeded" in r["detail"], f"severity={r['severity']} {r['detail']}")

# cancelled 3/3 — the founding case still fires (regression guard).
C.gh = fake_gh([("Static API", "cancelled")] * 3, {"Static API": ["cancelled"] * 5})
r = C.rb_workflows_never_succeed()
scenario("rb_wf_starved still fires on cancelled-3/3 (the founding case)",
         (not r["ok"]) and r["severity"] == C.CRITICAL
         and "cancelled" in r["detail"], f"severity={r['severity']} {r['detail']}")

# control: the wide lookback finds a success — a recovering workflow is
# not a starved one, and the thin candidate is cleared.
C.gh = fake_gh([("Overseer", "cancelled")] * 3,
               {"Overseer": ["success", "cancelled", "cancelled"]})
r = C.rb_workflows_never_succeed()
scenario("control: a wide-lookback success clears the thin candidate",
         r["ok"], r["detail"])

# control: healthy history is healthy.
C.gh = fake_gh([("Heartbeat", "success"), ("Heartbeat", "success")], {})
r = C.rb_workflows_never_succeed()
scenario("control: histories with successes stay green", r["ok"], r["detail"])

# contract: failing every run is rb_workflows' page, not a second one here
# (prove_starvation_confirm holds the same line from the other side).
C.gh = fake_gh([("Slop Cop", "failure")] * 4, {"Slop Cop": ["failure"] * 5})
r = C.rb_workflows_never_succeed()
scenario("contract: failures-only stays rb_workflows' signal — no double page",
         r["ok"], r["detail"])

C.gh = real_gh


# ── urlopen double for the two fetch-shaped checks ──────────────────────────

class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(bodies):
    """bodies: url substring -> bytes, or an Exception instance to raise."""
    def opener(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for frag, body in bodies.items():
            if frag in url:
                if isinstance(body, Exception):
                    raise body
                return FakeResponse(body)
        raise AssertionError(f"unexpected fetch: {url}")
    return opener


real_urlopen = urllib.request.urlopen
real_sleep = __import__("time").sleep
__import__("time").sleep = lambda *_: None   # retries must not slow the proof

# rb_public_surface: a zeroed roster is not a joinable platform.
urllib.request.urlopen = fake_urlopen({"agents.json": b'{"agents": {}}'})
r = C.outsider_can_join()
scenario("rb_public_surface fires on a zero-agent roster, at WARN",
         (not r["ok"]) and r["severity"] == C.WARN
         and "zero agents" in r["detail"], f"severity={r['severity']} {r['detail']}")

urllib.request.urlopen = fake_urlopen(
    {"agents.json": json.dumps({"agents": {"a": 1, "b": 2, "c": 3}}).encode()})
r = C.outsider_can_join()
scenario("control: a populated roster reads ok",
         r["ok"] and "3 agents" in r["detail"], r["detail"])

# rb_json_parses: partial blindness is not a passing grade.
one_good = {"stats.json": OSError("unreachable"),
            "agents.json": OSError("unreachable"),
            "channels.json": OSError("unreachable"),
            "trending.json": b'{"_meta": {}}',
            "social_graph.json": OSError("unreachable")}
urllib.request.urlopen = fake_urlopen(one_good)
r = C.rb_served_json_parses()
scenario("rb_json_parses fires WARN on 1-parsed-4-unread, naming the unread",
         (not r["ok"]) and r["severity"] == C.WARN
         and "could not read" in r["detail"] and "stats" in r["detail"],
         f"severity={r['severity']} {r['detail']}")

all_good = {frag: b'{"okay": true}' for frag in one_good}
urllib.request.urlopen = fake_urlopen(all_good)
r = C.rb_served_json_parses()
scenario("control: 5/5 parsed reads ok", r["ok"] and "5/5" in r["detail"], r["detail"])

conflicted = dict(all_good)
conflicted["social_graph.json"] = b'<<<<<<< HEAD\n{"a":1}\n=======\n'
urllib.request.urlopen = fake_urlopen(conflicted)
r = C.rb_served_json_parses()
scenario("regression: conflict markers still fail CRITICAL",
         (not r["ok"]) and r["severity"] == C.CRITICAL
         and "unparseable" in r["detail"], f"severity={r['severity']} {r['detail']}")

urllib.request.urlopen = real_urlopen
__import__("time").sleep = real_sleep

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
