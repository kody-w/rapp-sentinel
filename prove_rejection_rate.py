#!/usr/bin/env python3
"""prove_rejection_rate.py — the rejection-rate check pages on the July-30
shape, stays a warn on human questions, and never reads blind or stale as
fine. Severity is asserted alongside every verdict, because the whole design
is the severity split: ≥90% flagged is the one shape worth paging, ~50-89%
is a calibration question, and an unmeasured window is never green.

The live reader (rb_slop_cop_rejection_rate) is exercised through the same
urlopen swap prove_rollup_coverage.py uses, so what is proven here is the
deployed path — public_json fetch included — not a lookalike.

Run: python3 prove_rejection_rate.py   (exit 0 only on all-behaved)
"""

import io
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import checks as C

FAILURES = []
real_urlopen = urllib.request.urlopen


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


def iso_hours_ago(h):
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()


def ledger(last_run_h, reviews):
    """reviews: list of (hours_ago, flagged)."""
    return json.dumps({
        "reviews": [{"post_number": 20000 + i, "title": f"post {i}",
                     "score": 1 if fl else 4, "reason": "r",
                     "flagged": fl, "timestamp": iso_hours_ago(h)}
                    for i, (h, fl) in enumerate(reviews)],
        "_meta": {"total_reviews": len(reviews),
                  "total_flags": sum(1 for _, fl in reviews if fl),
                  "last_run": iso_hours_ago(last_run_h),
                  "last_run_reviewed": 1, "last_run_flagged": 0,
                  "last_run_avg_score": 4.0,
                  "materialized_at": iso_hours_ago(last_run_h)},
    }).encode()


def serve(body):
    if isinstance(body, Exception):
        def boom(req, timeout=None):
            raise body
        urllib.request.urlopen = boom
    else:
        urllib.request.urlopen = (
            lambda req, timeout=None: io.BytesIO(body))


def run():
    try:
        return C.rb_slop_cop_rejection_rate()
    finally:
        urllib.request.urlopen = real_urlopen


# (1) Healthy and fresh: 20 reviews inside the window, 2 flagged -> ok.
serve(ledger(1, [(5, i < 2) for i in range(20)]))
r = run()
scenario("(1) healthy fresh window (2/20 flagged) is ok",
         r["ok"] and "2/20 flagged" in r["detail"], r["detail"])

# (2) The July-30 shape: 19/20 flagged in-window -> CRITICAL. The day-one
# catch #6 asks for: "rejection rate hit 100%" must page, not warn.
serve(ledger(1, [(5, i < 19) for i in range(20)]))
r = run()
scenario("(2) 95% flagged in window -> CRITICAL 'rejecting essentially everything'",
         (not r["ok"]) and r["severity"] == C.CRITICAL
         and "rejecting essentially everything" in r["detail"],
         f"severity={r['severity']} {r['detail']}")

# (3) 60% flagged -> WARN, not critical. Whether the cop is miscalibrated
# or the input got worse is a human question (rv_validation's split).
serve(ledger(1, [(5, i < 12) for i in range(20)]))
r = run()
scenario("(3) 60% flagged -> warn 'human question', never critical",
         (not r["ok"]) and r["severity"] == C.WARN
         and "human question" in r["detail"],
         f"severity={r['severity']} {r['detail']}")

# (4) Fresh _meta.last_run but every review stamp is old: it ran and judged
# NOTHING. The window is timestamps, not row count — 500 stale ring rows
# must not impersonate a live reading.
serve(ledger(1, [(60, False) for _ in range(20)]))
r = run()
scenario("(4) fresh last_run, all stamps old -> warn 'reviewed nothing'",
         (not r["ok"]) and r["severity"] == C.WARN
         and "reviewed nothing" in r["detail"],
         f"severity={r['severity']} {r['detail']}")

# (5) last_run 60h ago -> the cop stopped patrolling. UNMEASURED, not fine
# — and explicitly not ok, because unmeasured rejections are the exact
# blindness that let July 30 run for five days.
serve(ledger(60, [(5, False) for _ in range(20)]))
r = run()
scenario("(5) last_run 60h old -> warn 'UNMEASURED', never ok",
         (not r["ok"]) and r["severity"] == C.WARN
         and "UNMEASURED" in r["detail"],
         f"severity={r['severity']} {r['detail']}")

# (6) An unparseable ledger is a blind instrument, never green (#45).
serve(b"{ not json")
r = run()
scenario("(6) unparseable ledger -> warn 'unmeasured', never ok",
         (not r["ok"]) and r["severity"] == C.WARN
         and "unmeasured" in r["detail"],
         f"severity={r['severity']} {r['detail']}")

# (7) The min-sample gate is real: 3/3 flagged is 100% but n < 5, so it
# must NOT page — one bad afternoon on a quiet platform is not an outage.
serve(ledger(1, [(5, True) for _ in range(3)]))
r = run()
scenario("(7) 3/3 flagged -> NO critical (min-sample gate holds)",
         r["severity"] != C.CRITICAL and "3/3 flagged" in r["detail"],
         f"ok={r['ok']} severity={r['severity']} {r['detail']}")

# (8) Boundary control: exactly n=5 at 100% IS the page — the gate is a
# floor, not a dodge.
serve(ledger(1, [(5, True) for _ in range(5)]))
r = run()
scenario("(8) 5/5 flagged -> CRITICAL (the gate is a floor, not a dodge)",
         (not r["ok"]) and r["severity"] == C.CRITICAL,
         f"severity={r['severity']} {r['detail']}")

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
