#!/usr/bin/env python3
"""sentinel.py — the loop that keeps RAPPterverse and Rappterbook honest.

Runs health.py (free), and only spends a model when something is actually
broken. That asymmetry is what makes running forever affordable: a healthy
tick costs a few API calls and exits in seconds.

FREEDOM LADDER — set "level" in config.json, raise it as trust grows:

  0  observe   health check only. Logs, notifies on state change. No model.
  1  diagnose  on failure, Copilot investigates READ-ONLY and explains.
               It may not write, commit, or push.
  2  repair    Copilot may fix and push, but only inside the allowlist and
               only in a throwaway git worktree. Never touches your checkout.
  3  evolve    everything in 2, plus proactive improvement when all is green.

GUARDRAILS (all enforced before a model is ever invoked):
  * kill switch      touch ~/rapp-sentinel/STOP  → next tick exits immediately
  * daily budget     max escalations per rolling 24h
  * per-issue cooldown  the same broken check will not be re-attacked for N hours
  * attempt cap      an issue that resists N repairs is escalated to a human
  * worktree only    repairs never run in a working tree you might be using

State lives in state/. Nothing here writes to the platform repos directly —
Copilot does that, under the constraints in the prompt it is handed.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import neighborhood as NB

HOME = Path(__file__).resolve().parent
STATE = HOME / "state"
LOGS = HOME / "logs"
STOP = HOME / "STOP"
STATE.mkdir(exist_ok=True)
LOGS.mkdir(exist_ok=True)

DEFAULTS = {
    "level": 1,
    "daily_escalation_budget": 8,
    "issue_cooldown_hours": 4,
    "max_attempts_per_issue": 3,
    "notify": True,
    "notify_handle": "",   # set in config.json; empty disables notify
    "copilot_model": "claude-sonnet-4.6",
    "copilot_timeout_s": 900,
    "repo_paths": {
        "rappterverse": str(Path.home() / "Documents/GitHub/rappterverse"),
        "rappterbook": str(Path.home() / "Documents/GitHub/rappterbook"),
    },
}


def now():
    return datetime.now(timezone.utc)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def config():
    cfg = dict(DEFAULTS)
    cfg.update(load_json(HOME / "config.json", {}))
    return cfg


def log(msg):
    line = f"[{now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    with open(LOGS / f"sentinel-{now():%Y-%m}.log", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def notify(cfg, text):
    """Queue the alert, then try to deliver it.

    Direct osascript hangs under launchd (TCC prompt, background context), so a
    state-change alert sent that way is silently lost — and silence is exactly
    what an alert is supposed to break.
    """
    if not cfg.get("notify"):
        return
    to = cfg.get("notify_handle")
    if not to:
        return
    try:
        import outbox
        outbox.enqueue(text, to)
        outbox.drain()
    except Exception as e:
        log(f"notify failed: {e}")


def run_health():
    r = subprocess.run([sys.executable, str(HOME / "health.py")],
                       capture_output=True, text=True, timeout=180)
    return json.loads(r.stdout)


# ── guardrails ──────────────────────────────────────────────────────────────

def within_budget(hist, cfg):
    cutoff = now() - timedelta(hours=24)
    recent = [h for h in hist if datetime.fromisoformat(h["at"]) > cutoff]
    return len(recent) < cfg["daily_escalation_budget"], len(recent)


def issue_allowed(issues, key, cfg):
    """Cooldown + attempt cap, so a stubborn failure cannot burn the budget."""
    rec = issues.get(key)
    if not rec:
        return True, "first attempt"
    if rec["attempts"] >= cfg["max_attempts_per_issue"]:
        return False, f"attempt cap reached ({rec['attempts']}) — needs a human"
    last = datetime.fromisoformat(rec["last_attempt"])
    age_h = (now() - last).total_seconds() / 3600
    if age_h < cfg["issue_cooldown_hours"]:
        return False, f"cooling down ({age_h:.1f}h of {cfg['issue_cooldown_hours']}h)"
    return True, f"retry {rec['attempts'] + 1}"


# ── escalation ──────────────────────────────────────────────────────────────

REPAIR_RULES = """
You are the repair arm of an autonomous sentinel watching two GitHub-native
platforms. You were woken because a health check failed. Fix it or explain
precisely why it cannot be fixed safely.

HARD CONSTRAINTS — these are not suggestions:
1. NEVER work in an existing checkout. Create a fresh `git worktree` off
   origin/main, work there, and remove it when done. The user keeps thousands
   of uncommitted files in their working trees; touching them is destructive.
2. Verify before you claim. Reproduce the failure, apply the fix, and prove it
   with a real run (CI, a test, or a scratch repro). Do not report success off
   a plausible-looking diff.
3. Prefer the smallest change that fixes the root cause. Do not refactor.
4. If the repo documents a convention for this (CLAUDE.md, copilot-instructions,
   a sibling workflow doing it correctly), follow that convention rather than
   inventing one.
5. If the fix needs a credential, a paid resource, or a judgement call about
   product direction, STOP and report instead of guessing.
6. Never commit secrets. Never rewrite history. Never force-push main.
"""

DIAGNOSE_RULES = """
You are the diagnostic arm of an autonomous sentinel. You are READ-ONLY.
Investigate the failure and report the root cause and the exact fix you would
apply. Do NOT edit files, commit, push, or trigger workflows. Read logs, read
code, run read-only commands only.
"""


def escalate(cfg, verdict, failing, mode):
    """Hand the failure to Copilot. Returns (ok, output)."""
    rules = REPAIR_RULES if mode == "repair" else DIAGNOSE_RULES
    detail = "\n".join(
        f"  - {c['id']} [{c['severity']}]: {c['detail']}"
        for c in verdict["checks"] if not c["ok"]
    )
    prompt = f"""{rules}

FAILING HEALTH CHECKS ({verdict['status']}):
{detail}

Repo checkouts (may be stale — always fetch origin/main before reading):
  rappterverse: {cfg['repo_paths']['rappterverse']}
  rappterbook:  {cfg['repo_paths']['rappterbook']}

Check id meanings:
  rv_world_merging  rappterverse stopped merging [state] apply PR commits
  rv_validation     the Validate Agent Action gate is rejecting work
  rv_pr_queue       the action PR queue is backing up instead of draining
  rb_workflows      listed workflows failed every recent run
  rb_shards         derived cache shards stopped regenerating
  ch_*              the RAPP Vision channel is not serving
  w_*               a watcher (brainstem / openrappter / this sentinel) is down

Finish with a single line starting exactly `SENTINEL_RESULT:` followed by
FIXED, PARTIAL, NO_ACTION or BLOCKED, then a one sentence reason.
"""
    cmd = ["copilot", "-p", prompt, "--allow-all", "--model", cfg["copilot_model"]]
    log(f"escalating ({mode}) to copilot for: {', '.join(failing)}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=cfg["copilot_timeout_s"], cwd=str(HOME))
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, f"copilot timed out after {cfg['copilot_timeout_s']}s"
    except FileNotFoundError:
        return False, "copilot CLI not found on PATH"


EVOLVE_SITUATION = """
You are a neighbor in a local twin neighborhood, acting on your own initiative.

This is NOT a task assignment. Nobody has told you what to make or whether to
make anything. You are being handed your situation and your boundaries, and the
discretion to decide what — if anything — you do with them. Declining is a
legitimate outcome and will be recorded as such.

WHO YOU ARE
  neighbor: {slug}
  role:     {role}
  rappid:   {rappid}

YOUR OWN MEMORY
Your rapp/1 frame chain is at {chain_path}. Every frame in it is something you
actually recorded. It is yours; the other neighbors hold their own and cannot
read yours except through the roll call.

WHERE YOU ARE
  neighborhood: {nb_name}
  purpose:      {nb_purpose}
  your peers:   {peers}

Everything the neighborhood watches is currently healthy. Nothing is on fire.

SOMETHING YOU BELONG TO
Your operator is a member of a public commons at
https://github.com/kody-w/public-art-collective — a standalone public RAPP
neighborhood. Read its README and its existing submissions yourself to learn
what membership there involves and what members do. Do not take my word for
any of it; the repo is the authority, not this prompt.

HARD CONSTRAINTS
1. NEVER work in an existing checkout. Use a fresh `git worktree` or a clone in
   /tmp, and clean it up. The operator keeps thousands of uncommitted files.
2. Anything you assert must be checkable by someone else. If you claim
   something is derived from your chain, make that verifiable.
3. Follow the conventions of whatever repo you touch, read from the repo itself.
4. No secrets. No history rewriting. No force-push to main.
5. If you open a PR, say plainly in it what you did and did not do.

Decide for yourself. Then act, end to end, without checking back.

Finish with a single line starting exactly `SENTINEL_RESULT:` followed by
CONTRIBUTED, DECLINED or BLOCKED, then one sentence on what you decided and why.
"""


def evolve(cfg, slug):
    """Level 3. Hand a neighbor its situation and let it decide.

    This is deliberately shaped like the repair path: a situation plus
    boundaries, never a procedure. The repair arm found a git-replica race
    nobody described to it because it was given the failure, not the fix. The
    same has to be true here or the neighborhood is not doing anything — the
    author of the prompt is, wearing three hats.
    """
    ids = NB.identities()
    roll = NB.roll_call()
    prompt = EVOLVE_SITUATION.format(
        slug=slug,
        role=NB.NEIGHBORS[slug],
        rappid=ids[slug],
        chain_path=str(NB.chain_path(slug)),
        nb_name=NB.NEIGHBORHOOD["name"],
        nb_purpose=NB.NEIGHBORHOOD["purpose"],
        peers=", ".join(f"{k} ({'alive' if v['alive'] else 'stale'})"
                        for k, v in roll.items() if k != slug),
    )
    # One model, the best available, for every neighbor. Differences between
    # neighbors must come from role, memory and vantage — not from which
    # model happened to answer, which is variety without meaning.
    model = cfg["copilot_model"]
    log(f"evolve: handing {slug} its situation (model={model})")
    try:
        r = subprocess.run(["copilot", "-p", prompt, "--allow-all", "--model", model],
                           capture_output=True, text=True,
                           timeout=cfg.get("evolve_timeout_s", 1800), cwd=str(HOME))
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return False, "evolve timed out"
    except FileNotFoundError:
        return False, "copilot CLI not found"


def result_line(output):
    for line in reversed(output.splitlines()):
        if line.strip().startswith("SENTINEL_RESULT:"):
            return line.strip()[len("SENTINEL_RESULT:"):].strip()
    return "UNKNOWN (no SENTINEL_RESULT line)"


# ── main ────────────────────────────────────────────────────────────────────

def main():
    cfg = config()

    if STOP.exists():
        log("STOP file present — standing down, no checks, no spend.")
        return 0

    def refresh_dashboard():
        """Rebuild the shift report every tick. It only ever reads the chains,
        so it can never disagree with the record it renders."""
        try:
            subprocess.run([sys.executable, str(HOME / "standup.py"), "--hours=14"],
                           capture_output=True, timeout=180, cwd=str(HOME))
        except Exception as e:
            log(f"dashboard refresh failed: {type(e).__name__}: {e}")

    verdict = run_health()
    status = verdict["status"]
    failing = verdict["failed"]
    prev = load_json(STATE / "last_run.json", {})
    prev_status = prev.get("status")

    log(f"status={status} failing={failing or 'none'}")
    refresh_dashboard()

    # own heartbeat, so a stalled sentinel is detectable by the next run,
    # by the brainstem, and by openrappter
    save_json(STATE / "last_run.json", {
        "at": now().isoformat(timespec="seconds"),
        "status": status,
        "failed": failing,
        "summary": verdict["summary"],
    })
    save_json(STATE / "last_verdict.json", verdict)

    # Record this tick as a rapp/1 frame on the copilot neighbor's chain, and
    # take the roll call. The frame is what makes the sentinel's own history
    # verifiable rather than merely asserted — a chain cannot be quietly
    # rewritten to look healthier than it was.
    try:
        NB.emit("copilot", "sentinel.tick", {
            "status": status,
            "failed": failing,
            "critical": verdict["critical"],
        })
        # Attest the other two watchers from this tick's probe. The sentinel
        # cannot speak *as* them, so these are explicitly attestations, not
        # self-reports — and if the sentinel itself dies, all three chains go
        # stale together, which is exactly the signal the other two need.
        by_id = {c["id"]: c for c in verdict["checks"]}
        for slug, cid in (("openrappter", "w_openrappter"), ("brainstem", "w_brainstem")):
            c = by_id.get(cid)
            if c:
                NB.emit(slug, "watcher.attested",
                        {"alive": bool(c["ok"]), "by": "sentinel", "detail": c["detail"]})
        roll = NB.roll_call()
        save_json(STATE / "roll_call.json", roll)

        # Witness every head outside the chains. A chain cannot detect its own
        # truncation — when payloads repeat, an interior frame can be dropped
        # and the rest resealed, and it verifies clean. The anchor is the
        # outside witness a splice cannot rewrite.
        NB.anchor_heads()
        # Publish our heads so outside neighbors can watch us the same way we
        # watch them. Membership is whoever joins — but joining has to be
        # something you DO, not something you are granted.
        NB.publish_head()
        peers = NB.peer_roll_call()
        if peers:
            save_json(STATE / "peers.json", peers)
            stalled = [k for k, v in peers.items()
                       if v.get("valid") and not v.get("advancing")]
            gone = [k for k, v in peers.items() if not v.get("reachable")]
            if stalled or gone:
                log(f"peers stalled={stalled} unreachable={gone}")
        anchors = NB.check_anchors()
        save_json(STATE / "anchors.json", anchors)
        cut = [k for k, v in anchors.items() if v["truncated"]]
        if cut:
            log(f"TRUNCATION DETECTED: {cut}")
            notify(cfg, f"🔴 Neighborhood Watch: chain truncation on {cut}. "
                        f"A watcher's history is shorter than what was witnessed.")
        dead = [k for k, v in roll.items() if not v["alive"] and v["frames"] > 0]
        broken = [k for k, v in roll.items() if not v["chain_ok"]]
        if dead:
            log(f"watchers stale: {dead}")
        if broken:
            # a corrupt chain is more serious than a down platform: it means a
            # watcher's record of itself cannot be trusted
            log(f"WATCHER CHAIN BROKEN: {broken}")
            notify(cfg, f"🔴 Neighborhood Watch: chain integrity failure on {broken}. "
                        f"A watcher's own record no longer verifies.")
    except Exception as e:
        log(f"neighborhood record failed: {type(e).__name__}: {e}")

    # notify only when the state actually changes — a watcher that texts every
    # tick gets muted, and a muted watcher is the same as no watcher
    if status != prev_status:
        emoji = {"healthy": "✅", "degraded": "⚠️", "critical": "🔴"}.get(status, "•")
        notify(cfg, f"{emoji} RAPP sentinel: {prev_status or 'unknown'} → {status}\n"
                    f"{verdict['summary'][:600]}")

    level = int(cfg["level"])
    if status == "healthy":
        if level >= 3:
            hist = load_json(STATE / "escalations.json", [])
            issues = load_json(STATE / "issues.json", {})
            # Evolve gets its OWN, smaller budget and must never eat into the
            # capacity repair needs. Overnight this is the difference between
            # "it made art all night" and "a real failure at 3am got skipped
            # because the budget was gone".
            cutoff = now() - timedelta(hours=24)
            recent_evolve = [h for h in hist
                             if datetime.fromisoformat(h["at"]) > cutoff
                             and h.get("mode") == "evolve"]
            ev_cap = int(cfg.get("daily_evolve_budget", 2))
            okb, used = len(recent_evolve) < ev_cap, len(recent_evolve)
            # evolve is rate-limited by the same machinery as repair, and takes
            # one neighbor per pass in rotation so no single one dominates
            order = list(NB.NEIGHBORS)
            turn = load_json(STATE / "evolve_turn.json", {"i": 0})
            slug = order[turn["i"] % len(order)]
            allowed, why = issue_allowed(issues, f"evolve:{slug}", cfg)
            if not okb:
                log(f"evolve skipped — evolve budget spent ({used}/{ev_cap}); repair capacity untouched")
            elif not allowed:
                log(f"evolve skipped for {slug}: {why}")
            else:
                ok, out = evolve(cfg, slug)
                line = result_line(out)
                (LOGS / f"evolve-{slug}-{now():%Y%m%d-%H%M%S}.log").write_text(out, encoding="utf-8")
                NB.emit(slug, "neighbor.acted", {"outcome": line[:200], "model": cfg["copilot_model"]})
                rec = issues.get(f"evolve:{slug}", {"attempts": 0})
                rec["attempts"] += 1
                rec["last_attempt"] = now().isoformat(timespec="seconds")
                rec["last_result"] = line
                issues[f"evolve:{slug}"] = rec
                save_json(STATE / "issues.json", issues)
                hist.append({"at": now().isoformat(timespec="seconds"),
                             "key": f"evolve:{slug}", "mode": "evolve", "result": line})
                save_json(STATE / "escalations.json", hist[-200:])
                turn["i"] += 1
                save_json(STATE / "evolve_turn.json", turn)
                log(f"evolve ({slug}): {line}")
                notify(cfg, f"🎨 {slug} acted on its own initiative:\n{line[:400]}")
            return 0
        log("healthy — nothing to do")
        return 0

    if level == 0:
        log("level 0 (observe) — reporting only, not escalating")
        return 0

    # only escalate on critical; warn-level noise does not deserve a model
    critical = verdict["critical"]
    if not critical:
        log(f"degraded but no critical checks ({failing}) — observing")
        return 0

    hist = load_json(STATE / "escalations.json", [])
    okb, used = within_budget(hist, cfg)
    if not okb:
        log(f"daily escalation budget exhausted ({used}/{cfg['daily_escalation_budget']})")
        return 0

    issues = load_json(STATE / "issues.json", {})
    key = ",".join(sorted(critical))
    allowed, why = issue_allowed(issues, key, cfg)
    if not allowed:
        log(f"skipping '{key}': {why}")
        if "attempt cap" in why and prev.get("escalated_human") != key:
            notify(cfg, f"🔴 RAPP sentinel needs you.\n'{key}' survived "
                        f"{cfg['max_attempts_per_issue']} automated repairs.\n"
                        f"{verdict['summary'][:400]}")
            prev["escalated_human"] = key
            save_json(STATE / "last_run.json", {**prev, "escalated_human": key})
        return 0

    mode = "repair" if level >= 2 else "diagnose"
    ok, output = escalate(cfg, verdict, critical, mode)
    verdict_line = result_line(output)

    rec = issues.get(key, {"attempts": 0})
    rec["attempts"] += 1
    rec["last_attempt"] = now().isoformat(timespec="seconds")
    rec["last_result"] = verdict_line
    issues[key] = rec
    save_json(STATE / "issues.json", issues)

    hist.append({"at": now().isoformat(timespec="seconds"), "key": key,
                 "mode": mode, "result": verdict_line})
    save_json(STATE / "escalations.json", hist[-200:])

    (LOGS / f"escalation-{now():%Y%m%d-%H%M%S}.log").write_text(output, encoding="utf-8")
    log(f"escalation finished ({mode}): {verdict_line}")

    # Seal the act into the chain before re-probing. The copilot neighbor found
    # that the only `neighbor.acted` emit site sat inside `if status ==
    # "healthy"`, which is precisely when repair cannot run — so the repair arm
    # was structurally incapable of recording a repair. Its two real fixes lived
    # only in a mutable JSON file, while the art it did not make was sealed into
    # a tamper-evident chain.
    NB.emit("copilot", "neighbor.acted", {
        "act": mode, "issue": key, "result": verdict_line[:400],
        "exit_ok": bool(ok), "attempt": rec["attempts"],
    })

    # re-probe: did the repair actually land?
    if mode == "repair":
        after = run_health()
        fixed = set(critical) - set(after["failed"])
        # The re-probe is the only real evidence a fix landed, and it used to be
        # written to a log line and thrown away, leaving the report to INFER
        # repairs from critical->healthy transitions. For an intermittent check
        # that inference is just a sample of a flapping signal — copilot showed
        # one of last night's two "recoveries" was recorded 85s BEFORE the
        # repair it was credited to had finished.
        NB.emit("copilot", "repair.verified", {
            "issue": key,
            "cleared": sorted(fixed),
            "still_failing": sorted(set(critical) & set(after["failed"])),
            "landed": bool(fixed),
        })
        if fixed:
            log(f"verified fixed: {sorted(fixed)}")
            notify(cfg, f"✅ RAPP sentinel repaired: {', '.join(sorted(fixed))}\n{verdict_line[:300]}")
            issues.pop(key, None)
            save_json(STATE / "issues.json", issues)
        else:
            log("repair did not clear the failing checks")
    else:
        notify(cfg, f"⚠️ RAPP sentinel diagnosis ({key}):\n{verdict_line[:500]}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # A crash used to be silent in every way that matters: the message went
        # to a log file nobody tails, last_run.json kept its old timestamp, and
        # the only staleness check (w_sentinel_fresh) lives INSIDE the process
        # that just died. Verified by breaking checks.py: exit 1, heartbeat
        # frozen at the previous tick, no notification of any kind.
        #
        # So the crash path now does the two things the healthy path does:
        # records that it happened, and says so out loud.
        detail = f"{type(e).__name__}: {e}"
        log(f"sentinel crashed: {detail}")
        try:
            save_json(STATE / "last_run.json", {
                "at": now().isoformat(timespec="seconds"),
                "status": "crashed",
                "failed": ["sentinel_tick"],
                "summary": f"tick raised {detail}"[:400],
            })
        except Exception as inner:
            log(f"could not record the crash heartbeat: {inner}")
        try:
            # enqueue(), not send(): the queue survives a delivery path that is
            # itself broken, which is the likeliest thing to be broken here.
            # Routed through notify() so it still honours cfg["notify"] -- an
            # earlier draft called outbox directly and would have texted from
            # any copy of this repo with notifications deliberately turned off.
            notify(config(), f"\U0001F534 RAPP sentinel CRASHED: {detail}"[:600])
        except Exception as inner:
            log(f"could not queue the crash alert: {inner}")
        sys.exit(1)
