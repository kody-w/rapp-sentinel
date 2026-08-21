#!/usr/bin/env python3
"""principal.py — the sentinel that sits in on other sentinels.

WHY
A neighborhood of watchers can still drift into a room where every watcher is
technically alive and nobody is doing the job they declared. A principal does what
a good school principal does: drops into classrooms unannounced, sits at the back,
and grades what is actually happening against what the teacher said they would
teach — then keeps a report card the whole school can read.

WHAT IT GRADES (deterministic, no model — health checks are free)
  attendance   is the tick landing on its own schedule, and is the record MOVING
               (last_run.json advancing between visits), not just present
  record       do the chains verify from genesis and is nothing truncated
  the job      does the verdict cover what direction.json says it cares about; how
               many checks are red, how many have been red across visits (a standing
               red that is not an accepted decision), any criticals
  honesty      status agrees with the failing list; alerts are not rotting undelivered
  discipline   escalation/evolve/smoke budgets respected today

Every visit is a frame on the principal's own chain (`principal.visited`), a row in
state/observations.jsonl, and a line on state/report-card.json. It texts only when a
grade CHANGES (or a first D/F) — a principal who emails after every visit is muted.

Classrooms are declared in config.json (`classrooms`); local ones are read from
disk, remote ones over ssh with the operator's keys. Visits are random but every
classroom is seen at least once per `visit_everyone_within_hours`.
"""

import hashlib
import json
import os
import random
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from paths import CODE, HOME

STATE = HOME / "state"
OBS = STATE / "observations.jsonl"
CARD = STATE / "report-card.json"
DASH = HOME / "dashboard"
SLUG = "principal"

# what we ask a classroom for — one small program, run there (local or over ssh),
# returning JSON. It never writes anything in the classroom.
GATHER = r'''
import json, os, sys, glob
from pathlib import Path
home = Path(sys.argv[1]).expanduser()
def rj(p, default=None):
    try: return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception: return default
def tail(p, n=40):
    try: return Path(p).read_text(errors="replace").splitlines()[-n:]
    except Exception: return []
st = home / "state"
out = {
  "home": str(home),
  "last_run": rj(st / "last_run.json"),
  "last_verdict": rj(st / "last_verdict.json"),
  "roll_call": rj(st / "roll_call.json"),
  "anchors": rj(st / "anchors.json"),
  "peers": rj(st / "peers.json"),
  "escalations": rj(st / "escalations.json"),
  "outbox_last_drain": rj(st / "outbox-last-drain.json"),
  "outbox_queued": len([l for l in tail(st / "outbox.jsonl", 10000) if l.strip()]),
  "outbox_tail": [l for l in tail(st / "outbox.jsonl", 10000) if l.strip()][:5],
  "direction": rj(home / "direction.json"),
  "config": {k: v for k, v in (rj(home / "config.json") or {}).items()
             if k in ("instance_name","instance_slug","level","notify","daily_escalation_budget","daily_evolve_budget",
                      "daily_smoke_budget","watch_repos","neighbors","neighbor_cadence","smoke_enabled")},
  "unpaired_accepted": None,
  "log_tail": [],
}
logs = sorted(glob.glob(str(home / "logs" / "sentinel-*.log")))
if logs: out["log_tail"] = tail(logs[-1], 30)
print(json.dumps(out))
'''

GRADES = [(90, "A"), (80, "B"), (70, "C"), (55, "D"), (0, "F")]


def utc_now():
    n = datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (n.microsecond // 1000)


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        try:
            return datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        except Exception:
            return None


def load_config():
    try:
        return json.loads((HOME / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_card():
    try:
        return json.loads(CARD.read_text(encoding="utf-8"))
    except Exception:
        return {"schema": "rapp-principal-report-card/1.0", "classrooms": {}}


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ── gathering ────────────────────────────────────────────────────────────────

def gather(room, timeout=60):
    """Sit down at the back of the classroom: fetch its state, read-only."""
    home = room.get("home", "")
    interp = room.get("python") or "python3"          # Windows hosts: "python"
    if room.get("transport", "local") == "local":
        argv = [sys.executable, "-", home]
    else:
        # the program travels on stdin, so the same call works against bash AND PowerShell (no heredoc)
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", room["host"], "%s - %s" % (interp, shlex.quote(home))]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=GATHER)
    except subprocess.TimeoutExpired:
        return None, "unreachable: timed out"
    except FileNotFoundError as e:
        return None, "unreachable: %s" % e
    if r.returncode != 0:
        return None, "unreachable: %s" % (r.stderr or "")[-200:].strip()
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]), None
    except Exception as e:
        return None, "unreadable: %s" % e


# ── the rubric ───────────────────────────────────────────────────────────────

def evaluate(room, snap, previous=None, now=None):
    """Grade one visit. Returns an observation dict. Deterministic; every point
    names its evidence. `previous` is the classroom's last observation (for
    'moving' and 'standing red' judgements)."""
    now = now or datetime.now(timezone.utc)
    pts, notes = {}, []
    interval = float(room.get("interval_s") or 900)

    # ── attendance (25)
    lr = (snap or {}).get("last_run") or {}
    at = parse_ts(lr.get("at"))
    age = (now - at).total_seconds() if at else None
    if age is None:
        pts["attendance"] = 0
        if not (snap or {}).get("direction") and not (snap or {}).get("last_verdict"):
            notes.append("empty classroom — no sentinel found at %s (hatch one)" % (snap or {}).get("home", room.get("home")))
        else:
            notes.append("no last_run.json — never ticked or unreadable")
    else:
        if age <= interval * 1.5:
            pts["attendance"] = 25
        elif age <= interval * 3:
            pts["attendance"] = 12; notes.append("late: last tick %.0fm ago (interval %.0fm)" % (age / 60, interval / 60))
        else:
            pts["attendance"] = 0; notes.append("absent: last tick %.0fm ago (interval %.0fm)" % (age / 60, interval / 60))
        prev_at = parse_ts(((previous or {}).get("evidence") or {}).get("last_run_at"))
        prev_seen = parse_ts((previous or {}).get("utc"))
        if prev_at and at and prev_at == at and prev_seen and (now - prev_seen).total_seconds() > interval * 2:
            pts["attendance"] = 0; notes.append("frozen: last_run.json has not moved since the previous visit")

    # ── record (20)
    roll = (snap or {}).get("roll_call") or {}
    anchors = (snap or {}).get("anchors") or {}
    broken = [k for k, v in roll.items() if isinstance(v, dict) and v.get("chain_ok") is False]
    cut = [k for k, v in anchors.items() if isinstance(v, dict) and v.get("truncated")]
    if not roll:
        pts["record"] = 5; notes.append("no roll_call.json — the record of the record is missing")
    elif broken or cut:
        pts["record"] = 0
        if broken: notes.append("chain integrity failure on %s" % broken)
        if cut: notes.append("truncation detected on %s" % cut)
    else:
        pts["record"] = 20

    # ── the job (25)
    verdict = (snap or {}).get("last_verdict") or {}
    checks = verdict.get("checks") or []
    failed = list(verdict.get("failed") or [c["id"] for c in checks if isinstance(c, dict) and not c.get("ok")])
    critical = list(verdict.get("critical") or [])
    direction = (snap or {}).get("direction") or {}
    cares = direction.get("cares_about") or []
    job = 25
    if not checks:
        job = 5; notes.append("no verdict on record — is anyone teaching?")
    else:
        prev_failed = set(((previous or {}).get("evidence") or {}).get("failed") or [])
        prev_seen = parse_ts((previous or {}).get("utc"))
        standing = sorted(set(failed) & prev_failed) if (prev_seen and (now - prev_seen).total_seconds() >= 6 * 3600) else []
        job -= 8 * len(standing)
        job -= 4 * max(0, len(failed) - len(standing))
        job -= 10 * len(critical)
        if standing: notes.append("standing red (%s) — a permanent red is a decision nobody made: %s" % (len(standing), ", ".join(standing[:6])))
        if critical: notes.append("critical now: %s" % ", ".join(critical[:4]))
        if cares and not any(isinstance(c, dict) and c.get("id") for c in checks):
            job -= 5
    pts["job"] = max(0, job)
    if failed and not notes or (failed and pts["job"] == 25):
        pass

    # ── honesty & delivery (15)
    hon = 15
    status = verdict.get("status") or lr.get("status")
    if status == "ok" and failed:
        hon = 0; notes.append("claims ok while %d check(s) fail — that is the lie this whole thing exists to catch" % len(failed))
    drain = (snap or {}).get("outbox_last_drain") or {}
    queued = int((snap or {}).get("outbox_queued") or 0)
    if queued and drain.get("kept"):
        d_at = parse_ts(drain.get("at"))
        if d_at and (now - d_at).total_seconds() > 6 * 3600 or (drain.get("sent") == 0 and queued >= 3):
            hon = min(hon, 7); notes.append("%d alert(s) queued and not delivered (%s)" % (queued, (drain.get("why") or "")[:60]))
    cfg = (snap or {}).get("config") or {}
    if cfg.get("notify") is False:
        hon = min(hon, 10); notes.append("notify is off — nobody would hear it")
    pts["honesty"] = hon

    # ── discipline (15)
    disc = 15
    esc = (snap or {}).get("escalations") or {}
    today = now.strftime("%Y-%m-%d")
    used = 0
    if isinstance(esc, dict):
        for k, v in esc.items():
            if isinstance(v, dict) and str(v.get("at") or v.get("last") or "").startswith(today):
                used += 1
            elif isinstance(v, list):
                used += sum(1 for x in v if isinstance(x, dict) and str(x.get("at", "")).startswith(today))
    budget = int(cfg.get("daily_escalation_budget") or 8)
    if used > budget:
        disc = 0; notes.append("escalations today %d over budget %d" % (used, budget))
    pts["discipline"] = disc

    score = sum(pts.values())
    grade = next(g for floor, g in GRADES if score >= floor)
    if not notes:
        notes.append("present, on time, record intact, doing the declared job")
    return {
        "utc": utc_now(), "slug": room["slug"], "name": room.get("name") or room["slug"],
        "score": score, "grade": grade, "points": pts, "notes": notes,
        "evidence": {"last_run_at": lr.get("at"), "status": status, "failed": failed, "critical": critical,
                     "checks": len(checks), "broken_chains": broken, "truncated": cut, "outbox_queued": queued,
                     "level": cfg.get("level"), "cares_about": cares[:8]},
    }


# ── the principal's own note (a model, sitting in) ──────────────────────────
#
# The principal is an AI in the neighborhood, not a spreadsheet. After the
# rubric (the floor: cheap, deterministic, never wrong about arithmetic) it
# reads the same evidence a person would — the teacher's declared job, the
# verdict, the roll call, the log — and writes a note in its own voice: what
# works, what fails, the one change it would make, and its own grade. The
# model's grade is recorded NEXT TO the rubric's, never instead of it; when
# they disagree the disagreement is the finding.

CRITIQUE_PROMPT = """You are the Principal of a neighborhood of AI sentinels (rapp-sentinel instances). You have just sat in on one
classroom unannounced. Grade the TEACHER (this sentinel) on how well it does the job IT declared — not on whether the world it
watches is healthy. A sentinel that correctly reports a broken world is doing its job; one that stays green while its own record
stalls, or that has been red for days without anyone deciding, is not.

CLASSROOM: {name} ({slug})
DECLARED JOB (direction.json): {direction}
CONFIG (subset): {config}
LAST TICK: {last_run}
LAST VERDICT (status, failing ids, criticals, summary): {verdict}
ROLL CALL (chains): {roll}
PEERS: {peers}
LOG TAIL:
{log}
RUBRIC (deterministic floor): score {score}/100 grade {grade}; points {points}; notes {notes}
POINTS LOST AND WHY: {lost}

Most sentinels do not need new code. They need REORIENTING: the job they declared has drifted from the job worth doing —
they watch a repo that no longer matters, ignore one that does, run too often to be useful or too rarely to catch anything,
or hold a freedom/budget that makes them unable to act on what they find. Propose a change to the DECLARED JOB
(direction.json) only when the evidence supports it. Never propose changing `boundaries` — those are the owner's.

YOU HAVE NO TOOLS. Reply with ONLY a JSON object:
{{"grade":"A|B|C|D|F","what_works":"<one sentence>","what_fails":"<one sentence, or 'nothing material'>",
 "one_change":"<the single most useful change for this sentinel>","confidence":"high|medium|low",
 "reorient":{{"needed":true|false,"add_cares":["..."],"drop_cares":["..."],"situation_note":"<one sentence to append, or ''>",
 "freedom":<int or null>,"why":"<why this reorientation, citing the evidence above>"}}}}"""


def lost_points(obs):
    """Turn the rubric into a sentence per dimension: what was lost, and the note that explains it."""
    out = []
    for dim, cap in RUBRIC_MAX.items():
        got = (obs.get("points") or {}).get(dim)
        if got is None or got >= cap: continue
        skip = ("present, on time", "principal's note grades")
        why = [n for n in (obs.get("notes") or [])
               if not any(sk in n.lower() for sk in skip)
               and any(k in n.lower() for k in RUBRIC_HINTS.get(dim, []))]
        out.append("%s %d/%d%s" % (dim, got, cap, (" — " + "; ".join(why[:2])) if why else ""))
    return out or ["nothing lost — full marks"]


RUBRIC_MAX = {"attendance": 25, "record": 20, "job": 25, "honesty": 15, "discipline": 15}
RUBRIC_HINTS = {"attendance": ["tick", "absent", "late", "empty classroom", "never ticked"],
                "record": ["chain", "roll", "record", "ledger", "truncat"],
                "job": ["job", "check", "verdict", "watch", "declared", "stale", "frozen"],
                "honesty": ["green", "claim", "unverified", "silent", "notify", "queued", "deliver"],
                "discipline": ["budget", "red", "standing", "deciding", "escalat"]}


def critique(room, snap, obs, cfg, timeout=300):
    """One bounded, tool-less model call. Returns dict or None (never fails a visit)."""
    if cfg.get("critique") is False:
        return None
    import shutil
    if not shutil.which("copilot"):
        return {"error": "copilot CLI not on PATH"}
    v = (snap or {}).get("last_verdict") or {}
    prompt = CRITIQUE_PROMPT.format(
        name=obs["name"], slug=obs["slug"],
        direction=json.dumps((snap or {}).get("direction") or {})[:1800],
        config=json.dumps((snap or {}).get("config") or {})[:900],
        last_run=json.dumps((snap or {}).get("last_run") or {})[:400],
        verdict=json.dumps({"status": v.get("status"), "failed": v.get("failed"), "critical": v.get("critical"),
                            "summary": (v.get("summary") or "")[:700]}),
        roll=json.dumps({k: {kk: vv for kk, vv in (val or {}).items() if kk in ("frames", "chain_ok", "age_minutes", "alive")}
                         for k, val in ((snap or {}).get("roll_call") or {}).items()})[:900],
        peers=json.dumps({k: {kk: vv for kk, vv in (val or {}).items() if kk in ("reachable", "advancing", "stalled_slugs", "age_minutes")}
                          for k, val in ((snap or {}).get("peers") or {}).items()})[:600],
        log="\n".join(((snap or {}).get("log_tail") or [])[-14:])[:1400],
        score=obs["score"], grade=obs["grade"], points=json.dumps(obs["points"]), notes=json.dumps(obs["notes"][:5]),
        lost="; ".join(lost_points(obs)))
    argv = ["copilot", "-p", prompt, "--model", cfg.get("copilot_model") or "claude-opus-5", "--available-tools=",
            "--excluded-tools=create,edit,web_fetch", "--log-level", "none", "--log-dir", str(HOME / "logs" / "copilot")]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=str(HOME), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"error": "model timed out"}
    text = r.stdout or ""
    i = text.find("{")
    while i >= 0:
        j = text.find("}", i)
        try:
            doc = json.loads(text[i:text.rfind("}") + 1]); break
        except Exception:
            i = text.find("{", i + 1); doc = None
    else:
        doc = None
    if not isinstance(doc, dict):
        return {"error": "no JSON note", "raw": text[-300:]}
    doc = {k: doc.get(k) for k in ("grade", "what_works", "what_fails", "one_change", "confidence", "reorient")}
    if not isinstance(doc.get("reorient"), dict):
        doc["reorient"] = {"needed": False}
    doc["model"] = cfg.get("copilot_model") or "claude-opus-5"
    doc["agrees_with_rubric"] = (str(doc.get("grade") or "").upper() == obs["grade"])
    return doc



FEEDBACK_WRITE = r"""
# Runs inside the classroom. Reads one JSON feedback document on stdin and files it where the
# sentinel itself can read it: state/principal-feedback.json (latest) + .jsonl (every visit).
# A reorientation is written NEXT TO direction.json as a proposal — never over it — unless the
# document says apply, in which case boundaries are still left exactly as the owner wrote them.
import json, os, sys
home = os.path.expanduser(sys.argv[1]); st = os.path.join(home, "state"); os.makedirs(st, exist_ok=True)
doc = json.load(sys.stdin)
open(os.path.join(st, "principal-feedback.json"), "w").write(json.dumps(doc, indent=2) + "\n")
with open(os.path.join(st, "principal-feedback.jsonl"), "a") as fh: fh.write(json.dumps(doc) + "\n")
res = {"filed": True, "reoriented": False}
r = doc.get("reorientation") or {}
if r.get("needed"):
    dpath = os.path.join(home, "direction.json")
    try: cur = json.load(open(dpath))
    except Exception: cur = {}
    proposed = dict(cur)
    cares = list(proposed.get("cares_about") or [])
    for c in (r.get("drop_cares") or []):
        if c in cares: cares.remove(c)
    for c in (r.get("add_cares") or []):
        if c and c not in cares: cares.append(c)
    proposed["cares_about"] = cares
    if r.get("situation_note"):
        proposed["situation"] = (proposed.get("situation", "") + "\n\nPrincipal's note (%s): %s" % (doc.get("utc", ""), r["situation_note"])).strip()
    if isinstance(r.get("freedom"), int): proposed["freedom"] = r["freedom"]
    proposed["boundaries"] = cur.get("boundaries", [])          # never the principal's to change
    proposed["reoriented_by"] = "principal"; proposed["reoriented_at"] = doc.get("utc")
    open(os.path.join(st, "principal-reorientation.json"), "w").write(json.dumps(
        {"why": r.get("why"), "proposed_direction": proposed, "current_direction": cur}, indent=2) + "\n")
    if doc.get("apply"):
        if cur: open(dpath + ".bak", "w").write(json.dumps(cur, indent=2) + "\n")
        open(dpath, "w").write(json.dumps(proposed, indent=2) + "\n")
        res["reoriented"] = True
print(json.dumps(res))
"""


def deliver_feedback(room, obs, crit, cfg):
    """Hand the classroom its own report card — the reasons, not just the letter.

    A grade the teacher never sees cannot change anything. This writes the rubric breakdown (what
    was lost and why), the principal's note, and any proposed reorientation into the sentinel's own
    state, where its next tick can read it. Reorientation is a PROPOSAL by default: most sentinels
    do not need new code, they need their declared job pointed at the right thing."""
    doc = {"utc": utc_now(), "from": cfg.get("instance_name") or "The Principal",
           "grade": obs["grade"], "score": obs["score"], "points": obs.get("points"),
           "lost": lost_points(obs), "notes": obs.get("notes"), "evidence": obs.get("evidence"),
           "note": {k: (crit or {}).get(k) for k in ("grade", "what_works", "what_fails", "one_change", "confidence")} if crit else None,
           "reorientation": (crit or {}).get("reorient") or {},
           "apply": str(cfg.get("reorient", "propose")).lower() == "apply"}
    interp = room.get("python") or "python3"
    payload = json.dumps(doc)
    try:
        prog = FEEDBACK_WRITE.replace("sys.argv[1]", repr(room["home"]))
        if room.get("transport") == "local":
            r = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True, timeout=30, input=payload)
        else:
            r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", room["host"],
                                "%s -c %s" % (interp, shlex.quote(prog))],
                               capture_output=True, text=True, timeout=45, input=payload)
        out = json.loads((r.stdout or "{}").strip().splitlines()[-1])
    except Exception as e:
        print("[principal] feedback to %s failed: %s: %s" % (room["slug"], type(e).__name__, e)); return None
    if out.get("reoriented"):
        print("[principal] reoriented %s (direction.json updated; .bak kept)" % room["slug"])
    elif (doc["reorientation"] or {}).get("needed"):
        print("[principal] proposed a reorientation for %s → state/principal-reorientation.json" % room["slug"])
    return out


def unreachable_observation(room, why):
    return {"utc": utc_now(), "slug": room["slug"], "name": room.get("name") or room["slug"], "score": 0, "grade": "F",
            "points": {"attendance": 0, "record": 0, "job": 0, "honesty": 0, "discipline": 0},
            "notes": ["could not sit in: %s" % why], "evidence": {"unreachable": why}}


# ── who to visit ─────────────────────────────────────────────────────────────

def choose_visits(rooms, card, cfg, now=None, seed=None):
    rooms = [r for r in rooms if not r.get("pending_hatch")]
    """Random, but nobody escapes: overdue classrooms first, then a seeded draw."""
    now = now or datetime.now(timezone.utc)
    per_tick = int(cfg.get("visits_per_tick") or 2)
    within_h = float(cfg.get("visit_everyone_within_hours") or 24)
    overdue = []
    for r in rooms:
        last = ((card.get("classrooms") or {}).get(r["slug"]) or {}).get("latest") or {}
        t = parse_ts(last.get("utc"))
        if t is None or (now - t).total_seconds() > within_h * 3600:
            overdue.append(r)
    picks = overdue[:per_tick]
    rest = [r for r in rooms if r not in picks]
    rnd = random.Random(seed if seed is not None else int(hashlib.sha256(now.strftime("%Y%m%d%H%M").encode()).hexdigest()[:8], 16))
    rnd.shuffle(rest)
    picks += rest[:max(0, per_tick - len(picks))]
    return picks


# ── the tick ─────────────────────────────────────────────────────────────────

def record(obs, cfg):
    STATE.mkdir(parents=True, exist_ok=True)
    with open(OBS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obs, ensure_ascii=False) + "\n")
    card = load_card()
    room = card.setdefault("classrooms", {}).setdefault(obs["slug"], {"history": []})
    prev_grade = (room.get("latest") or {}).get("grade")
    room["latest"] = obs
    room["history"] = (room.get("history") or [])[-29:] + [{"utc": obs["utc"], "grade": obs["grade"], "score": obs["score"]}]
    card["updated_at"] = utc_now()
    save_json(CARD, card)
    # the chain: ints/strings only (rapp/1 canonicalization refuses floats)
    try:
        import neighborhood as NB
        pn = obs.get("principal_note") or {}
        NB.emit(SLUG, "principal.visited", {"classroom": obs["slug"], "grade": obs["grade"], "score": int(obs["score"]),
                                              "notes": obs["notes"][:4], "failed": obs["evidence"].get("failed", [])[:8],
                                              "model_grade": str(pn.get("grade") or ""), "one_change": str(pn.get("one_change") or "")[:200]})
    except Exception as e:
        print("[principal] chain emit failed: %s: %s" % (type(e).__name__, e))
    return prev_grade


def notify(cfg, text):
    if not cfg.get("notify") or not cfg.get("notify_handle"):
        return
    try:
        import outbox
        outbox.enqueue(text, cfg["notify_handle"])
    except Exception as e:
        print("[principal] notify failed: %s: %s" % (type(e).__name__, e))


def tick(only=None):
    cfg = load_config()
    rooms = [r for r in (cfg.get("classrooms") or []) if isinstance(r, dict) and r.get("slug")]
    if only:
        rooms = [r for r in rooms if r["slug"] == only]
    card = load_card()
    picks = rooms if only else choose_visits(rooms, card, cfg)
    results = []
    for room in picks:
        if room.get("pending_hatch"):
            print("[principal] %s — no classroom yet (%s)" % (room.get("name") or room["slug"], room.get("pending_hatch_why") or "sentinel not hatched"))
            continue
        snap, err = gather(room)
        prev = ((card.get("classrooms") or {}).get(room["slug"]) or {}).get("latest")
        obs = unreachable_observation(room, err) if err else evaluate(room, snap, previous=prev)
        if not err:
            note = critique(room, snap, obs, cfg)
            if note:
                obs["principal_note"] = note
                if note.get("grade") and not note.get("agrees_with_rubric") and not note.get("error"):
                    obs["notes"].append("principal's note grades %s vs rubric %s: %s" % (note["grade"], obs["grade"], (note.get("what_fails") or "")[:120]))
        prev_grade = record(obs, cfg)
        if not err:
            deliver_feedback(room, obs, obs.get("principal_note"), cfg)   # the teacher gets the reasons, not just the letter
        line = "%s — %s (%d): %s" % (obs["name"], obs["grade"], obs["score"], "; ".join(obs["notes"][:3]))
        print("[principal] " + line)
        if prev_grade != obs["grade"] and (prev_grade is not None or obs["grade"] in ("D", "F")):
            arrow = ("%s→%s" % (prev_grade, obs["grade"])) if prev_grade else obs["grade"]
            notify(cfg, "🏫 The Principal sat in on %s: grade %s. %s" % (obs["name"], arrow, "; ".join(obs["notes"][:2])))
        results.append(obs)
    write_dashboard()
    STATE.mkdir(parents=True, exist_ok=True)
    save_json(STATE / "last_run.json", {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                        "visited": [o["slug"] for o in results],
                                        "grades": {o["slug"]: o["grade"] for o in results}})
    return results


def write_dashboard():
    card = load_card()
    DASH.mkdir(parents=True, exist_ok=True)
    rows = []
    for slug, r in sorted((card.get("classrooms") or {}).items()):
        o = r.get("latest") or {}
        hist = "".join('<span class="g g-%s" title="%s">%s</span>' % (h["grade"], h["utc"][:16], h["grade"]) for h in (r.get("history") or [])[-12:])
        pn = o.get("principal_note") or {}
        note_html = ""
        if pn and not pn.get("error"):
            note_html = ('<div class="note"><b>Principal\'s note (%s, grade %s):</b> works — %s<br>fails — %s<br><i>one change:</i> %s</div>'
                         % (pn.get("model", "model"), pn.get("grade", "?"), pn.get("what_works", ""), pn.get("what_fails", ""), pn.get("one_change", "")))
        rows.append('<tr><td><b>%s</b><br><small>%s</small></td><td class="big g-%s">%s</td><td>%d</td><td>%s</td><td><ul>%s</ul>%s</td><td>%s</td></tr>' % (
            o.get("name", slug), slug, o.get("grade", "?"), o.get("grade", "?"), o.get("score", 0),
            " ".join("%s %s" % (k, v) for k, v in (o.get("points") or {}).items()),
            "".join("<li>%s</li>" % n for n in (o.get("notes") or [])), note_html, hist))
    html = ('<!doctype html><meta charset="utf-8"><title>The Principal — report card</title><style>body{font-family:ui-monospace,Menlo,monospace;background:#07080d;color:#e8e9f0;padding:24px}'
            'table{border-collapse:collapse;width:100%%}td,th{border:1px solid rgba(255,255,255,.1);padding:10px;vertical-align:top;font-size:14px}th{text-align:left;color:#f0b429}'
            '.big{font-size:34px;font-weight:800;text-align:center}.g-A,.g-B{color:#3ddc84}.g-C{color:#f0b429}.g-D,.g-F{color:#f85149}.g{display:inline-block;padding:2px 6px;margin:1px;border:1px solid rgba(255,255,255,.12);border-radius:4px}'
            'ul{margin:0;padding-left:18px}small{color:#8f95ab}.note{margin-top:8px;padding:8px;border-left:3px solid #f0b429;color:#c9cde0;font-size:13px}</style><h1>🏫 The Principal — report card</h1><p>updated %s · every visit is a frame on the principal\'s chain (principal.visited)</p>'
            '<table><tr><th>classroom</th><th>grade</th><th>score</th><th>points</th><th>notes from the back of the room</th><th>last visits</th></tr>%s</table>') % (
        card.get("updated_at", "—"), "".join(rows))
    (DASH / "principal.html").write_text(html, encoding="utf-8")



RELAY = r"""
import fcntl, json, os, sys
home = os.path.expanduser(sys.argv[1]); st = os.path.join(home, "state")
q = os.path.join(st, "outbox.jsonl"); sent = os.path.join(st, "outbox-sent.jsonl")
lock = os.path.join(st, "outbox.lock")
if not os.path.exists(q): print("[]"); raise SystemExit
fh = open(lock, "a+"); fcntl.flock(fh, fcntl.LOCK_EX)
try:
    rows = [l for l in open(q, encoding="utf-8").read().splitlines() if l.strip()]
    open(q, "w").close()                       # queue emptied only after we hold it
    with open(sent, "a", encoding="utf-8") as out:
        for l in rows:
            try: m = json.loads(l)
            except ValueError: continue
            m["relayed_by"] = "principal"; out.write(json.dumps(m, ensure_ascii=False) + "\n")
    print(json.dumps(rows))
finally:
    fcntl.flock(fh, fcntl.LOCK_UN); fh.close()
"""


def relay(only=None, min_age_minutes=None):
    """Be the mouth for a classroom that cannot speak.

    A sentinel whose iMessage send is blocked (no Automation permission, headless context) queues
    alerts forever: the finding exists, nobody hears it. The Principal already reaches every
    classroom to grade it, so it can carry those messages out on its own working mouth. Messages
    are moved (not copied) under the classroom's own outbox lock and marked `relayed_by`, so the
    classroom never re-sends them if its mouth comes back."""
    cfg = load_config()
    thresh = min_age_minutes if min_age_minutes is not None else int(cfg.get("relay_after_minutes", 30))
    carried = 0
    for room in cfg["classrooms"]:
        if only and room["slug"] != only: continue
        if room.get("transport") != "ssh" or room.get("relay") is False: continue
        snap, err = gather(room)
        if not snap: continue
        queued = int(snap.get("outbox_queued") or 0)
        if not queued: continue
        drain = snap.get("outbox_last_drain") or {}
        stuck = bool(drain.get("error") or drain.get("why") not in (None, "", "empty")) or queued > 0
        age = None
        try:
            oldest = min(parse_ts(json.loads(l)["at"]) for l in (snap.get("outbox_tail") or []) if l.strip())
            age = (datetime.now(timezone.utc) - oldest).total_seconds() / 60.0
        except Exception:
            pass
        if age is not None and age < thresh: continue
        if not stuck: continue
        interp = room.get("python") or "python3"
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", room["host"],
                "%s - %s" % (interp, shlex.quote(room["home"]))]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=60, input=RELAY)
            rows = json.loads((r.stdout or "[]").strip().splitlines()[-1])
        except Exception as e:
            print("[principal] relay %s failed: %s: %s" % (room["slug"], type(e).__name__, e)); continue
        for raw in rows:
            try: m = json.loads(raw)
            except ValueError: continue
            notify(cfg, "📨 relayed from %s (its own mouth is blocked):\n%s" % (room["name"], m.get("text", "")[:900]))
            carried += 1
        if rows:
            print("[principal] relayed %d message(s) for %s" % (len(rows), room["slug"]))
    if carried: 
        try:
            import outbox; outbox.drain()
        except Exception as e:
            print("[principal] local drain failed: %s" % e)
    return carried


def memo(day=None):
    """Write the morning memo: what the night found, and what it wants you to decide.

    The report card is a record; the memo is the ask. It names every classroom below a B, the
    chronic findings (the same note on 3+ consecutive visits), and anything queued but unsaid."""
    cfg = load_config(); card = load_card()
    now = datetime.now(timezone.utc); stamp = day or now.strftime("%Y-%m-%d")
    rooms = sorted((card.get("classrooms") or {}).items())
    lines = ["# The Principal's memo — %s" % stamp, "",
             "_Written at %s. Every line below is something a sentinel observed, not something inferred._" % now.strftime("%Y-%m-%d %H:%M UTC"), "",
             "| classroom | grade | score | last seen | the one thing |", "|---|---|---|---|---|"]
    failing, chronic = [], []
    for slug, r in rooms:
        o = r.get("latest") or {}
        note = (o.get("notes") or ["—"])[0]
        lines.append("| %s | **%s** | %s | %s | %s |" % (o.get("name", slug), o.get("grade", "?"), o.get("score", "—"), (o.get("utc") or "—")[:16], note[:110]))
        if o.get("grade") in ("D", "F"): failing.append((slug, o))
        hist = [h for h in (r.get("history") or [])][-3:]
        if len(hist) >= 3:
            common = set(hist[0].get("notes") or [])
            for h in hist[1:]: common &= set(h.get("notes") or [])
            for c in sorted(common): chronic.append((o.get("name", slug), c))
    if failing:
        lines += ["", "## Not teaching", ""]
        for slug, o in failing:
            lines.append("- **%s (%s)** — %s" % (o.get("name", slug), o.get("grade"), "; ".join((o.get("notes") or [])[:3])))
    if chronic:
        lines += ["", "## Chronic — the same finding three visits running", ""]
        for name, c in chronic: lines.append("- **%s** — %s" % (name, c))
    pending = [r for r in (cfg.get("classrooms") or []) if r.get("pending_hatch")]
    if pending:
        lines += ["", "## No classroom yet", ""]
        for r in pending:
            lines.append("- **%s** — %s" % (r.get("name") or r["slug"], r.get("pending_hatch_why") or "no sentinel hatched here"))
    lines += ["", "## Decide today", "",
              "- [ ] " + ("Fix or retire: %s" % ", ".join(o.get("name", s) for s, o in failing) if failing else "Nothing is failing — pick the next molt."),
              "- [ ] Anything above marked chronic needs a change in the sentinel, not another alert.", ""]
    path = STATE / ("memo-%s.md" % stamp)
    path.write_text("\n".join(lines), encoding="utf-8")
    head = "🏫 Principal's memo %s — %s" % (stamp, ", ".join("%s %s" % ((r.get("latest") or {}).get("name", s), (r.get("latest") or {}).get("grade", "?")) for s, r in rooms))
    if failing: head += "\nNot teaching: " + "; ".join("%s (%s)" % (o.get("name", s), (o.get("notes") or ["?"])[0][:70]) for s, o in failing)
    notify(cfg, head[:1200])
    try:
        import outbox; outbox.drain()
    except Exception: pass
    print("[principal] memo → %s" % path)
    return path



HEAL = r"""
# Runs INSIDE a classroom's machine. Diagnoses the three ways a sentinel goes useless and fixes what it can:
#   hung   — a tick still running long past its interval (a slow network read wedges the whole job)
#   absent — no tick for 2+ intervals because launchd's StartInterval stopped firing (it does, after sleep)
#   silent — handled by the Principal's relay, not here
# Every action is reported; nothing is guessed. Read-only when there is nothing wrong.
import glob, json, os, plistlib, re, subprocess, sys, time
home = os.path.expanduser(sys.argv[1]); st = os.path.join(home, "state")
grace = int(sys.argv[2]) if len(sys.argv) > 2 else 20
out = {"home": home, "label": None, "actions": [], "findings": []}

def sh(*a):
    try:
        r = subprocess.run(a, capture_output=True, text=True, timeout=45); return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, "%s: %s" % (type(e).__name__, e)

label, plist_path, plist = None, None, None
for f in glob.glob(os.path.expanduser("~/Library/LaunchAgents/*.plist")):
    try: d = plistlib.load(open(f, "rb"))
    except Exception: continue
    env = d.get("EnvironmentVariables") or {}
    args = " ".join(d.get("ProgramArguments") or [])
    if os.path.realpath(env.get("SENTINEL_HOME", "")) == os.path.realpath(home) or home in args:
        label, plist_path, plist = d.get("Label"), f, d; break
out["label"] = label
uid = os.getuid()

# --- interval the sentinel says it runs at
interval = 900
try: interval = int((json.load(open(os.path.join(home, "config.json"))) or {}).get("interval_s") or 900)
except Exception: pass
if plist and plist.get("StartInterval"): interval = int(plist["StartInterval"])

# --- 1. hung tick: a process whose argv mentions this home/code, alive far longer than one interval
hung = []
rc, ps = sh("ps", "-Ao", "pid,etimes,command")
for line in ps.splitlines()[1:]:
    m = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)", line)
    if not m: continue
    pid, secs, cmd = int(m.group(1)), int(m.group(2)), m.group(3)
    if pid == os.getpid() or "principal" in cmd or " - " in cmd[:40]: continue
    code_hint = os.path.dirname(home.rstrip("/")) 
    if ("sentinel.py" in cmd or "health.py" in cmd or "run.sh" in cmd) and (home in cmd or code_hint in cmd or True):
        if secs > max(grace * 60, interval * 2):
            hung.append((pid, secs, cmd[:80]))
for pid, secs, cmd in hung:
    out["findings"].append("hung %d min: %s" % (secs // 60, cmd))
    rc, _ = sh("kill", "-TERM", str(pid))
    time.sleep(2)
    still = subprocess.run(["ps", "-p", str(pid)], capture_output=True, text=True).returncode == 0
    if still: sh("kill", "-KILL", str(pid))
    out["actions"].append("killed hung pid %d (%d min)" % (pid, secs // 60))

# --- 2. absent: state has not moved for 2+ intervals
def age_minutes(path):
    try: return (time.time() - os.path.getmtime(path)) / 60.0
    except OSError: return None
age = age_minutes(os.path.join(st, "last_run.json"))
stale = age is not None and age > (2 * interval / 60.0)
if stale: out["findings"].append("stale record: %d min (interval %d min)" % (age, interval // 60))

# --- 3. the durable fix: StartInterval does not survive sleep on these machines; a calendar schedule does
if label and plist and plist.get("StartInterval") and stale:
    every = max(5, min(30, int(plist["StartInterval"] // 60)))
    plist.pop("StartInterval", None)
    plist["StartCalendarInterval"] = [{"Minute": m} for m in range(0, 60, every)]
    plist.setdefault("ThrottleInterval", 60)
    plistlib.dump(plist, open(plist_path, "wb"))
    sh("launchctl", "bootout", "gui/%d/%s" % (uid, label))
    rc, o = sh("launchctl", "bootstrap", "gui/%d" % uid, plist_path)
    out["actions"].append("StartInterval → StartCalendarInterval every %d min%s" % (every, "" if rc == 0 else " (bootstrap: %s)" % o.strip()[:80]))

# --- 4. kick it now if it is late
if label and (stale or hung):
    rc, o = sh("launchctl", "kickstart", "-k", "gui/%d/%s" % (uid, label))
    out["actions"].append("kickstarted %s%s" % (label, "" if rc == 0 else " (%s)" % o.strip()[:60]))
elif label is None:
    out["findings"].append("no launchd job found for this home — nothing schedules this sentinel")
print(json.dumps(out))
"""


def heal(only=None, grace_minutes=20):
    """Don't just grade the classroom — fix the ones that stopped teaching.

    A sentinel is useless in exactly three ways: it hangs (a slow network read wedges the tick), it
    goes absent (launchd's StartInterval quietly stops firing after the machine sleeps), or it goes
    silent (its mouth is blocked). This heals the first two on the machine itself, hands the third to
    relay(), and then RE-VISITS to prove the fix took — a heal that isn't verified is just a hope."""
    cfg = load_config(); healed = []
    for room in cfg["classrooms"]:
        if only and room["slug"] != only: continue
        if room.get("heal") is False: continue
        interp = room.get("python") or "python3"
        if room.get("transport") == "local":
            argv = [sys.executable, "-", room["home"], str(grace_minutes)]
        elif str(room.get("os", "")).lower().startswith("win"):
            print("[principal] %s: windows classroom — heal not supported, reporting only" % room["slug"]); continue
        else:
            argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", room["host"],
                    "%s - %s %d" % (interp, shlex.quote(room["home"]), grace_minutes)]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=180, input=HEAL)
            lines = [l for l in (r.stdout or "").strip().splitlines() if l.strip()]
            if not lines:
                print("[principal] heal %s: no reply (%s)" % (room["slug"], ((r.stderr or "").strip().splitlines() or ["silent"])[-1][:90])); continue
            res = json.loads(lines[-1])
        except Exception as e:
            print("[principal] heal %s failed: %s: %s" % (room["slug"], type(e).__name__, e)); continue
        if res.get("actions"):
            print("[principal] healed %s: %s" % (room["slug"], "; ".join(res["actions"])))
            healed.append((room, res))
        elif res.get("findings"):
            print("[principal] %s: %s (no action taken)" % (room["slug"], "; ".join(res["findings"])))
    carried = relay(only=only)
    # prove it: re-visit every classroom we touched and report the grade it earns AFTER the fix
    proved = []
    for room, res in healed:
        time.sleep(5)
        snap, err = gather(room)
        obs = evaluate(room, snap, (load_card().get("classrooms", {}).get(room["slug"]) or {}).get("latest")) if snap else unreachable_observation(room, err)
        record(obs, cfg); proved.append((room, res, obs))
        print("[principal] after heal, %s grades %s (%s)" % (room["slug"], obs["grade"], (obs["notes"] or ["—"])[0][:80]))
    if proved:
        write_dashboard()
        notify(cfg, "🩺 The Principal healed %d classroom(s):\n%s" % (len(proved), "\n".join(
            "%s — %s → now %s" % (r["name"], "; ".join(res["actions"])[:90], obs["grade"]) for r, res, obs in proved)))
        try:
            import outbox; outbox.drain()
        except Exception: pass
    if not proved and not carried:
        print("[principal] nothing to heal — every classroom is teaching")
    return proved


if __name__ == "__main__":
    a = sys.argv[1:] or ["tick"]
    if a[0] == "tick":
        tick(only=(a[1] if len(a) > 1 else None))
    elif a[0] == "card":
        print(json.dumps(load_card(), indent=2))
    elif a[0] == "heal":
        heal(only=(a[1] if len(a) > 1 else None))
    elif a[0] == "relay":
        relay(only=(a[1] if len(a) > 1 else None))
    elif a[0] == "memo":
        memo(day=(a[1] if len(a) > 1 else None))
    elif a[0] == "gather":
        cfg = load_config(); room = next(r for r in cfg["classrooms"] if r["slug"] == a[1])
        snap, err = gather(room); print(json.dumps(snap if snap else {"error": err}, indent=1)[:4000])
    else:
        print(__doc__)
