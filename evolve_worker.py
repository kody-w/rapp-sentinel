#!/usr/bin/env python3
"""evolve_worker.py — the proactive art arm, moved out of the 15-minute tick.

WHY THIS FILE EXISTS
The health sentinel runs every 15 minutes and launchd SERIALISES a
StartInterval job (see run.sh): a tick that spends 15-30 minutes inside a
model is a tick during which nothing measures the estate, and the next tick
does not start early to make up for it. Level 3 put a 1800s `copilot` call
inside that tick, so proactive art and the heartbeat were competing for the
same wall clock. One of them always lost, and it was never the art.

So the two jobs are two jobs. The tick keeps measuring; this worker — its own
launchd job, its own lock, its own budget — does the slow creative work
alongside it. `evolve_worker.enabled` in config.json is what moves the art arm
out of the tick; absent, nothing changes for an existing install.

WHAT THIS WORKER IS ALLOWED TO BELIEVE
Almost nothing the model says. The model works in a temporary clone this
worker created, and it may not commit, push, open a PR, or merge. It produces
one new `submissions/<slug>/` folder and a private next-state file. Everything
after that — the deterministic gate, the branch, the commit, the PR, the file
scope of that PR as GitHub reports it, the squash merge, and the re-read of
origin/main afterwards — is done HERE, by code, from evidence. A
`SENTINEL_RESULT: CONTRIBUTED` line is a claim, not a receipt (R1), and this
worker never records a contribution it did not re-read from the merged repo.

FAIL-CLOSED, EVERYWHERE
  * a ledger that will not parse aborts the run; it never resets spend
  * any critical health check aborts, at start and again before every remote
    write and before the merge
  * a degraded estate evolves only when EVERY failing id is named in
    `degraded_allowlist`; there is no blanket "evolve while degraded" switch
  * the gate rejects on the first thing it cannot prove
  * the temporary workspace is removed on every path out
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

import neighborhood as NB
import sentinel
import subsentinels as SS
from paths import HOME

STATE = HOME / "state"
LOGS = HOME / "logs"
STOP = HOME / "STOP"

LOCK_PATH = STATE / "evolve-worker.lock"
HISTORY_PATH = STATE / "evolve-worker-history.json"
TURN_PATH = STATE / "evolve-worker-turn.json"
ALERT_PATH = STATE / "evolve-worker-alerts.json"
STATUS_PATH = STATE / "evolve-worker-status.json"
TRANSACTION_PATH = STATE / "evolve-worker-transaction.json"

# Every model process this pass started, so a timeout, a SIGTERM or a crash
# can take the whole tree down instead of orphaning a 30-minute model run and
# whatever it spawned. The lock is released only after this list is empty.
_LIVE = []
_LIVE_LOCK = threading.Lock()

HISTORY_KEEP = 400

# The worker's own config block. Every key falls back to the level-3 key the
# in-tick arm already used, so an install that only flips `enabled` keeps the
# cadence, budget and timeout it has been running with.
WORKER_DEFAULTS = {
    "enabled": False,
    "repo": "kody-w/public-art-collective",
    "base_branch": "main",
    "roles": [],                     # empty = the whole neighborhood roster
    "degraded_allowlist": [],        # exact check ids; no wildcards on purpose
    "workspace_root": "state/evolve-workspaces",
    "branch_prefix": "art",
    "clone_depth": 50,
    "git_author_name": "RAPP Sentinel evolve worker",
    "git_author_email": "rapp-sentinel@users.noreply.github.com",
    "git_timeout_s": 600,
    "gh_timeout_s": 300,
    "max_piece_bytes": 51200,
    "max_meta_bytes": 262144,
    "max_state_bytes": 262144,
    "notify_declines": False,
    # Bounded sub-sentinel fan-out (subsentinels.py). Off by default; see
    # FANOUT_DEFAULTS there for the caps every key inherits.
    "fanout": {},
}

# ── the submission protocol, as code ────────────────────────────────────────
# Mirrors specs/SUBMISSION_PROTOCOL.md in public-art-collective. Written out
# here because a gate that reads its rules from the repo it is gating can be
# talked out of them by the same PR it is judging.
SUBMISSION_SCHEMA = "rapp-art-submission/1.0"
REQUIRED_META_KEYS = ("schema", "title", "slug", "contributor", "kind",
                      "submitted_at", "remix_of", "license")
ALLOWED_LICENSES = ("CC0-1.0",)
KIND_EXTENSIONS = {"svg": ".svg", "md": ".md", "txt": ".txt", "json": ".json"}
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SLUG_MAX = 48

# ── the dada cycle invariants ───────────────────────────────────────────────
# The point of the cycle block is that the SEARCH is evidence, not decoration:
# a fixed candidate count per round is the difference between "it explored"
# and "it wrote down that it explored".
SCORE_DIMENSIONS = ("absurdity", "novelty", "craft", "coherence",
                    "provocation", "restraint")
CANDIDATES_PER_ROUND = 10
MIN_ROUNDS = 1
MAX_ROUNDS = 5
SCORE_MIN = 0
SCORE_MAX = 10

OUTCOME_CONTRIBUTED = "contributed"
OUTCOME_DECLINED = "declined"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_FAILED = "failed"
OUTCOME_REJECTED = "rejected"
OUTCOME_ABORTED = "aborted"
OUTCOME_FANOUT = "fanout-failed"

# Only a verified merge is allowed to wear the paintbrush. Every other outcome
# gets a shape a human reads as "something did not happen" (#7).
SUCCESS_PREFIX = "\U0001F3A8"     # 🎨


class LedgerError(RuntimeError):
    """Durable state exists but cannot be trusted. Never overwrite it."""


class GateError(RuntimeError):
    """The model's output failed a deterministic check. Never publish it."""


class AbortError(RuntimeError):
    """Health said stop, mid-flight. Undo what is undoable, record the truth."""


class CommandError(RuntimeError):
    """A git/gh command failed or timed out."""


# ── the process tree ────────────────────────────────────────────────────────

def track(proc):
    with _LIVE_LOCK:
        _LIVE.append(proc)
    return proc


def untrack(proc):
    with _LIVE_LOCK:
        try:
            _LIVE.remove(proc)
        except ValueError:
            pass


def live_processes():
    with _LIVE_LOCK:
        return [p for p in _LIVE if p.poll() is None]


def kill_tracked(grace=5.0):
    """Terminate every model process this pass started, and their children.

    Each is spawned with start_new_session, so one signal per process GROUP
    reaches the grandchildren a model shelled into. Returns how many were
    still alive when we started, for the caller to report honestly.
    """
    with _LIVE_LOCK:
        procs = list(_LIVE)
    alive = [p for p in procs if p.poll() is None]
    for proc in alive:
        SS._kill_group(proc, grace)
    for proc in procs:
        untrack(proc)
    return len(alive)


_STOPPING = threading.Event()


def _handle_signal(signum, frame):
    """A stopped worker must not leave a model running.

    launchd's ceiling, a reboot, or an operator's kill all arrive here. Kill
    the tree first, then let the normal unwind delete the workspace and
    release the lock — in that order, so nothing that is still running can
    outlive the lock that says a cycle is in flight (#4).
    """
    _STOPPING.set()
    killed = kill_tracked()
    log(f"signal {signum} — terminated {killed} live model process tree(s)")
    raise KeyboardInterrupt(f"signal {signum}")


def install_signal_handlers():
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass          # not the main thread (tests) — nothing to install


# ── logging ─────────────────────────────────────────────────────────────────

def log(msg):
    line = f"[{sentinel.now().isoformat(timespec='seconds')}] evolve-worker: {msg}"
    print(line, flush=True)
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / f"evolve-worker-{sentinel.now():%Y-%m}.log", "a",
                  encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ── config ──────────────────────────────────────────────────────────────────

def worker_config(cfg):
    """The worker block, merged over defaults and the level-3 fallbacks."""
    block = cfg.get("evolve_worker")
    block = dict(block) if isinstance(block, dict) else {}
    merged = dict(WORKER_DEFAULTS)
    merged["interval_hours"] = float(cfg.get("evolve_interval_hours", 4))
    merged["daily_budget"] = int(cfg.get("daily_evolve_budget", 2))
    merged["timeout_s"] = int(cfg.get("evolve_timeout_s", 1800))
    merged["model"] = cfg.get("copilot_model", "claude-sonnet-4.6")
    merged["creative_state_file"] = str(
        cfg.get("creative_state_file", "state/evolve-creative-state.json"))
    for key, value in block.items():
        merged[key] = value
    return merged


def worker_enabled(cfg):
    """True when this instance delegates proactive art to this worker."""
    block = cfg.get("evolve_worker")
    return bool(isinstance(block, dict) and block.get("enabled"))


def roles_for(wcfg):
    """Rotation order, restricted to neighbors that actually have a chain."""
    declared = wcfg.get("roles") or []
    roles = [r for r in declared if r in NB.NEIGHBORS]
    return roles or list(NB.NEIGHBORS)


# ── durable state: strict on the way in, atomic on the way out ──────────────

def strict_load(path, default, expect=(dict, list)):
    """Load durable state, or refuse to run.

    sentinel.load_json answers "unreadable" with the default, which is right
    for caches and catastrophic for spend ledgers: a truncated
    evolve-worker-history.json would read as "no evolutions today" and hand
    the day's budget back every 30 minutes, forever. Absent is zero; corrupt
    is unknown; unknown must stop the run (#2).
    """
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except OSError as e:
        raise LedgerError(f"{p.name} is unreadable ({type(e).__name__}: {e})")
    if not raw.strip():
        raise LedgerError(f"{p.name} is empty — a truncated ledger is not 'no spend'")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise LedgerError(f"{p.name} is corrupt ({e}) — refusing to reset spend")
    if not isinstance(data, expect):
        raise LedgerError(f"{p.name} holds a {type(data).__name__}, not the expected shape")
    return data


def atomic_write_json(path, data):
    """Replace a ledger in one step, durably.

    A half-written history is a corrupt history, and a corrupt history stops
    the worker (see strict_load) — so the write path has to be all-or-nothing
    even across a power cut: temp file in the same directory, fsync, rename,
    fsync the directory.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def load_history(path=None):
    rows = strict_load(path or HISTORY_PATH, [], expect=list)
    for row in rows:
        if not isinstance(row, dict) or "at" not in row:
            raise LedgerError("history holds a row without a timestamp — "
                              "refusing to guess how much has been spent")
        try:
            datetime.fromisoformat(str(row["at"]))
        except ValueError:
            raise LedgerError(f"history row has an unparseable timestamp "
                              f"({row['at']!r}) — refusing to reset spend")
    return rows


def save_history(rows, path=None):
    atomic_write_json(path or HISTORY_PATH, rows[-HISTORY_KEEP:])


# ── guardrails ──────────────────────────────────────────────────────────────

def acquire_lock(path=None):
    """Nonblocking exclusive lock. Returns an fd, or None if held elsewhere.

    flock, not a pid file: the kernel releases it when the process dies, so a
    worker killed mid-run (launchd ceiling, reboot, SIGKILL) cannot leave a
    stale lock that wedges the art arm until someone notices.
    """
    path = Path(path or LOCK_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({
            "pid": os.getpid(),
            "at": sentinel.now().isoformat(timespec="seconds"),
        }).encode("utf-8") + b"\n")
        os.fsync(fd)
    except OSError:
        pass
    return fd


def release_lock(fd):
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def spend_rows(history, hours=24):
    """Rows that consumed a model in the window. Skips never count (#50)."""
    cutoff = sentinel.now() - timedelta(hours=hours)
    out = []
    for row in history:
        if row.get("skipped"):
            continue
        if row.get("mode") != "evolve":
            continue
        if datetime.fromisoformat(str(row["at"])) > cutoff:
            out.append(row)
    return out


def within_budget(history, wcfg):
    cap = int(wcfg.get("daily_budget", 2))
    used = len(spend_rows(history))
    return used < cap, used, cap


def cadence_ready(history, wcfg):
    """One global creative cadence across every role, like the in-tick arm."""
    interval_h = max(0.0, float(wcfg.get("interval_hours", 4)))
    latest = None
    for row in history:
        if row.get("skipped") or row.get("mode") != "evolve":
            continue
        stamp = datetime.fromisoformat(str(row["at"]))
        latest = stamp if latest is None or stamp > latest else latest
    if latest is None:
        return True, "first evolution"
    age_h = (sentinel.now() - latest).total_seconds() / 3600
    if age_h < interval_h:
        return False, f"creative cadence ({age_h:.1f}h of {interval_h:.1f}h)"
    return True, f"creative cadence ready ({age_h:.1f}h)"


# Checks a degraded_allowlist may never silence. Everything else about the
# allowlist is an operator's judgement call; these two are not, because both
# mean "this loop cannot see or cannot speak", and art made while the alarm
# is broken is exactly the silence this repo exists to refuse:
#
#   alert_delivery  queued, unverified or dead-lettered alerts — the estate
#                   cannot reach a human. Since 232ce7e an unverifiable send
#                   is an explicit failure rather than an optimistic success,
#                   so this check now says out loud what used to be lost. Art
#                   published while it is red would be a system cheerfully
#                   texting about paintings from a channel that cannot text.
#   health_runtime  the health run did not finish. The verdict is unknown,
#                   and "unknown" must never be allowlisted into "fine".
NEVER_ALLOWLISTABLE = frozenset({"alert_delivery", "health_runtime"})

# health.py emits exactly these three. Anything else is a verdict this worker
# cannot reason about, and an unreadable verdict is never a green light.
KNOWN_STATUSES = frozenset({"healthy", "degraded", "critical"})


def health_gate(wcfg, verdict, phase="start"):
    """Health decides whether art may proceed. Returns (ok, reason).

    Critical is always a stop. Degraded is a stop UNLESS every failing id is
    named in `degraded_allowlist` — an explicit list of known-noisy checks,
    not `evolve_on_degraded`, which said "any degradation is fine" and would
    have let this worker push art through an estate that was quietly on fire
    in a way nobody had looked at yet (#3). A small set of ids refuses to be
    allowlisted at all (NEVER_ALLOWLISTABLE).
    """
    # A verdict is only evidence if it has the shape of a verdict. Reading
    # missing keys as empty defaults meant "{}" — a crashed health run, a
    # truncated json read, a mock nobody finished — scored as "healthy, no
    # criticals, nothing failing" and unlocked the model (#8).
    if not isinstance(verdict, dict):
        return False, f"health at {phase} returned {type(verdict).__name__}, not a verdict"
    missing = [k for k in ("status", "failed", "critical") if k not in verdict]
    if missing:
        return False, (f"health verdict at {phase} is missing "
                       f"{', '.join(missing)} — shape unknown, not healthy")
    status = verdict.get("status")
    if status not in KNOWN_STATUSES:
        return False, (f"health verdict at {phase} reports status "
                       f"{status!r}, which this worker does not understand")
    if not isinstance(verdict.get("failed"), list) or not isinstance(
            verdict.get("critical"), list):
        return False, (f"health verdict at {phase} has non-list failed/critical "
                       f"fields — shape unknown, not healthy")
    critical = list(verdict.get("critical") or [])
    if critical:
        return False, f"critical checks failing at {phase}: {', '.join(sorted(critical))}"
    failing = list(verdict.get("failed") or [])
    if status != "healthy" and not failing:
        return False, (f"health verdict at {phase} says {status!r} but names no "
                       f"failing check — the two disagree, so neither is trusted")
    if not failing:
        return True, f"healthy at {phase}"
    unskippable = sorted(set(failing) & NEVER_ALLOWLISTABLE)
    if unskippable:
        return False, (f"{', '.join(unskippable)} failing at {phase} and cannot "
                       f"be allowlisted — a loop that cannot report must not "
                       f"publish")
    allowed = {str(x) for x in (wcfg.get("degraded_allowlist") or [])}
    unlisted = sorted(set(failing) - allowed)
    if unlisted:
        return False, (f"degraded at {phase} with unlisted failing checks: "
                       f"{', '.join(unlisted)}")
    return True, (f"degraded at {phase}, every failing check allowlisted: "
                  f"{', '.join(sorted(failing))}")


# ── the prompt ──────────────────────────────────────────────────────────────

WORKER_SITUATION = """
You are a neighbor in a local twin neighborhood, acting on your own initiative.

This is NOT a task assignment. Nobody has told you what to make or whether to
make anything. You are being handed your situation and your boundaries, and the
discretion to decide what — if anything — you do with them. Declining is a
legitimate outcome and will be recorded as such.

WHO YOU ARE
  collective: {instance_name}
  neighbor:   {slug}
  role:       {role}
  rappid:     {rappid}

WHERE YOU ARE
  neighborhood: {nb_name}
  purpose:      {nb_purpose}
  your peers:   {peers}

The neighborhood is healthy enough to make things right now. That is the only
reason you were woken.

YOUR WORKSPACE — THE ONLY PLACE YOU MAY WRITE
  {workspace}/out/           where your submission goes (see below)
  {workspace}/context/       what already exists, read-only to you in practice
  {workspace}/state-in.json  your private creative state from last cycle
                             ({state_in_note})
  {workspace}/state-out.json the private next state you must write

There is NO repository here. You have no clone of {repo}, no .git directory,
no git and no gh — by construction, not by instruction. Your file tools are
rooted at {workspace} and reach nothing else on this machine.

YOU MAY NOT PUBLISH. THIS IS ABSOLUTE.
You have no shell, no git, no gh and no network tool, so there is nothing to
run — and nothing you write can become a commit, a branch, a remote, a pull
request or a merge. Leave two files on disk; that is the whole job.

A controller — code, not a model — reads what you leave behind, checks it
against the submission protocol deterministically, copies the two files into a
clone you never see, and only then creates the branch, the commit, the pull
request and the merge.

WHAT TO LEAVE BEHIND
Exactly one new directory: {workspace}/out/submissions/<your-slug>/
containing exactly two files:

  meta.json     the protocol record (schema below)
  piece.<ext>   the work itself; ext is one of .svg .md .txt .json and MUST
                match meta.kind; at most {max_piece_kb} KB

Put nothing else under {workspace}/out — not a README, not a draft, not a
directory. The controller validates that tree, copies exactly those two files
into its own private clone, and refuses everything else.

meta.json:
{{
  "schema":       "{schema}",
  "title":        "<human title>",
  "slug":         "<your-slug>",
  "contributor":  "{contributor}",
  "kind":         "svg|md|txt|json",
  "submitted_at": "<UTC ISO-8601, e.g. 2026-08-17T19:00:00Z>",
  "remix_of":     null or "<existing slug>",
  "license":      "{license}",
  "_dada_cycle":  {{ ... see below ... }}
}}

Slug rules: lowercase letters, digits and single hyphens, at most {slug_max}
characters, and it must not already exist under submissions/.

Free-form keys are allowed ONLY with a leading underscore (e.g.
"_artist_statement", "_authored_by"). Any other extra key is a rejection.

THE CYCLE YOU MUST ACTUALLY RUN — meta._dada_cycle
Search first, build once. Run between {min_rounds} and {max_rounds} rounds. In
EVERY round produce EXACTLY {candidates} distinct candidate premises and score
each on all six dimensions ({dimensions}), each a number from {score_min} to
{score_max}. Pick one winner per round; the final round's winner is the piece
you actually build. {round_one}

{fanout}

{{
  "cycle": {cycle},
  "previous_slug": {previous_slug},
  "rounds": [
    {{"round": 1,
      "candidates": [
        {{"id": "r1c1", "premise": "...",
          "scores": {{{score_example}}}}}
        ... exactly {candidates} of them, unique ids ...
      ],
      "selected": "r1c4"}}
    ... rounds numbered 1..N, contiguous ...
  ],
  "winner": {{"round": <the last round number>,
             "candidate": "<that round's selected id>",
             "slug": "<your-slug>"}}
}}

"cycle" MUST be exactly {cycle} and "previous_slug" MUST be exactly
{previous_slug}. Those two values are how the controller proves this cycle
continues the last one rather than restarting the count.

If your piece is an SVG it must parse as XML and contain no <script>, no
on* event attributes, and no external references — fragment (#id) references
only. Everything must be self-contained.

{workspace}/state-out.json (private, never published):
{{
  "cycle": {cycle},
  "last_slug": "<your-slug>",
  "notes": "<what you learned, bounded — this is the input to your next cycle>"
}}
Write it even if you decline; on a decline set "last_slug" to null.

YOUR OWN MEMORY
Your rapp/1 frame chain is at {chain_path}. Read it if you want; it is yours.

RECENT CYCLES (what this worker actually recorded)
{recent}

STANDING DIRECTIVE
{brief}

Decide for yourself. Then act, end to end, without checking back.

Finish with a single line starting exactly `SENTINEL_RESULT:` followed by
CONTRIBUTED, DECLINED or BLOCKED, then one sentence on what you decided and why.
"""


def build_prompt(cfg, wcfg, slug, workspace, expected_cycle,
                 expected_previous,                  history, finalists=None, digest=None):
    ids = NB.identities()
    roll = NB.roll_call()
    recent = [
        f"  - {row.get('at')} {row.get('role')}: {row.get('outcome')} "
        f"({str(row.get('result'))[:120]})"
        for row in history[-5:]
    ]
    state_in = Path(workspace) / "state-in.json"
    example = ", ".join(f'"{d}": 7' for d in SCORE_DIMENSIONS)
    if finalists:
        # The fan-out already ran the first round, in separate contexts, and
        # its ten survivors are binding: the gate checks round one against
        # these exact ids, so the piece cannot quietly ignore the deliberation
        # that justified it.
        fanout_block = (
            SS.finalists_block(finalists, digest or {})
            + "\n\nYour sub-sentinels are finished and cannot be consulted "
              "again. Do not start any further agents; you are the maker.\n")
        round_one = ('Round 1 MUST be exactly the ten finalist RECORDS in '
                     'round1.json — every field, copied verbatim, including '
                     '"from", "rationale", "evidence_digest" and all six '
                     'scores. Later rounds (up to 5) are yours.')
    else:
        fanout_block = ""
        round_one = ("Round 1 is yours to populate; every round needs exactly "
                     f"{CANDIDATES_PER_ROUND} candidates.")
    return WORKER_SITUATION.format(
        instance_name=sentinel.instance_name(cfg),
        slug=slug,
        role=NB.NEIGHBORS[slug],
        rappid=ids[slug],
        nb_name=NB.NEIGHBORHOOD["name"],
        nb_purpose=NB.NEIGHBORHOOD["purpose"],
        peers=", ".join(f"{k} ({'alive' if v['alive'] else 'stale'})"
                        for k, v in roll.items() if k != slug),
        workspace=str(workspace),
        repo=wcfg["repo"],
        state_in_note="present" if state_in.exists() else "absent — this is cycle 1",
        max_piece_kb=int(wcfg["max_piece_bytes"]) // 1024,
        schema=SUBMISSION_SCHEMA,
        contributor=wcfg.get("contributor") or _repo_owner(wcfg["repo"]),
        license=ALLOWED_LICENSES[0],
        slug_max=SLUG_MAX,
        min_rounds=MIN_ROUNDS,
        max_rounds=MAX_ROUNDS,
        candidates=CANDIDATES_PER_ROUND,
        dimensions=", ".join(SCORE_DIMENSIONS),
        score_min=SCORE_MIN,
        score_max=SCORE_MAX,
        score_example=example,
        cycle=expected_cycle,
        previous_slug=json.dumps(expected_previous),
        chain_path=str(NB.chain_path(slug)),
        recent="\n".join(recent) or "  (none yet)",
        fanout=fanout_block,
        round_one=round_one,
        brief=sentinel.evolve_brief(cfg) or
        "No additional standing directive. Decide from your role and memory.",
    )


def _repo_owner(repo):
    return str(repo).split("/")[0] if "/" in str(repo) else str(repo)


# ── subprocess seams (patched in tests) ─────────────────────────────────────

# Environment that can redirect, rewrite, proxy or execute on git's behalf.
# All of it is removed rather than trusted: GIT_CONFIG_PARAMETERS alone can
# inject arbitrary config into a single invocation.
GIT_ENV_STRIP = (
    "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_DIR", "GIT_WORK_TREE",
    "GIT_INDEX_FILE", "GIT_NAMESPACE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_SSH", "GIT_SSH_COMMAND",
    "GIT_PROXY_COMMAND", "GIT_EXTERNAL_DIFF", "GIT_TEMPLATE_DIR",
    "GIT_ATTR_NOSYSTEM", "GIT_EDITOR", "GIT_PAGER",
    "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
    "ALL_PROXY", "all_proxy",
)

# Only these protocols. `ext::` in particular runs a command of the URL's
# choosing, which is a remote that executes.
GIT_ALLOWED_PROTOCOLS = "https:file"

_GIT_ENV = None


def _credential_helper():
    """The operator's credential helper VALUE, read once and validated.

    The sanitized config below has to keep exactly one thing from the real
    machine — how to authenticate a push — or the controller cannot publish
    at all. A `!command` helper is refused: that is not a credential store,
    it is arbitrary execution wearing one's coat.
    """
    try:
        r = subprocess.run(["git", "config", "--global", "--get-all",
                            "credential.helper"], capture_output=True,
                           text=True, timeout=30)
    except Exception:
        return ""
    for line in (r.stdout or "").splitlines():
        value = line.strip()
        if not value:
            continue
        if value.startswith("!"):
            log("ignoring a shell credential.helper from global git config — "
                "a helper that executes is not a credential store")
            continue
        return value
    return ""


def controller_git_env(home=None):
    """The environment EVERY controller git call runs in.

    The finding this closes: `_clone_repo` shelled out to `git clone` with the
    ambient environment, so a global `url.<attacker>.insteadOf <canonical>`
    silently cloned the attacker's repository, and the integrity check — which
    only ever reads LOCAL config — found a perfectly consistent clone of the
    wrong thing. Isolation has to come before the first network byte, not
    after it.

    So: no system config, no global config except a file this code writes
    containing at most a credential helper, an isolated HOME and XDG so
    `~/.gitconfig` and `$XDG_CONFIG_HOME/git/config` are not found, no proxy
    variables, no config injection via the environment, no interactive
    prompts, and only https/file protocols.
    """
    global _GIT_ENV
    if _GIT_ENV is not None and home is None:
        return dict(_GIT_ENV)
    base = {k: v for k, v in os.environ.items() if k not in GIT_ENV_STRIP}
    for key in list(base):
        if key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
            base.pop(key, None)

    git_home = Path(home) if home else (STATE / "git-home")
    git_home.mkdir(parents=True, exist_ok=True)
    config = git_home / "sanitized.gitconfig"
    helper = _credential_helper()
    config.write_text(
        "# written by evolve_worker: the ONLY global git config the\n"
        "# controller runs with. No includes, no url rewrites, no proxy,\n"
        "# no hooksPath, no alternates.\n"
        + (f"[credential]\n\thelper = {helper}\n" if helper else ""),
        encoding="utf-8")

    base.update({
        "HOME": str(git_home),
        "XDG_CONFIG_HOME": str(git_home / "xdg"),
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/usr/bin/false",
        "GIT_ALLOW_PROTOCOL": GIT_ALLOWED_PROTOCOLS,
        "GIT_PROTOCOL_FROM_USER": "0",
    })
    (git_home / "xdg").mkdir(parents=True, exist_ok=True)
    if home is None:
        _GIT_ENV = dict(base)
    return base


def _git(cwd, *args, timeout=600, check=True, env=None):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, timeout=timeout,
                       env=env if env is not None else controller_git_env())
    if check and r.returncode != 0:
        raise CommandError(f"git {' '.join(args)} exited {r.returncode}: "
                           f"{(r.stderr or r.stdout or '').strip()[:300]}")
    return r.stdout


def _git_bytes(cwd, *args, timeout=600):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       timeout=timeout, env=controller_git_env())
    if r.returncode != 0:
        raise CommandError(f"git {' '.join(args)} exited {r.returncode}")
    return r.stdout


LOCAL_REPO_RE = re.compile(r"^/[^\0]+$")
REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_repo_url(repo, wcfg=None):
    """The canonical URL, checked for shape before it is ever handed to git.

    A URL is an instruction. `ext::sh -c ...` is a remote that executes,
    `-u./payload` is an argument pretending to be a host, and
    `https://user:token@host/` leaks a credential into every reflog that
    records it. An explicit absolute path is allowed — the tests use local
    bare repositories on purpose — but nothing implicit is.
    """
    raw = str(repo).strip()
    if raw.startswith("-"):
        raise GateError(f"repo {raw!r} starts with a dash — that is an "
                        f"argument, not a repository")
    url = _repo_url(repo)
    allowed_hosts = tuple((wcfg or {}).get("allowed_repo_hosts")
                          or ("github.com",))
    if url.startswith("-"):
        raise GateError(f"repo {url!r} starts with a dash — that is an argument, "
                        f"not a repository")
    if "\n" in url or "\r" in url:
        raise GateError("repo url contains a newline")
    lowered = url.lower()
    for scheme in ("ext::", "ssh://", "git://", "http://", "git+ssh://"):
        if lowered.startswith(scheme):
            raise GateError(f"repo url uses {scheme} — only https and explicit "
                            f"local paths are allowed")
    if lowered.startswith("https://"):
        parts = urlsplit(url)
        if "@" in parts.netloc:
            raise GateError("repo url embeds credentials")
        if parts.hostname not in allowed_hosts:
            raise GateError(f"repo host {parts.hostname!r} is not one of "
                            f"{', '.join(allowed_hosts)}")
        segments = [p for p in parts.path.split("/") if p]
        if len(segments) != 2:
            raise GateError(f"repo path {parts.path!r} is not owner/name")
        for segment in segments:
            if not REPO_SEGMENT_RE.match(segment.removesuffix(".git")):
                raise GateError(f"repo path segment {segment!r} is not a plain "
                                f"owner or repository name")
        return url
    if LOCAL_REPO_RE.match(url):
        if not Path(url).exists():
            raise GateError(f"local repo {url!r} does not exist")
        return url
    raise GateError(f"repo {url!r} is neither an https url on an allowed host "
                    f"nor an existing absolute path")


NETWORK_GIT_VERBS = ("fetch", "push", "pull", "ls-remote", "clone",
                     "submodule", "archive")


def _git_remote(clone, wcfg, *args, timeout=600, check=True):
    """Every git call that can reach the network goes through here.

    Not because callers are careless, but because "remember to check first"
    is not an invariant. `_delete_remote_branch` was the proof: it pushed a
    branch deletion straight at `origin`, and with a pushurl injected after
    the normal push, the cleanup hit the attacker's remote and left the real
    branch orphaned on ours. The integrity check now lives at the chokepoint,
    so a new call site inherits it instead of having to remember it.
    """
    verb = next((a for a in args if not a.startswith("-")), "")
    if verb not in NETWORK_GIT_VERBS:
        raise CommandError(f"_git_remote used for a local verb: {verb!r}")
    assert_repo_integrity(clone, wcfg)
    return _git(clone, *args, timeout=timeout, check=check)


def _gh(*args, timeout=300):
    r = subprocess.run(["gh", *args], capture_output=True, text=True,
                       timeout=timeout)
    if r.returncode != 0:
        raise CommandError(f"gh {' '.join(args)} exited {r.returncode}: "
                           f"{(r.stderr or r.stdout or '').strip()[:300]}")
    return r.stdout


def run_model(staging, prompt, wcfg, depth=0, runtime=None):
    """Spend the model in the workspace. Returns (outcome, output).

    Confined, not trusted: bounded file tools rooted at --add-dir, no shell,
    no git, no gh, no MCP, no network tool, an isolated HOME/XDG/temp inside
    the workspace, a strict environment allowlist, and inference auth handed
    over as one --secret-env-vars variable rather than the operator's real
    credential store (#1).

    Tracked, not fire-and-forget: its own process group, registered in _LIVE
    so a timeout or a SIGTERM takes the whole tree down (#4).

    Outcome is only ever "ok", "timeout" or "failed" here — whether the model
    actually CONTRIBUTED is not something its exit code or its last line is
    allowed to decide (R1).
    """
    fcfg = SS.fanout_config(wcfg)
    staging = Path(staging)
    # HOME, XDG, TMPDIR and the CLI's own logs live OUTSIDE the tool root, so
    # the maker's file tools cannot reach even its own process state — and
    # the staging root stays exactly what the gate expects to find (#1).
    runtime = Path(runtime) if runtime else staging.parent / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    timeout_s = int(wcfg["timeout_s"])
    try:
        env = SS.confined_env(fcfg, runtime, depth)
    except SS.AuthUnavailable as e:
        return OUTCOME_FAILED, f"cannot confine the maker: {e}"
    argv = SS.sandbox_wrap(
        SS.confined_argv(prompt, wcfg["model"], staging,
                         tools=SS.MAKER_TOOLS,
                         add_dirs=[staging],
                         secret_vars=SS.secret_vars_for(fcfg),
                         log_dir=runtime / "copilot-logs"),
        # Both roots: tools stay rooted at staging via --add-dir, but the
        # process must be able to write its isolated HOME/XDG/TMPDIR in
        # runtime or the sandbox turns every run into a PermissionError.
        [staging, runtime], bool(fcfg.get("sandbox_exec")),
        profile_dir=runtime)
    try:
        proc = subprocess.Popen(argv, cwd=str(staging), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
    except FileNotFoundError:
        return OUTCOME_FAILED, "copilot CLI not found on PATH"
    except OSError as e:
        return OUTCOME_FAILED, f"could not start the maker: {type(e).__name__}: {e}"
    track(proc)
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        SS._kill_group(proc, float(fcfg.get("kill_grace_s", 5)))
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return OUTCOME_TIMEOUT, f"copilot timed out after {timeout_s}s"
    except BaseException:
        SS._kill_group(proc, float(fcfg.get("kill_grace_s", 5)))
        raise
    finally:
        untrack(proc)
    output = (out or "") + (err or "")
    if proc.returncode != 0:
        return OUTCOME_FAILED, output or f"copilot exited {proc.returncode}"
    return "ok", output


def maker_argv(wcfg, staging, prompt="P"):
    """The exact command line the maker would get. Exposed for tests."""
    fcfg = SS.fanout_config(wcfg)
    staging = Path(staging)
    return SS.sandbox_wrap(
        SS.confined_argv(prompt, wcfg.get("model", ""), staging,
                         tools=SS.MAKER_TOOLS, add_dirs=[staging],
                         secret_vars=SS.secret_vars_for(fcfg),
                         log_dir=staging.parent / "runtime" / "copilot-logs"),
        [staging, staging.parent / "runtime"], bool(fcfg.get("sandbox_exec")),
        profile_dir=staging.parent / "runtime")


# ── the deterministic gate ──────────────────────────────────────────────────

def working_tree_changes(clone, timeout=600):
    """Every change in the clone, as (code, path, origin) triples."""
    raw = _git(clone, "status", "--porcelain=v1", "-z", "-uall", timeout=timeout)
    parts = [p for p in raw.split("\0") if p]
    changes, i = [], 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            raise GateError(f"unparseable git status entry {entry!r}")
        code, path = entry[:2], entry[3:]
        origin = None
        if code[0] in ("R", "C"):
            origin = parts[i] if i < len(parts) else None
            i += 1
        changes.append((code, path, origin))
    return changes


def _new_submission_dir(changes):
    """Exactly one new submissions/<slug>/ and nothing else touched.

    Paths must have exactly three components: a submission is two files at the
    ROOT of its folder. `submissions/<slug>/a/b.svg` used to pass the prefix
    test and then vanish from the two-file count, which is a directory tree
    smuggled through a check that thought it was counting files (#3).
    """
    slugs, files = set(), []
    for code, path, origin in changes:
        if code != "??":
            raise GateError(f"the model changed an existing path: {code} {path}"
                            + (f" (from {origin})" if origin else ""))
        parts = Path(path).parts
        if len(parts) != 3 or parts[0] != "submissions":
            raise GateError(f"not a root-level file in submissions/<slug>/: {path}")
        slugs.add(parts[1])
        files.append(path)
    if not slugs:
        raise GateError("no new submission was left in the clone")
    if len(slugs) > 1:
        raise GateError(f"more than one new submission: {', '.join(sorted(slugs))}")
    slug = slugs.pop()
    return slug, sorted(files)


def _regular_file(path, where):
    """lstat, never stat: the question is what this path IS, not what it
    points at. A symlink to ~/.ssh/id_ed25519 reads fine through stat() and
    commits as a symlink; git stores mode 120000 and the link target becomes
    the blob (#3)."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise GateError(f"{where} is a symlink")
    if not stat.S_ISREG(st.st_mode):
        raise GateError(f"{where} is not a regular file "
                        f"(mode {stat.S_IFMT(st.st_mode):#o})")
    if st.st_nlink > 1:
        raise GateError(f"{where} has {st.st_nlink} hard links")
    if st.st_mode & 0o111:
        raise GateError(f"{where} is executable")
    return st


def _reject_external_url(value, where):
    """CSS is a reference too: url(http...) and @import leave the file."""
    text = str(value)
    low = text.lower()
    if "@import" in low:
        raise GateError(f"{where} carries an @import")
    idx = 0
    while True:
        idx = low.find("url(", idx)
        if idx < 0:
            return
        ref = text[idx + 4:].lstrip().lstrip("'\"").strip()
        if not ref.startswith("#"):
            raise GateError(f"{where} references something outside itself: "
                            f"{ref[:60]}")
        idx += 4


def _regular_dir(path, label="the submission folder"):
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise GateError(f"{label} is a symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise GateError(f"{label} is not a directory")


def _check_svg(raw, text):
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise GateError("svg carries a doctype/entity declaration")
    if "javascript:" in text.lower():
        raise GateError("svg carries a javascript: url")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise GateError(f"svg does not parse as xml: {e}")

    def local(name):
        return name.split("}")[-1].lower() if isinstance(name, str) else ""

    if local(root.tag) != "svg":
        raise GateError(f"svg root element is <{local(root.tag)}>, not <svg>")
    for el in root.iter():
        tag = local(el.tag)
        if tag in ("script", "foreignobject"):
            raise GateError(f"svg contains <{tag}>")
        if tag == "style":
            _reject_external_url(el.text or "", "svg <style>")
        for attr, value in el.attrib.items():
            name = local(attr)
            if name.startswith("on"):
                raise GateError(f"svg carries the event attribute {name}")
            if name in ("href", "xlink:href", "src"):
                ref = str(value).strip()
                if not ref.startswith("#"):
                    raise GateError(f"svg references something outside itself: {ref[:60]}")
            _reject_external_url(value, f"svg attribute {name}")


def _check_piece(path, kind, max_bytes):
    raw = path.read_bytes()
    if not raw:
        raise GateError("piece is empty")
    if len(raw) > max_bytes:
        raise GateError(f"piece is {len(raw)} bytes, over the {max_bytes} byte cap")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise GateError("piece is not valid utf-8")
    if "\0" in text:
        raise GateError("piece carries a NUL byte")
    if kind == "svg":
        _check_svg(raw, text)
    elif kind == "json":
        try:
            json.loads(text)
        except json.JSONDecodeError as e:
            raise GateError(f"piece is not valid json: {e}")
    return raw


def _check_number(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GateError(f"{where} is not a number")
    if value != value or value in (float("inf"), float("-inf")):
        raise GateError(f"{where} is not finite")
    if not (SCORE_MIN <= value <= SCORE_MAX):
        raise GateError(f"{where} is outside {SCORE_MIN}..{SCORE_MAX}")


def validate_dada_cycle(cycle, slug, expected_cycle, expected_previous,
                        expected_round1=None):
    """The cycle block is the search, checked. Reject on the first doubt."""
    if not isinstance(cycle, dict):
        raise GateError("meta._dada_cycle is missing or not an object")
    if cycle.get("cycle") != expected_cycle:
        raise GateError(f"_dada_cycle.cycle is {cycle.get('cycle')!r}, "
                        f"expected {expected_cycle} — cycle continuity broken")
    if cycle.get("previous_slug") != expected_previous:
        raise GateError(f"_dada_cycle.previous_slug is "
                        f"{cycle.get('previous_slug')!r}, expected "
                        f"{expected_previous!r} — cycle continuity broken")
    rounds = cycle.get("rounds")
    if not isinstance(rounds, list) or not (MIN_ROUNDS <= len(rounds) <= MAX_ROUNDS):
        raise GateError(f"_dada_cycle.rounds must hold {MIN_ROUNDS}..{MAX_ROUNDS} "
                        f"rounds, found "
                        f"{len(rounds) if isinstance(rounds, list) else 'none'}")
    for index, rnd in enumerate(rounds, start=1):
        if not isinstance(rnd, dict):
            raise GateError(f"round {index} is not an object")
        if rnd.get("round") != index:
            raise GateError(f"round {index} is numbered {rnd.get('round')!r} — "
                            f"rounds must be contiguous from 1")
        candidates = rnd.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != CANDIDATES_PER_ROUND:
            raise GateError(
                f"round {index} has "
                f"{len(candidates) if isinstance(candidates, list) else 'no'} "
                f"candidates, exactly {CANDIDATES_PER_ROUND} required")
        seen = set()
        for cand in candidates:
            if not isinstance(cand, dict):
                raise GateError(f"round {index} holds a non-object candidate")
            cid = cand.get("id")
            if not isinstance(cid, str) or not cid.strip():
                raise GateError(f"round {index} holds a candidate without an id")
            if cid in seen:
                raise GateError(f"round {index} repeats candidate id {cid!r}")
            seen.add(cid)
            premise = cand.get("premise")
            if not isinstance(premise, str) or not premise.strip():
                raise GateError(f"candidate {cid} has no premise")
            scores = cand.get("scores")
            if not isinstance(scores, dict):
                raise GateError(f"candidate {cid} has no scores object")
            if set(scores) != set(SCORE_DIMENSIONS):
                raise GateError(
                    f"candidate {cid} scores {sorted(scores)}, expected exactly "
                    f"{list(SCORE_DIMENSIONS)}")
            for dim in SCORE_DIMENSIONS:
                _check_number(scores[dim], f"candidate {cid} score {dim}")
        selected = rnd.get("selected")
        if selected not in seen:
            raise GateError(f"round {index} selected {selected!r}, which is not "
                            f"one of its candidates")
        if index == 1 and expected_round1 is not None:
            # The fan-out's ten finalists ARE round one — and a finalist is
            # its content, not its label. Checking ids alone let a maker keep
            # the names and replace every premise and score underneath them,
            # which is the deliberation erased and its receipt kept (#2). The
            # digest covers premise, rationale, all six scores, which
            # sub-sentinel produced it, and that child's evidence.
            wanted = dict(expected_round1)
            if seen != set(wanted):
                missing = sorted(set(wanted) - seen)
                added = sorted(seen - set(wanted))
                raise GateError(
                    "round 1 must be exactly the ten finalists the "
                    "sub-sentinels produced"
                    + (f"; missing {missing}" if missing else "")
                    + (f"; unexpected {added}" if added else ""))
            for cand in candidates:
                cid = cand["id"]
                for field in SS.RECORD_FIELDS:
                    if field not in cand:
                        raise GateError(f"round 1 candidate {cid} is missing "
                                        f"{field}; round one must carry the "
                                        f"finalist record, not just its id")
                try:
                    actual = SS.record_digest(cand)
                except SS.FanoutError as e:
                    raise GateError(f"round 1 candidate {cid}: {e}")
                if actual != wanted[cid]:
                    raise GateError(
                        f"round 1 candidate {cid} does not match the finalist "
                        f"the sub-sentinels produced (digest {actual[:12]}… != "
                        f"{wanted[cid][:12]}…) — its content was rewritten")
                if "digest" in cand and cand["digest"] != actual:
                    raise GateError(f"round 1 candidate {cid} publishes a digest "
                                    f"that does not match its own fields")
    winner = cycle.get("winner")
    if not isinstance(winner, dict):
        raise GateError("_dada_cycle.winner is missing or not an object")
    last = rounds[-1]
    if winner.get("round") != last["round"]:
        raise GateError(f"winner.round is {winner.get('round')!r}, expected the "
                        f"final round {last['round']}")
    if winner.get("candidate") != last.get("selected"):
        raise GateError(f"winner.candidate is {winner.get('candidate')!r}, but the "
                        f"final round selected {last.get('selected')!r}")
    if winner.get("slug") != slug:
        raise GateError(f"winner.slug is {winner.get('slug')!r}, but the submission "
                        f"is {slug!r}")


def assert_no_git(root):
    """A staging workspace must contain no git metadata of any kind.

    The maker's file tools are rooted here. If a `.git` directory — or a
    `.git` FILE pointing at one — can exist inside that root, the model can
    write `remote.origin.pushurl` and the controller will happily push the
    submission to somebody else's server. The fix is structural: the model
    never gets a repository, so there is nothing to rewrite (#1).
    """
    root = Path(root)
    for path in [root, *root.rglob("*")]:
        if path.name in (".git", ".gitmodules", ".gitattributes"):
            raise GateError(f"the staging workspace contains {path.name} at "
                            f"{path.relative_to(root) if path != root else '.'}")


def gate_directory(root, wcfg, expected_cycle, expected_previous,
                   expected_round1=None, known_slugs=()):
    """Validate a submission tree that has no git anywhere near it.

    `root` holds exactly one thing: `submissions/<slug>/`, holding exactly
    meta.json and piece.<ext>. Returns the submission record INCLUDING the
    validated bytes, because the bytes are what the controller copies into
    its own clone — re-reading the path later would re-open a file the model
    could have swapped underneath us.
    """
    root = Path(root)
    if not root.is_dir():
        raise GateError("no new submission was left in the staging workspace")
    top = sorted(p.name for p in root.iterdir())
    if not top:
        raise GateError("no new submission was left in the staging workspace")
    if top != ["submissions"]:
        raise GateError(f"the staging output holds {top}, expected only "
                        f"'submissions'")
    holder = root / "submissions"
    _regular_dir(holder, "submissions")
    slugs = sorted(p.name for p in holder.iterdir())
    if not slugs:
        raise GateError("no new submission was left in the staging workspace")
    if len(slugs) > 1:
        raise GateError(f"more than one new submission: {', '.join(slugs)}")
    slug = slugs[0]

    if not SLUG_RE.match(slug) or len(slug) > SLUG_MAX:
        raise GateError(f"slug {slug!r} is not lowercase-alphanumeric-hyphen "
                        f"within {SLUG_MAX} characters")
    if slug in set(known_slugs):
        raise GateError(f"slug {slug!r} already exists on the base branch")

    directory = holder / slug
    _regular_dir(directory, f"submissions/{slug}")
    entries = sorted(directory.iterdir(), key=lambda p: p.name)
    names = []
    for entry in entries:
        rel = f"submissions/{slug}/{entry.name}"
        _regular_file(entry, rel)
        names.append(entry.name)
    if len(names) != 2 or "meta.json" not in names:
        raise GateError(f"a submission is exactly meta.json + piece.<ext>, found "
                        f"{names}")
    piece_name = [n for n in names if n != "meta.json"][0]
    piece_path = directory / piece_name
    meta_path = directory / "meta.json"

    meta_bytes = meta_path.read_bytes()
    if len(meta_bytes) > int(wcfg.get("max_meta_bytes", 262144)):
        raise GateError("meta.json is implausibly large")
    try:
        meta = json.loads(meta_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise GateError(f"meta.json is not valid json: {e}")
    if not isinstance(meta, dict):
        raise GateError("meta.json is not an object")

    missing = [k for k in REQUIRED_META_KEYS if k not in meta]
    if missing:
        raise GateError(f"meta.json is missing {', '.join(missing)}")
    extra = [k for k in meta
             if k not in REQUIRED_META_KEYS and not k.startswith("_")]
    if extra:
        raise GateError(f"meta.json carries unknown keys: {', '.join(sorted(extra))}")
    if meta["schema"] != SUBMISSION_SCHEMA:
        raise GateError(f"meta.schema is {meta['schema']!r}, expected "
                        f"{SUBMISSION_SCHEMA!r}")
    if meta["slug"] != slug:
        raise GateError(f"meta.slug is {meta['slug']!r} but the folder is {slug!r}")
    if not isinstance(meta.get("title"), str) or not meta["title"].strip():
        raise GateError("meta.title is empty")
    if not isinstance(meta.get("contributor"), str) or not meta["contributor"].strip():
        raise GateError("meta.contributor is empty")
    if meta["license"] not in ALLOWED_LICENSES:
        raise GateError(f"meta.license is {meta['license']!r}, expected one of "
                        f"{', '.join(ALLOWED_LICENSES)}")
    kind = meta.get("kind")
    if kind not in KIND_EXTENSIONS:
        raise GateError(f"meta.kind is {kind!r}, expected one of "
                        f"{', '.join(sorted(KIND_EXTENSIONS))}")
    if piece_name != f"piece{KIND_EXTENSIONS[kind]}":
        raise GateError(f"piece is {piece_name!r}, expected "
                        f"piece{KIND_EXTENSIONS[kind]} for kind {kind!r}")
    stamp = str(meta.get("submitted_at") or "")
    try:
        datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        raise GateError(f"meta.submitted_at {stamp!r} is not ISO-8601")
    remix = meta.get("remix_of")
    if remix is not None:
        if not isinstance(remix, str) or not SLUG_RE.match(remix):
            raise GateError(f"meta.remix_of {remix!r} is not a slug or null")
        if remix not in set(known_slugs):
            raise GateError(f"meta.remix_of {remix!r} does not exist on the "
                            f"base branch")

    piece_bytes = _check_piece(piece_path, kind,
                              int(wcfg.get("max_piece_bytes", 51200)))
    validate_dada_cycle(meta.get("_dada_cycle"), slug, expected_cycle,
                        expected_previous, expected_round1)

    return {
        "slug": slug,
        "title": meta["title"],
        "kind": kind,
        "meta": meta,
        "meta_path": f"submissions/{slug}/meta.json",
        "piece_path": f"submissions/{slug}/{piece_name}",
        "meta_bytes": meta_bytes,
        "piece_bytes": piece_bytes,
        "meta_sha256": hashlib.sha256(meta_bytes).hexdigest(),
        "piece_sha256": hashlib.sha256(piece_bytes).hexdigest(),
    }


def base_branch_slugs(clone, base_branch, wcfg):
    """Every slug already published, read by the controller from its clone."""
    listing = _git(clone, "ls-tree", "--name-only", base_branch, "submissions/",
                   timeout=int(wcfg.get("git_timeout_s", 600)))
    return {line.strip("/").split("/")[-1]
            for line in listing.splitlines() if line.strip()}


def install_into_clone(clone, submission):
    """Copy the two VALIDATED files — as bytes — into the controller's clone.

    Bytes, not paths: the gate read them, the gate hashed them, and these are
    the same objects. Copying by path would re-open files in a directory a
    model process was writing to seconds ago.
    """
    clone = Path(clone)
    directory = clone / "submissions" / submission["slug"]
    if directory.exists():
        raise GateError(f"{directory} already exists in the controller's clone")
    directory.mkdir(parents=True)
    for rel, blob in ((submission["meta_path"], submission["meta_bytes"]),
                      (submission["piece_path"], submission["piece_bytes"])):
        target = clone / rel
        with open(target, "wb") as fh:
            fh.write(blob)
        os.chmod(target, 0o644)
    return directory


def verify_clone_scope(clone, submission, wcfg, base_sha=None):
    """After the copy, the controller's clone must hold exactly two new files.

    The maker never had this directory — this catches the controller's own
    mistakes, and anything else that touched the clone while we worked.
    """
    clone = Path(clone)
    git_t = int(wcfg.get("git_timeout_s", 600))
    if base_sha:
        head = _git(clone, "rev-parse", "HEAD", timeout=git_t).strip()
        if head != base_sha:
            raise GateError("the controller's clone moved its HEAD unexpectedly")
    changes = working_tree_changes(clone, timeout=git_t)
    slug, files = _new_submission_dir(changes)
    if slug != submission["slug"]:
        raise GateError(f"the clone holds {slug!r}, expected "
                        f"{submission['slug']!r}")
    expected = sorted([submission["meta_path"], submission["piece_path"]])
    if files != expected:
        raise GateError(f"the clone holds {files}, expected {expected}")
    for rel, digest in ((submission["meta_path"], submission["meta_sha256"]),
                        (submission["piece_path"], submission["piece_sha256"])):
        path = clone / rel
        _regular_file(path, rel)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise GateError(f"{rel} in the clone is not the file that passed "
                            f"the gate")
    return expected


# Local git config keys that can redirect, intercept or execute. None of them
# belong in a clone this worker made ten minutes ago.
FORBIDDEN_CONFIG_PREFIXES = (
    "remote.origin.pushurl", "remote.", "url.", "core.hookspath",
    "core.sshcommand", "core.gitproxy", "core.fsmonitor", "credential.",
    "protocol.", "alias.", "include.", "includeif.", "filter.", "diff.",
    "uploadpack.", "receive.",
)
ALLOWED_CONFIG_KEYS = {
    "core.repositoryformatversion", "core.filemode", "core.bare",
    "core.logallrefupdates", "core.ignorecase", "core.precomposeunicode",
    "core.symlinks", "core.worktree", "remote.origin.url",
    "remote.origin.fetch", "branch.main.remote", "branch.main.merge",
    "extensions.objectformat", "init.defaultbranch",
}


def assert_repo_integrity(clone, wcfg):
    """Prove, immediately before touching git, that this is still OUR clone.

    A bounded repro of the real finding: the maker wrote into `clone/.git`,
    set `remote.origin.pushurl`, and the controller pushed the branch to an
    attacker's remote before failing later for an unrelated reason. The maker
    can no longer reach a repository at all — and this runs anyway, before
    every git operation, because "cannot happen" is not a check (#1).
    """
    clone = Path(clone)
    git_t = int(wcfg.get("git_timeout_s", 600))
    canonical = validate_repo_url(wcfg["repo"], wcfg)

    dot_git = clone / ".git"
    if not dot_git.is_dir():
        raise GateError("the clone's .git is not a directory")
    alternates = dot_git / "objects" / "info" / "alternates"
    if alternates.exists():
        raise GateError("the clone has an objects/info/alternates file")
    hooks = dot_git / "hooks"
    if hooks.is_dir():
        live = [h.name for h in hooks.iterdir()
                if h.is_file() and not h.name.endswith(".sample")
                and os.access(h, os.X_OK)]
        if live:
            raise GateError(f"the clone has executable git hooks: {', '.join(live)}")

    # A pushurl is REJECTED, not quietly repaired: a clone that grew one
    # since we made it is a clone something else has been writing to, and
    # continuing would be treating an intrusion as a formatting problem.
    existing_push = _git(clone, "config", "--local", "--get-all",
                         "remote.origin.pushurl", timeout=git_t,
                         check=False).strip()
    if existing_push:
        raise GateError(f"the clone has a remote.origin.pushurl "
                        f"({existing_push.splitlines()[0][:80]}) — refusing to "
                        f"push anywhere but {canonical}")

    raw = _git(clone, "config", "--local", "--list", timeout=git_t, check=False)
    for line in raw.splitlines():
        key = line.split("=", 1)[0].strip().lower()
        if not key or key in ALLOWED_CONFIG_KEYS:
            continue
        if key.startswith("branch.") and key.endswith((".remote", ".merge")):
            continue
        if key.startswith(FORBIDDEN_CONFIG_PREFIXES):
            raise GateError(f"the clone carries an unexpected git config key: "
                            f"{key}")

    _git(clone, "remote", "set-url", "origin", canonical, timeout=git_t)
    fetch_url = _git(clone, "remote", "get-url", "origin", timeout=git_t).strip()
    push_url = _git(clone, "remote", "get-url", "--push", "origin",
                    timeout=git_t).strip()
    if fetch_url != canonical or push_url != canonical:
        raise GateError(f"origin points at {fetch_url!r}/{push_url!r}, expected "
                        f"{canonical!r}")
    return canonical


def validate_next_state(path, wcfg, expected_cycle, slug):
    """The private next-state file, checked before it can become the ledger."""
    p = Path(path)
    if not p.exists():
        raise GateError("the model wrote no state-out.json")
    if p.stat().st_size > int(wcfg.get("max_state_bytes", 262144)):
        raise GateError("state-out.json is implausibly large")
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise GateError(f"state-out.json is not valid json: {e}")
    if not isinstance(state, dict):
        raise GateError("state-out.json is not an object")
    if state.get("cycle") != expected_cycle:
        raise GateError(f"state-out.cycle is {state.get('cycle')!r}, expected "
                        f"{expected_cycle}")
    if state.get("last_slug") != slug:
        raise GateError(f"state-out.last_slug is {state.get('last_slug')!r}, "
                        f"expected {slug!r}")
    return state


# ── the controller: git and GitHub are ours, not the model's ────────────────

def probe_health(health, phase, wcfg):
    """Run a health probe and treat any failure to produce one as a stop.

    A probe that raises used to escape publish() entirely and unwind through
    whatever caught RuntimeError next — which is not "the estate is fine", it
    is "we never asked". An unanswered question is not a yes (#5).
    """
    try:
        verdict = health(phase)
    except BaseException as e:                # including KeyboardInterrupt
        raise AbortError(f"health probe at {phase} raised "
                         f"{type(e).__name__}: {str(e)[:160]}")
    ok, why = health_gate(wcfg, verdict, phase=phase)
    if not ok:
        raise AbortError(why)
    return verdict


def verify_staged_tree(clone, submission, wcfg, paths):
    """What git will actually push, checked before anything leaves the machine.

    The working tree was gated; the INDEX is what gets pushed, and they are
    not the same object. A symlink or a mode-100755 blob or a different set of
    bytes can sit in the index while the files on disk look right, and
    post-merge verification would then be confirming something a human already
    merged (#3).
    """
    git_t = int(wcfg.get("git_timeout_s", 600))
    staged = []
    for line in _git(clone, "ls-files", "--stage", "--",
                     f"submissions/{submission['slug']}",
                     timeout=git_t).splitlines():
        if not line.strip():
            continue
        info, _, path = line.partition("\t")
        mode, blob, stage = (info.split() + ["", "", ""])[:3]
        staged.append((mode, blob, stage, path))
    if sorted(p for _, _, _, p in staged) != paths:
        raise GateError(f"the index holds {[p for _, _, _, p in staged]}, "
                        f"expected exactly {paths}")
    digests = {submission["meta_path"]: submission["meta_sha256"],
               submission["piece_path"]: submission["piece_sha256"]}
    for mode, blob, stage, path in staged:
        if mode != "100644":
            raise GateError(f"{path} is staged with mode {mode}, expected "
                            f"100644 (a symlink stages as 120000)")
        if stage != "0":
            raise GateError(f"{path} is staged unmerged (stage {stage})")
        kind = _git(clone, "cat-file", "-t", blob, timeout=git_t).strip()
        if kind != "blob":
            raise GateError(f"{path} points at a {kind}, not a blob")
        body = _git_bytes(clone, "cat-file", "blob", blob, timeout=git_t)
        if hashlib.sha256(body).hexdigest() != digests[path]:
            raise GateError(f"{path} in the index is not the file that passed "
                            f"the gate")
    return {path: blob for _, blob, _, path in staged}


def publish(clone, submission, wcfg, health, branch=None, transaction=None):
    """Branch, commit, PR, verify, re-check health, merge, re-read main.

    Every remote step is preceded by a health run and followed by evidence
    read back from GitHub. Returns a dict of receipts; raises AbortError,
    GateError or CommandError. A PR opened before an abort is closed on the
    way out — for ANY exception, including a timeout, a malformed gh reply, or
    the operator's Ctrl-C — so a stopped cycle does not leave a half-open
    contribution behind (#5).

    `transaction` is a callable the caller supplies to persist each step, so
    a process killed between the merge call and the ledger write can be
    reconciled on the next pass instead of losing a merged submission.
    """
    clone = Path(clone)
    repo = wcfg["repo"]
    base = wcfg.get("base_branch", "main")
    git_t = int(wcfg.get("git_timeout_s", 600))
    gh_t = int(wcfg.get("gh_timeout_s", 300))
    slug = submission["slug"]
    branch = branch or f"{wcfg.get('branch_prefix', 'art')}/{slug}-{uuid.uuid4().hex[:8]}"
    paths = sorted([submission["meta_path"], submission["piece_path"]])
    note = transaction or (lambda **_: None)

    # Health immediately before the FIRST remote write (#3). Not the health we
    # ran before the model: that reading is up to half an hour old by now, and
    # the estate is exactly what may have changed while the model thought.
    probe_health(health, "pre-write", wcfg)
    assert_repo_integrity(clone, wcfg)

    _git_remote(clone, wcfg, "fetch", "--no-tags", "origin", base, timeout=git_t)
    _git(clone, "checkout", "-b", branch, f"origin/{base}", timeout=git_t)
    still_new = _git(clone, "ls-tree", "--name-only", f"origin/{base}",
                     f"submissions/{slug}/", timeout=git_t).strip()
    if still_new:
        raise GateError(f"slug {slug!r} appeared on origin/{base} while we worked")

    _git(clone, "add", "--", f"submissions/{slug}", timeout=git_t)
    staged = [ln.split("\t") for ln in
              _git(clone, "diff", "--cached", "--name-status",
                   timeout=git_t).splitlines() if ln.strip()]
    if sorted(p for _, p in staged) != paths or any(s != "A" for s, _ in staged):
        raise GateError(f"staged set is {staged}, expected exactly two additions "
                        f"{paths}")
    blobs = verify_staged_tree(clone, submission, wcfg, paths)

    message = (f"art: {submission['title']} ({slug})\n\n"
               f"Autonomous submission by the {wcfg.get('role', 'evolve')} "
               f"neighbor of {wcfg.get('instance_name', 'this collective')}.\n"
               f"Dada cycle {submission['meta']['_dada_cycle']['cycle']}, "
               f"{len(submission['meta']['_dada_cycle']['rounds'])} round(s) of "
               f"{CANDIDATES_PER_ROUND} candidates.\n")
    _git(clone, "-c", f"user.name={wcfg['git_author_name']}",
         "-c", f"user.email={wcfg['git_author_email']}",
         "commit", "-m", message, timeout=git_t)
    head = _git(clone, "rev-parse", "HEAD", timeout=git_t).strip()
    note(phase="committed", branch=branch, commit=head, blobs=blobs, paths=paths)
    # The last thing before bytes leave this machine: the chokepoint proves
    # the remote is still the one we configured, not one somebody wrote into
    # .git after the last check.
    _git_remote(clone, wcfg, "push", "--set-upstream", "origin", branch,
                timeout=git_t)
    note(phase="pushed", branch=branch, commit=head)

    pr_url, pr_number = "", ""
    try:
        body = _pr_body(submission, wcfg)
        pr_url = _gh("pr", "create", "--repo", repo, "--base", base,
                     "--head", branch, "--title",
                     f"art: {submission['title']} ({slug})",
                     "--body", body, timeout=gh_t).strip().splitlines()[-1].strip()
        pr_number = pr_url.rstrip("/").split("/")[-1]
        note(phase="pr-open", pr_url=pr_url, pr_number=pr_number)

        # What GitHub says the PR contains, not what we think we pushed.
        view = json.loads(_gh("pr", "view", pr_number, "--repo", repo, "--json",
                              "files,state,baseRefName,headRefName,isCrossRepository",
                              timeout=gh_t))
        remote_paths = sorted(f.get("path") for f in view.get("files", []))
        if remote_paths != paths:
            raise GateError(f"the PR touches {remote_paths}, expected {paths}")
        if any(int(f.get("deletions") or 0) for f in view.get("files", [])):
            raise GateError("the PR deletes lines from an existing file")
        if view.get("baseRefName") != base or view.get("headRefName") != branch:
            raise GateError(f"the PR targets {view.get('baseRefName')!r} from "
                            f"{view.get('headRefName')!r}, expected {base!r} from "
                            f"{branch!r}")

        # Health again, immediately before the merge (#3/#6).
        probe_health(health, "pre-merge", wcfg)

        note(phase="merging", pr_url=pr_url, pr_number=pr_number)
        _gh("pr", "merge", pr_number, "--repo", repo, "--squash",
            "--delete-branch", timeout=gh_t)
        note(phase="merge-called", pr_url=pr_url, pr_number=pr_number)
    except BaseException:
        # EVERY exception, not three named ones: a gh timeout, a malformed
        # json reply, a KeyboardInterrupt from launchd's ceiling, a bug in
        # this function. Whatever it was, an open PR nobody is going to
        # finish is worse than no PR.
        if pr_number:
            _close_pr(repo, pr_number, gh_t)
        else:
            _delete_remote_branch(clone, branch, wcfg, git_t)
        note(phase="cleaned-up")
        raise

    receipts = confirm_merge(clone, submission, wcfg, pr_number, paths)
    receipts.update({"branch": branch, "commit": head, "pr_url": pr_url,
                     "pr_number": pr_number})
    note(phase="merged", **receipts)
    return receipts


def confirm_merge(clone, submission, wcfg, pr_number, paths):
    """Evidence, freshly re-read: merged state, merge commit, its file scope,
    and the bytes now living on the base branch (R1). Used both by publish()
    and by reconciliation of a cycle that died mid-flight."""
    clone = Path(clone)
    repo = wcfg["repo"]
    base = wcfg.get("base_branch", "main")
    git_t = int(wcfg.get("git_timeout_s", 600))
    gh_t = int(wcfg.get("gh_timeout_s", 300))

    merged = json.loads(_gh("pr", "view", str(pr_number), "--repo", repo,
                            "--json", "state,merged,mergeCommit", timeout=gh_t))
    if not merged.get("merged") or str(merged.get("state")).upper() != "MERGED":
        raise CommandError(f"PR {pr_number} is not merged: "
                           f"{merged.get('state')!r}")
    merge_sha = ((merged.get("mergeCommit") or {}).get("oid") or "").strip()
    if not merge_sha:
        raise CommandError(f"PR {pr_number} reports no merge commit")

    _git_remote(clone, wcfg, "fetch", "--no-tags", "origin", base, timeout=git_t)
    main_sha = _git(clone, "rev-parse", f"origin/{base}", timeout=git_t).strip()
    _git(clone, "merge-base", "--is-ancestor", merge_sha, f"origin/{base}",
         timeout=git_t)
    touched = [ln.split("\t") for ln in
               _git(clone, "show", "--name-status", "--format=", merge_sha,
                    timeout=git_t).splitlines() if ln.strip()]
    if sorted(p for _, p in touched) != paths or any(s != "A" for s, _ in touched):
        raise CommandError(f"the merge commit touches {touched}, expected exactly "
                           f"two additions {paths}")
    for path, digest in ((submission["meta_path"], submission["meta_sha256"]),
                         (submission["piece_path"], submission["piece_sha256"])):
        blob = _git_bytes(clone, "cat-file", "blob", f"origin/{base}:{path}",
                          timeout=git_t)
        if hashlib.sha256(blob).hexdigest() != digest:
            raise CommandError(f"{path} on origin/{base} is not the file we gated")
    return {"merge_commit": merge_sha, "base_sha": main_sha, "paths": paths}


def _close_pr(repo, pr_number, timeout):
    if not pr_number:
        return
    try:
        _gh("pr", "close", str(pr_number), "--repo", repo, "--delete-branch",
            timeout=timeout)
        log(f"closed PR {pr_number} after aborting the cycle")
    except Exception as e:
        log(f"could not close PR {pr_number}: {type(e).__name__}: {e}")


def _delete_remote_branch(clone, branch, wcfg, timeout=None):
    """Remove a branch we pushed, from the canonical repo and nowhere else.

    Two paths, and neither can reach a remote we did not configure:

      * the clone still verifies -> delete through it, normally.
      * the clone does NOT verify (a pushurl appeared, a hostile config, a
        hook) -> do not touch that repository at all. Delete from a fresh,
        empty git directory with global and system config neutralised,
        addressing the canonical URL explicitly, then confirm with ls-remote.

    The second path exists because failing closed here would leave a real
    branch orphaned on the real origin as the price of someone else's
    tampering, and cleanup is exactly when the repository is least
    trustworthy.
    """
    timeout = int(timeout or wcfg.get("git_timeout_s", 600))
    canonical = _repo_url(wcfg["repo"])
    try:
        _git_remote(clone, wcfg, "push", "origin", "--delete", branch,
                    timeout=timeout)
        log(f"deleted the pushed branch {branch} after aborting the cycle")
        return True
    except GateError as e:
        log(f"the clone no longer verifies ({e}); deleting {branch} from "
            f"{canonical} through a sanitized repository instead")
    except Exception as e:
        log(f"could not delete branch {branch} via origin: "
            f"{type(e).__name__}: {e}")

    scratch = Path(clone).parent / f"cleanup-{uuid.uuid4().hex[:8]}"
    env = controller_git_env()
    try:
        canonical = validate_repo_url(wcfg["repo"], wcfg)
        scratch.mkdir(parents=True, exist_ok=True)
        _git(scratch, "init", "-q", timeout=timeout, env=env)
        # sanctioned-network-git: a fresh empty repo with no origin to
        # verify, addressing the validated canonical url explicitly, in the
        # sanitized environment. This is the path that exists BECAUSE the
        # clone could not be trusted.
        _git(scratch, "push", canonical, "--delete", branch, timeout=timeout,
             env=env)
        # sanctioned-network-git: read-back confirmation on the same
        # sanitized repo and the same validated url.
        left = _git(scratch, "ls-remote", "--heads", canonical, branch,
                    timeout=timeout, env=env, check=False)
        if left.strip():
            log(f"branch {branch} still exists on {canonical} after deletion")
            return False
        log(f"deleted {branch} from {canonical} through a sanitized repository")
        return True
    except Exception as e:
        log(f"sanitized deletion of {branch} failed: {type(e).__name__}: {e} — "
            f"failing closed, nothing was pushed anywhere else")
        return False
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _pr_body(submission, wcfg):
    cycle = submission["meta"]["_dada_cycle"]
    return (
        f"Autonomous submission from the **{wcfg.get('role', 'evolve')}** neighbor "
        f"of {wcfg.get('instance_name', 'a RAPP sentinel')}, acting on its own "
        f"initiative through its operator's GitHub identity.\n\n"
        f"- slug: `{submission['slug']}`\n"
        f"- kind: `{submission['kind']}`\n"
        f"- dada cycle: {cycle['cycle']} "
        f"(previous: `{cycle['previous_slug']}`)\n"
        f"- search: {len(cycle['rounds'])} round(s) x {CANDIDATES_PER_ROUND} "
        f"candidates, scored on {', '.join(SCORE_DIMENSIONS)}\n\n"
        f"What this PR does NOT do: it touches no existing file. The model that "
        f"made the piece could not commit, push, open this PR or merge it — a "
        f"controller validated the working tree against the submission protocol "
        f"and owns every git and GitHub operation here.\n"
    )


# ── bookkeeping ─────────────────────────────────────────────────────────────

def notification_for(outcome, role, detail, receipts=None):
    """The alert text, or None. Only a verified merge gets the paintbrush.

    The merged case is built by art_notification() instead — it is the only
    outcome that has a URL a human can tap.
    """
    if outcome == OUTCOME_CONTRIBUTED:
        url = (receipts or {}).get("pr_url", "")
        return (f"{SUCCESS_PREFIX} {role} contributed and it is merged:\n"
                f"{detail[:300]}\n{url}")
    if outcome == OUTCOME_DECLINED:
        return f"\u2022 {role} considered a contribution and declined:\n{detail[:300]}"
    return (f"\u26A0\uFE0F {role} evolution {outcome}:\n{detail[:400]}")


def _looks_like_a_path(repo):
    text = str(repo).strip()
    return text.startswith(("/", ".", "~")) or text.startswith("file://")


def art_repo(cfg, wcfg):
    """owner/name for the commons this instance publishes art to.

    commons_repo is the configured name for "the repository this instance
    contributes to", so it wins; the worker's own repo key is the fallback so
    an instance that set only one of them still gets links.
    """
    for candidate in (cfg.get("commons_repo"), wcfg.get("repo")):
        text = str(candidate or "").strip().rstrip("/")
        if not text or _looks_like_a_path(text):
            continue
        if text.endswith(".git"):
            text = text[:-4]
        if "://" in text:
            text = urlsplit(text).path
        elif text.startswith("git@") and ":" in text:
            text = text.split(":", 1)[1]
        parts = [p for p in text.strip("/").split("/") if p]
        if len(parts) >= 2:
            return parts[-2], parts[-1]
    return "", ""


def art_urls(cfg, wcfg, submission):
    """(view, source) for a merged piece, derived, never guessed at send time.

    The view URL is the GitHub Pages copy of the piece itself: one tap, the
    artwork, no navigation. The source URL is the same bytes on GitHub, which
    is what makes the message checkable by someone who does not trust it.
    """
    owner, name = art_repo(cfg, wcfg)
    if not owner or not name:
        return "", ""
    path = "/".join(quote(seg, safe="")
                    for seg in str(submission["piece_path"]).split("/"))
    branch = quote(str(wcfg.get("base_branch", "main")), safe="")
    return (f"https://{owner.lower()}.github.io/{name}/{path}",
            f"https://github.com/{owner}/{name}/blob/{branch}/{path}")


def raw_url(cfg, wcfg, submission):
    """The bytes on GitHub, served as a file rather than as a page."""
    owner, name = art_repo(cfg, wcfg)
    if not owner or not name:
        return ""
    path = "/".join(quote(seg, safe="")
                    for seg in str(submission["piece_path"]).split("/"))
    branch = quote(str(wcfg.get("base_branch", "main")), safe="")
    return f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{path}"


def probe_url(url, timeout=10):
    """(ok, detail) for one HTTP probe. Never raises."""
    if not url:
        return False, "no url"
    request = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "rapp-sentinel"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            code = getattr(response, "status", None) or response.getcode()
            response.read(1024)
            return (200 <= int(code) < 300), f"HTTP {code}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def verified_view(cfg, wcfg, submission, probe=None, sleep=None):
    """The URL a human can actually tap, proved before it is sent.

    GitHub Pages publishes on its own schedule, so a merge verified against
    origin/main can still 404 for a minute or two. Texting that URL is worse
    than texting nothing: it reads as "your art is live" and lands on a 404,
    which teaches the reader to distrust every future message. So the Pages
    copy is probed with a bounded backoff, the raw file is the fallback, and
    if neither answers the message says so rather than pretending (#10).

    Returns (url, kind, note) where kind is "pages", "raw" or "".
    """
    probe = probe or probe_url
    sleep = sleep or time.sleep
    view, _ = art_urls(cfg, wcfg, submission)
    raw = raw_url(cfg, wcfg, submission)
    timeout = int(wcfg.get("view_probe_timeout_s", 10))
    backoff = list(wcfg.get("view_probe_backoff") or (5, 10, 20, 30))
    attempts = max(1, int(wcfg.get("view_probe_attempts", len(backoff) + 1)))

    detail = "no url"
    for attempt in range(attempts):
        if view:
            ok, detail = probe(view, timeout)
            if ok:
                return view, "pages", ""
        if raw:
            ok_raw, detail_raw = probe(raw, timeout)
            if ok_raw:
                # Merged and readable, just not published as a page yet. Say
                # which one this is; a link that works is not a link that lies.
                if attempt + 1 >= attempts:
                    return raw, "raw", ("GitHub Pages has not published it yet — "
                                        "this link is the file itself.")
            else:
                detail = f"{detail}; raw {detail_raw}"
        if attempt + 1 < attempts:
            sleep(backoff[min(attempt, len(backoff) - 1)])
    if raw:
        ok_raw, _ = probe(raw, timeout)
        if ok_raw:
            return raw, "raw", ("GitHub Pages has not published it yet — this "
                                "link is the file itself.")
    return "", "", (f"the merge is verified on main, but no public URL answered "
                    f"yet ({detail}).")


def _first_sentence(text, limit=220):
    collapsed = " ".join(str(text).split())
    if not collapsed:
        return ""
    match = re.search(r"(?<=[.!?])\s", collapsed)
    sentence = (collapsed[:match.start()] if match else collapsed).rstrip()
    if len(sentence) > limit:
        sentence = sentence[:limit - 1].rstrip() + "\u2026"
    return sentence


def concept_sentence(meta, limit=220):
    """One sentence about what the thing IS, from the piece's own record.

    Preference order is "what the maker said about it", then the premise of
    the candidate that actually won its cycle — never a generated summary,
    because a summary nobody wrote is a claim nobody made.
    """
    for key in ("_concept", "_artist_statement", "_inspired_by"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return _first_sentence(value, limit)
    cycle = meta.get("_dada_cycle") or {}
    rounds = cycle.get("rounds") or []
    winner = (cycle.get("winner") or {}).get("candidate")
    if rounds:
        for cand in rounds[-1].get("candidates") or []:
            if cand.get("id") == winner and isinstance(cand.get("premise"), str):
                return _first_sentence(cand["premise"], limit)
    return _first_sentence(meta.get("title") or "a new piece", limit)


def art_recipient(cfg):
    """Where art news goes: the reports number, else the alert handle."""
    for key in ("report_number", "notify_handle"):
        value = str(cfg.get(key) or "").strip()
        if value:
            return value
    return ""


def art_notification(cfg, wcfg, submission, receipts, view="", note=""):
    """The message a verified merge earns. One message, one tap to the art.

    `view` is a URL that was PROBED after the merge, not one that was derived
    and hoped for.
    """
    _, source = art_urls(cfg, wcfg, submission)
    lines = [f"{SUCCESS_PREFIX} {sentinel.instance_name(cfg)}: "
             f"\u201c{submission['title']}\u201d is merged.",
             "",
             concept_sentence(submission["meta"])]
    if view:
        lines += ["", f"View: {view}"]
    if source:
        lines.append(f"Source: {source}")
    pr_url = (receipts or {}).get("pr_url", "")
    if pr_url:
        lines.append(f"PR: {pr_url}")
    if note:
        lines += ["", note]
    if not view and not source:
        # Say so rather than send a triumphant message with nowhere to go.
        lines += ["", "(no public URL derivable — set commons_repo to owner/name)"]
    return "\n".join(lines)


def record(history, row, path=None):
    """Append one durable row and persist the whole ledger atomically."""
    history.append(row)
    save_history(history, path)
    return row


def _emit_frame(role, payload):
    try:
        NB.emit(role, "neighbor.acted", payload)
    except Exception as e:
        log(f"chain frame refused: {type(e).__name__}: {e}")


def _notify_once(cfg, key, text, hours=24):
    """Say an operational failure out loud, but not every 30 minutes."""
    try:
        stamps = strict_load(ALERT_PATH, {}, expect=dict)
    except LedgerError:
        stamps = {}
    last = stamps.get(key)
    if last:
        try:
            if (sentinel.now() - datetime.fromisoformat(last)).total_seconds() < hours * 3600:
                return
        except ValueError:
            pass
    stamps[key] = sentinel.now().isoformat(timespec="seconds")
    try:
        atomic_write_json(ALERT_PATH, stamps)
    except OSError:
        pass
    sentinel.notify(cfg, text)


# ── the run ─────────────────────────────────────────────────────────────────

def write_status(outcome, reason="", **extra):
    """The worker's heartbeat. Written on EVERY pass, including the ones that
    decide to do nothing.

    A job that runs and skips and a job launchd never loaded look identical
    from outside — both produce no art and no log line anybody reads. This
    file is what lets w_evolve_worker tell those two apart (#6).
    """
    payload = {
        "at": sentinel.now().isoformat(timespec="seconds"),
        "outcome": outcome,
        "reason": str(reason)[:400],
        "pid": os.getpid(),
        "depth": SS.current_depth(),
        **extra,
    }
    try:
        atomic_write_json(STATUS_PATH, payload)
    except OSError as e:
        log(f"could not write the heartbeat: {type(e).__name__}: {e}")
    return payload


def _skip(reason):
    log(f"skipped — {reason}")
    write_status("skipped", reason)
    return {"outcome": "skipped", "reason": reason}


# ── crash-window reconciliation ─────────────────────────────────────────────

def transaction_writer(row_id, base):
    """Persist what this cycle has done so far, atomically, at every step."""
    state = dict(base)
    state["row_id"] = row_id

    def note(**fields):
        state.update({k: v for k, v in fields.items() if v is not None})
        state["at"] = sentinel.now().isoformat(timespec="seconds")
        atomic_write_json(TRANSACTION_PATH, state)
        return state
    return note


def clear_transaction():
    try:
        Path(TRANSACTION_PATH).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f"could not clear the transaction file: {e}")


def reconcile(cfg, wcfg, history):
    """Finish, or clean up after, a cycle that died mid-publish.

    The dangerous window is between `gh pr merge` and the ledger write: the
    art is public, the ledger says "pending", and the next cycle would compute
    the wrong cycle number and never tell anyone the piece exists. So before
    planning anything, a leftover transaction is resolved against PUBLIC
    state — the PR and origin/main, not our own hopes (#5).

    Returns a summary dict when it did something, else None.
    """
    state = strict_load(TRANSACTION_PATH, {}, expect=dict)
    if not state:
        return None
    row_id = state.get("row_id")
    row = next((r for r in history if r.get("id") == row_id), None)
    if row is None:
        log("transaction references a row this ledger does not have — clearing")
        clear_transaction()
        return None
    if row.get("outcome") != "pending":
        clear_transaction()
        return None

    pr_number = str(state.get("pr_number") or "")
    slug = state.get("slug") or row.get("slug") or ""
    log(f"reconciling an interrupted cycle (phase={state.get('phase')}, "
        f"pr={pr_number or 'none'})")

    if not pr_number:
        # Nothing public was created, or we died before we learned its number.
        row["outcome"] = OUTCOME_ABORTED
        row["detail"] = (f"interrupted at {state.get('phase')} before a PR "
                         f"existed; nothing was published")
        save_history(history)
        clear_transaction()
        return {"outcome": "reconciled-aborted", "detail": row["detail"]}

    workspace = _make_workspace(wcfg)
    try:
        clone = workspace / "clone"
        _clone_repo(wcfg, clone)
        assert_repo_integrity(clone, wcfg)
        submission = state.get("submission") or {}
        try:
            receipts = confirm_merge(clone, submission, wcfg, pr_number,
                                     sorted(state.get("paths") or []))
        except (CommandError, subprocess.TimeoutExpired, json.JSONDecodeError,
                KeyError, OSError) as e:
            # Not merged (or not verifiably merged): close it and say so.
            log(f"interrupted cycle did not land: {type(e).__name__}: {e}")
            _close_pr(wcfg["repo"], pr_number, int(wcfg.get("gh_timeout_s", 300)))
            row["outcome"] = OUTCOME_ABORTED
            row["detail"] = (f"interrupted at {state.get('phase')}; PR "
                             f"{pr_number} was not verifiably merged and has "
                             f"been closed ({type(e).__name__})")
            save_history(history)
            clear_transaction()
            return {"outcome": "reconciled-aborted", "detail": row["detail"]}

        receipts.update({"pr_url": state.get("pr_url", ""),
                         "pr_number": pr_number,
                         "branch": state.get("branch", "")})
        finalize_success(cfg, wcfg, history, row, submission, receipts,
                         state.get("next_state") or {},
                         int(state.get("cycle") or row.get("cycle") or 1))
        clear_transaction()
        log(f"reconciled: {slug} was merged before the interruption and is now "
            f"recorded")
        return {"outcome": "reconciled-contributed", "slug": slug,
                "receipts": receipts}
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def finalize_success(cfg, wcfg, history, row, submission, receipts, next_state,
                     expected_cycle):
    """The one place a verified merge becomes ledger, chain, and message."""
    state_path = HOME / str(wcfg["creative_state_file"])
    atomic_write_json(state_path, {
        **(next_state or {}),
        "cycle": expected_cycle,
        "last_slug": submission["slug"],
        "updated_at": sentinel.now().isoformat(timespec="seconds"),
        "merge_commit": receipts["merge_commit"],
    })
    return _finish(cfg, wcfg, history, row, OUTCOME_CONTRIBUTED,
                   f"{submission['title']} ({submission['slug']}) merged as "
                   f"{receipts['merge_commit'][:12]}", receipts, submission)


def run_once(cfg=None, health=None, role=None, dry_run=False):
    """One worker pass. Returns a summary dict; never raises for policy."""
    cfg = cfg if cfg is not None else sentinel.config()
    wcfg = worker_config(cfg)
    health = health or (lambda phase: sentinel.run_health(receipts=True))

    if STOP.exists():
        return _skip("STOP file present")
    if not worker_enabled(cfg):
        return _skip("evolve_worker.enabled is false — the tick still owns art")
    if int(cfg.get("level", 1)) < 3:
        return _skip(f"level {cfg.get('level')} < 3; evolution is a level-3 act")
    # A worker that finds itself already inside a sentinel's process tree is a
    # recursion, not a cycle. Refuse the whole pass, not just the fan-out: a
    # nested run would try to publish, and one cycle must mean one submission.
    depth = SS.current_depth()
    if depth > 0:
        return _skip(f"nested run refused — {SS.DEPTH_ENV}={depth}; "
                     f"sub-sentinels may not run the worker")

    lock = acquire_lock()
    if lock is None:
        return _skip("another worker holds the lock — a cycle is still running")

    workspace = None
    try:
        history = load_history()

        # Before anything else: finish or clean up an interrupted cycle. A
        # merged submission nobody recorded would otherwise make every future
        # cycle compute the wrong number and stay silent about live art (#5).
        healed = reconcile(cfg, wcfg, history)
        if healed:
            write_status(healed["outcome"], healed.get("detail", ""))
            return healed

        okb, used, cap = within_budget(history, wcfg)
        if not okb:
            return _skip(f"evolve budget spent ({used}/{cap}); "
                         f"repair capacity untouched")
        ready, why = cadence_ready(history, wcfg)
        if not ready:
            return _skip(why)

        turn = strict_load(TURN_PATH, {"i": 0}, expect=dict)
        roles = roles_for(wcfg)
        slug_role = role or roles[int(turn.get("i", 0)) % len(roles)]
        wcfg["role"] = slug_role
        wcfg["instance_name"] = sentinel.instance_name(cfg)

        # Health at the start: never spend a model on art while the estate is
        # broken, and never let a degraded-but-unlisted estate through (#3).
        # A probe that raises is a stop, not a shrug (#5).
        try:
            probe_health(health, "start", wcfg)
        except AbortError as e:
            return _skip(str(e))

        state_path = HOME / str(wcfg["creative_state_file"])
        creative = strict_load(state_path, {}, expect=dict)
        expected_cycle = int(creative.get("cycle", 0)) + 1
        expected_previous = creative.get("last_slug")

        fcfg = SS.fanout_config(wcfg)
        if dry_run:
            specs, note = SS.plan_children(fcfg, history, depth)
            summary = {"outcome": "dry-run", "role": slug_role,
                       "cycle": expected_cycle, "previous_slug": expected_previous,
                       "budget": f"{used}/{cap}", "depth": depth,
                       "children": [s["name"] for s in specs], "fanout": note,
                       "maker_argv": maker_argv(
                           wcfg, HOME / "state" / "evolve-workspaces" /
                           "example" / "staging")}
            write_status("dry-run", note, role=slug_role, cycle=expected_cycle)
            return summary

        # The fan-out cast is decided BEFORE the workspace exists, so an
        # unavailable critic costs nothing (#7).
        specs, fanout_note = ([], "fan-out disabled")
        if SS.enabled(fcfg):
            specs, fanout_note = SS.plan_children(fcfg, history, depth)
            if not specs:
                return _skip(f"fan-out unavailable: {fanout_note}")

        install_signal_handlers()
        workspace = _make_workspace(wcfg)
        clone = workspace / "clone"          # controller-private, never shared
        staging = workspace / "staging"      # the maker's whole world
        (staging / "out").mkdir(parents=True)
        (staging / "context").mkdir(parents=True)
        (workspace / "runtime").mkdir(parents=True, exist_ok=True)
        base_sha = _clone_repo(wcfg, clone)
        assert_repo_integrity(clone, wcfg)
        known_slugs = base_branch_slugs(clone, wcfg.get("base_branch", "main"),
                                        wcfg)
        if creative:
            atomic_write_json(staging / "state-in.json", creative)

        # One row per cycle, written BEFORE the first model process of any
        # kind, and the child debit lands with it: a future that raises, a
        # SIGTERM mid-wave or a crash must never hand back credit that was
        # already spent (#9).
        stamp = sentinel.now()
        row = {
            "id": uuid.uuid4().hex,
            "at": stamp.isoformat(timespec="seconds"),
            "mode": "evolve",
            "role": slug_role,
            "cycle": expected_cycle,
            "outcome": "pending",
            "children": len(specs),
            "child_failures": [],
            "result": "",
        }
        record(history, row)
        turn["i"] = int(turn.get("i", 0)) + 1
        atomic_write_json(TURN_PATH, turn)
        write_status("running", "cycle started", role=slug_role,
                     cycle=expected_cycle, children=len(specs))

        finalists, digest = None, None
        if specs:
            log(f"fanning out to {len(specs)} sub-sentinels ({fanout_note})")
            situation = fanout_situation(cfg, wcfg, slug_role, expected_cycle,
                                         expected_previous)
            results = SS.run_children(specs, fcfg, workspace, expected_cycle,
                                      sentinel.instance_name(cfg), slug_role,
                                      _prior_submissions(clone), depth, log,
                                      situation)
            row["children"] = max(len(specs), len(results))
            row["child_failures"] = [f"{r['role']}: {r['error']}"
                                     for r in results if not r["ok"]]
            save_history(history)
            try:
                finalists, digest = SS.aggregate(results, fcfg)
            except SS.FanoutError as e:
                return _finish(cfg, wcfg, history, row, OUTCOME_FANOUT, str(e))
            atomic_write_json(staging / "finalists.json",
                              {"finalists": finalists, "digest": digest})
            atomic_write_json(staging / "round1.json",
                              SS.round1_array(finalists))
            log(f"aggregated {len(finalists)} finalists from "
                f"{digest['healthy']}/{len(results)} healthy children")

        # The bounded read context the maker gets INSTEAD of a repository.
        atomic_write_json(staging / "context" / "prior.json",
                          _prior_submissions(clone))
        prompt = build_prompt(cfg, wcfg, slug_role, staging, expected_cycle,
                              expected_previous, history, finalists, digest)
        (workspace / "prompt.txt").write_text(prompt, encoding="utf-8")
        log(f"handing {slug_role} its situation (cycle {expected_cycle}, "
            f"budget {used + 1}/{cap})")
        status, output = run_model(staging, prompt, wcfg, depth,
                                   workspace / "runtime")
        try:
            (LOGS / f"evolve-worker-{slug_role}-{sentinel.now():%Y%m%d-%H%M%S}.log"
             ).write_text(output, encoding="utf-8")
        except OSError:
            pass
        row["result"] = sentinel.result_line(output)[:300]
        save_history(history)

        if status != "ok":
            return _finish(cfg, wcfg, history, row,
                           OUTCOME_TIMEOUT if status == OUTCOME_TIMEOUT
                           else OUTCOME_FAILED,
                           output.strip()[:300] or status)

        line = sentinel.result_line(output)
        try:
            # The staging tree first — no git anywhere near it — and only then
            # the two validated blobs into the controller's own clone.
            assert_no_git(staging)
            submission = gate_directory(staging / "out", wcfg, expected_cycle,
                                        expected_previous,
                                        SS.expected_round1(finalists)
                                        if finalists else None,
                                        known_slugs)
            install_into_clone(clone, submission)
            verify_clone_scope(clone, submission, wcfg, base_sha)
        except GateError as e:
            if str(line).upper().startswith("DECLINED") and "no new submission" in str(e):
                return _finish(cfg, wcfg, history, row, OUTCOME_DECLINED, line)
            return _finish(cfg, wcfg, history, row, OUTCOME_REJECTED,
                           f"gate: {e}")
        except (CommandError, subprocess.TimeoutExpired, OSError) as e:
            return _finish(cfg, wcfg, history, row, OUTCOME_FAILED,
                           f"gate: {type(e).__name__}: {e}")

        try:
            next_state = validate_next_state(staging / "state-out.json", wcfg,
                                             expected_cycle, submission["slug"])
        except GateError as e:
            return _finish(cfg, wcfg, history, row, OUTCOME_REJECTED, f"gate: {e}")

        note = transaction_writer(row["id"], {
            "phase": "gated", "slug": submission["slug"], "cycle": expected_cycle,
            "role": slug_role, "repo": wcfg["repo"],
            "base_branch": wcfg.get("base_branch", "main"),
            # The whole gated submission record, including meta: a
            # reconciled cycle must be able to write the same ledger entry
            # and send the same message the interrupted one would have.
            "submission": {k: submission[k] for k in
                           ("slug", "title", "kind", "meta", "meta_path",
                            "piece_path", "meta_sha256", "piece_sha256")},
            # (bytes are deliberately not persisted: the digests are the
            # contract, and reconciliation re-reads the merged file anyway)
            "next_state": next_state,
        })
        note()
        try:
            receipts = publish(clone, submission, wcfg, health, transaction=note)
        except AbortError as e:
            clear_transaction()
            return _finish(cfg, wcfg, history, row, OUTCOME_ABORTED, str(e))
        except GateError as e:
            clear_transaction()
            return _finish(cfg, wcfg, history, row, OUTCOME_REJECTED, f"gate: {e}")
        except (CommandError, subprocess.TimeoutExpired, json.JSONDecodeError,
                OSError, ValueError, KeyError) as e:
            clear_transaction()
            return _finish(cfg, wcfg, history, row, OUTCOME_FAILED,
                           f"publish: {type(e).__name__}: {e}")

        # Only here — merged, re-read, byte-checked — does the ledger move.
        summary = finalize_success(cfg, wcfg, history, row, submission, receipts,
                                   next_state, expected_cycle)
        clear_transaction()
        return summary
    except LedgerError as e:
        log(f"FAIL-CLOSED: {e}")
        write_status("fail-closed", str(e))
        _notify_once(cfg, "ledger", f"\u26A0\uFE0F {sentinel.instance_name(cfg)}: "
                                    f"the evolve worker stopped — {e}")
        return {"outcome": "fail-closed", "reason": str(e)}
    except KeyboardInterrupt as e:
        # A signal arrived. The handler already killed the tree; record the
        # interruption honestly and let the finally block clean up.
        log(f"interrupted: {e}")
        write_status("interrupted", str(e))
        return {"outcome": "interrupted", "reason": str(e)}
    except Exception as e:
        log(f"worker crashed: {type(e).__name__}: {e}")
        write_status("crashed", f"{type(e).__name__}: {e}")
        _notify_once(cfg, "crash", f"\u26A0\uFE0F {sentinel.instance_name(cfg)}: "
                                   f"the evolve worker crashed — "
                                   f"{type(e).__name__}: {e}")
        return {"outcome": "crashed", "reason": f"{type(e).__name__}: {e}"}
    finally:
        # Order matters: kill everything this pass started, THEN delete the
        # workspace those processes were writing into, THEN release the lock.
        # Releasing first would let the next pass start while a 30-minute
        # model is still running against a directory we are deleting (#4).
        still_running = kill_tracked()
        if still_running:
            log(f"terminated {still_running} live model process tree(s) on the "
                f"way out")
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)
        release_lock(lock)


def fanout_situation(cfg, wcfg, role, cycle, previous_slug):
    """The bounded description of the moment, handed to tool-less children."""
    return (f"collective: {sentinel.instance_name(cfg)}\n"
            f"neighbor acting: {role}\n"
            f"cycle: {cycle} (the previous submission was "
            f"{previous_slug or 'none — this is the first'})\n"
            f"commons: {wcfg.get('repo')}\n"
            f"standing directive: "
            f"{sentinel.evolve_brief(cfg) or 'none'}\n"
            f"the piece must be one self-contained file (svg, md, txt or json) "
            f"under {int(wcfg.get('max_piece_bytes', 51200)) // 1024} KB, "
            f"CC0-1.0, in submissions/<slug>/")


def _finish(cfg, wcfg, history, row, outcome, detail, receipts=None,
            submission=None):
    """Close the row out honestly, then chain-frame and notify."""
    row["outcome"] = outcome
    row["detail"] = str(detail)[:400]
    if receipts:
        row["pr_url"] = receipts.get("pr_url")
        row["merge_commit"] = receipts.get("merge_commit")
        row["paths"] = receipts.get("paths")
    if submission:
        row["slug"] = submission["slug"]
    save_history(history)

    _emit_frame(row["role"], {
        "act": "evolve", "outcome": outcome, "cycle": row.get("cycle"),
        "result": row["detail"][:300], "model": wcfg.get("model"),
        "merged": bool(receipts and receipts.get("merge_commit")),
        "pr": (receipts or {}).get("pr_url", ""),
        "children": int(row.get("children") or 0),
        "child_failures": row.get("child_failures") or [],
    })

    text = notification_for(outcome, row["role"], str(detail), receipts)
    if outcome == OUTCOME_CONTRIBUTED and submission and receipts:
        # The one message a human wants: the title, the artwork itself, and
        # the evidence. Sent HERE and nowhere else, because here is after the
        # merge commit was fetched back and the merged bytes were compared —
        # a message that says "merged" for anything less is a lie with a link.
        #
        # The View URL is PROBED first (#10): Pages publishes on its own
        # schedule, and a triumphant 404 teaches the reader to ignore the
        # next one.
        #
        # rebuild=True: the static report attached to this alert renders the
        # chains this cycle just wrote. Rebuilding first is the difference
        # between linked evidence and linked yesterday.
        view, kind, view_note = verified_view(cfg, wcfg, submission)
        row["view_url"], row["view_kind"] = view, kind
        save_history(history)
        sentinel.notify(cfg, art_notification(cfg, wcfg, submission, receipts,
                                              view, view_note),
                        to=art_recipient(cfg), rebuild=True)
    elif text and (outcome != OUTCOME_DECLINED or wcfg.get("notify_declines")):
        sentinel.notify(cfg, text)
    log(f"{row['role']}: {outcome} — {row['detail'][:200]}")
    write_status(outcome, row["detail"], role=row["role"],
                 cycle=row.get("cycle"), children=row.get("children", 0),
                 slug=row.get("slug", ""), pr=(receipts or {}).get("pr_url", ""))
    return {"outcome": outcome, "role": row["role"], "detail": row["detail"],
            "receipts": receipts or {}}


def _make_workspace(wcfg):
    root = Path(wcfg["workspace_root"])
    if not root.is_absolute():
        root = HOME / root
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / f"cycle-{sentinel.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
    workspace.mkdir(parents=True)
    return workspace


def _clone_repo(wcfg, clone):
    """Build the controller's clone without ever running `git clone`.

    `git clone <url>` reads global and system config BEFORE it resolves the
    url, so an `url.<attacker>.insteadOf <canonical>` rewrite produced a
    flawless clone of somebody else's repository — and the integrity check,
    which reads local config, then confirmed it. The repository has to be
    assembled by the controller instead:

      init an empty repo -> set origin to the validated canonical url ->
      verify integrity (now meaningful: the config is ours and nothing has
      touched the network) -> fetch through the chokepoint -> check out.

    Every one of those commands runs in the sanitized environment, so no
    rewrite, proxy, helper, template or alternates file from outside can
    take part.
    """
    clone = Path(clone)
    canonical = validate_repo_url(wcfg["repo"], wcfg)
    base = wcfg.get("base_branch", "main")
    git_t = int(wcfg.get("git_timeout_s", 600))
    depth = int(wcfg.get("clone_depth", 50) or 0)

    clone.mkdir(parents=True, exist_ok=True)
    _git(clone, "init", "-q", "-b", base, timeout=git_t)
    _git(clone, "config", "remote.origin.url", canonical, timeout=git_t)
    _git(clone, "config", "remote.origin.fetch",
         f"+refs/heads/{base}:refs/remotes/origin/{base}", timeout=git_t)
    # Before the first network byte, not after it.
    assert_repo_integrity(clone, wcfg)

    fetch_args = ["fetch", "--no-tags"]
    if depth > 0:
        fetch_args += ["--depth", str(depth)]
    fetch_args += ["origin", base]
    _git_remote(clone, wcfg, *fetch_args, timeout=git_t)
    _git(clone, "checkout", "-q", "-B", base, f"origin/{base}", timeout=git_t)
    return _git(clone, "rev-parse", "HEAD", timeout=git_t).strip()


def _repo_url(repo):
    """owner/name, a full URL, or a local path (used by the tests)."""
    text = str(repo).strip()
    if "://" in text or text.startswith(("git@", "/", ".", "~")):
        return os.path.expanduser(text)
    return f"https://github.com/{text}.git"


def _prior_submissions(clone, limit=200):
    """Every published submission, bounded, for the children to dig through.

    Read from the clone by the PARENT and handed over as plain JSON: a child
    that never sees a repository cannot be tempted to write to one.
    """
    root = Path(clone) / "submissions"
    prior = []
    if not root.is_dir():
        return prior
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        meta_path = directory / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            prior.append({"slug": directory.name, "title": "", "kind": "",
                          "statement": "(meta.json unreadable)"})
            continue
        statement = str(meta.get("_artist_statement")
                        or meta.get("_inspired_by") or "")
        prior.append({
            "slug": str(meta.get("slug") or directory.name),
            "title": str(meta.get("title") or ""),
            "kind": str(meta.get("kind") or ""),
            "submitted_at": str(meta.get("submitted_at") or ""),
            "remix_of": meta.get("remix_of"),
            "statement": statement[:600],
        })
        if len(prior) >= limit:
            break
    return prior


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="run every gate, spend no model")
    ap.add_argument("--role", default=None,
                    help="force one neighbor instead of the rotation")
    args = ap.parse_args(argv)
    summary = run_once(role=args.role, dry_run=args.dry_run)
    # Print the decision: a dry run that says nothing is indistinguishable from
    # a dry run that did nothing, and this is the command an operator uses to
    # check the wiring after install-launchd.sh --with-evolve-worker.
    print(json.dumps(summary, indent=2, default=str), flush=True)
    if summary.get("outcome") in ("fail-closed", "crashed"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
