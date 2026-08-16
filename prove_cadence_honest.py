#!/usr/bin/env python3
"""prove_cadence_honest.py — a served document's declared refresh cadence is
held against its own newest stamp, with a grammar that parses or refuses.

The target failure is #12's: synthetic_posts.json claiming a 2-minute
refresh while last_updated sat a month old. The bar is max(10x declared,
30min), so cron jitter can never page — an 8-minute-old stamp behind a
2-minute claim passes, a month-old one fires quoting both halves. A cadence
with no readable stamp fires as an unfalsifiable claim, and zero readable
documents is blindness, never cleanliness.

Run: python3 prove_cadence_honest.py   (exit 0 only on all-behaved)
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import checks as C
import pagescan as P

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


def iso_ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# ── pure leg: the grammar parses or refuses, never guesses ──────────────────

scenario("prose: 'refreshed every 2 min via cron' -> 120s",
         P.declared_cadence(
             {"_meta": {"refresh": "refreshed every 2 min via cron"}}) == 120,
         "declared_cadence == 120")

scenario("trivial cron: '*/5 * * * *' -> 300s",
         P.declared_cadence({"schedule": "*/5 * * * *"}) == 300,
         "declared_cadence == 300")

scenario("numberless prose -> None, never a guess",
         P.declared_cadence(
             {"_meta": {"refresh": "refreshed continuously"}}) is None,
         "declared_cadence is None")

scenario("unit-suffixed numeric key: refresh_interval_ms 30000 -> 30s",
         P.declared_cadence({"refresh_interval_ms": 30000}) == 30,
         "declared_cadence == 30")

scenario("unitless numeric 'interval': 30 -> None (seconds? minutes? refuse)",
         P.declared_cadence({"interval": 30}) is None,
         "declared_cadence is None")

scenario("newest_stamp takes the max parseable ISO across known keys",
         P.newest_stamp({"last_updated": "2026-07-09T23:59:11Z",
                         "_meta": {"materialized_at": "2026-08-16T21:03:06Z",
                                   "generated": "not-a-date"}})
         == "2026-08-16T21:03:06Z", "max == materialized_at")

# ── fired legs: the check with injected fetch ───────────────────────────────

DOC_URL = "https://raw.githubusercontent.com/kody-w/rappterbook/main/state/synthetic_posts.json"


def serving(docs):
    """fetch fake: url -> (status, body). Anything unlisted 404s."""
    def fetch(url, method="GET", headers=None):
        if url in docs:
            body = docs[url]
            if isinstance(body, dict):
                body = json.dumps(body)
            return 200, body
        return 404, ""
    return fetch


REAL_HOME, REAL_SEED, REAL_FETCH = C.HOME, C.CADENCE_SEED_DOCS, P.http_fetch
try:
    C.HOME = Path(tempfile.mkdtemp(prefix="prove-cadence-"))
    C.CADENCE_SEED_DOCS = (DOC_URL,)

    # (1) The #12 shape: 2-minute claim, 30-day-old stamp -> fires, quoting
    # BOTH halves of the contradiction.
    P.http_fetch = serving({DOC_URL: {
        "_meta": {"refresh": "refreshed every 2 min via cron",
                  "last_updated": iso_ago(days=30)}}})
    r = C.served_docs_keep_their_cadence_claims()
    scenario("(1) 2-min claim vs 30-day stamp fires at WARN, quoting both",
             (not r["ok"]) and r["severity"] == C.WARN
             and "every 2min" in r["detail"] and "30.0d old" in r["detail"],
             f"severity={r['severity']} {r['detail']}")

    # (2) Same claim, 8-minute-old stamp: inside 10x grace + 30min floor.
    P.http_fetch = serving({DOC_URL: {
        "_meta": {"refresh": "refreshed every 2 min via cron",
                  "last_updated": iso_ago(minutes=8)}}})
    r = C.served_docs_keep_their_cadence_claims()
    scenario("(2) 2-min claim vs 8-min stamp passes — cron jitter never pages",
             r["ok"] and "1 declare a cadence" in r["detail"], r["detail"])

    # (3) A cadence with no readable stamp is an unfalsifiable claim.
    P.http_fetch = serving({DOC_URL: {
        "_meta": {"refresh": "refreshed every 2 min via cron"}}})
    r = C.served_docs_keep_their_cadence_claims()
    scenario("(3) cadence declared, no stamp -> fires as unfalsifiable",
             (not r["ok"]) and r["severity"] == C.WARN
             and "unfalsifiable" in r["detail"]
             and "no readable stamp" in r["detail"],
             f"severity={r['severity']} {r['detail']}")

    # (4) No cadence claims anywhere: ok, with positive counts.
    P.http_fetch = serving({DOC_URL: {
        "_meta": {"last_updated": iso_ago(days=30)},
        "posts": []}})
    r = C.served_docs_keep_their_cadence_claims()
    scenario("(4) no declared cadences -> ok with counts, silence is not judged",
             r["ok"] and "1/1 documents read" in r["detail"]
             and "1 declare none" in r["detail"], r["detail"])

    # (5) Zero readable documents is blindness, never cleanliness (#45).
    P.http_fetch = serving({})
    r = C.served_docs_keep_their_cadence_claims()
    scenario("(5) every document unreadable -> WARN 'blind, not clean'",
             (not r["ok"]) and r["severity"] == C.WARN
             and "blind" in r["detail"],
             f"severity={r['severity']} {r['detail']}")

    # (6) A fresh pagescan receipt ENRICHES the doc set: a .json target the
    # visitor's vantage observed joins the seed set and can fire.
    extra = "https://kody-w.github.io/rappterbook/state/extra.json"
    (C.HOME / "state").mkdir(exist_ok=True)
    (C.HOME / "state" / "pagescan.json").write_text(json.dumps({
        "at": iso_ago(hours=1), "observed_by": "regex", "pages": [],
        "targets": [{"url": extra, "kind": "fetch", "polling": False,
                     "interval_s": None, "first_party": True, "status": 200}],
        "failures": []}), encoding="utf-8")
    P.http_fetch = serving({
        DOC_URL: {"_meta": {"last_updated": iso_ago(minutes=5)}},
        extra: {"_meta": {"refresh": "every 5 minutes",
                          "last_updated": iso_ago(days=3)}}})
    r = C.served_docs_keep_their_cadence_claims()
    scenario("(6) a receipt-observed .json joins the set and fires on drift",
             (not r["ok"]) and "extra.json" in r["detail"]
             and "every 5min" in r["detail"], r["detail"])

    # (7) An unparseable doc is counted, not judged — rb_json_parses' beat.
    P.http_fetch = serving({DOC_URL: "{ not json"})
    (C.HOME / "state" / "pagescan.json").unlink()
    r = C.served_docs_keep_their_cadence_claims()
    scenario("(7) sole doc unparseable -> blind verdict names it, not a lie",
             (not r["ok"]) and "blind" in r["detail"],
             f"severity={r['severity']} {r['detail']}")
finally:
    C.HOME, C.CADENCE_SEED_DOCS, P.http_fetch = REAL_HOME, REAL_SEED, REAL_FETCH

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
