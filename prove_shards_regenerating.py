#!/usr/bin/env python3
"""prove_shards_regenerating.py — rb_shards now requires parse + count +
measured-cadence freshness, and the old 200-check's blindness is reproduced.

The old check was `url_check` on one shard URL: a bare HTTP 200 — the full
#11 triple in a single line, because reachable is not parseable is not
current. The first scenario below reproduces that blindness against the old
logic (a 200 carrying git conflict markers read as ok), then every branch of
the new check is broken one condition at a time, with a healthy control.

Freshness evidence: the shard's `_meta` carries no timestamp (verified
2026-08-16), so the check judges the path's newest commit against 15h —
three times the worst regeneration gap measured across 2026-08-15/16 (4.9h).

Run: python3 prove_shards_regenerating.py   (exit 0 only on all-behaved)
"""

import io
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import checks as C

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


def iso_hours_ago(h):
    return (datetime.now(timezone.utc) - timedelta(hours=h)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def serve(body):
    def opener(req, timeout=None):
        if isinstance(body, Exception):
            raise body
        return FakeResponse(body)
    return opener


def gh_dates(rows):
    def gh(args, default=None):
        if rows is None:
            return default
        return list(rows)
    return gh


GOOD = json.dumps({"_meta": {"range_start": 20750, "range_end": 20999,
                             "count": 213}, "discussions": []}).encode()
CONFLICTED = b'<<<<<<< HEAD\n{"_meta": {"count": 213}}\n=======\n'

real_urlopen = urllib.request.urlopen
real_gh = C.gh
real_http_status = C.http_status

# ── the old code's blindness, reproduced ────────────────────────────────────
# Pre-fix (see git history): the whole check was
#     url_check("rb_shards", <shard url>)
# Serve conflict markers with a 200 and the old logic reads green.
C.http_status = lambda url: 200
old = C.url_check("rb_shards", "https://example/shard.json")
scenario("OLD logic: a 200 carrying conflict markers read as ok — the blindness",
         old["ok"], f"old verdict ok={old['ok']} on unparseable bytes")
C.http_status = real_http_status

# ── the new check, one broken condition at a time ───────────────────────────

urllib.request.urlopen = serve(GOOD)
C.gh = gh_dates([iso_hours_ago(3)])
r = C.derived_data_regenerating()
scenario("control: parses, 213 discussions, regenerated 3h ago -> ok",
         r["ok"] and "213 discussions" in r["detail"] and "3.0h old" in r["detail"],
         r["detail"])

C.gh = gh_dates([iso_hours_ago(16)])
r = C.derived_data_regenerating()
scenario("stale: 16h against the 15h bar -> CRITICAL",
         (not r["ok"]) and r["severity"] == C.CRITICAL and "stale" in r["detail"],
         f"severity={r['severity']} {r['detail']}")

urllib.request.urlopen = serve(CONFLICTED)
C.gh = gh_dates([iso_hours_ago(3)])
r = C.derived_data_regenerating()
scenario("conflict markers -> CRITICAL unparseable (the old check passed this)",
         (not r["ok"]) and r["severity"] == C.CRITICAL
         and "unparseable" in r["detail"], f"severity={r['severity']} {r['detail']}")

urllib.request.urlopen = serve(json.dumps({"_meta": {"count": 0}}).encode())
r = C.derived_data_regenerating()
scenario("zero-count shard -> CRITICAL 'reports no discussions'",
         (not r["ok"]) and r["severity"] == C.CRITICAL
         and "no discussions" in r["detail"], f"severity={r['severity']} {r['detail']}")

urllib.request.urlopen = serve(OSError("connection dropped"))
r = C.derived_data_regenerating()
scenario("unreachable shard -> WARN blind-not-broken",
         (not r["ok"]) and r["severity"] == C.WARN
         and "cannot read shard" in r["detail"], f"severity={r['severity']} {r['detail']}")

urllib.request.urlopen = serve(GOOD)
C.gh = gh_dates(None)
r = C.derived_data_regenerating()
scenario("commit history unreadable -> WARN (freshness unknown, no repair budget)",
         (not r["ok"]) and r["severity"] == C.WARN
         and "cannot read" in r["detail"], f"severity={r['severity']} {r['detail']}")

C.gh = gh_dates([])
r = C.derived_data_regenerating()
scenario("path never committed -> CRITICAL 'carries no timestamp'",
         (not r["ok"]) and r["severity"] == C.CRITICAL
         and "no timestamp" in r["detail"], f"severity={r['severity']} {r['detail']}")

urllib.request.urlopen = real_urlopen
C.gh = real_gh

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
