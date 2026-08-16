#!/usr/bin/env python3
"""prove_outsider_smoke.py — the front door is exercised from the tick, and
only the evidence file is ever believed (#5 ask 2, R1 from #2).

Two halves, because the loop has two halves:

  the check   w_outsider_smoke (checks.py) reads participation.jsonl and
              pages, at warn, on absence, on a failed newest smoke, and on
              staleness — never on a receipt.

  the wiring  sentinel.outsider_smoke spends a smoke-only budget, rotates
              platforms, runs participate.py as a subprocess, and decides
              landed/failed by RE-READING participation.jsonl. A subprocess
              that exits 0 without writing the log is NO-RECORD — a
              failure — because an exit code is a receipt, not evidence
              (R1). That sentence is asserted here against a stub that
              exits 0 and writes nothing.

Everything runs against synthetic state in temp dirs; no real smoke is run
and no real GitHub issue is filed.

Run: python3 prove_outsider_smoke.py   (exit 0 only on all-behaved)
"""

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import checks as C
import participate as P
import sentinel as S

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


def utc(hours_ago=0.0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


PLATFORMS = sorted(P.PLATFORMS)
BAR = 2 * C._smoke_interval_hours() + 24

# ════════════════════════════════════════════════════════════════════════════
# Half 1 — the read-only check, against a synthetic participation.jsonl
# ════════════════════════════════════════════════════════════════════════════

CHECK_TMP = Path(tempfile.mkdtemp(prefix="prove-smoke-check-"))
REAL_LOG_GLOBAL = C.PARTICIPATION_LOG
C.PARTICIPATION_LOG = CHECK_TMP / "participation.jsonl"


def write_log(rows):
    C.PARTICIPATION_LOG.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def landed_row(platform, hours_ago=1.0, ok=True):
    return {"utc": utc(hours_ago), "kind": "smoke.landed",
            "platform": platform, "ok": ok,
            "detail": "present, status=active, joined=2026-08-16"}


try:
    write_log([landed_row(p) for p in PLATFORMS])
    r = C.outsider_smoke_exercised()
    scenario("fresh verified smoke on every platform -> passes",
             r["ok"] and f"{len(PLATFORMS)} platform(s)" in r["detail"],
             f"ok={r['ok']} {r['detail']}")

    C.PARTICIPATION_LOG.unlink()
    r = C.outsider_smoke_exercised()
    scenario("no participation log -> 'never exercised' at WARN "
             "(absence pages; a fresh clone is not critical on tick 1)",
             (not r["ok"]) and r["severity"] == C.WARN
             and "never exercised" in r["detail"],
             f"severity={r['severity']} {r['detail']}")

    write_log([landed_row(PLATFORMS[0])])
    r = C.outsider_smoke_exercised()
    scenario("one platform smoked, the others never -> fires naming the "
             "unexercised platform(s)",
             (not r["ok"])
             and all(f"{p}: outsider path never exercised" in r["detail"]
                     for p in PLATFORMS[1:])
             and f"{PLATFORMS[0]}: outsider" not in r["detail"],
             r["detail"])

    write_log([landed_row(p) for p in PLATFORMS] + [{
        "utc": utc(0.5), "kind": "smoke.register_agent",
        "platform": PLATFORMS[0], "ok": False,
        "detail": "issue create failed: HTTP 403: rate limited"}])
    r = C.outsider_smoke_exercised()
    scenario("newest smoke FAILED -> fires quoting the recorded detail",
             (not r["ok"]) and r["severity"] == C.WARN
             and "issue create failed: HTTP 403" in r["detail"],
             r["detail"])

    write_log([landed_row(p, hours_ago=BAR + 10) for p in PLATFORMS])
    r = C.outsider_smoke_exercised()
    scenario(f"stale rows (older than 2x interval + 24h = {BAR:.0f}h) -> "
             f"fires per platform",
             (not r["ok"]) and f"(bar {BAR:.0f}h)" in r["detail"]
             and all(p in r["detail"] for p in PLATFORMS),
             r["detail"])

    write_log([landed_row(p) for p in PLATFORMS[1:]] + [{
        "utc": utc(0.2), "kind": "smoke.register_agent",
        "platform": PLATFORMS[0], "ok": True,
        "detail": "DRY RUN — would open issue"}])
    r = C.outsider_smoke_exercised()
    scenario("an ok row that never reached published-state verification "
             "(dry run / died mid-run) is not 'exercised' — that would be "
             "a receipt (R1)",
             (not r["ok"]) and "never reached published-state" in r["detail"],
             r["detail"])
finally:
    C.PARTICIPATION_LOG = REAL_LOG_GLOBAL

# ════════════════════════════════════════════════════════════════════════════
# Half 2 — the wiring, with participate.py stubbed at the subprocess seam
# ════════════════════════════════════════════════════════════════════════════

CFG = {"level": 3, "daily_smoke_budget": 1, "smoke_interval_hours": 72,
       "smoke_timeout_s": 5, "max_attempts_per_issue": 3,
       "issue_cooldown_hours": 4, "notify": False}

REAL = dict(state=S.STATE, logs=S.LOGS, nb=S.NB, run=subprocess.run)
SPAWNS = []
EMITS = []


class _FakeNB:
    @staticmethod
    def emit(slug, kind, payload):
        EMITS.append((slug, kind, payload))


class _Run:
    returncode, stdout, stderr = 0, "", ""


def fresh_state():
    d = Path(tempfile.mkdtemp(prefix="prove-smoke-wire-"))
    S.STATE = d / "state"
    S.LOGS = d / "logs"
    S.STATE.mkdir()
    S.LOGS.mkdir()
    del SPAWNS[:]
    del EMITS[:]
    return d


def stub_run(writes_rows):
    """A subprocess.run stand-in: exits 0, optionally writing evidence."""
    def fake(cmd, **kwargs):
        SPAWNS.append(cmd)
        if writes_rows:
            with (S.STATE / "participation.jsonl").open("a", encoding="utf-8") as fh:
                for row in writes_rows:
                    fh.write(json.dumps(row) + "\n")
        return _Run()
    return fake


def state(name, default):
    p = S.STATE / name
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


S.NB = _FakeNB
try:
    # ── landed: stub writes the published-state verdict and exits 0 ─────────
    fresh_state()
    first = PLATFORMS[0]
    subprocess.run = stub_run([
        {"utc": utc(), "kind": "smoke.read", "platform": first, "ok": True,
         "detail": "3 agents"},
        {"utc": utc(), "kind": "smoke.register_agent", "platform": first,
         "ok": True, "detail": "https://github.com/x/issues/1"},
        {"utc": utc(), "kind": "smoke.landed", "platform": first, "ok": True,
         "detail": "present, status=active"},
    ])
    try:
        S.outsider_smoke(CFG)
    finally:
        subprocess.run = REAL["run"]
    hist = state("escalations.json", [])
    issues = state("issues.json", {})
    turn = state("smoke_turn.json", {"i": 0})
    scenario("landed smoke: escalations row {mode:'smoke'} written with a "
             "LANDED result, budget spent",
             len(hist) == 1 and hist[0]["mode"] == "smoke"
             and hist[0]["key"] == f"smoke:{first}"
             and hist[0]["result"].startswith("LANDED"),
             f"hist={hist}")
    scenario("landed smoke: rotation turn advanced, no failure attempt "
             "recorded, subprocess ran exactly once",
             turn["i"] == 1 and f"smoke:{first}" not in issues
             and len(SPAWNS) == 1
             and "participate.py" in " ".join(map(str, SPAWNS[0])),
             f"turn={turn} issues={issues} spawns={len(SPAWNS)}")
    scenario("landed smoke: sealed into the chain as neighbor.acted "
             "(additive kind reuse, no new frame kind)",
             len(EMITS) == 1 and EMITS[0][0] == "copilot"
             and EMITS[0][1] == "neighbor.acted"
             and EMITS[0][2].get("landed") is True
             and EMITS[0][2].get("act") == "smoke",
             f"emits={EMITS}")

    # ── exit 0, wrote NOTHING: the exit code is never believed (R1) ─────────
    fresh_state()
    subprocess.run = stub_run([])
    try:
        S.outsider_smoke(CFG)
    finally:
        subprocess.run = REAL["run"]
    hist = state("escalations.json", [])
    issues = state("issues.json", {})
    scenario("exit 0 + no participation row -> recorded as NO-RECORD failure "
             "(R1: a receipt is not evidence — the exit code is never "
             "believed)",
             len(SPAWNS) == 1 and len(hist) == 1
             and hist[0]["result"].startswith("NO-RECORD")
             and "not evidence" in hist[0]["result"],
             f"hist={hist}")
    scenario("exit 0 + no row: the failure counts against the attempt cap",
             issues.get(f"smoke:{first}", {}).get("attempts") == 1
             and issues[f"smoke:{first}"]["last_result"].startswith("NO-RECORD"),
             f"issues={issues}")
    scenario("exit 0 + no row: chain frame records landed=False",
             len(EMITS) == 1 and EMITS[0][2].get("landed") is False,
             f"emits={EMITS}")

    # ── budget already spent: no subprocess is spawned at all ───────────────
    fresh_state()
    (S.STATE / "escalations.json").write_text(json.dumps([
        {"at": S.now().isoformat(timespec="seconds"),
         "key": f"smoke:{first}", "mode": "smoke", "result": "LANDED"}]),
        encoding="utf-8")
    subprocess.run = stub_run([])
    try:
        S.outsider_smoke(CFG)
    finally:
        subprocess.run = REAL["run"]
    scenario("budget already spent in the rolling 24h -> no subprocess "
             "spawned, turn untouched",
             len(SPAWNS) == 0
             and state("smoke_turn.json", {"i": 0})["i"] == 0,
             f"spawns={len(SPAWNS)}")

    # ── a skipped smoke never consumes the budget it protects (#50) ─────────
    fresh_state()
    (S.STATE / "escalations.json").write_text(json.dumps([
        {"at": S.now().isoformat(timespec="seconds"),
         "key": f"smoke:{first}", "mode": "smoke",
         "result": "SKIPPED", "skipped": True}]), encoding="utf-8")
    subprocess.run = stub_run([
        {"utc": utc(), "kind": "smoke.landed", "platform": first, "ok": True,
         "detail": "present"}])
    try:
        S.outsider_smoke(CFG)
    finally:
        subprocess.run = REAL["run"]
    scenario("a skipped row does not consume the smoke budget (#50)",
             len(SPAWNS) == 1, f"spawns={len(SPAWNS)}")

    # ── interval gate: a recently smoked platform is skipped BEFORE spend ───
    fresh_state()
    (S.STATE / "participation.jsonl").write_text(json.dumps(
        {"utc": utc(1.0), "kind": "smoke.landed", "platform": first,
         "ok": True, "detail": "present"}) + "\n", encoding="utf-8")
    subprocess.run = stub_run([])
    try:
        S.outsider_smoke(CFG)
    finally:
        subprocess.run = REAL["run"]
    scenario("platform smoked 1h ago (interval 72h) -> skipped before "
             "spending: no subprocess, no escalations row, turn advanced so "
             "a fresh platform cannot wedge the rotation",
             len(SPAWNS) == 0 and state("escalations.json", []) == []
             and state("smoke_turn.json", {"i": 0})["i"] == 1,
             f"spawns={len(SPAWNS)}")

    # ── attempt cap: three failed smokes stop the knocking, one human page ──
    fresh_state()
    (S.STATE / "issues.json").write_text(json.dumps({
        f"smoke:{first}": {"attempts": 3,
                           "last_attempt": S.now().isoformat(timespec="seconds"),
                           "last_result": "NO-RECORD"}}), encoding="utf-8")
    subprocess.run = stub_run([])
    notified = []
    real_notify = S.notify
    S.notify = lambda cfg, text: notified.append(text)
    try:
        S.outsider_smoke(CFG)
        S.outsider_smoke(CFG)      # second pass must NOT notify again
    finally:
        subprocess.run = REAL["run"]
        S.notify = real_notify
    # The capped platform is never re-knocked and pages exactly once — but it
    # YIELDS its slot: the second tick must smoke the OTHER platform. The
    # first version of this scenario asserted zero spawns across both ticks,
    # which encoded the rotation wedge as a virtue — a capped door starved
    # every other door forever (2026-08-16 review sweep).
    others = [p for p in sorted(__import__("participate").PLATFORMS) if p != first]
    scenario("attempt cap: one human page, capped door never re-knocked, and "
             "the rotation yields — the OTHER platform still gets smoked",
             len(notified) == 1 and first in notified[0]
             and len(SPAWNS) == 1 and others[0] in " ".join(SPAWNS[0])
             and state("smoke_turn.json", {"i": 0})["i"] == 2,
             f"spawns={len(SPAWNS)} notified={len(notified)} "
             f"turn={state('smoke_turn.json', {})}")

    # ── level gate: below 2 the sentinel promises not to write ──────────────
    fresh_state()
    subprocess.run = stub_run([])
    try:
        S.outsider_smoke({**CFG, "level": 1})
    finally:
        subprocess.run = REAL["run"]
    scenario("level < 2 -> no smoke: a smoke files a real issue, and levels "
             "0-1 promise the sentinel writes nothing",
             len(SPAWNS) == 0 and state("escalations.json", []) == [],
             f"spawns={len(SPAWNS)}")
finally:
    S.STATE, S.LOGS, S.NB = REAL["state"], REAL["logs"], REAL["nb"]
    subprocess.run = REAL["run"]

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
