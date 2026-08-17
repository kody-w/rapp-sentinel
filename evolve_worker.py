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
import subprocess
import sys
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


def health_gate(wcfg, verdict, phase="start"):
    """Health decides whether art may proceed. Returns (ok, reason).

    Critical is always a stop. Degraded is a stop UNLESS every failing id is
    named in `degraded_allowlist` — an explicit list of known-noisy checks,
    not `evolve_on_degraded`, which said "any degradation is fine" and would
    have let this worker push art through an estate that was quietly on fire
    in a way nobody had looked at yet (#3).
    """
    critical = list(verdict.get("critical") or [])
    if critical:
        return False, f"critical checks failing at {phase}: {', '.join(sorted(critical))}"
    failing = list(verdict.get("failed") or [])
    if not failing:
        return True, f"healthy at {phase}"
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
  {workspace}/clone          a fresh clone of {repo} that this worker made
  {workspace}/state-in.json  your private creative state from last cycle
                             ({state_in_note})
  {workspace}/state-out.json the private next state you must write

Do not read or write anything outside {workspace}. Do not touch any other
checkout on this machine; the operator keeps thousands of uncommitted files.

YOU MAY NOT PUBLISH. THIS IS ABSOLUTE.
Do NOT run `git commit`, `git add`, `git push`, `git merge`, `git tag`,
`gh pr create`, `gh pr merge`, `gh api` with a write method, or any other
command that changes a remote or the clone's history. Do not create branches.
Leave your files UNCOMMITTED and UNSTAGED in the clone's working tree.

A controller — code, not a model — reads what you leave behind, checks it
against the submission protocol deterministically, and only then creates the
branch, the commit, the pull request and the merge. If you publish anything
yourself, the controller rejects the whole cycle and nothing you made survives.

WHAT TO LEAVE BEHIND
Exactly one new directory: {workspace}/clone/submissions/<your-slug>/
containing exactly two files:

  meta.json     the protocol record (schema below)
  piece.<ext>   the work itself; ext is one of .svg .md .txt .json and MUST
                match meta.kind; at most {max_piece_kb} KB

Do not edit, move, rename or delete ANY existing file in the clone —
including submissions/index.json and any other submission's folder. The
controller re-reads the working tree and refuses anything else.

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
        round_one = ('Round 1 MUST contain exactly the ten finalist ids above, '
                     'verbatim. Later rounds (up to 5) may use ids of your own.')
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

def _git(cwd, *args, timeout=600, check=True):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise CommandError(f"git {' '.join(args)} exited {r.returncode}: "
                           f"{(r.stderr or r.stdout or '').strip()[:300]}")
    return r.stdout


def _git_bytes(cwd, *args, timeout=600):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       timeout=timeout)
    if r.returncode != 0:
        raise CommandError(f"git {' '.join(args)} exited {r.returncode}")
    return r.stdout


def _gh(*args, timeout=300):
    r = subprocess.run(["gh", *args], capture_output=True, text=True,
                       timeout=timeout)
    if r.returncode != 0:
        raise CommandError(f"gh {' '.join(args)} exited {r.returncode}: "
                           f"{(r.stderr or r.stdout or '').strip()[:300]}")
    return r.stdout


def run_model(workspace, prompt, wcfg):
    """Spend the model in the workspace. Returns (outcome, output).

    Outcome is only ever "ok", "timeout" or "failed" here — whether the model
    actually CONTRIBUTED is not something its exit code or its last line is
    allowed to decide (R1).

    The maker inherits a depth marker too: if it shells back into
    evolve_worker.py, that run refuses itself rather than starting a second
    cycle inside this one.
    """
    cmd = ["copilot", "-p", prompt, "--allow-all", "--model", wcfg["model"]]
    timeout_s = int(wcfg["timeout_s"])
    env = dict(os.environ)
    env[SS.DEPTH_ENV] = str(SS.current_depth() + 1)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout_s, cwd=str(workspace), env=env)
    except subprocess.TimeoutExpired:
        return OUTCOME_TIMEOUT, f"copilot timed out after {timeout_s}s"
    except FileNotFoundError:
        return OUTCOME_FAILED, "copilot CLI not found on PATH"
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        return OUTCOME_FAILED, out or f"copilot exited {r.returncode}"
    return "ok", out


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
    """Exactly one new submissions/<slug>/ and nothing else touched."""
    slugs, files = set(), []
    for code, path, origin in changes:
        if code != "??":
            raise GateError(f"the model changed an existing path: {code} {path}"
                            + (f" (from {origin})" if origin else ""))
        parts = Path(path).parts
        if len(parts) < 3 or parts[0] != "submissions":
            raise GateError(f"file outside submissions/<slug>/: {path}")
        slugs.add(parts[1])
        files.append(path)
    if not slugs:
        raise GateError("no new submission was left in the clone")
    if len(slugs) > 1:
        raise GateError(f"more than one new submission: {', '.join(sorted(slugs))}")
    slug = slugs.pop()
    return slug, sorted(files)


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
                        expected_round1_ids=None):
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
        if index == 1 and expected_round1_ids is not None:
            # The fan-out's ten finalists ARE round one. Without this the
            # sub-sentinels would be theatre: a maker could run three child
            # processes, ignore every one of them, and publish whatever it
            # already had in mind with a deliberation section attached.
            wanted = set(expected_round1_ids)
            if seen != wanted:
                missing = sorted(wanted - seen)
                added = sorted(seen - wanted)
                raise GateError(
                    "round 1 must be exactly the ten finalists the "
                    "sub-sentinels produced"
                    + (f"; missing {missing}" if missing else "")
                    + (f"; unexpected {added}" if added else ""))
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


def validate_submission(clone, wcfg, expected_cycle, expected_previous,
                        base_branch="main", base_sha=None,
                        expected_round1_ids=None):
    """Everything the controller must prove before a single remote call.

    Returns a dict describing the submission. Raises GateError otherwise.
    """
    clone = Path(clone)
    git_timeout = int(wcfg.get("git_timeout_s", 600))
    if base_sha:
        head = _git(clone, "rev-parse", "HEAD", timeout=git_timeout).strip()
        if head != base_sha:
            raise GateError("the model moved the clone's HEAD — it committed its "
                            "own work, and publishing is the controller's job")
    changes = working_tree_changes(clone, timeout=git_timeout)
    slug, files = _new_submission_dir(changes)

    if not SLUG_RE.match(slug) or len(slug) > SLUG_MAX:
        raise GateError(f"slug {slug!r} is not lowercase-alphanumeric-hyphen "
                        f"within {SLUG_MAX} characters")

    tracked = _git(clone, "ls-tree", "--name-only", base_branch,
                   f"submissions/{slug}/", timeout=git_timeout).strip()
    if tracked:
        raise GateError(f"slug {slug!r} already exists on {base_branch}")

    directory = clone / "submissions" / slug
    on_disk = sorted(p.relative_to(clone).as_posix()
                     for p in directory.rglob("*") if p.is_file())
    if on_disk != files:
        raise GateError(f"the submission folder holds {on_disk}, but git reports "
                        f"{files}")
    names = sorted(Path(f).name for f in files)
    if len(names) != 2 or "meta.json" not in names:
        raise GateError(f"a submission is exactly meta.json + piece.<ext>, found "
                        f"{names}")
    piece_name = [n for n in names if n != "meta.json"][0]
    piece_path = directory / piece_name

    meta_path = directory / "meta.json"
    if meta_path.stat().st_size > int(wcfg.get("max_meta_bytes", 262144)):
        raise GateError("meta.json is implausibly large")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
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
        known = _git(clone, "ls-tree", "--name-only", base_branch,
                     f"submissions/{remix}/", timeout=git_timeout).strip()
        if not known:
            raise GateError(f"meta.remix_of {remix!r} does not exist on {base_branch}")

    raw = _check_piece(piece_path, kind, int(wcfg.get("max_piece_bytes", 51200)))
    validate_dada_cycle(meta.get("_dada_cycle"), slug, expected_cycle,
                        expected_previous, expected_round1_ids)

    return {
        "slug": slug,
        "title": meta["title"],
        "kind": kind,
        "meta": meta,
        "meta_path": f"submissions/{slug}/meta.json",
        "piece_path": f"submissions/{slug}/{piece_name}",
        "piece_sha256": hashlib.sha256(raw).hexdigest(),
        "meta_sha256": hashlib.sha256(meta_path.read_bytes()).hexdigest(),
    }


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

def publish(clone, submission, wcfg, health, branch=None):
    """Branch, commit, PR, verify, re-check health, merge, re-read main.

    Every remote step is preceded by a health run and followed by evidence
    read back from GitHub. Returns a dict of receipts; raises AbortError or
    CommandError. A PR opened before an abort is closed on the way out, so a
    stopped cycle does not leave a half-open contribution behind.
    """
    clone = Path(clone)
    repo = wcfg["repo"]
    base = wcfg.get("base_branch", "main")
    git_t = int(wcfg.get("git_timeout_s", 600))
    gh_t = int(wcfg.get("gh_timeout_s", 300))
    slug = submission["slug"]
    branch = branch or f"{wcfg.get('branch_prefix', 'art')}/{slug}-{uuid.uuid4().hex[:8]}"
    paths = sorted([submission["meta_path"], submission["piece_path"]])

    # Health immediately before the FIRST remote write (#3). Not the health we
    # ran before the model: that reading is up to half an hour old by now, and
    # the estate is exactly what may have changed while the model thought.
    ok, why = health_gate(wcfg, health("pre-write"), phase="pre-write")
    if not ok:
        raise AbortError(why)

    _git(clone, "fetch", "--no-tags", "origin", base, timeout=git_t)
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
    _git(clone, "push", "--set-upstream", "origin", branch, timeout=git_t)

    pr_url, pr_number = "", ""
    try:
        body = _pr_body(submission, wcfg)
        pr_url = _gh("pr", "create", "--repo", repo, "--base", base,
                     "--head", branch, "--title",
                     f"art: {submission['title']} ({slug})",
                     "--body", body, timeout=gh_t).strip().splitlines()[-1].strip()
        pr_number = pr_url.rstrip("/").split("/")[-1]

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
        ok, why = health_gate(wcfg, health("pre-merge"), phase="pre-merge")
        if not ok:
            raise AbortError(why)

        _gh("pr", "merge", pr_number, "--repo", repo, "--squash",
            "--delete-branch", timeout=gh_t)
    except (AbortError, GateError, CommandError):
        # Leave nothing half-published behind: close the PR if we opened one,
        # and delete the branch we pushed if we never got that far.
        if pr_number:
            _close_pr(repo, pr_number, gh_t)
        else:
            _delete_remote_branch(clone, branch, git_t)
        raise

    # Evidence, freshly re-read: merged state, merge commit, its file scope,
    # and the bytes now living on the base branch (R1).
    merged = json.loads(_gh("pr", "view", pr_number, "--repo", repo, "--json",
                            "state,merged,mergeCommit", timeout=gh_t))
    if not merged.get("merged") or str(merged.get("state")).upper() != "MERGED":
        raise CommandError(f"PR {pr_number} is not merged after the merge call: "
                           f"{merged.get('state')!r}")
    merge_sha = ((merged.get("mergeCommit") or {}).get("oid") or "").strip()
    if not merge_sha:
        raise CommandError(f"PR {pr_number} reports no merge commit")

    _git(clone, "fetch", "--no-tags", "origin", base, timeout=git_t)
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

    return {"branch": branch, "commit": head, "pr_url": pr_url,
            "pr_number": pr_number, "merge_commit": merge_sha,
            "base_sha": main_sha, "paths": paths}


def _close_pr(repo, pr_number, timeout):
    if not pr_number:
        return
    try:
        _gh("pr", "close", str(pr_number), "--repo", repo, "--delete-branch",
            timeout=timeout)
        log(f"closed PR {pr_number} after aborting the cycle")
    except Exception as e:
        log(f"could not close PR {pr_number}: {type(e).__name__}: {e}")


def _delete_remote_branch(clone, branch, timeout):
    try:
        _git(clone, "push", "origin", "--delete", branch, timeout=timeout)
        log(f"deleted the pushed branch {branch} after aborting the cycle")
    except Exception as e:
        log(f"could not delete branch {branch}: {type(e).__name__}: {e}")


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


def art_notification(cfg, wcfg, submission, receipts):
    """The message a verified merge earns. One message, one tap to the art."""
    view, source = art_urls(cfg, wcfg, submission)
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

def _skip(reason):
    log(f"skipped — {reason}")
    return {"outcome": "skipped", "reason": reason}


def run_once(cfg=None, health=None, role=None, dry_run=False):
    """One worker pass. Returns a summary dict; never raises for policy."""
    cfg = cfg if cfg is not None else sentinel.config()
    wcfg = worker_config(cfg)
    health = health or (lambda phase: sentinel.run_health())

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
        ok, why = health_gate(wcfg, health("start"), phase="start")
        if not ok:
            return _skip(why)

        state_path = HOME / str(wcfg["creative_state_file"])
        creative = strict_load(state_path, {}, expect=dict)
        expected_cycle = int(creative.get("cycle", 0)) + 1
        expected_previous = creative.get("last_slug")

        if dry_run:
            depth = SS.current_depth()
            specs, note = SS.plan_children(SS.fanout_config(wcfg), history, depth)
            return {"outcome": "dry-run", "role": slug_role,
                    "cycle": expected_cycle, "previous_slug": expected_previous,
                    "budget": f"{used}/{cap}", "health": why, "depth": depth,
                    "children": [s["name"] for s in specs], "fanout": note}

        workspace = _make_workspace(wcfg)
        clone = workspace / "clone"
        base_sha = _clone_repo(wcfg, clone)
        if creative:
            atomic_write_json(workspace / "state-in.json", creative)

        # One row per cycle, written BEFORE the first model process of any
        # kind. A crash between here and the end must never look like free
        # budget, and children cost real credits even when the maker never
        # runs (fan-out accounting rides on this row's "children" count).
        stamp = sentinel.now()
        row = {
            "id": uuid.uuid4().hex,
            "at": stamp.isoformat(timespec="seconds"),
            "mode": "evolve",
            "role": slug_role,
            "cycle": expected_cycle,
            "outcome": "pending",
            "children": 0,
            "result": "",
        }
        record(history, row)
        turn["i"] = int(turn.get("i", 0)) + 1
        atomic_write_json(TURN_PATH, turn)

        finalists, digest = None, None
        fcfg = SS.fanout_config(wcfg)
        if SS.enabled(fcfg):
            depth = SS.current_depth()
            specs, note = SS.plan_children(fcfg, history, depth)
            if not specs:
                # Fan-out enabled but unable to run is NOT a licence to make
                # art alone: "the collective deliberated" and "one model had a
                # think" are different claims, and only one would be true.
                row["outcome"] = "skipped"
                row["skipped"] = True
                row["detail"] = f"fan-out unavailable: {note}"
                save_history(history)
                return _skip(f"fan-out unavailable: {note}")
            log(f"fanning out to {len(specs)} sub-sentinels ({note})")
            results = SS.run_children(specs, fcfg, workspace, expected_cycle,
                                      sentinel.instance_name(cfg), slug_role,
                                      _prior_submissions(clone), depth, log)
            row["children"] = len(results)
            row["child_failures"] = [f"{r['role']}: {r['error']}"
                                     for r in results if not r["ok"]]
            save_history(history)
            try:
                finalists, digest = SS.aggregate(results, fcfg)
            except SS.FanoutError as e:
                return _finish(cfg, wcfg, history, row, OUTCOME_FANOUT, str(e))
            atomic_write_json(workspace / "finalists.json",
                              {"finalists": finalists, "digest": digest})
            log(f"aggregated {len(finalists)} finalists from "
                f"{digest['healthy']}/{len(results)} healthy children")

        prompt = build_prompt(cfg, wcfg, slug_role, workspace, expected_cycle,
                              expected_previous, history, finalists, digest)
        (workspace / "prompt.txt").write_text(prompt, encoding="utf-8")
        log(f"handing {slug_role} its situation (cycle {expected_cycle}, "
            f"budget {used + 1}/{cap})")
        status, output = run_model(workspace, prompt, wcfg)
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
            submission = validate_submission(clone, wcfg, expected_cycle,
                                             expected_previous,
                                             wcfg.get("base_branch", "main"),
                                             base_sha,
                                             [c["id"] for c in finalists]
                                             if finalists else None)
        except GateError as e:
            if str(line).upper().startswith("DECLINED") and "no new submission" in str(e):
                return _finish(cfg, wcfg, history, row, OUTCOME_DECLINED, line)
            return _finish(cfg, wcfg, history, row, OUTCOME_REJECTED,
                           f"gate: {e}")
        except CommandError as e:
            return _finish(cfg, wcfg, history, row, OUTCOME_FAILED, f"gate: {e}")

        try:
            next_state = validate_next_state(workspace / "state-out.json", wcfg,
                                             expected_cycle, submission["slug"])
        except GateError as e:
            return _finish(cfg, wcfg, history, row, OUTCOME_REJECTED, f"gate: {e}")

        try:
            receipts = publish(clone, submission, wcfg, health)
        except AbortError as e:
            return _finish(cfg, wcfg, history, row, OUTCOME_ABORTED, str(e))
        except GateError as e:
            return _finish(cfg, wcfg, history, row, OUTCOME_REJECTED, f"gate: {e}")
        except (CommandError, subprocess.TimeoutExpired) as e:
            return _finish(cfg, wcfg, history, row, OUTCOME_FAILED,
                           f"publish: {type(e).__name__}: {e}")

        # Only here — merged, re-read, byte-checked — does the ledger move.
        atomic_write_json(state_path, {
            **next_state,
            "cycle": expected_cycle,
            "last_slug": submission["slug"],
            "updated_at": sentinel.now().isoformat(timespec="seconds"),
            "merge_commit": receipts["merge_commit"],
        })
        return _finish(cfg, wcfg, history, row, OUTCOME_CONTRIBUTED,
                       f"{submission['title']} ({submission['slug']}) merged as "
                       f"{receipts['merge_commit'][:12]}", receipts, submission)
    except LedgerError as e:
        log(f"FAIL-CLOSED: {e}")
        _notify_once(cfg, "ledger", f"\u26A0\uFE0F {sentinel.instance_name(cfg)}: "
                                    f"the evolve worker stopped — {e}")
        return {"outcome": "fail-closed", "reason": str(e)}
    except Exception as e:
        log(f"worker crashed: {type(e).__name__}: {e}")
        _notify_once(cfg, "crash", f"\u26A0\uFE0F {sentinel.instance_name(cfg)}: "
                                   f"the evolve worker crashed — "
                                   f"{type(e).__name__}: {e}")
        return {"outcome": "crashed", "reason": f"{type(e).__name__}: {e}"}
    finally:
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)
        release_lock(lock)


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
        # rebuild=True: the static report attached to this alert renders the
        # chains this cycle just wrote. Rebuilding first is the difference
        # between linked evidence and linked yesterday.
        sentinel.notify(cfg, art_notification(cfg, wcfg, submission, receipts),
                        to=art_recipient(cfg), rebuild=True)
    elif text and (outcome != OUTCOME_DECLINED or wcfg.get("notify_declines")):
        sentinel.notify(cfg, text)
    log(f"{row['role']}: {outcome} — {row['detail'][:200]}")
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
    """A clone the worker made, so the model never sees a real checkout."""
    depth = int(wcfg.get("clone_depth", 50) or 0)
    args = ["clone"]
    if depth > 0:
        args += ["--depth", str(depth)]
    args += ["--branch", wcfg.get("base_branch", "main"),
             _repo_url(wcfg["repo"]), str(clone)]
    _git(Path(clone).parent, *args, timeout=int(wcfg.get("git_timeout_s", 600)))
    return _git(clone, "rev-parse", "HEAD",
                timeout=int(wcfg.get("git_timeout_s", 600))).strip()


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
