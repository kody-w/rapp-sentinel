#!/usr/bin/env python3
"""Reproduction for w_neighbor_moving — a seated AI that stopped, and one that
only pretends.

WAR STORY. The children's-book RBox on the mini Rappters seats its author loop
as the `storyteller` neighbor: every cycle appends a rapp/1 frame — written,
declined, or failed. Before this check the sentinel's own doctrine was that
"no check fails on staleness — only chain-break/truncation notifies", which is
right for the default cast (a watcher that stops is a watcher, not the product)
and wrong for a seated worker whose whole job is to keep making things. Two
failures were invisible: (1) the launchd job dies, the chain freezes, and every
verdict stays green because the chain still VERIFIES; (2) worse and quieter,
the model keeps returning drafts lint refuses, the loop keeps ticking, the
chain keeps growing with `storyteller.failed` frames — advancing beautifully,
producing nothing. R2: ran is not worked.

Four legs, each through the real check against a scratch SENTINEL_HOME so
what is measured is the code path health.py runs, not a re-implementation:

  1. CONTROL   — fresh `storyteller.written` frame inside both bars → ok.
  2. BREAK     — the last frame of any kind is older than max_stale_minutes → fail.
  3. BREAK     — recent frames, but every recent one is `failed`/`declined`;
                 the last `written` frame is past max_unworked_minutes → fail,
                 with "ran is not worked" in the detail (the quiet failure).
  4. DECLARED  — no `neighbor_cadence` in config → ok that SAYS nothing is
                 declared (present and unarmed, never a silent green); and an
                 undeclared slug with a frozen chain does not fail (the default
                 cast's behaviour is unchanged — additive molt).

Every leg is a subprocess with SENTINEL_HOME set; the code tree is never
written. Exit 1 on the first deviation.
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

CODE = Path(__file__).resolve().parent
PY = sys.executable
BASE_ENV = {k: v for k, v in os.environ.items() if k != "SENTINEL_HOME"}


def utc(minutes_ago):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def run_check(home):
    env = dict(BASE_ENV, SENTINEL_HOME=str(home))
    code = ("import json, health; print(json.dumps(health._neighbors_moving()))")
    r = subprocess.run([PY, "-c", code], cwd=str(CODE), env=env,
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise SystemExit(f"check raised:\n{r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def emit(home, slug, kind, payload, minutes_ago):
    """Append a frame through the sentinel's own emit(), then re-stamp its utc
    to the past by re-minting through the same code path with a patched clock —
    so the chain still verifies from genesis (the check must never be fooled by
    an edited utc, and this proof must not depend on one)."""
    env = dict(BASE_ENV, SENTINEL_HOME=str(home))
    code = f"""
import json, sys, neighborhood as NB
NB.utc_now = lambda: {utc(minutes_ago)!r}
print(json.dumps(NB.emit(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]))))
"""
    r = subprocess.run([PY, "-c", code, slug, kind, json.dumps(payload)], cwd=str(CODE),
                       env=env, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise SystemExit(f"emit failed:\n{r.stderr}")


def verify(home, slug):
    env = dict(BASE_ENV, SENTINEL_HOME=str(home))
    r = subprocess.run([PY, "-c", "import sys, neighborhood as NB, json; print(json.dumps(NB.verify(sys.argv[1])))",
                        slug], cwd=str(CODE), env=env, capture_output=True, text=True, timeout=120)
    return json.loads(r.stdout.strip().splitlines()[-1])


def make_home(cadence):
    home = Path(tempfile.mkdtemp(prefix="nbmove-"))
    (home / "neighborhood").mkdir()
    cfg = {"level": 0, "notify": False,
           "neighbors": {"storyteller": "the author under test"}}
    if cadence is not None:
        cfg["neighbor_cadence"] = cadence
    (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return home


CAD = {"storyteller": {"max_stale_minutes": 90,
                       "kinds": ["storyteller.written", "storyteller.declined", "storyteller.failed"],
                       "worked_kinds": ["storyteller.written"], "max_unworked_minutes": 480}}


def expect(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


def main():
    print("1. CONTROL — fresh written frame")
    h = make_home(CAD)
    emit(h, "storyteller", "storyteller.written", {"slug": "a", "html_sha256": "x"}, 10)
    r = run_check(h)
    expect(r["ok"] and "worked 10m ago" in r["detail"], f"ok with detail: {r['detail']}")
    ok, _ = verify(h, "storyteller")
    expect(ok, "chain verifies from genesis after the proof's emits")

    print("2. BREAK — the loop died: last frame 200m ago (bar 90m)")
    h = make_home(CAD)
    emit(h, "storyteller", "storyteller.written", {"slug": "a"}, 200)
    r = run_check(h)
    expect(not r["ok"] and "200m ago (bar 90m)" in r["detail"], f"fails: {r['detail']}")
    expect(r["severity"] == "warn", "non-critical (a stopped author is a state change, not an outage)")

    print("3. BREAK — alive but not working: written 600m ago, then only failed/declined frames")
    h = make_home(CAD)
    emit(h, "storyteller", "storyteller.written", {"slug": "a"}, 600)
    for m in (400, 300, 200, 100, 20):
        emit(h, "storyteller", "storyteller.failed", {"slug": "b", "findings": ["blocked words"]}, m)
    emit(h, "storyteller", "storyteller.declined", {"reason": "budget"}, 5)
    r = run_check(h)
    expect(not r["ok"] and "ran is not worked" in r["detail"] and "600m ago" in r["detail"],
           f"fails on R2: {r['detail']}")

    print("3b. BREAK — has spoken, never worked")
    h = make_home(CAD)
    emit(h, "storyteller", "storyteller.declined", {"reason": "nothing queued"}, 5)
    r = run_check(h)
    expect(not r["ok"] and "never worked" in r["detail"], f"fails: {r['detail']}")

    print("4. DECLARED — nothing declared → ok that says so; undeclared frozen slug does not fail")
    h = make_home(None)
    emit(h, "storyteller", "storyteller.written", {"slug": "a"}, 5000)
    r = run_check(h)
    expect(r["ok"] and "no neighbor cadences declared" in r["detail"], f"present and unarmed: {r['detail']}")
    h = make_home({"other-ai": {"max_stale_minutes": 30}})
    emit(h, "storyteller", "storyteller.written", {"slug": "a"}, 5000)
    r = run_check(h)
    expect(not r["ok"] and "other-ai: no frames yet" in r["detail"] and "storyteller" not in r["detail"],
           f"only the declared slug is judged: {r['detail']}")

    print("4b. DECLARED — malformed cadence is ignored, never a crash")
    h = make_home({"storyteller": {"max_stale_minutes": "soon"}, "x": 5, 7: {}})
    r = run_check(h)
    expect(r["ok"] and "no neighbor cadences declared" in r["detail"], f"malformed → unarmed: {r['detail']}")
    print("all legs held")


if __name__ == "__main__":
    main()
