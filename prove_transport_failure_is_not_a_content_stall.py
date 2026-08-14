"""Reproduction: a roll-up we could not FETCH is not a platform that stopped.

Live, 2026-08-14T14:29:57Z. rb_content_moving reported

    cannot read roll-up state (<urlopen error _ssl.c:1112: The handshak)

at CRITICAL and woke the repair arm. In the SAME verdict, rb_rollup_coverage
read THE SAME URL -- state/trending.json on raw.githubusercontent.com -- well
enough to report "73.5% of corpus (11634/15832)", and re-running the check five
times immediately after returned "roll-up 0.6h old, 11634 posts" every time.
One TCP connection died. Nothing had stopped moving.

The check fetched once and treated ANY exception from that single sample as a
statement about the platform. That is the error `outsider_can_join` was already
taught against this exact host ("a single dropped connection to
raw.githubusercontent.com woke the repair arm at 19:12 on 2026-08-05"), and the
error the UNREADABLE sentinel exists for (#45, #51, #58, #59, #60).

The distinction this locks in:

    fetch failed        -> warn, "cannot read ... after 3 attempts"  (unknown)
    fetch flaked once   -> ok, the retry sees the truth              (green)
    read it, it is old  -> critical, the roll-up really is stale     (stands)
    read it, no stamp   -> critical, served but wrong                (stands)

The stale and zero-posts arms are the entire point of the check (#39), so they
are exercised in both directions. A fix that silenced them would pass a lazier
test.
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

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


def meta_body(age_h=0.5, posts=11634, stamp_key="materialized_at"):
    when = datetime.now(timezone.utc) - timedelta(hours=age_h)
    meta = {"total_posts_analyzed": posts}
    if stamp_key:
        meta[stamp_key] = when.isoformat().replace("+00:00", "Z")
    return json.dumps({"_meta": meta}).encode()


HANDSHAKE = urllib.error.URLError("_ssl.c:1112: The handshake operation timed out")


def serving(*script):
    """Patch urlopen to replay `script`: exceptions raise, bytes are served.

    The last entry repeats, so a 1-element script is a steady state. Counts
    calls so the test can assert the check actually retried.
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


def case(name, want_ok, want_critical, script, forbid=None, min_calls=None):
    calls = serving(*script)
    try:
        got = checks.rb_content_moving()
    finally:
        restore()
    is_crit = got["severity"] == checks.CRITICAL and not got["ok"]
    good = got["ok"] is want_ok and is_crit is want_critical
    # A verdict can carry the right severity and still tell a lie. The incident
    # was a true-shaped sentence asserting something false about the platform,
    # so assert on the sentence too.
    if forbid and forbid in got["detail"]:
        good = False
    if min_calls is not None and len(calls) < min_calls:
        good = False
    CASES.append((good, name, want_ok, want_critical, got["ok"], is_crit,
                  len(calls), got["detail"][:56]))


# ── the incident: the fetch failed, the platform is fine ────────────────────
case("transport dead -> warn, not a page", False, False,
     [HANDSHAKE], min_calls=3)
case("transport dead -> retried before believed", False, False,
     [HANDSHAKE], min_calls=3)

# ── the flake that caused it: recovers on a later attempt -> green ──────────
case("one dropped connection then served -> ok", True, False,
     [HANDSHAKE, meta_body()], min_calls=2)
case("two dropped connections then served -> ok", True, False,
     [HANDSHAKE, HANDSHAKE, meta_body()], min_calls=3)

# ── #39 must survive: an observed stall still pages ─────────────────────────
case("read it and it is 13h stale -> still CRITICAL", False, True,
     [meta_body(age_h=13)])
case("read it and posts == 0 -> still CRITICAL", False, True,
     [meta_body(posts=0)])
case("served without a materialization stamp -> still CRITICAL", False, True,
     [meta_body(stamp_key=None)])

# ── the documented fallback: last_updated when materialized_at is absent ────
case("falls back to last_updated -> ok", True, False,
     [meta_body(stamp_key="last_updated")])

# ── control: a healthy roll-up still reads green in one call ───────────────
case("CONTROL fresh roll-up -> ok", True, False, [meta_body()])

bad = 0
for good, name, w_ok, w_crit, g_ok, g_crit, n, detail in CASES:
    bad += not good
    print(f"  [{'ok' if good else 'XX'}] {name}\n"
          f"        expected ok={w_ok} critical={w_crit}  "
          f"got ok={g_ok} critical={g_crit} fetches={n}  {detail}")
print(f"\n{len(CASES)-bad}/{len(CASES)} scenarios behaved as specified")
sys.exit(1 if bad else 0)
