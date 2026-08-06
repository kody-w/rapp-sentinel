"""Reproduction for rb_content_moving (rapp-sentinel#39).

Proves the check measures roll-up liveness, NOT discussion recency:
  1. discussion silence + fresh roll-up  -> PASS  (the policy case)
  2. stalled roll-up                     -> FAIL  (the outage case)
  3. zero posts analyzed                 -> FAIL  (no-op that exits 0)
  4. unreadable state                    -> FAIL
"""
import io, json, sys, urllib.request
from datetime import datetime, timedelta, timezone
import checks


def serve(payload):
    """Force the check to read a synthetic trending.json."""
    def fake(req, timeout=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return io.BytesIO(body)
    urllib.request.urlopen = fake


def stamp(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


def state(hours_ago, posts=11634):
    return {"_meta": {"materialized_at": stamp(hours_ago),
                      "total_posts_analyzed": posts}}


CASES = [
    ("fresh roll-up, zero new discussions", state(2.0), True),
    ("roll-up just inside window (11.5h)", state(11.5), True),
    ("roll-up stalled (13h)", state(13.0), False),
    ("roll-up long dead (120h, the historical freeze)", state(120.0), False),
    ("roll-up fresh but analyzed zero posts", state(1.0, posts=0), False),
    ("state unreadable", b"{ not json", False),
]

real = urllib.request.urlopen
failures = 0
for name, payload, want_ok in CASES:
    serve(payload)
    try:
        got = checks.rb_content_moving()
    finally:
        urllib.request.urlopen = real
    good = got["ok"] is want_ok
    failures += not good
    print(f"  [{'ok' if good else 'XX'}] {name}\n"
          f"        expected ok={want_ok}  got ok={got['ok']}  {got['detail'][:52]}")

print(f"\n{len(CASES)-failures}/{len(CASES)} scenarios behaved as specified")
sys.exit(1 if failures else 0)
