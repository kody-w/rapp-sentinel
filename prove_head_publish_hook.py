#!/usr/bin/env python3
"""prove_head_publish_hook.py — the low-noise head hook publishes, throttles,
survives its own failure, and does nothing when unconfigured.

#1 ask 6: chains advance every 15 minutes and Pages hosting means either
stale heads or a commit per tick. The hook is the supported path — an
operator-supplied command, throttled and hard-timed-out, whose failure is
logged and stamped but never allowed to take the anchors or the roll call
down with it. That last property is the one worth proving: a publish hook
that can kill the tick is a new single point of failure wearing a
convenience feature's clothes.

Run: python3 prove_head_publish_hook.py   (exit 0 only on all-behaved)
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sentinel as S

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


TMP = Path(tempfile.mkdtemp(prefix="prove-head-hook-"))
(TMP / "public").mkdir()
HEAD = TMP / "public" / "sentinel-head.json"
HEAD.write_text('{"schema": "rapp-sentinel-head/1.0", "probe": true}\n',
                encoding="utf-8")
SERVED = TMP / "served.json"
STAMP_DIR = TMP / "state"
STAMP_DIR.mkdir()

real = dict(HOME=S.HOME, STATE=S.STATE)
S.HOME, S.STATE = TMP, STAMP_DIR
STAMP = STAMP_DIR / "head_publish_stamp.json"

CP_CFG = {"head_publish_cmd": f'cp "$SENTINEL_HEAD_PATH" {SERVED}',
          "head_publish_min_minutes": 10}

try:
    # (1) No config: no invocation, no stamp — absent config is exactly
    # current behavior.
    S.publish_head_hook({})
    scenario("(1) unconfigured: nothing runs, nothing is stamped",
             not SERVED.exists() and not STAMP.exists(),
             f"served={SERVED.exists()} stamp={STAMP.exists()}")

    # (2) Configured: the head is published byte-identical, stamped rc=0.
    S.publish_head_hook(CP_CFG)
    same = SERVED.exists() and SERVED.read_bytes() == HEAD.read_bytes()
    st = json.loads(STAMP.read_text(encoding="utf-8"))
    scenario("(2) hook publishes the head byte-identical, stamps rc=0",
             same and st.get("rc") == 0, f"identical={same} stamp={st}")

    # (3) Immediate re-run: throttled — the served copy does not move.
    SERVED.unlink()
    S.publish_head_hook(CP_CFG)
    scenario("(3) immediate re-run is throttled (min_minutes respected)",
             not SERVED.exists(), f"served re-written={SERVED.exists()}")

    # (4) Aged stamp: runs again.
    aged = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(
        timespec="seconds")
    STAMP.write_text(json.dumps({"at": aged, "rc": 0}), encoding="utf-8")
    S.publish_head_hook(CP_CFG)
    scenario("(4) aged stamp: the hook runs again", SERVED.exists(),
             f"served={SERVED.exists()}")

    # (5) A failing command is stamped and logged, and the call RETURNS —
    # the rest of the neighborhood work (anchors, roll call) survives.
    STAMP.write_text(json.dumps({"at": aged, "rc": 0}), encoding="utf-8")
    raised = False
    try:
        S.publish_head_hook({"head_publish_cmd": "exit 7",
                             "head_publish_min_minutes": 10})
    except Exception:
        raised = True
    st = json.loads(STAMP.read_text(encoding="utf-8"))
    scenario("(5) `exit 7` hook: failure stamped rc=7, never raises",
             (not raised) and st.get("rc") == 7,
             f"raised={raised} stamp={st}")
finally:
    S.HOME, S.STATE = real["HOME"], real["STATE"]

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
