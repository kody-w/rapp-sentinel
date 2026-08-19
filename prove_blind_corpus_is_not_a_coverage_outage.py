#!/usr/bin/env python3
"""Reproduction: a corpus we could not COUNT is not a site computed from less.

Live, 2026-08-19T22:20:21Z. rb_rollup_coverage reported

    cannot read corpus size

at CRITICAL and woke the repair arm. In the SAME verdict, rb_derived_truth had
already read that corpus -- "15884 reported / 15884 analyzed posts" -- and
rb_content_moving read the same roll-up green at "1.3h old, 15884 posts". The
platform was at 100% coverage. One `gh api graphql` subprocess died; re-running
it five times immediately after returned totalCount=15884 in 0.34-0.63s with
4793 GraphQL points remaining.

This is the exact mirror of the 2026-08-14 incident in
prove_transport_failure_is_not_a_content_stall.py, where rb_content_moving
raised the false page and THIS check was cited as the healthy control that read
the same URL in the same tick. The fix landed on the sibling only, so the
un-retried single-sample read survived here -- and the two checks eventually
swapped roles. A lesson applied to one of two call sites is not learned.

Two blind reads were being spoken as claims about rappterbook:

  * the roll-up fetch was one urlopen, and ANY exception paged CRITICAL
  * gh() returns its default on ANY failure, so `default=None` made a dead
    subprocess and a real answer the same value -- and the resulting sentence
    carried no reason at all, which is the whole diagnosis the repair arm is
    woken with

The distinction this locks in, identical to the sibling's:

    fetch failed        -> warn, "cannot read ... after 3 attempts: <reason>"
    fetch flaked once   -> ok, the retry sees the truth
    gh read failed      -> warn, "cannot read corpus size (gh ... failed)"
    read it, no count   -> critical, served but wrong
    read it, corpus 0   -> critical, the platform really is empty
    read it, 73.8%      -> warn, the #41 shortfall arm, unchanged

The #41 coverage arms are the entire point of the check, so they are exercised
in both directions. A fix that silenced them would pass a lazier test.

Run: python3 prove_blind_corpus_is_not_a_coverage_outage.py  (exit 0 = behaved)
"""
import io
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import checks as C

_REAL_URLOPEN = urllib.request.urlopen
_REAL_SLEEP = time.sleep
_REAL_GH = C.gh

CASES = []


def restore():
    urllib.request.urlopen = _REAL_URLOPEN
    time.sleep = _REAL_SLEEP
    C.gh = _REAL_GH


def rollup_bytes(analyzed=15884, hours_ago=0.5):
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps({"_meta": {"materialized_at": stamp,
                                 "total_posts_analyzed": analyzed}}).encode()


DROPPED = urllib.error.URLError("[Errno 60] Operation timed out")


def serving(*script):
    """Replay `script` through urlopen: exceptions raise, bytes are served.

    The last entry repeats, so a 1-element script is a steady state. Counts
    calls so a scenario can assert the check actually re-sampled.
    """
    calls = []

    def stub(req, timeout=None):
        step = script[min(len(calls), len(script) - 1)]
        calls.append(1)
        if isinstance(step, BaseException):
            raise step
        return io.BytesIO(step)

    urllib.request.urlopen = stub
    time.sleep = lambda s: None
    return calls


def corpus(total):
    return lambda *a, **k: {
        "data": {"repository": {"discussions": {"totalCount": total}}}}


def gh_blind(*a, **k):
    """What gh() actually does when the subprocess dies: return its default."""
    return k.get("default", a[1] if len(a) > 1 else None)


def case(name, want_ok, want_critical, script, gh_stub,
         want_in="", forbid="", min_calls=None):
    calls = serving(*script)
    C.gh = gh_stub
    try:
        got = C.rb_rollup_covers_corpus()
    finally:
        restore()
    is_crit = got["severity"] == C.CRITICAL and not got["ok"]
    good = got["ok"] is want_ok and is_crit is want_critical
    # A verdict can carry the right severity and still tell a lie. The incident
    # was a true-shaped sentence asserting something false about rappterbook,
    # so assert on the sentence too.
    if want_in and want_in not in got["detail"]:
        good = False
    if forbid and forbid in got["detail"]:
        good = False
    if min_calls is not None and len(calls) < min_calls:
        good = False
    CASES.append((good, name, want_ok, want_critical,
                  got["ok"], is_crit, len(calls), got["detail"][:60]))


# ── the incident: the corpus read died, the platform is at 100% ─────────────
case("THE INCIDENT: gh blind -> warn, never a page", False, False,
     [rollup_bytes()], gh_blind)
case("THE INCIDENT: gh blind -> the sentence carries a reason", False, False,
     [rollup_bytes()], gh_blind, want_in="gh")

# ── the sibling's incident, on this check's own fetch ───────────────────────
case("roll-up transport dead -> warn, not a page", False, False,
     [DROPPED], corpus(15884), want_in="3 attempts", min_calls=3)
case("one dropped connection then served -> ok, the retry sees 100%", True,
     False, [DROPPED, rollup_bytes()], corpus(15884),
     want_in="100.0%", min_calls=2)
case("two dropped connections then served -> ok", True, False,
     [DROPPED, DROPPED, rollup_bytes()], corpus(15884), min_calls=3)

# ── a blind read must never be spoken as the shortfall it is not ───────────
case("blind read never claims a coverage percentage", False, False,
     [rollup_bytes()], gh_blind, forbid="% of corpus")

# ── served but wrong still pages ───────────────────────────────────────────
case("gh answers with no totalCount -> CRITICAL served-but-wrong", False, True,
     [rollup_bytes()], lambda *a, **k: {"data": {"repository": {}}})
case("corpus reports zero discussions -> CRITICAL", False, True,
     [rollup_bytes()], corpus(0), want_in="zero")

# ── #41 must survive: the shortfall arms, both directions ──────────────────
case("#41 frozen 11634 vs 15884 (73.8%) -> warn shortfall", False, False,
     [rollup_bytes(analyzed=11634)], corpus(15884), want_in="% of corpus")
case("#41 the 8000 scrape cap -> warn shortfall", False, False,
     [rollup_bytes(analyzed=8000)], corpus(15884), want_in="% of corpus")
case("#41 exactly at the 90% threshold -> ok", True, False,
     [rollup_bytes(analyzed=14296)], corpus(15884))
case("#41 mild materialization lag (95%) -> ok", True, False,
     [rollup_bytes(analyzed=15090)], corpus(15884))

# ── control: the live state on the day of the incident ─────────────────────
case("CONTROL live 15884/15884 -> ok in one fetch", True, False,
     [rollup_bytes()], corpus(15884), want_in="100.0%")

bad = 0
for good, name, w_ok, w_crit, g_ok, g_crit, n, detail in CASES:
    bad += not good
    print(f"  [{'ok' if good else 'XX'}] {name}\n"
          f"        expected ok={w_ok} critical={w_crit}  "
          f"got ok={g_ok} critical={g_crit} fetches={n}\n"
          f"        {detail}")
print(f"\n{len(CASES)-bad}/{len(CASES)} scenarios behaved as specified")
sys.exit(1 if bad else 0)
