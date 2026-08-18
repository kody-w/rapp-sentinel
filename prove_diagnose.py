#!/usr/bin/env python3
"""prove_diagnose.py — the dependency page answers without leaking, and the
redaction is a guarantee that fails loudly, not a habit.

diagnose.py exists because three confident inferences about a credential
were wrong and a two-minute measurement was right (#4). A diagnostic that
printed the credential it was measuring would be worse than the disease, so
every value read is registered and the single emit() printer refuses lines
containing one — proven here by feeding it a fake token and grepping the
transcript, then by asking emit() to leak on purpose and watching it raise.

Run: python3 prove_diagnose.py   (exit 0 only on all-behaved)
"""

import contextlib
import io
import json
import os
import sys
import time
import urllib.error

import diagnose as D

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


FAKE = "ghp_FAKESECRETVALUE1234567890abcdefghijk"

# ── canned probes for scenarios (a)-(d): no network, no real subprocesses.
# Scenario (e) is the exception, ON PURPOSE and said out loud: it spawns the
# real `sentinel.py diagnose` subprocess — live brainstem turn, real gh call,
# real public fetches — because the claim under test there is the DISPATCH
# (that the command runs without ticking), and a stubbed dispatch would prove
# nothing. The first version's banner claimed no-network for the whole file,
# which was false (2026-08-16 review sweep). ─────────────────────────────────

def fake_http(url, headers=None, method="GET", data=None, timeout=15):
    auth = (headers or {}).get("Authorization", "")
    if "api.github.com/user" in url:
        if FAKE in auth:
            return 200, {"X-OAuth-Scopes": "repo"}, \
                json.dumps({"login": "kody-w"}).encode(), 0.1
        return 401, {}, b"", 0.1
    if "githubstatus" in url:
        return 503, {}, b"", 0.2
    if "rappterbook" in url:
        return None, {}, b"", None          # timeout shape
    return 200, {}, b"{}", 0.1


def fake_run(cmd, timeout=20):
    if cmd[:2] == ["gh", "api"]:
        return 0, "kody-w\n"
    if cmd[:2] == ["gh", "auth"]:
        return 0, "  Token scopes: 'repo'\n"
    if cmd[0] == "copilot":
        return 0, "copilot 1.0\n"
    return 1, ""


real = dict(http=D._http, run=D._run)
D._http, D._run = fake_http, fake_run

# health/checks imports inside probes hit the real modules; stub the two
# watcher probes so nothing touches localhost.
import health as H
real_brainstem = H._brainstem_answers_turns
real_pid = H._listening_pid
H._brainstem_answers_turns = lambda: {"id": "w_brainstem", "ok": True,
                                      "severity": "warn", "detail": "stubbed"}
H._listening_pid = lambda port: None

os.environ["GH_PAT"] = FAKE
for env in ("GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN"):
    os.environ.pop(env, None)

buf = io.StringIO()
try:
    with contextlib.redirect_stdout(buf):
        rc = D.main()
finally:
    D._http, D._run = real["http"], real["run"]
    H._brainstem_answers_turns = real_brainstem
    H._listening_pid = real_pid
    os.environ.pop("GH_PAT", None)

out = buf.getvalue()

scenario("(a) the token's identity and scope appear as a row",
         "GH_PAT" in out and "user=kody-w" in out and "scopes=[repo]" in out,
         next((l.strip() for l in out.splitlines() if "GH_PAT" in l), "row missing"))

scenario("(b) the secret value appears NOWHERE in the transcript",
         FAKE not in out, f"transcript {len(out)} chars, value absent={FAKE not in out}")

scenario("(b2) unset tokens are rendered as 'unset', the #4 root-cause shape",
         "GITHUB_TOKEN" in out and "unset" in out,
         next((l.strip() for l in out.splitlines() if "GITHUB_TOKEN" in l), "?"))

leaked = False
try:
    D._SECRETS.append(FAKE)
    D.emit(f"oops the token is {FAKE}")
except RuntimeError:
    leaked = True
finally:
    D._SECRETS.remove(FAKE)
scenario("(c) emit() called with a leaking line RAISES — redaction is a guarantee",
         leaked, "RuntimeError raised" if leaked else "printed silently")

scenario("(d) a 503 endpoint renders its code; a timeout renders 'unverified'; no crash",
         rc == 0 and "http=503" in out and "unverified (no answer)" in out,
         f"rc={rc}")

# (e) `python3 sentinel.py diagnose` dispatches without running a tick.
import pathlib
import subprocess
beat = pathlib.Path(__file__).resolve().parent / "state" / "last_run.json"
before = beat.stat().st_mtime if beat.exists() else None
r = subprocess.run([sys.executable, "sentinel.py", "diagnose"],
                   capture_output=True, text=True, timeout=180,
                   cwd=str(pathlib.Path(__file__).resolve().parent))
after = beat.stat().st_mtime if beat.exists() else None
scenario("(e) sentinel.py diagnose dispatches: prints the page, never ticks",
         "sentinel diagnose" in r.stdout and before == after,
         f"rc={r.returncode}, heartbeat mtime unchanged={before == after}")

# ── (f) the chat.db probe must fail where its CONSUMER fails ────────────────
# Regression guard for a real ten-hour blind-green: the probe answered with
# os.access(R_OK), which reads POSIX bits and cannot see macOS TCC. chat.db is
# rw------- and user-owned, so os.access() returns True even in the contexts
# where the open is refused -- the page printed "readable" while every outbox
# drain reported "delivery unverifiable: chat.db unreadable" and 10 alerts
# went undelivered. The mutation below denies the open the way TCC does and
# asserts os.access is STILL True, so a regression to the old predicate cannot
# pass this scenario by accident.
import sqlite3

_real_connect = sqlite3.connect
sqlite3.connect = lambda *a, **k: (_ for _ in ()).throw(
    sqlite3.OperationalError("unable to open database file"))
try:
    denied_ident, _, denied_reach = D._chat_db_reachability()
finally:
    sqlite3.connect = _real_connect

access_still_true = D.CHAT_DB.exists() and os.access(D.CHAT_DB, os.R_OK)
scenario("(f) a TCC-denied chat.db reads NOT readable, though os.access lies",
         denied_ident == "NOT readable (TCC?)"
         and "OperationalError" in denied_reach
         and str(D.CHAT_DB) not in denied_reach,
         f"denied -> {denied_ident!r}; os.access still True={access_still_true}; "
         f"path leaked={str(D.CHAT_DB) in denied_reach}")

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
