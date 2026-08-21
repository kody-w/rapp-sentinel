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
from paths import CODE, HOME

STATE = HOME / "state"
LOGS = HOME / "logs"
STOP = HOME / "STOP"
STATE.mkdir(exist_ok=True)
LOGS.mkdir(exist_ok=True)

DEFAULTS = {
    "instance_name": "RAPP Sentinel",
    "level": 1,
    "repair_enabled": True,
    "daily_escalation_budget": 8,
    "issue_cooldown_hours": 4,
    "max_attempts_per_issue": 3,
    "notify": True,
    "notify_handle": "",   # set in config.json; empty disables notify
    "notify_queue_only": False,
    # all: existing operational alerts + reports; art-only: only a fully
    # deployed art receipt; off: no outbound messages.
    "notification_mode": "all",
    "copilot_model": "claude-sonnet-4.6",
    "copilot_timeout_s": 900,
    # Outsider smoke (#5 ask 2): a smoke test is a WRITE (it files a real
    # GitHub issue), so it gets evolve's treatment — its own small budget,
    # never repair's. The live config predates these keys; defaults apply
    # (growth path).
    "daily_smoke_budget": 1,
    "smoke_enabled": True,
    "smoke_interval_hours": 72,
    "smoke_timeout_s": 600,
    "daily_evolve_budget": 2,
    "evolve_interval_hours": 4,
    "evolve_on_degraded": False,
    "evolve_brief": {},
    "creative_state_file": "state/evolve-creative-state.json",
    # Proactive art out of the 15-minute tick (evolve_worker.py). Default off:
    # an existing install must keep the behaviour it has until its operator
    # says otherwise, and the worker needs its own launchd job to be useful.
    "evolve_worker": {"enabled": False},
    "repo_paths": {
        "rappterverse": str(Path.home() / "Documents/GitHub/rappterverse"),
        "rappterbook": str(Path.home() / "Documents/GitHub/rappterbook"),
    },
    "contribution_targets": [
        "https://github.com/kody-w/public-art-collective",
        "https://github.com/kody-w/rappterbook",
        "https://github.com/kody-w/rappterverse",
    ],
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


def instance_name(cfg):
    """Human-facing name for this sentinel instance."""
    return str(cfg.get("instance_name") or "RAPP Sentinel").strip()


def evolve_brief(cfg):
    """Render a structured or free-form standing creative directive."""
    brief = cfg.get("evolve_brief") or {}
    if isinstance(brief, (dict, list)):
        return json.dumps(brief, indent=2, ensure_ascii=False)
    return str(brief).strip()


def log(msg):
    line = f"[{now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    with open(LOGS / f"sentinel-{now():%Y-%m}.log", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def notification_allowed(cfg, kind="operational"):
    mode = str(cfg.get("notification_mode") or "all").strip().lower()
    if mode not in ("all", "art-only", "off"):
        log(f"invalid notification_mode={mode!r}; failing closed to off")
        return False
    if mode == "off":
        return False
    return mode == "all" or kind == "art"


def notify(cfg, text, to=None, rebuild=False, kind="operational",
           attach_report=True):
    """Queue the alert, then try to deliver it.

    Direct osascript hangs under launchd (TCC prompt, background context), so a
    state-change alert sent that way is silently lost — and silence is exactly
    what an alert is supposed to break.

    `to` overrides the default handle for channels that have their own
    destination (the art arm reports to report_number). `rebuild` forces the
    static report to be re-rendered first, for callers that just changed the
    record the report renders — a link to yesterday's evidence attached to
    today's news is worse than no link.
    """
    if not cfg.get("notify") or not notification_allowed(cfg, kind):
        return
    to = to or cfg.get("notify_handle")
    if not to:
        return
    try:
        import outbox
        payload = text
        if attach_report:
            urls = []
            try:
                import standup
                snapshot = standup.portable_snapshot(rebuild=rebuild)
                urls = standup.publish_snapshot(snapshot)
            except Exception as e:
                log(f"static report generation failed: {type(e).__name__}: {e}")
            suffix = ("\n\nStatic HTML report:\n" + "\n".join(urls) if urls
                      else "\n\nStatic HTML report generation failed; alert preserved.")
            payload += suffix
        outbox.enqueue(payload, to)
        if not cfg.get("notify_queue_only"):
            outbox.drain()
    except Exception as e:
        log(f"notify failed: {e}")


def publish_head_hook(cfg):
    """Push the published head somewhere low-noise, throttled (#1 ask 6).

    Chains advance every 15 minutes; serving heads from Pages means either
    stale heads or a commit every tick on a repo whose history is the
    product. The supported low-noise path is an operator-supplied command —
    for this estate, `gh gist edit <id> --filename sentinel-head.json
    "$SENTINEL_HEAD_PATH"` — run after every publish, throttled by
    head_publish_min_minutes.

    Absent config = exactly current behavior: no default command exists ON
    PURPOSE. head_publish_cmd executes operator-supplied shell from
    config.json — the same trust boundary as the installed plists, and a
    default would turn a config file into an execution vector nobody wrote.

    Failure is logged and stamped, never fatal: a broken publish hook must
    not take the anchors, roll call, or the rest of the tick down with it.
    Hard subprocess timeout — never lean on run.sh's 2100s ceiling.
    """
    cmd = cfg.get("head_publish_cmd")
    if not cmd:
        return
    stamp_path = STATE / "head_publish_stamp.json"
    min_minutes = float(cfg.get("head_publish_min_minutes", 10))
    stamp = load_json(stamp_path, {})
    last = stamp.get("at")
    if last:
        try:
            age_m = (now() - datetime.fromisoformat(last)).total_seconds() / 60
            if age_m < min_minutes:
                return
        except Exception:
            pass
    env = dict(os.environ)
    env["SENTINEL_HEAD_PATH"] = str(HOME / "public" / "sentinel-head.json")
    try:
        r = subprocess.run(cmd, shell=True, env=env, capture_output=True,
                           text=True,
                           timeout=int(cfg.get("head_publish_timeout_s", 120)))
        rc, err = r.returncode, (r.stderr or "")[:150]
    except subprocess.TimeoutExpired:
        rc, err = -1, "timed out"
    except Exception as e:
        rc, err = -1, f"{type(e).__name__}: {e}"
    save_json(stamp_path, {"at": now().isoformat(timespec="seconds"),
                           "rc": rc, "error": err if rc != 0 else ""})
    if rc != 0:
        log(f"head publish hook exited {rc}: {err}")


def run_health(receipts=False):
    # CODE, not HOME: the runner is a code artifact. The child inherits this
    # process's environment, so it resolves the same SENTINEL_HOME.
    #
    # 600s, and a timeout is a VERDICT, not a crash. The 180s budget was
    # sized for a dozen cheap checks; the check workload has since grown
    # (page_fetch probes up to 40 targets, cadence_honest reads up to 15
    # documents), so on a slow network a legitimate health run could outlive
    # the budget — and TimeoutExpired used to propagate to the crash
    # handler, which pages. A sentinel that texts "CRASHED" every 15
    # minutes because the network is slow is the cry-wolf failure this repo
    # exists to refuse; a health run we could not finish is "blind", said
    # once per state change like any other degradation (found by the
    # 2026-08-16 review sweep).
    env = dict(os.environ)
    if receipts:
        # A secondary runner (the evolve worker) probes health WHILE the tick
        # is probing it. coverage.json and pagescan.json are receipts, not
        # shared state, and two writers interleaving produce a document that
        # is neither run's answer. Give the secondary its own directory.
        scratch = HOME / "state" / "health-receipts" / "worker"
        scratch.mkdir(parents=True, exist_ok=True)
        env["SENTINEL_HEALTH_RECEIPTS"] = str(scratch)
    try:
        r = subprocess.run([sys.executable, str(CODE / "health.py")],
                           capture_output=True, text=True, env=env,
                           timeout=int(os.environ.get("SENTINEL_HEALTH_TIMEOUT_S", "600")))
        return json.loads(r.stdout)
    except subprocess.TimeoutExpired:
        return {
            "generated": now().isoformat(timespec="seconds"),
            "status": "degraded",
            "checks": [{"id": "health_runtime", "ok": False, "severity": "warn",
                        "detail": "health run exceeded its time budget - "
                                  "verdict unknown, not healthy",
                        "produced_by": "run_health"}],
            "failed": ["health_runtime"],
            "critical": [],
            "summary": "health_runtime: health run exceeded its time budget",
        }


# ── guardrails ──────────────────────────────────────────────────────────────

def within_budget(hist, cfg):
    cutoff = now() - timedelta(hours=24)
    # Skipped entries record a decision NOT to spend a model, so they must not
    # consume the budget they exist to protect (#50). Counting them would let
    # an 8-hour GitHub outage exhaust the day without a single repair attempt.
    #
    # And ONLY repair/diagnose rows count. evolve and smoke carry their own
    # caps and their code comments have always claimed "repair capacity
    # untouched" — but this counter had no mode filter, so every evolve and
    # smoke row silently consumed one of the repair slots it promised not to
    # touch. Two art runs plus a smoke could eat three of the eight slots a
    # 3am outage needed (found by the 2026-08-16 review sweep).
    recent = [h for h in hist
              if datetime.fromisoformat(h["at"]) > cutoff
              and not h.get("skipped")
              and h.get("mode") in (None, "repair", "diagnose")]
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


def evolution_allowed(history, cfg):
    """Rate-limit recurring evolution without repair's lifetime attempt cap."""
    interval_h = max(0.0, float(cfg.get("evolve_interval_hours", 4)))
    latest = None
    for row in history:
        if row.get("mode") != "evolve":
            continue
        try:
            stamp = datetime.fromisoformat(row["at"])
        except (KeyError, TypeError, ValueError):
            continue
        latest = stamp if latest is None or stamp > latest else latest
    if latest is None:
        return True, "first evolution"
    age_h = (now() - latest).total_seconds() / 3600
    if age_h < interval_h:
        return False, f"creative cadence ({age_h:.1f}h of {interval_h:.1f}h)"
    return True, f"creative cadence ready ({age_h:.1f}h)"


def evolution_worker_enabled(cfg):
    """True when proactive art belongs to evolve_worker.py, not to this tick.

    launchd SERIALISES a StartInterval job, so a 15-30 minute model call
    inside the tick is 15-30 minutes with nobody measuring the estate — and
    the next tick does not start early to make up for it. When an instance
    opts in, this loop never invokes Copilot for art at all: the worker has
    its own job, its own lock and its own budget, and health keeps ticking
    beside it. Diagnose and repair are untouched; a critical Rappterbook or
    RAPPterverse failure still escalates on this tick, still honouring
    repair_enabled.
    """
    block = cfg.get("evolve_worker")
    return bool(isinstance(block, dict) and block.get("enabled"))


def ensure_evolution_worker_loaded(cfg, launchctl="/bin/launchctl", plist=None,
                                   platform=None, uid=None):
    """Reconcile an enabled art arm with launchd.

    A disabled launchd override survives plist rewrites and reboots. Merely
    reporting w_evolve_worker every 15 minutes leaves the collective silent
    forever, so the already-running health tick repairs this one local
    supervision invariant deterministically. It never touches watched repos
    and never runs when the worker is disabled in config.
    """
    platform = platform or sys.platform
    if not evolution_worker_enabled(cfg) or platform != "darwin":
        return False
    label = "com.rapp.evolve-worker"
    domain = f"gui/{os.getuid() if uid is None else uid}"
    service = f"{domain}/{label}"
    present = subprocess.run(
        [launchctl, "print", service], capture_output=True, text=True)
    if present.returncode == 0:
        return True
    plist = Path(plist) if plist else (
        Path.home() / "Library" / "LaunchAgents" / f"{label}.plist")
    if not plist.is_file():
        log(f"{label} is enabled but {plist} is missing; rerun install-launchd.sh")
        return False
    subprocess.run([launchctl, "enable", service], capture_output=True, text=True)
    loaded = subprocess.run(
        [launchctl, "bootstrap", domain, str(plist)],
        capture_output=True, text=True)
    if loaded.returncode != 0 and "service already loaded" not in (
            (loaded.stderr or "") + (loaded.stdout or "")).lower():
        log(f"could not bootstrap {label}: "
            f"{(loaded.stderr or loaded.stdout or '').strip()[:240]}")
        return False
    started = subprocess.run(
        [launchctl, "kickstart", service], capture_output=True, text=True)
    if started.returncode != 0:
        log(f"could not kickstart {label}: "
            f"{(started.stderr or started.stdout or '').strip()[:240]}")
        return False
    log(f"restored missing launchd job {label}")
    return True


def evolution_status_allowed(cfg, verdict):
    """Evolve only without critical failures; optionally tolerate warnings."""
    if verdict.get("critical"):
        return False
    return (verdict.get("status") == "healthy"
            or bool(cfg.get("evolve_on_degraded", False)))


def escalation_mode(cfg, level):
    """Keep repair authority independent from proactive contribution."""
    return ("repair" if level >= 2 and cfg.get("repair_enabled", True)
            else "diagnose")


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
7. When two inferences about the same cause have failed, stop inferring and
   measure. Build or run the smallest thing that reports ground truth
   (`python3 diagnose.py` prints the identity, scope and reachability of
   every credential and endpoint, values never printed). An unverified
   diagnosis is a guess wearing a lab coat.
"""

DIAGNOSE_RULES = """
You are the diagnostic arm of an autonomous sentinel. You are READ-ONLY.
Investigate the failure and report the root cause and the exact fix you would
apply. Do NOT edit files, commit, push, or trigger workflows. Read logs, read
code, run read-only commands only.
When two inferences about the same cause have failed, stop inferring and
measure: `python3 diagnose.py` reports the identity, scope and reachability
of every credential and endpoint this loop depends on, values never printed.
"""


def method_change_block(attempt, last_result):
    """The paragraph that breaks a repair out of a wrong-diagnosis loop (#4).

    Three fixes landed in modules that were never on the call path because
    each attempt re-inferred the same cause with more confidence. From the
    second attempt on, the prompt carries the count and the prior result and
    says the quiet part out loud: repeated failure of the same repair is
    evidence the DIAGNOSIS is wrong, not that the fix needs another try.
    Absent from attempt 1 on purpose — diluted into every prompt it would be
    noise, and noise trains the reader to scroll past (#39's lesson, applied
    to prompts).
    """
    if attempt < 2 or not last_result:
        return ""
    return f"""
THIS IS ATTEMPT {attempt}. {attempt - 1} PRIOR ATTEMPT(S) DID NOT CLEAR IT.
The previous attempt ended: {last_result[:300]}
Repeated failure of the same repair is evidence the DIAGNOSIS is wrong — do
NOT retry the previous method. Before proposing anything, measure: run
`python3 diagnose.py` and reproduce the failure with the smallest direct
probe you can build. If your new diagnosis matches the failed attempt's,
that is a finding to report, not a fix to repeat.
"""


# Checks that CANNOT pass while GitHub Actions or Pages is down. Every one of
# them measures something GitHub runs on our behalf, so during an outage their
# failure is a report about GitHub, not about us (#50).
GITHUB_DEPENDENT = {
    "rv_validation", "rv_world_merging", "rv_meaningful_activity", "rv_pr_queue",
    "rb_workflows", "rb_wf_starved", "rb_shards",
    "rb_content_moving", "rb_derived_truth", "rb_rollup_coverage", "eco_sweep",
}


def github_degraded():
    """Components in outage, using the same source as the gh_status check.

    Returns [] when GitHub is healthy AND when the status page cannot be read.
    Unreachable must not imply "outage", or an unreachable status page would
    silently disable escalation entirely -- trading a noisy failure for a
    silent one, which is the trade this repo exists to refuse.
    """
    import urllib.request
    try:
        req = urllib.request.Request(
            "https://www.githubstatus.com/api/v2/components.json",
            headers={"User-Agent": "rapp-sentinel"})
        with urllib.request.urlopen(req, timeout=20) as r:
            comps = json.loads(r.read().decode("utf-8")).get("components", [])
    except Exception as e:
        # Documented posture, but never a silent one: the swallowed reason is
        # the difference between "status page 503'd" and "DNS is broken here",
        # and losing it made the fallback undebuggable (#1, ask 5).
        log(f"github status unreadable ({type(e).__name__}: {str(e)[:60]}) — "
            f"treating as no outage so escalation still runs")
        return []
    return [c["name"] for c in comps
            if c.get("name") in ("Actions", "Pages")
            and c.get("status") in ("major_outage", "partial_outage")]


def escalate(cfg, verdict, failing, mode, attempt=1, last_result=None):
    """Hand the failure to Copilot. Returns (ok, output).

    `attempt` and `last_result` come from the issue record BEFORE this call,
    so a repeat attempt carries its own history — the prompt used to be
    byte-identical on attempts 1 through 3, which is how the same wrong
    diagnosis got three confident retries (#4).
    """
    rules = REPAIR_RULES if mode == "repair" else DIAGNOSE_RULES
    detail = "\n".join(
        f"  - {c['id']} [{c['severity']}]: {c['detail']}"
        for c in verdict["checks"] if not c["ok"]
    )
    prompt = f"""{rules}
{method_change_block(attempt, last_result)}
FAILING HEALTH CHECKS ({verdict['status']}):
{detail}

Repo checkouts (may be stale — always fetch origin/main before reading):
  rappterverse: {cfg['repo_paths']['rappterverse']}
  rappterbook:  {cfg['repo_paths']['rappterbook']}

Check id meanings:
  rv_world_merging  rappterverse stopped merging [state] apply PR commits
  rv_meaningful_activity commits continue but the world lacks fresh, diverse activity
  rv_validation     the Validate Agent Action gate is rejecting work
  rv_pr_queue       the action PR queue is backing up instead of draining
  rb_workflows      listed workflows failed every recent run
  rb_shards         derived cache shards stopped regenerating
  rb_derived_truth  published counters or engagement analytics disagree
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
  collective: {instance_name}
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

PLACES YOU MAY CONTRIBUTE
{contribution_targets}

STANDING DIRECTIVE
{evolve_brief}

CONTINUITY
Your private creative state is at {creative_state_path}. Read it if it exists.
Treat prior public submissions and that bounded state as the input to this
cycle. Update the state atomically before finishing so a decline still becomes
useful input to the next cycle. Never load unbounded logs or histories.

These are the only places this instance authorizes for contribution. Read each
target's own instructions and public participation path. Choose at most ONE
place and ONE contribution this turn. A standing directive may narrow the list
further or declare other repositories read-only evidence. Repairs stay in the
repair path; this is participation, not owner maintenance. Do not take this
prompt's description as authority — each repository is.

A CONTRIBUTION MAY BE A CRITIQUE OF THE PLATFORM ITSELF.
You live in these platforms. If something about the software you run inside
is broken, badly designed, or quietly costing everyone something, saying so
IS a contribution — file it on that repository's tracker like any other
finding. Two things make such a critique worth reading:

  * Evidence. A file, a line, a run id, a timestamp. An assertion you did
    not check is worth less than silence.
  * Nothing is off the table, including your own constraints. The quality
    rails you operate under were written months ago against weaker models,
    and at least one of them has already caused a total outage by rejecting
    correct output. An argument that a rail should be relaxed is a valuable
    finding, not a malfunction.

And the standing rule holds here too: deciding there is nothing worth saying
is a legitimate outcome, recorded as such. Do not manufacture a critique to
have produced something.

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
        instance_name=instance_name(cfg),
        slug=slug,
        role=NB.NEIGHBORS[slug],
        rappid=ids[slug],
        chain_path=str(NB.chain_path(slug)),
        nb_name=NB.NEIGHBORHOOD["name"],
        nb_purpose=NB.NEIGHBORHOOD["purpose"],
        peers=", ".join(f"{k} ({'alive' if v['alive'] else 'stale'})"
                        for k, v in roll.items() if k != slug),
        contribution_targets="\n".join(
            f"  - {target}" for target in cfg.get("contribution_targets", [])),
        evolve_brief=evolve_brief(cfg) or
        "No additional standing directive. Decide from your role and memory.",
        creative_state_path=str(
            HOME / str(cfg.get(
                "creative_state_file", "state/evolve-creative-state.json"))),
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


# ── outsider smoke (#5 ask 2) ───────────────────────────────────────────────

def smoke_rows(platform):
    """Every smoke.* participation row for `platform`, oldest first.

    participation.jsonl is participate.py's own evidence file — every row's
    ok came from re-reading published state, never from a receipt. Reading
    it back is therefore the only way this loop is allowed to decide whether
    a smoke landed (R1).
    """
    path = STATE / "participation.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        if (row.get("platform") == platform
                and str(row.get("kind", "")).startswith("smoke.")):
            rows.append(row)
    return rows


def outsider_smoke(cfg):
    """Exercise the public write path from the tick, budgeted like evolve.

    Issue #5 ask 2: the resident fleet runs inside the platforms with repo
    secrets, so a green heartbeat proves nothing about onboarding. This runs
    participate.py's smoke — GitHub Issue in, published state re-read out —
    on one platform per spend, rotating over every platform participate.py
    names (today: rappterbook and rappterverse, both via the github-issue
    write path).

    Only from the HEALTHY branch: smoking a critical platform measures the
    outage, not the front door. Only at level >= 2: a smoke files a real
    issue, and levels 0-1 promise the sentinel writes nothing.

    EVIDENCE (R1): after the subprocess, participation.jsonl is RE-READ. The
    smoke landed only if a NEW row exists whose kind is smoke.landed with
    ok=True — that row is wait_for_state's verdict from PUBLISHED state. A
    subprocess that exited 0 without writing the log is NO-RECORD, which is
    a failure; the exit code is never believed.

    Honest identity limit: this proves the PUBLIC WRITE PATH works without
    repo access. It cannot prove a true stranger's identity onboarding —
    the platform binds agent_id to the authenticated issue author, so the
    smoke joins as the owner's login walking the stranger's road. And
    rappterverse's consent-delegation (`delegates`, #5 ask 3) stays
    uncovered: participate.py does not implement that path yet.
    """
    if not cfg.get("smoke_enabled", True):
        log("smoke skipped — disabled for this instance")
        return
    if int(cfg.get("level", 1)) < 2:
        log("smoke skipped — level < 2 and a smoke files a real issue")
        return

    import participate
    platforms = sorted(participate.PLATFORMS)
    if not platforms:
        return

    # Budget: smoke's OWN rolling-24h cap, separate from repair's and from
    # evolve's — same reasoning as evolve: overnight, a spent shared budget
    # is a real failure at 3am getting skipped. Skipped rows are decisions
    # not to spend and never consume the budget (#50).
    hist = load_json(STATE / "escalations.json", [])
    cutoff = now() - timedelta(hours=24)
    recent = [h for h in hist
              if datetime.fromisoformat(h["at"]) > cutoff
              and h.get("mode") == "smoke" and not h.get("skipped")]
    cap = int(cfg.get("daily_smoke_budget", 1))
    if len(recent) >= cap:
        log(f"smoke skipped — smoke budget spent ({len(recent)}/{cap}); "
            f"repair capacity untouched")
        return

    turn = load_json(STATE / "smoke_turn.json", {"i": 0})
    platform = platforms[turn["i"] % len(platforms)]

    # Interval gate BEFORE spending: the newest smoke row for this platform,
    # whatever its verdict, marks the last knock on this door. A recently
    # smoked platform advances the turn anyway, so a fresh platform can
    # never wedge the rotation and starve a stale one.
    rows = smoke_rows(platform)
    interval_h = float(cfg.get("smoke_interval_hours", 72))
    if rows:
        age_h = (now() - datetime.fromisoformat(
            rows[-1]["utc"].replace("Z", "+00:00"))).total_seconds() / 3600
        if age_h < interval_h:
            log(f"smoke skipped for {platform}: last smoke {age_h:.1f}h ago "
                f"(interval {interval_h:.0f}h)")
            turn["i"] += 1
            save_json(STATE / "smoke_turn.json", turn)
            return

    issues = load_json(STATE / "issues.json", {})
    key = f"smoke:{platform}"
    allowed, why = issue_allowed(issues, key, cfg)
    if not allowed:
        log(f"smoke skipped for {platform}: {why}")
        # A blocked platform must yield its slot, exactly like a recently
        # smoked one. Without this advance the rotation pinned on a capped
        # platform forever and starved every other door of smokes — until
        # their evidence aged out and staleness paged about platforms with
        # no defect at all (found by the 2026-08-16 review sweep).
        turn["i"] += 1
        save_json(STATE / "smoke_turn.json", turn)
        rec = issues.get(key) or {}
        if "attempt cap" in why and not rec.get("human_notified"):
            # Three failed smokes is a broken front door; re-knocking will
            # not fix it. Say so once, then hold.
            notify(cfg, f"🔴 {instance_name(cfg)}: the outsider path on {platform} "
                        f"failed {cfg['max_attempts_per_issue']} smoke "
                        f"attempts — the front door needs a human.\n"
                        f"last: {str(rec.get('last_result'))[:300]}")
            rec["human_notified"] = True
            issues[key] = rec
            save_json(STATE / "issues.json", issues)
        return

    n0 = len(rows)
    timeout_s = int(cfg.get("smoke_timeout_s", 600))
    log(f"smoke: exercising the outsider path on {platform} "
        f"(budget {len(recent) + 1}/{cap})")
    try:
        r = subprocess.run(
            [sys.executable, str(CODE / "participate.py"),
             "smoke", "--platform", platform],
            capture_output=True, text=True, timeout=timeout_s, cwd=str(HOME))
        receipt = f"exit {r.returncode}"
        out = (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        receipt, out = f"timed out after {timeout_s}s", ""
    except FileNotFoundError:
        receipt, out = "participate.py not found", ""

    # The verdict comes from the evidence file, never the receipt (R1).
    after = smoke_rows(platform)
    new = after[n0:]
    newest = new[-1] if new else None
    if newest and newest.get("kind") == "smoke.landed" and newest.get("ok") is True:
        landed = True
        result = f"LANDED — {str(newest.get('detail'))[:200]}"
    elif newest:
        landed = False
        result = (f"FAILED — {newest.get('kind')}: "
                  f"{str(newest.get('detail'))[:200]} (subprocess {receipt})")
    else:
        landed = False
        result = (f"NO-RECORD — subprocess {receipt} but participation.jsonl "
                  f"gained no row; an exit code is a receipt, not evidence (R1)")

    (LOGS / f"smoke-{platform}-{now():%Y%m%d-%H%M%S}.log").write_text(
        out, encoding="utf-8")
    NB.emit("copilot", "neighbor.acted", {
        "act": "smoke", "platform": platform,
        "result": result[:200], "landed": landed,
    })

    if landed:
        issues.pop(key, None)          # a clean smoke resets the failure count
    else:
        rec = issues.get(key, {"attempts": 0})
        rec["attempts"] += 1
        rec["last_attempt"] = now().isoformat(timespec="seconds")
        rec["last_result"] = result
        issues[key] = rec
    save_json(STATE / "issues.json", issues)

    hist.append({"at": now().isoformat(timespec="seconds"),
                 "key": key, "mode": "smoke", "result": result})
    save_json(STATE / "escalations.json", hist[-200:])
    turn["i"] += 1
    save_json(STATE / "smoke_turn.json", turn)
    log(f"smoke ({platform}): {result}")
    # True = a subprocess actually ran this tick, so the caller can defer
    # evolve and keep the tick under run.sh's ceiling.
    return True


# ── main ────────────────────────────────────────────────────────────────────

def main():
    cfg = config()

    if STOP.exists():
        log("STOP file present — standing down, no checks, no spend.")
        return 0

    ensure_evolution_worker_loaded(cfg)

    def refresh_dashboard():
        """Rebuild the shift report every tick. It only ever reads the chains,
        so it can never disagree with the record it renders."""
        try:
            subprocess.run([sys.executable, str(CODE / "standup.py"), "--hours=14"],
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
        # Publishing is isolated from witnessing: a refused head publish
        # (e.g. a malformed attests_for claim in config, which correctly
        # raises) must not take down the anchor, roll-call and truncation
        # checks below — those are the things this block exists for, and a
        # config typo was silently disabling chain-integrity detection every
        # tick (found by the 2026-08-16 review sweep).
        try:
            NB.publish_head()
            publish_head_hook(cfg)
        except Exception as e:
            log(f"head publish refused/failed: {type(e).__name__}: {e}")
        peers = NB.peer_roll_call()
        if peers:
            save_json(STATE / "peers.json", peers)
            # advancing is three-valued (None on first sight); truthiness
            # would read None as stalled, so the classifier does the identity
            # check on False.
            stalled = NB.stalled_peers(peers)
            gone = [k for k, v in peers.items() if not v.get("reachable")]
            if stalled or gone:
                log(f"peers stalled={stalled} unreachable={gone}")
            # The second opinion across devices: a peer that ticks but whose
            # seated author (or any slug) stopped. Logged per slug so "go see
            # what the other sentinel says" is a grep, not a guess.
            slug_stalls = {k: v["stalled_slugs"] for k, v in peers.items()
                           if v.get("stalled_slugs")}
            if slug_stalls:
                log(f"peer slugs not advancing since last look: {slug_stalls}")
        anchors = NB.check_anchors()
        save_json(STATE / "anchors.json", anchors)
        cut = [k for k, v in anchors.items() if v["truncated"]]
        if cut:
            log(f"TRUNCATION DETECTED: {cut}")
            notify(cfg, f"🔴 {instance_name(cfg)}: chain truncation on {cut}. "
                        f"A watcher's history is shorter than what was witnessed.")
        dead = [k for k, v in roll.items() if not v["alive"] and v["frames"] > 0]
        broken = [k for k, v in roll.items() if not v["chain_ok"]]
        if dead:
            log(f"watchers stale: {dead}")
        if broken:
            # a corrupt chain is more serious than a down platform: it means a
            # watcher's record of itself cannot be trusted
            log(f"WATCHER CHAIN BROKEN: {broken}")
            notify(cfg, f"🔴 {instance_name(cfg)}: chain integrity failure on {broken}. "
                        f"A watcher's own record no longer verifies.")
    except Exception as e:
        log(f"neighborhood record failed: {type(e).__name__}: {e}")

    # notify only when the state actually changes — a watcher that texts every
    # tick gets muted, and a muted watcher is the same as no watcher
    if status != prev_status:
        emoji = {"healthy": "✅", "degraded": "⚠️", "critical": "🔴"}.get(status, "•")
        notify(cfg, f"{emoji} {instance_name(cfg)}: {prev_status or 'unknown'} → {status}\n"
                    f"{verdict['summary'][:600]}")

    level = int(cfg["level"])
    # Smoke on any NON-CRITICAL tick, not only on healthy ones. The original
    # gate was `status == "healthy"`, and it latched: one failed smoke makes
    # w_outsider_smoke warn, a warn makes the verdict degraded, degraded
    # skipped the smoke arm, so the failed row stayed newest forever and the
    # retry/attempt-cap/human-page machinery inside outsider_smoke was
    # unreachable. The #5 rationale — do not smoke a platform mid-outage —
    # only requires excluding CRITICAL states; a warn-degraded estate still
    # has a front door worth knocking on, and the attempt cap bounds the
    # knocking (found live by the 2026-08-16 review sweep: the organism was
    # latched within an hour of the wiring merging).
    smoked = False
    if not verdict["critical"]:
        try:
            smoked = bool(outsider_smoke(cfg))
        except Exception as e:
            log(f"outsider smoke failed: {type(e).__name__}: {e}")
    if level >= 3 and evolution_worker_enabled(cfg):
        # The art arm lives in its own launchd job now (evolve_worker.py). The
        # tick states the delegation and moves on WITHOUT returning: a
        # delegated instance still has to escalate a critical failure on this
        # tick, which the old level-3 early return would have skipped.
        log("evolve delegated to evolve_worker.py — this tick spends no model "
            "on art and keeps measuring")
    elif level >= 3 and evolution_status_allowed(cfg, verdict):
        if smoked:
            # One model-spending arm per tick: a smoke (subprocess up to
            # 600s) stacked on an evolve (up to 1800s) in the same tick
            # walks past run.sh's ceiling and gets the tick killed
            # mid-evolve. Evolve loses nothing — the next healthy tick is
            # 15 minutes away.
            log("evolve deferred - a smoke already ran this tick")
        else:
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
            # Evolution recurs on its own global cadence; repair's lifetime
            # attempt cap must never turn a creative loop off permanently.
            # One neighbor acts per pass in rotation so no one dominates.
            order = list(NB.NEIGHBORS)
            turn = load_json(STATE / "evolve_turn.json", {"i": 0})
            slug = order[turn["i"] % len(order)]
            allowed, why = evolution_allowed(hist, cfg)
            if not okb:
                log(f"evolve skipped — evolve budget spent ({used}/{ev_cap}); repair capacity untouched")
            elif not allowed:
                log(f"evolve skipped: {why}")
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

    if status == "healthy":
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

    # Before spending budget, ask whose outage this is. During today's incident
    # (2026-08-06, Actions + Pages major_outage) this fired four times in four
    # hours -- the first 32 seconds after GitHub opened the incident -- and
    # returned BLOCKED, PARTIAL, BLOCKED, UNKNOWN. Never FIXED, because there
    # was nothing here to fix. Each spawned `copilot --allow-all` against the
    # real repositories.
    #
    # Only skip when EVERY failing critical is explainable by the outage. A
    # real defect that happens during an outage must still escalate, or this
    # trades a noisy failure for a silent one (#50).
    external = [c for c in critical if c in GITHUB_DEPENDENT]
    if len(external) == len(critical):
        degraded = github_degraded()
        if degraded:
            log(f"skipping escalation: {', '.join(degraded)} in outage and all "
                f"failing criticals ({', '.join(sorted(critical))}) depend on it")
            hist.append({"at": now().isoformat(timespec="seconds"),
                         "key": ",".join(sorted(critical)),
                         "result": f"SKIPPED — external outage: {', '.join(degraded)}",
                         "skipped": True})
            save_json(STATE / "escalations.json", hist)
            return 0

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
            notify(cfg, f"🔴 {instance_name(cfg)} needs you.\n'{key}' survived "
                        f"{cfg['max_attempts_per_issue']} automated repairs.\n"
                        f"{verdict['summary'][:400]}")
            prev["escalated_human"] = key
            save_json(STATE / "last_run.json", {**prev, "escalated_human": key})
        return 0

    mode = escalation_mode(cfg, level)
    # Read the record BEFORE escalating so the prompt carries its own attempt
    # history (#4); the bookkeeping below stays where it was, so attempt
    # accounting cannot double-count.
    rec = issues.get(key, {"attempts": 0})
    ok, output = escalate(cfg, verdict, critical, mode,
                          attempt=rec["attempts"] + 1,
                          last_result=rec.get("last_result"))
    verdict_line = result_line(output)

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
            notify(cfg, f"✅ {instance_name(cfg)} repaired: "
                        f"{', '.join(sorted(fixed))}\n{verdict_line[:300]}")
            issues.pop(key, None)
            save_json(STATE / "issues.json", issues)
        else:
            log("repair did not clear the failing checks")
    else:
        notify(cfg, f"⚠️ {instance_name(cfg)} diagnosis ({key}):\n"
                    f"{verdict_line[:500]}")

    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["diagnose"]:
        # Read-only dependency page (#4). Dispatched before the tick so the
        # launchd entrypoint — which passes no arguments — is untouched.
        import diagnose
        sys.exit(diagnose.main())
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
            cfg = config()
            notify(cfg, f"\U0001F534 {instance_name(cfg)} CRASHED: {detail}"[:600])
        except Exception as inner:
            log(f"could not queue the crash alert: {inner}")
        sys.exit(1)
