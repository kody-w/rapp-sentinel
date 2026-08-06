"""Reproduction for rb_rollup_coverage (rapp-sentinel#41).

Covers the case PR #40 missed: a FROZEN QUANTITY behind a FRESH TIMESTAMP.
rb_content_moving passes green in every scenario below where the timestamp is
fresh, which is why coverage is measured separately.
"""
import io, json, sys, urllib.request
from datetime import datetime, timedelta, timezone
import checks

real_urlopen, real_gh = urllib.request.urlopen, checks.gh


def rig(analyzed, actual, hours_ago=0.5):
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {"_meta": {"materialized_at": stamp,
                         "total_posts_analyzed": analyzed}}
    urllib.request.urlopen = lambda req, timeout=None: io.BytesIO(
        json.dumps(payload).encode())
    checks.gh = lambda *a, **k: {
        "data": {"repository": {"discussions": {"totalCount": actual}}}}


CASES = [
    ("full coverage",                        (15754, 15754), True),
    ("quiet platform, both counts still",    (15754, 15754), True),
    ("mild materialization lag (95%)",       (14966, 15754), True),
    ("exactly at threshold (90%)",           (14179, 15754), True),
    ("TODAY: frozen 11634 vs 15754 (73.8%)", (11634, 15754), False),
    ("the 8000 scrape cap (50.8%)",          ( 8000, 15754), False),
]

failures = 0
for name, (analyzed, actual), want_ok in CASES:
    rig(analyzed, actual)
    try:
        got = checks.rb_rollup_covers_corpus()
        # Prove the sibling check is blind to the same state.
        sib = checks.rb_content_moving()
    finally:
        urllib.request.urlopen, checks.gh = real_urlopen, real_gh
    good = got["ok"] is want_ok
    failures += not good
    print(f"  [{'ok' if good else 'XX'}] {name}\n"
          f"        coverage ok={got['ok']:<5} {got['detail'][:44]}\n"
          f"        rb_content_moving says ok={sib['ok']} <- blind when timestamp is fresh")

print(f"\n{len(CASES)-failures}/{len(CASES)} scenarios behaved as specified")
sys.exit(1 if failures else 0)
