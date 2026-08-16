#!/usr/bin/env python3
"""prove_outsider_coverage.py — the outsider vantage is enforced, not
asserted, and per-platform coverage is compared every tick (issue #5).

Every check used to run with the owner's gh credentials, so a green proved
the platform works FOR THE OWNER and said nothing about the front door.
checks.outsider_check stamps vantage="outsider" only after proving the
execution never routed through gh() — the one credentialed helper in
checks.py — by snapshotting the _GH_CALLS counter around the check. A check
that touched gh() has its own result REPLACED with a critical failure and
stamped vantage="credentialed", so health.check_outsider_coverage cannot
count a violation as coverage.

Honest limit, restated where it is proven: the counter sees gh() only. A
check shelling out to `gh` directly is a code-review property, the same
class as w_checks_complete's inability to detect its own removal.

Run: python3 prove_outsider_coverage.py   (exit 0 only on all-behaved)
"""

import json
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import checks as C
import health as H

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


# ── the decorator fires: a credentialed execution is replaced ───────────────

saved_registry = C._REGISTRY[:]
saved_subprocess_run = C.subprocess.run


def _refuse_real_gh(*args, **kwargs):
    raise RuntimeError("no real gh in a prove harness")


C.subprocess.run = _refuse_real_gh
try:
    @C.outsider_check("fakeland")
    def fake_credentialed():
        C.gh(["api", "user"])      # reaches for the owner's token
        return C.ok("fake_outsider", "green produced with the owner's token")

    r = fake_credentialed()
    scenario("(a) a check that calls gh() has its result REPLACED with the "
             "credential-violation fail",
             (not r["ok"]) and r["severity"] == C.CRITICAL
             and r["id"] == "fake_outsider"
             and "owner credentials" in r["detail"],
             f"ok={r['ok']} severity={r['severity']} {r['detail']}")
    scenario("(b) the violation is stamped 'credentialed', never 'outsider', "
             "and still names its platform",
             r.get("vantage") == "credentialed" and r.get("platform") == "fakeland",
             f"vantage={r.get('vantage')} platform={r.get('platform')}")
    violation_result = dict(r)

    # A failed gh call is still a credentialed reach: the counter moves on
    # entry, before any outcome — proven by the fact that the stubbed
    # subprocess RAISED above and the decorator still fired.

    @C.outsider_check("fakeland")
    def fake_clean_fail():
        return C.fail("fake_outsider2", "the door is stuck", critical=False)

    r = fake_clean_fail()
    scenario("(c) CONTROL: a gh-free check keeps its own result and earns "
             "vantage='outsider' — ran-and-FAILED included",
             (not r["ok"]) and r["detail"] == "the door is stuck"
             and r.get("vantage") == "outsider"
             and r.get("platform") == "fakeland",
             f"ok={r['ok']} vantage={r.get('vantage')} {r['detail']}")
    ran_and_failed = dict(r)
finally:
    C.subprocess.run = saved_subprocess_run
    C._REGISTRY[:] = saved_registry


# ── the three converted checks stay unprivileged (network stubbed) ──────────

class _FakeResp:
    def __init__(self, body):
        self._body = body
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


real_urlopen = urllib.request.urlopen
urllib.request.urlopen = lambda req, timeout=None: _FakeResp(
    json.dumps({"agents": {"a": {}, "b": {}}}).encode())
before = C._GH_CALLS
try:
    r = C.outsider_can_join()
finally:
    urllib.request.urlopen = real_urlopen
scenario("(d) outsider_can_join: unauthenticated read, gh counter untouched, "
         "vantage tags present",
         r["ok"] and C._GH_CALLS == before
         and r.get("vantage") == "outsider"
         and r.get("platform") == "rappterbook",
         f"gh_calls_delta={C._GH_CALLS - before} vantage={r.get('vantage')} "
         f"platform={r.get('platform')} {r['detail']}")

now = datetime.now(timezone.utc).isoformat()
_DOCS = {
    "state/actions.json": ({"actions": [
        {"agentId": f"agent{i % 5}", "type": f"type{i % 3}", "data": {"i": i}}
        for i in range(30)]}, ""),
    "state/chat.json": ({"messages": [{"body": "hi"}],
                         "_meta": {"lastUpdate": now}}, ""),
    "state/agents.json": ({"_meta": {"lastUpdate": now}}, ""),
}
real_public_json = C.public_json
C.public_json = lambda repo, path, attempts=2: _DOCS[path]
before = C._GH_CALLS
try:
    r = C.world_is_meaningfully_active()
finally:
    C.public_json = real_public_json
scenario("(e) world_is_meaningfully_active: public raw fetches only, gh "
         "counter untouched, tagged rappterverse",
         r["ok"] and C._GH_CALLS == before
         and r.get("vantage") == "outsider"
         and r.get("platform") == "rappterverse",
         f"gh_calls_delta={C._GH_CALLS - before} vantage={r.get('vantage')} "
         f"platform={r.get('platform')} {r['detail'][:60]}")

real_http_status = C.http_status
C.http_status = lambda url: 200
before = C._GH_CALLS
try:
    r = C.channel_serving()
finally:
    C.http_status = real_http_status
scenario("(f) channel_serving: unauthenticated url_check only, gh counter "
         "untouched, tagged channel",
         r["ok"] and C._GH_CALLS == before
         and r.get("vantage") == "outsider"
         and r.get("platform") == "channel",
         f"gh_calls_delta={C._GH_CALLS - before} vantage={r.get('vantage')} "
         f"platform={r.get('platform')} {r['detail']}")


# ── coverage: the manifest's outsider_platforms is compared per tick ────────

def result(platform=None, vantage=None, ok=True):
    r = {"id": "x", "ok": ok, "severity": C.WARN, "detail": ""}
    if platform is not None:
        r["platform"] = platform
    if vantage is not None:
        r["vantage"] = vantage
    return r


FULL = [result("rappterbook", "outsider"),
        result("rappterverse", "outsider"),
        result("channel", "outsider")]

REAL_HOME = H.CODE

r = H.check_outsider_coverage(FULL)
scenario("(g) all three platforms carry an outsider result -> ok",
         r["ok"] and "3 platform(s)" in r["detail"], r["detail"])

r = H.check_outsider_coverage([result("rappterbook", "outsider"),
                               result("channel", "outsider")])
scenario("(h) a platform with NO outsider result fires at warn, naming it",
         (not r["ok"]) and r["severity"] == C.WARN
         and "rappterverse" in r["detail"] and "rappterbook" not in r["detail"],
         f"severity={r['severity']} {r['detail']}")

r = H.check_outsider_coverage([result("rappterbook", "outsider"),
                               result("channel", "outsider"),
                               result("rappterverse", "outsider", ok=False)])
scenario("(i) CONTROL: a check that RAN and FAILED still covers its platform",
         r["ok"], r["detail"])

r = H.check_outsider_coverage([result("rappterbook", "outsider"),
                               result("channel", "outsider"),
                               result("rappterverse", "credentialed", ok=False)])
scenario("(j) a credential VIOLATION does not count as coverage — the "
         "vantage is earned, not declared",
         (not r["ok"]) and "rappterverse" in r["detail"],
         f"severity={r['severity']} {r['detail']}")

# End to end: the decorator's OWN outputs feed the coverage check — no
# hand-built dicts, the two halves must agree on what counts.
r = H.check_outsider_coverage([violation_result])
scenario("(k) end to end: the replaced violation covers NOTHING — every "
         "manifest platform reads uncovered",
         (not r["ok"]) and "rappterbook" in r["detail"]
         and "rappterverse" in r["detail"] and "channel" in r["detail"],
         r["detail"])
r = H.check_outsider_coverage(FULL + [ran_and_failed])
scenario("(l) end to end: the decorator's honest ran-and-failed result "
         "rides through coverage without poisoning it",
         r["ok"], r["detail"])


def with_manifest(doc_or_bytes):
    d = Path(tempfile.mkdtemp(prefix="prove-outsider-"))
    p = d / "required_checks.json"
    if isinstance(doc_or_bytes, bytes):
        p.write_bytes(doc_or_bytes)
    else:
        p.write_text(json.dumps(doc_or_bytes), encoding="utf-8")
    H.CODE = d
    return d


try:
    committed = json.loads(
        (REAL_HOME / "required_checks.json").read_text(encoding="utf-8"))
    doc = json.loads(json.dumps(committed))
    del doc["outsider_platforms"]
    with_manifest(doc)
    r = H.check_outsider_coverage(FULL)
    scenario("(m) manifest key missing (mid-deploy) -> warn 'coverage "
             "unknown', never raise, never green",
             (not r["ok"]) and r["severity"] == C.WARN
             and "outsider_platforms missing - coverage unknown" in r["detail"],
             f"severity={r['severity']} {r['detail']}")

    with_manifest(b"{ not json")
    r = H.check_outsider_coverage(FULL)
    scenario("(n) unreadable manifest -> warn about the instrument, not green",
             (not r["ok"]) and r["severity"] == C.WARN
             and "unreadable" in r["detail"],
             f"severity={r['severity']} {r['detail']}")
finally:
    H.CODE = REAL_HOME

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
