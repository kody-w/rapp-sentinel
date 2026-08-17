#!/usr/bin/env python3
"""prove_shards_regenerating.py — rb_shards now requires parse + count +
measured-cadence freshness, and the old 200-check's blindness is reproduced.

The old check was `url_check` on one shard URL: a bare HTTP 200 — the full
#11 triple in a single line, because reachable is not parseable is not
current. The first scenario below reproduces that blindness against the old
logic (a 200 carrying git conflict markers read as ok), then every branch of
the new check is broken one condition at a time, with a healthy control.

Freshness evidence: individual shard bytes carry no timestamp, but the
generator's own manifest (`state/cache_shards/index.json`) carries
`_meta.generated_at`, written on every run. The check judges that stamp
against 15h — three times the worst regeneration gap measured across
2026-08-15/16 (4.9h).

The rollover scenario below is the regression that took this check critical-red
on 2026-08-17: shards are range-partitioned, so the shard the check used to be
pinned to went immutable when discussion ids crossed 21000, while the generator
kept running perfectly.

Run: python3 prove_shards_regenerating.py   (exit 0 only on all-behaved)
"""

import io
import json
import sys
import urllib.request
import urllib.error
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


def serve(routes):
    """Route by URL fragment. A fragment with no route 404s, so 'the index
    names a shard the site does not serve' is expressible."""
    if not isinstance(routes, dict):
        routes = {"": routes}

    def opener(req, timeout=None):
        url = getattr(req, "full_url", None) or str(req)
        for frag, val in routes.items():
            if frag in url:
                if isinstance(val, Exception):
                    raise val
                return FakeResponse(val)
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    return opener


def index_bytes(generated_h_ago, shards, omit_stamp=False):
    meta = {"shard_size": 250, "total_shards": len(shards)}
    if not omit_stamp:
        meta["generated_at"] = iso_hours_ago(generated_h_ago)
    return json.dumps({"_meta": meta, "shards": shards}).encode()


def shard_bytes(count):
    return json.dumps({"_meta": {"range_start": 21000, "range_end": 21249,
                                 "count": count}, "discussions": []}).encode()


# The live shape on 2026-08-17: 20750 full and frozen, 21000 active.
SHARDS = {"20500": {"file": "shard_20500.json", "count": 218},
          "20750": {"file": "shard_20750.json", "count": 213},
          "21000": {"file": "shard_21000.json", "count": 15}}
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

urllib.request.urlopen = serve({"index.json": index_bytes(3, SHARDS),
                                "shard_21000.json": shard_bytes(15)})
r = C.derived_data_regenerating()
scenario("control: index parses, newest shard served, generated 3h ago -> ok",
         r["ok"] and "shard_21000.json" in r["detail"]
         and "15 discussions" in r["detail"] and "3.0h old" in r["detail"],
         r["detail"])

# The regression this fix exists for: the OLD check pinned shard_20750.json and
# read freshness from that path's commit history. Here that shard is frozen
# (ids rolled past its range_end) while the generator is demonstrably healthy —
# the old logic went critical-red, the new logic must stay green.
urllib.request.urlopen = serve({"index.json": index_bytes(0.5, SHARDS),
                                "shard_21000.json": shard_bytes(15)})
r = C.derived_data_regenerating()
scenario("ROLLOVER: pinned shard frozen but generator fresh -> ok (was false red)",
         r["ok"] and "shard_21000.json" in r["detail"], r["detail"])

urllib.request.urlopen = serve({"index.json": index_bytes(16, SHARDS),
                                "shard_21000.json": shard_bytes(15)})
r = C.derived_data_regenerating()
scenario("stale: 16h against the 15h bar -> CRITICAL",
         (not r["ok"]) and r["severity"] == C.CRITICAL and "stale" in r["detail"],
         f"severity={r['severity']} {r['detail']}")

urllib.request.urlopen = serve({"index.json": CONFLICTED})
r = C.derived_data_regenerating()
scenario("conflict markers -> CRITICAL unparseable (the old check passed this)",
         (not r["ok"]) and r["severity"] == C.CRITICAL
         and "unparseable" in r["detail"], f"severity={r['severity']} {r['detail']}")

# The original outage: the index names a shard the site 404s on.
urllib.request.urlopen = serve({"index.json": index_bytes(1, SHARDS)})
r = C.derived_data_regenerating()
scenario("newest shard 404s -> CRITICAL (the outage this check was built for)",
         (not r["ok"]) and r["severity"] == C.CRITICAL
         and "not served" in r["detail"], f"severity={r['severity']} {r['detail']}")

urllib.request.urlopen = serve({"index.json": index_bytes(1, SHARDS),
                                "shard_21000.json": shard_bytes(0)})
r = C.derived_data_regenerating()
scenario("zero-count newest shard -> CRITICAL 'reports no discussions'",
         (not r["ok"]) and r["severity"] == C.CRITICAL
         and "no discussions" in r["detail"], f"severity={r['severity']} {r['detail']}")

urllib.request.urlopen = serve({"index.json": index_bytes(1, SHARDS),
                                "shard_21000.json": CONFLICTED})
r = C.derived_data_regenerating()
scenario("newest shard unparseable -> CRITICAL",
         (not r["ok"]) and r["severity"] == C.CRITICAL
         and "unparseable" in r["detail"], f"severity={r['severity']} {r['detail']}")

urllib.request.urlopen = serve({"index.json": OSError("connection dropped")})
r = C.derived_data_regenerating()
scenario("unreachable index -> WARN blind-not-broken",
         (not r["ok"]) and r["severity"] == C.WARN
         and "cannot read shard index" in r["detail"],
         f"severity={r['severity']} {r['detail']}")

urllib.request.urlopen = serve({"index.json": index_bytes(1, SHARDS, omit_stamp=True),
                                "shard_21000.json": shard_bytes(15)})
r = C.derived_data_regenerating()
scenario("index carries no generated_at -> CRITICAL 'carries no timestamp'",
         (not r["ok"]) and r["severity"] == C.CRITICAL
         and "no timestamp" in r["detail"], f"severity={r['severity']} {r['detail']}")

urllib.request.urlopen = serve({"index.json": json.dumps({"_meta": {}}).encode()})
r = C.derived_data_regenerating()
scenario("index with no shards map -> CRITICAL unusable (never silently green)",
         (not r["ok"]) and r["severity"] == C.CRITICAL
         and "unusable" in r["detail"], f"severity={r['severity']} {r['detail']}")

urllib.request.urlopen = real_urlopen
C.gh = real_gh

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
