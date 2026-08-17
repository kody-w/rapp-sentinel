"""Reproduction: a surface we could not FETCH is not a platform nobody can join.

Live, 2026-08-17T11:47Z, during a GitHub partial outage (API Requests, Issues,
Pull Requests and Actions all `major_outage` on the status page).
`rb_public_surface` reported

    an outsider cannot read platform state after 3 attempts:
    HTTPError: HTTP Error 503: first byte timeout

at CRITICAL and woke the repair arm. In the SAME tick `diagnose.py` fetched
that same host -- raw.githubusercontent.com, `state/agents.json` -- in 0.4s
with http=200, and the rb site, rv site and channel all served 200. Nothing
about the platform's joinability had changed; GitHub's edge was shedding load,
and the sibling warns in the same tick (`rb_shards`, `rb_derived_truth`,
`rb_json_parses`) all carried transport errors too.

`outsider_can_join` already had HALF this fix. It retries three times before
believing a failure -- `rb_content_moving`'s docstring even cites it as the
precedent, "exactly as `outsider_can_join` already does against this host" --
but the branch that runs when those retries are EXHAUSTED still returned the
default severity, which is critical. So the check was taught to doubt one
sample and then paged on the doubt anyway.

The severities were also inverted against each other. A roster that parses and
lists ZERO agents -- an observed, content-level fact -- was deliberately warn
(#50: "the repair arm cannot restore a roster it did not delete"), while a
dropped TCP connection, which says nothing at all about the roster, paged.
The weaker evidence carried the louder alarm.

The distinction this locks in, the same table
`prove_transport_failure_is_not_a_content_stall.py` and
`prove_unreadable_is_not_absence.py` already lock in for their checks:

    fetch failed        -> warn, "cannot read ... after 3 attempts"  (unknown)
    fetch flaked once   -> ok, the retry sees the truth              (green)
    read it, zero roster-> warn, observed and real, but not ours     (#50 stands)
    read it, populated  -> ok                                        (green)

`required_checks.json` types this check `kind: "reachability"`. An unreachable
reachability probe is the definition of "we do not know", and there is no
repair the repair arm can perform against GitHub's CDN.

The zero-roster arm is the entire point of the #50 fix, so it is exercised in
both directions. A fix that silenced it would pass a lazier test.
"""
import json
import sys
import time
import urllib.error
import urllib.request

import checks

_REAL_URLOPEN = urllib.request.urlopen
_REAL_SLEEP = time.sleep


def restore():
    urllib.request.urlopen = _REAL_URLOPEN
    time.sleep = _REAL_SLEEP


class _Resp:
    def __init__(self, body):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def roster(n):
    return json.dumps({"agents": {f"a{i}": {} for i in range(n)}}).encode()


def http(code, msg):
    return urllib.error.HTTPError(
        "https://raw.githubusercontent.com/kody-w/rappterbook/main/state/agents.json",
        code, msg, {}, None)


# The two transport failures actually observed in the waking verdict.
TIMEOUT_503 = http(503, "first byte timeout")
RATE_429 = http(429, "Too Many Requests")
HANDSHAKE = urllib.error.URLError("_ssl.c:1112: The handshake operation timed out")


def serving(*script):
    """Patch urlopen to replay `script`: exceptions raise, bytes are served.

    The last entry repeats, so a 1-element script is a steady state. Counts
    calls so the test can assert the check actually retried before believing.
    """
    calls = []

    def stub(req, timeout=None):
        step = script[min(len(calls), len(script) - 1)]
        calls.append(1)
        if isinstance(step, BaseException):
            raise step
        return _Resp(step)

    urllib.request.urlopen = stub
    time.sleep = lambda s: None
    return calls


CASES = []


def case(name, want_ok, want_critical, script, expect=None, forbid=None,
         min_calls=None):
    calls = serving(*script)
    try:
        got = checks.outsider_can_join()
    finally:
        restore()
    is_crit = got["severity"] == checks.CRITICAL and not got["ok"]
    good = got["ok"] is want_ok and is_crit is want_critical
    # A verdict can carry the right severity and still tell a lie. The incident
    # was a true-shaped sentence asserting something false about the platform,
    # so assert on the sentence too.
    if expect and expect not in got["detail"]:
        good = False
    if forbid and forbid in got["detail"]:
        good = False
    if min_calls is not None and len(calls) < min_calls:
        good = False
    # The vantage tag is what w_outsider_coverage counts; a fix that dropped it
    # would silently uncover the outsider path.
    if got.get("vantage") != "outsider":
        good = False
    CASES.append((good, name, want_ok, want_critical, got["ok"], is_crit,
                  len(calls), got["detail"][:58]))


# ── the incident: the fetch failed, the front door is fine ──────────────────
case("503 first byte timeout -> warn, not a page", False, False,
     [TIMEOUT_503], expect="503", min_calls=3)
case("503 -> retried 3x before believed", False, False,
     [TIMEOUT_503], min_calls=3)
case("429 rate limit -> warn, not a page", False, False,
     [RATE_429], expect="429", min_calls=3)
case("dead TCP handshake -> warn, not a page", False, False,
     [HANDSHAKE], min_calls=3)

# the reason must survive: "HTTPError" alone cannot separate 503 (wait) from
# 404 (fix the URL), and that string is the whole diagnosis the arm is woken
# with.
case("exhausted read still names the reason", False, False,
     [TIMEOUT_503], expect="first byte timeout", min_calls=3)

# ── the flake that caused it: recovers on a later attempt -> green ──────────
case("one dropped connection then served -> ok", True, False,
     [TIMEOUT_503, roster(3)], min_calls=2)
case("two dropped connections then served -> ok", True, False,
     [TIMEOUT_503, RATE_429, roster(3)], min_calls=3)
case("recovery is disclosed, not hidden", True, False,
     [TIMEOUT_503, roster(3)], expect="recovered after 2 attempts")

# ── #50 must survive: an OBSERVED zero roster still fails, at warn ──────────
case("zero-agent roster -> still fails at WARN", False, False,
     [roster(0)], expect="zero agents")

# ── controls: a populated roster still reads green in one call ─────────────
case("CONTROL populated roster -> ok", True, False,
     [roster(3)], expect="3 agents", min_calls=1)
case("CONTROL populated roster does not claim unreadable", True, False,
     [roster(12)], forbid="cannot read")

bad = 0
for good, name, w_ok, w_crit, g_ok, g_crit, n, detail in CASES:
    bad += not good
    print(f"  [{'ok' if good else 'XX'}] {name}\n"
          f"        expected ok={w_ok} critical={w_crit}  "
          f"got ok={g_ok} critical={g_crit} fetches={n}  {detail}")
print(f"\n{len(CASES)-bad}/{len(CASES)} scenarios behaved as specified")
sys.exit(1 if bad else 0)
