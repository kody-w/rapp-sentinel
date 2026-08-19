#!/usr/bin/env python3
"""subsentinels.py — a bounded fan-out of read-only sub-sentinels.

WHY
One model deciding alone what to make is one model's blind spot, published.
A collective can do better by splitting the work the way a studio does:
somebody digs through everything already made, somebody thinks about what can
actually be built, and somebody whose whole job is to attack the result before
anyone else sees it. Those are separate CONTEXTS, not separate paragraphs of
one prompt — a critic who shares the maker's context is a rubber stamp.

WHAT A SUB-SENTINEL IS ALLOWED TO BE
A child is a separate `copilot` process with:
  * no git clone, no repository, and no GitHub credentials in its environment
  * an isolated temporary workspace it may write exactly one file into
  * a strict, bounded JSON schema for that file
  * a hard timeout, a process group of its own, and no way to spawn more
    sentinels (RAPP_SENTINEL_DEPTH is checked before any fan-out)

A child cannot publish anything. It cannot commit, push, open a PR or merge —
not because it was asked nicely, but because it has no repository and no token,
and because the parent controller is the only code in this system that ever
calls git or gh. The children's entire influence on the world is a JSON file
this module parses deterministically.

FAILURE IS EXPLICIT
A child that times out, crashes, writes nothing, writes malformed JSON, or
breaks a single bound is a NAMED failure carried into the ledger. It is never
an empty success and never silently dropped: the parent may continue only if
the surviving candidates still satisfy the exactly-ten-finalists invariant, and
otherwise the cycle fails, loudly, having published nothing.
"""

import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CHILD_SCHEMA = "rapp-subsentinel-report/1.0"
DEPTH_ENV = "RAPP_SENTINEL_DEPTH"

# ── model confinement ───────────────────────────────────────────────────────
# --allow-all is three permissions in a trench coat: all tools, all paths, all
# URLs. Nothing here needs any of that, and the blast radius of getting it
# wrong is the operator's whole machine. So both layers are enumerated.
#
# A sub-sentinel gets NO tools at all: it reads what the parent handed it in
# the prompt and answers with JSON on stdout. A tool it does not have is a
# tool that cannot be talked into anything.
CHILD_TOOLS = ()

# The maker may read and write files, inside --add-dir, and nothing else. No
# shell, no git, no gh, no MCP, no fetch: the controller owns every operation
# that touches the world.
MAKER_TOOLS = ("view", "glob", "grep", "create", "edit", "write", "apply_patch")

# Named explicitly rather than "everything else", so a new tool in a future
# CLI release is not silently granted by an allowlist we forgot to update —
# --available-tools already restricts to the allowlist; this is the second
# lock on the same door.
DENIED_TOOLS = ("bash", "read_bash", "stop_bash", "powershell",
                "read_powershell", "stop_powershell", "fetch", "web_search",
                "task", "ask_user", "lsp")

# Everything the CLI does on its own that we did not ask for.
CONFINEMENT_FLAGS = (
    "--disable-builtin-mcps",     # no github-mcp-server, no write-capable MCP
    "--no-custom-instructions",   # no AGENTS.md/CLAUDE.md from any repo
    "--no-bash-env",              # BASH_ENV cannot smuggle a shell init file
    "--disallow-temp-dir",        # no automatic access to the system temp dir
    "--no-ask-user",              # nobody is at the terminal
    "--no-remote",                # no remote control of this session
    "--no-remote-export",
    "--no-auto-update",           # a sentinel must not swap its own binary
    "--no-experimental",
)

# The only variables that survive into a model process. Everything else —
# including the operator's real tokens, agent sockets and shell rc state — is
# absent rather than trusted.
ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
                 "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS")

# Exactly ten finalists reach the maker. The number is not decoration: it is
# the round-one candidate set the submission's own _dada_cycle must contain,
# which is what ties the piece to the search that produced it.
FINALISTS = 10
MAX_WAVES = 2

SCORE_DIMENSIONS = ("absurdity", "novelty", "craft", "coherence",
                    "provocation", "restraint")
SEVERITIES = ("low", "medium", "high")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# The default cast. Three children, three genuinely different jobs, one of
# which exists to argue with the other two.
DEFAULT_ROLES = [
    {
        "name": "novelty-archaeologist",
        "wave": 1,
        "brief": "Dig through every prior submission in prior.json before you "
                 "propose anything. Your job is to make sure this cycle is not "
                 "a thing the collective has already said. Name the closest "
                 "prior work for each candidate you propose, and file a "
                 "high-severity critique against any candidate — including "
                 "your own — that repeats an existing piece's premise.",
    },
    {
        "name": "execution-designer",
        "wave": 1,
        "brief": "Think in the medium. A premise that cannot be built as a "
                 "self-contained SVG, markdown, text or json file under 50 KB "
                 "is not a candidate, it is a wish. For each candidate say "
                 "concretely what the artifact IS, and score craft honestly.",
    },
    {
        "name": "adversarial-verifier",
        "wave": 2,
        "verifier": True,
        "brief": "You see the candidates the others proposed, in pool.json. "
                 "Attack them. A high-severity critique VETOES a candidate and "
                 "removes it from the cycle, so spend that authority on "
                 "claims you can defend: unfalsifiable assertions, borrowed "
                 "credit, hidden dependencies, anything that would embarrass "
                 "the collective when a stranger checks it. Then propose "
                 "candidates that survive your own standard.",
    },
]

FANOUT_DEFAULTS = {
    "enabled": False,
    "children": 3,                  # conservative default cast
    "max_children": 5,              # hard ceiling on a single cycle
    "roles": [],                    # empty = DEFAULT_ROLES
    "candidates_per_child": 6,
    "max_candidates_per_child": 8,
    "max_evidence": 10,
    "max_critique": 10,
    "max_text": 400,
    "child_timeout_s": 600,
    "total_timeout_s": 1200,
    "concurrency": 3,
    "max_processes_per_cycle": 6,   # children + the maker
    "daily_child_budget": 24,       # credit-like rolling cap
    "min_healthy_children": 2,
    "max_depth": 1,
    "max_report_bytes": 65536,
    "kill_grace_s": 5,
    "model": "",                    # empty = the worker's model
    "require_verifier": True,       # wave 2 critic is not optional
    "isolated_home": True,          # HOME/XDG/GH config inside the workspace
    "auth_env_var": "COPILOT_GITHUB_TOKEN",
    "sandbox_exec": False,          # macOS sandbox-exec, defence in depth
    "format_repair_attempts": 1,    # one more process, only for bad JSON
    "max_transcript_bytes": 262144, # what is kept on disk per attempt
    # A child with a GitHub token is a child that can publish. These come out
    # of the environment before exec, and gh is pointed at an empty config
    # directory, so "no write authority" is a property of the process rather
    # than a sentence in a prompt.
    "strip_env": ["GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
                  "GITHUB_ENTERPRISE_TOKEN", "SSH_AUTH_SOCK"],
}


class FanoutError(RuntimeError):
    """The fan-out cannot produce a trustworthy set of finalists."""


class ChildError(RuntimeError):
    """One child failed. Named, recorded, never silently dropped."""


# ── config ──────────────────────────────────────────────────────────────────

def fanout_config(wcfg):
    block = wcfg.get("fanout")
    block = dict(block) if isinstance(block, dict) else {}
    merged = dict(FANOUT_DEFAULTS)
    merged.update(block)
    merged["_parent_model"] = wcfg.get("model", "")
    return merged


def enabled(fcfg):
    return bool(fcfg.get("enabled"))


def current_depth(env=None):
    """How deep this process already is. 0 for a job launchd started."""
    raw = (env or os.environ).get(DEPTH_ENV, "0").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        # Unreadable depth is treated as "deep", never as "surely the top" —
        # a typo in an inherited environment must not authorise a fan-out.
        return 10_000


def roles_for(fcfg):
    declared = fcfg.get("roles") or []
    roles = []
    for spec in (declared or DEFAULT_ROLES):
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or "").strip()
        if not name or not ID_RE.match(name):
            continue
        wave = int(spec.get("wave", 1) or 1)
        roles.append({"name": name,
                      "wave": min(max(wave, 1), MAX_WAVES),
                      "verifier": bool(spec.get("verifier")) or wave >= MAX_WAVES,
                      "brief": str(spec.get("brief") or "").strip()})
    return roles


def children_spent(history, hours=24, now=None):
    """Credit-like accounting: how many child processes ran in the window."""
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    total = 0
    for row in history:
        if row.get("skipped"):
            continue
        try:
            stamp = datetime.fromisoformat(str(row["at"]))
        except (KeyError, TypeError, ValueError):
            # The caller loads history strictly; anything unparseable here is
            # counted as spend rather than ignored (fail closed).
            total += int(FANOUT_DEFAULTS["children"])
            continue
        if stamp > cutoff:
            total += int(row.get("children") or 0)
    return total


def plan_children(fcfg, history, depth, now=None):
    """Which children may run this cycle. Returns (specs, reason).

    An empty list with a reason is a decision NOT to fan out; the caller must
    treat that as a skipped cycle rather than quietly making art alone, because
    "the collective deliberated" and "one model had a think" are different
    claims and only one of them would be true.
    """
    if not enabled(fcfg):
        return [], "fan-out disabled"
    max_depth = int(fcfg.get("max_depth", 1))
    if depth >= max_depth:
        return [], (f"depth {depth} has reached max_depth {max_depth}; "
                    f"sub-sentinels may not spawn sub-sentinels")
    roles = roles_for(fcfg)
    if not roles:
        return [], "no usable child roles configured"

    ceiling = min(int(fcfg.get("children", 3)),
                  int(fcfg.get("max_children", 5)),
                  max(0, int(fcfg.get("max_processes_per_cycle", 6)) - 1),
                  len(roles))
    if ceiling <= 0:
        return [], "process cap leaves no room for children"

    used = children_spent(history, now=now)
    remaining = int(fcfg.get("daily_child_budget", 24)) - used
    if remaining <= 0:
        return [], f"child budget spent ({used}/{fcfg.get('daily_child_budget', 24)})"
    count = min(ceiling, remaining)

    # Keep wave order stable and never leave a wave-2 critic with nothing to
    # criticise: generators are seated first.
    ordered = sorted(roles, key=lambda r: (r["wave"], roles.index(r)))
    chosen = ordered[:count]

    # The critic is not the seat that gets cut when the budget is tight. A
    # fan-out whose adversarial verifier was quietly dropped for capacity is
    # exactly the shape of "we deliberated" with the disagreement removed —
    # so if the cast cannot include one, there is no cast (#7).
    if fcfg.get("require_verifier", True):
        verifiers = [r for r in roles if r.get("verifier")]
        if not verifiers:
            return [], ("no adversarial verifier is configured and "
                        "require_verifier is on")
        if not any(r.get("verifier") for r in chosen):
            if len(chosen) < 2:
                return [], (f"{len(chosen)} slot(s) cannot seat both a "
                            f"generator and the adversarial verifier")
            chosen = chosen[:count - 1] + [verifiers[0]]
        if not any(not r.get("verifier") for r in chosen):
            return [], "the cast is all critics and no generators"

    if len(chosen) < int(fcfg.get("min_healthy_children", 2)):
        return [], (f"only {len(chosen)} child slot(s) available, "
                    f"min_healthy_children is {fcfg.get('min_healthy_children', 2)}")
    return chosen, f"{len(chosen)} children ({used}/{fcfg.get('daily_child_budget', 24)} spent today)"


# ── confinement ─────────────────────────────────────────────────────────────

def confined_argv(prompt, model, cwd, tools=(), add_dirs=(), secret_vars=(),
                  log_dir=None, json_output=False):
    """The exact command line a model process is allowed to have.

    Two independent restrictions, because one of them being wrong should not
    be enough: --available-tools decides what the model can even see, and
    --deny-tool/--excluded-tools name the dangerous ones explicitly so a tool
    added by a future CLI release cannot arrive pre-approved. --add-dir is the
    only place file tools may touch; no --allow-all-paths, no --allow-all.
    """
    argv = ["copilot", "-p", str(prompt), "--model", str(model),
            "--available-tools=" + ",".join(tools),
            "--excluded-tools=" + ",".join(DENIED_TOOLS),
            "--deny-tool=" + ",".join(DENIED_TOOLS)]
    if tools:
        argv.append("--allow-tool=" + ",".join(tools))
    argv += list(CONFINEMENT_FLAGS)
    # JSONL events, so the report is read from a typed `assistant.message`
    # rather than scraped out of prose. Text mode keeps --silent, where the
    # response IS the stream.
    argv += ["--output-format", "json"] if json_output else ["--silent"]
    argv += ["-C", str(cwd)]
    for directory in add_dirs:
        argv += ["--add-dir", str(directory)]
    if secret_vars:
        argv.append("--secret-env-vars=" + ",".join(secret_vars))
    argv += ["--log-level", "none", "--log-dir",
             str(log_dir or Path(cwd) / "copilot-logs")]
    return argv


SANDBOX_PROFILE_HEADER = """(version 1)
;; Defence in depth behind the CLI's own permissions: even a tool we did not
;; expect cannot write outside the roots below. Reads and network stay open
;; because model inference needs both.
;;
;; TWO roots, not one. The maker's FILE TOOLS are rooted at staging alone
;; (--add-dir), but the process itself must write its isolated HOME, XDG,
;; TMPDIR and CLI logs, which live in a sibling runtime directory on purpose —
;; so that the tools cannot reach even the model's own process state. A
;; profile that allowed only staging turned every confined run into a
;; PermissionError, which is a sandbox that protects nothing because nobody
;; can leave it on.
;;
;; The controller's clone and the operator's real HOME are never roots here.
(allow default)
(deny file-write*)
(allow file-write* (literal "/dev/null") (literal "/dev/dtracehelper"))
(allow file-write-data (regex #"^/dev/(tty|fd|std(in|out|err)|null|zero|random|urandom)"))
"""


def sandbox_profile(write_roots):
    body = "".join(f'(allow file-write* (subpath "{Path(root).resolve()}"))\n'
                   for root in write_roots)
    return SANDBOX_PROFILE_HEADER + body


def sandbox_wrap(argv, write_roots, enabled, profile_dir=None):
    """Optionally re-exec the model under macOS sandbox-exec.

    Off by default: it is a second belt, and a second belt that silently
    strangles inference would be worse than none. When on, the profile denies
    every write outside the listed roots — the sanitized staging directory the
    model's tools are rooted at, and the runtime directory holding its
    isolated HOME/XDG/TMPDIR. Never the controller's clone, never a real HOME.
    """
    roots = [Path(r) for r in (write_roots if isinstance(write_roots, (list, tuple))
                               else [write_roots])]
    if not enabled:
        return argv
    target = Path(profile_dir) if profile_dir else roots[0]
    target.mkdir(parents=True, exist_ok=True)
    profile = target / "sandbox.sb"
    profile.write_text(sandbox_profile(roots), encoding="utf-8")
    return ["/usr/bin/sandbox-exec", "-f", str(profile), *argv]


class AuthUnavailable(RuntimeError):
    """No inference credential to hand a confined process."""


def confined_env(fcfg, workspace, depth, env=None):
    """A model process's whole environment, built up rather than filtered down.

    Only ENV_ALLOWLIST survives from the parent. HOME, XDG, TMPDIR, the gh
    config and the git config all point inside the workspace, so the model
    cannot read the operator's credentials, cannot find a gh auth token, and
    cannot leave state behind after the workspace is deleted.

    Inference auth arrives as ONE named variable, which is also passed to
    --secret-env-vars so the CLI strips it from any shell or MCP environment
    and redacts it from output. Missing it is an explicit failure: quietly
    falling back to the real HOME would hand a model the operator's whole
    credential set to save one line of config.
    """
    # sandbox_exec makes the workspace roots the only writable places. An
    # un-isolated HOME lives outside them by definition, so the combination
    # cannot work — and the way it failed was a bare PermissionError with an
    # empty stderr, which is the worst possible way to learn it.
    if fcfg.get("sandbox_exec") and not fcfg.get("isolated_home", True):
        raise AuthUnavailable(
            "sandbox_exec requires isolated_home: the sandbox only permits "
            "writes inside the staging and runtime roots, and a shared HOME "
            "is not one of them")

    src = dict(env if env is not None else os.environ)
    out = {k: src[k] for k in ENV_ALLOWLIST if k in src}
    out.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")

    workspace = Path(workspace)
    isolated = bool(fcfg.get("isolated_home", True))
    home = workspace / "home"
    for path in (home, workspace / "tmp", workspace / "gh-config",
                 workspace / "xdg-config", workspace / "xdg-data",
                 workspace / "xdg-cache", workspace / "copilot-logs"):
        path.mkdir(parents=True, exist_ok=True)
    git_config = workspace / "gitconfig"
    if not git_config.exists():
        git_config.write_text("# empty: model processes have no git identity\n",
                              encoding="utf-8")

    out["HOME"] = str(home) if isolated else src.get("HOME", str(home))
    out["TMPDIR"] = str(workspace / "tmp")
    out["XDG_CONFIG_HOME"] = str(workspace / "xdg-config")
    out["XDG_DATA_HOME"] = str(workspace / "xdg-data")
    out["XDG_CACHE_HOME"] = str(workspace / "xdg-cache")
    out["GH_CONFIG_DIR"] = str(workspace / "gh-config")
    out["GIT_CONFIG_GLOBAL"] = str(git_config)
    out["GIT_CONFIG_SYSTEM"] = os.devnull
    out["GIT_TERMINAL_PROMPT"] = "0"
    out["GIT_ASKPASS"] = "/usr/bin/false"
    out["COPILOT_CUSTOM_INSTRUCTIONS_DIRS"] = ""
    out[DEPTH_ENV] = str(depth + 1)

    auth_var = str(fcfg.get("auth_env_var") or "COPILOT_GITHUB_TOKEN")
    token = src.get(auth_var, "")
    if token:
        out[auth_var] = token
    elif isolated:
        raise AuthUnavailable(
            f"{auth_var} is not set, and an isolated HOME has no credentials "
            f"of its own — set it, or set evolve_worker.fanout.isolated_home "
            f"to false to run models against the operator's real HOME")
    return out


def secret_vars_for(fcfg):
    return (str(fcfg.get("auth_env_var") or "COPILOT_GITHUB_TOKEN"),)


# ── the child prompt ────────────────────────────────────────────────────────

CHILD_PROMPT = """
You are a SUB-SENTINEL of {collective}, working for the {parent_role} neighbor
on cycle {cycle}. Your role is: {role}

{brief}

WHAT YOU HAVE
You have NO tools. Not a shell, not a file editor, not a browser, not git, not
gh, not an MCP server — nothing. Everything you need is in this prompt, and
your entire output is the JSON below. That is deliberate: a tool you do not
have is a tool nobody can talk you into misusing, and a parent controller —
code, not a model — owns every operation that touches the world.

Do not describe running commands. Do not claim to have opened a pull request.
Do not start another agent or sub-sentinel; you are the deepest layer this
system permits.

THE SITUATION
{situation}

WHAT ALREADY EXISTS (every published submission)
{prior}

{pool}

YOUR ANSWER — reply with ONE json object, and nothing else:
{{
  "schema": "{schema}",
  "role": "{role}",
  "cycle": {cycle},
  "candidates": [
    {{"id": "c1",
      "premise": "one concrete thing to make, <= {max_text} characters",
      "rationale": "why it is worth making, <= {max_text} characters",
      "scores": {{{score_example}}}}}
    ... exactly {want} of them, unique ids ...
  ],
  "evidence": [
    {{"claim": "something checkable you actually checked",
      "source": "where a stranger can check it"}}
    ... at most {max_evidence} ...
  ],
  "critique": [
    {{"target": "<a candidate id from the pool above, or one of yours>",
      "finding": "what is wrong with it, <= {max_text} characters",
      "severity": "low|medium|high"}}
    ... at most {max_critique} ...
  ]
}}

Every score is a number from 0 to 10 on all six dimensions
({dimensions}). A "high" severity critique VETOES that candidate — it is
removed from the cycle deterministically, so spend that authority on claims
you can defend.

Bounds are enforced by a parser, not by goodwill: more than
{max_candidates} candidates, a missing dimension, a non-numeric score, an
unknown key, or a reply over {max_bytes} bytes makes your whole report a named
failure that is recorded and shown to the maker.

If you genuinely cannot do the work, still answer with the object above, an
empty candidates list, and a critique saying why. Silence is the one thing
that cannot be read.
"""


REPAIR_PROMPT = """
Your previous reply could not be read, and this is the ONLY retry.

WHAT WENT WRONG
{error}

WHAT YOU SENT (truncated)
{prior}

Send the SAME analysis again, as ONE json object and nothing else — no
prose before it, no commentary after it, no markdown fence needed. Do not
change your candidates to make them easier to format; change only the
format. You still have no tools.

{{
  "schema": "{schema}",
  "role": "{role}",
  "cycle": {cycle},
  "candidates": [
    {{"id": "c1", "premise": "...", "rationale": "...",
      "scores": {{{score_example}}}}}
    ... at most {max_candidates}, unique ids, every score a number 0-10 ...
  ],
  "evidence": [{{"claim": "...", "source": "..."}}],
  "critique": [{{"target": "...", "finding": "...",
                "severity": "low|medium|high"}}]
}}
"""


def repair_prompt(spec, fcfg, cycle, error, prior_content):
    example = ", ".join(f'"{d}": 7' for d in SCORE_DIMENSIONS)
    bound = int(fcfg.get("max_text", 400)) * 4
    return REPAIR_PROMPT.format(
        error=str(error)[:400],
        prior=(prior_content or "(nothing)")[:bound],
        schema=CHILD_SCHEMA,
        role=spec["name"],
        cycle=cycle,
        max_candidates=int(fcfg.get("max_candidates_per_child", 8)),
        score_example=example,
    )


def child_prompt(spec, fcfg, cycle, collective, parent_role, situation, prior,
                 pool):
    """Everything the child gets. It has no tools, so this is the whole world.

    prior and pool are inlined and bounded here rather than left on disk: a
    child with no file tools cannot read a file, and handing it a path it
    cannot open would be a prompt that lies about its own situation.
    """
    example = ", ".join(f'"{d}": 7' for d in SCORE_DIMENSIONS)
    prior_text = "\n".join(
        f"  - {row.get('slug')}: {row.get('title')} [{row.get('kind')}] "
        f"{str(row.get('statement') or '')[:200]}"
        for row in (prior or [])[:60]) or "  (nothing published yet)"
    pool_text = ""
    if pool:
        pool_text = ("THE CANDIDATES THE EARLIER WAVE PROPOSED — critique these\n"
                     + "\n".join(f'  - id "{c["id"]}" (from {c["from"]}): '
                                 f'{c["premise"][:200]}' for c in pool))
    return CHILD_PROMPT.format(
        collective=collective,
        parent_role=parent_role,
        cycle=cycle,
        role=spec["name"],
        brief=spec.get("brief") or "Decide from your role.",
        situation=situation,
        prior=prior_text,
        pool=pool_text,
        schema=CHILD_SCHEMA,
        want=int(fcfg.get("candidates_per_child", 6)),
        max_candidates=int(fcfg.get("max_candidates_per_child", 8)),
        max_evidence=int(fcfg.get("max_evidence", 10)),
        max_critique=int(fcfg.get("max_critique", 10)),
        max_text=int(fcfg.get("max_text", 400)),
        max_bytes=int(fcfg.get("max_report_bytes", 65536)),
        dimensions=", ".join(SCORE_DIMENSIONS),
        score_example=example,
    )


# ── strict report parsing ───────────────────────────────────────────────────

def _text(value, limit, where):
    if not isinstance(value, str) or not value.strip():
        raise ChildError(f"{where} is missing or not a string")
    if len(value) > limit:
        raise ChildError(f"{where} is {len(value)} characters, over the {limit} cap")
    return value.strip()


def _score(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChildError(f"{where} is not a number")
    if value != value or value in (float("inf"), float("-inf")):
        raise ChildError(f"{where} is not finite")
    if not (0 <= value <= 10):
        raise ChildError(f"{where} is outside 0..10")
    return float(value)


MAX_TRANSCRIPT_BYTES = 4 * 1024 * 1024
MAX_TRANSCRIPT_EVENTS = 20000
ASSISTANT_MESSAGE = "assistant.message"


def extract_assistant_message(stdout, max_bytes=MAX_TRANSCRIPT_BYTES,
                              max_events=MAX_TRANSCRIPT_EVENTS):
    """The final assistant message, and nothing else in the transcript.

    The CLI's JSONL carries the same words in several places: reasoning
    deltas, `assistant.reasoning`, `model.message`, `model.response`,
    snapshots. A child asked for JSON usually thinks about that JSON out loud
    first — measured: the reasoning event for a one-line reply contained the
    exact object the reply did. Scraping "the last JSON-looking thing" would
    therefore read the model's THOUGHTS as its answer, which is a different
    claim by a different speaker.

    So this accepts exactly one event type, takes `data.content`, and ignores
    stats, tool data and everything ephemeral. Bounded in bytes and events,
    because a transcript is attacker-adjacent input like any other.
    """
    if stdout is None:
        raise ChildError("the child produced no output at all")
    size = len(stdout.encode("utf-8", "replace"))
    if size > max_bytes:
        raise ChildError(f"transcript is {size} bytes, over the {max_bytes} cap")
    content, events = None, 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        events += 1
        if events > max_events:
            raise ChildError(f"transcript holds more than {max_events} events")
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(event, dict) or event.get("type") != ASSISTANT_MESSAGE:
            continue
        data = event.get("data")
        if isinstance(data, dict) and isinstance(data.get("content"), str):
            content = data["content"]
    if content is None:
        raise ChildError("the transcript holds no assistant.message event")
    return content


def extract_report(text, limit):
    """Find the one JSON object in a model's reply, or say why there is none.

    The reply is the whole channel — children have no tools and no file to
    write — so this has to cope with a model that wrapped its answer in a
    fence while staying strict about what counts: something that PARSES as an
    object. Prose alone, a truncated object, or a wall of text over the cap is
    a named failure, not an empty report.
    """
    if text is None:
        raise ChildError("the child produced no output at all")
    raw = text.strip()
    if not raw:
        raise ChildError("the child produced no output at all")
    size = len(raw.encode("utf-8"))
    if size > limit:
        raise ChildError(f"reply is {size} bytes, over the {limit} cap")

    candidates = []
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S):
        candidates.append(match.group(1))
    depth, start = 0, None
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(raw[start:i + 1])
    for blob in reversed(candidates):
        try:
            doc = json.loads(blob)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(doc, dict):
            return doc
    raise ChildError("the reply contains no parseable json object")


def validate_report(source, spec, fcfg, cycle):
    """Parse one child's report, or say exactly what is wrong with it."""
    limit = int(fcfg.get("max_report_bytes", 65536))
    doc = extract_report(source, limit)

    known = {"schema", "role", "cycle", "candidates", "evidence", "critique"}
    extra = [k for k in doc if k not in known and not str(k).startswith("_")]
    if extra:
        raise ChildError(f"report carries unknown keys: {', '.join(sorted(extra))}")
    if doc.get("schema") != CHILD_SCHEMA:
        raise ChildError(f"schema is {doc.get('schema')!r}, expected {CHILD_SCHEMA!r}")
    if doc.get("role") != spec["name"]:
        raise ChildError(f"role is {doc.get('role')!r}, expected {spec['name']!r}")
    if doc.get("cycle") != cycle:
        raise ChildError(f"cycle is {doc.get('cycle')!r}, expected {cycle}")

    candidates = doc.get("candidates")
    if not isinstance(candidates, list):
        raise ChildError("candidates is missing or not a list")
    cap = int(fcfg.get("max_candidates_per_child", 8))
    if len(candidates) > cap:
        raise ChildError(f"{len(candidates)} candidates, over the {cap} cap")
    max_text = int(fcfg.get("max_text", 400))
    seen, parsed = set(), []
    for i, cand in enumerate(candidates, start=1):
        if not isinstance(cand, dict):
            raise ChildError(f"candidate {i} is not an object")
        cid = cand.get("id")
        if not isinstance(cid, str) or not ID_RE.match(cid):
            raise ChildError(f"candidate {i} has an unusable id {cid!r}")
        if cid in seen:
            raise ChildError(f"candidate id {cid!r} appears twice")
        seen.add(cid)
        scores = cand.get("scores")
        if not isinstance(scores, dict) or set(scores) != set(SCORE_DIMENSIONS):
            raise ChildError(f"candidate {cid} must score exactly "
                             f"{list(SCORE_DIMENSIONS)}")
        parsed.append({
            "id": cid,
            "premise": _text(cand.get("premise"), max_text, f"candidate {cid} premise"),
            "rationale": _text(cand.get("rationale"), max_text,
                               f"candidate {cid} rationale")
            if cand.get("rationale") is not None else "",
            "scores": {d: _score(scores[d], f"candidate {cid} score {d}")
                       for d in SCORE_DIMENSIONS},
        })

    evidence = doc.get("evidence") or []
    if not isinstance(evidence, list):
        raise ChildError("evidence is not a list")
    if len(evidence) > int(fcfg.get("max_evidence", 10)):
        raise ChildError(f"{len(evidence)} evidence entries, over the cap")
    facts = []
    for i, item in enumerate(evidence, start=1):
        if not isinstance(item, dict):
            raise ChildError(f"evidence {i} is not an object")
        facts.append({"claim": _text(item.get("claim"), max_text, f"evidence {i} claim"),
                      "source": _text(item.get("source"), max_text,
                                      f"evidence {i} source")})

    critique = doc.get("critique") or []
    if not isinstance(critique, list):
        raise ChildError("critique is not a list")
    if len(critique) > int(fcfg.get("max_critique", 10)):
        raise ChildError(f"{len(critique)} critique entries, over the cap")
    findings = []
    for i, item in enumerate(critique, start=1):
        if not isinstance(item, dict):
            raise ChildError(f"critique {i} is not an object")
        severity = item.get("severity")
        if severity not in SEVERITIES:
            raise ChildError(f"critique {i} severity is {severity!r}, expected one "
                             f"of {', '.join(SEVERITIES)}")
        findings.append({
            "target": _text(item.get("target"), 128, f"critique {i} target"),
            "finding": _text(item.get("finding"), max_text, f"critique {i} finding"),
            "severity": severity,
        })

    return {"role": spec["name"], "candidates": parsed, "evidence": facts,
            "critique": findings}


# ── running children ────────────────────────────────────────────────────────

def _kill_group(proc, grace):
    """Take down the child AND anything it started.

    A model process that shelled out leaves grandchildren holding the pipe; a
    plain proc.kill() reaps the parent and lets the rest run on invisibly.
    start_new_session gave the child its own process group precisely so this
    can be one signal.
    """
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        pgid = None
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 2)):
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + max(0.1, wait)
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.05)


def _write_transcript(transcript_dir, cycle, role, attempt, stdout, stderr,
                      fcfg):
    """Keep the evidence outside the workspace that is about to be deleted.

    A child that failed is exactly the child whose transcript is worth having,
    and until now it lived in a directory the worker removes on its way out.
    Bounded, and scrubbed of the one value that could be in an environment
    this process was given.
    """
    if not transcript_dir:
        return ""
    limit = int(fcfg.get("max_transcript_bytes", 262144))
    secret = os.environ.get(str(fcfg.get("auth_env_var")
                                or "COPILOT_GITHUB_TOKEN"), "")
    body = ((stdout or "") + ("\n--- stderr ---\n" + stderr if stderr else ""))
    if secret:
        body = body.replace(secret, "<redacted>")
    if len(body) > limit:
        body = body[:limit] + f"\n--- truncated at {limit} bytes ---\n"
    try:
        target = Path(transcript_dir)
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{cycle}-{role}-{attempt}.log"
        path.write_text(body, encoding="utf-8")
        return str(path)
    except OSError:
        return ""


def _spawn_child(argv, ws, env, budget, fcfg, live):
    """One process. Returns (exit_code, timed_out, stdout, stderr)."""
    try:
        proc = subprocess.Popen(argv, cwd=str(ws), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
    except FileNotFoundError:
        return None, False, "", "copilot CLI not found on PATH"
    except OSError as e:
        return None, False, "", f"could not start the child: {type(e).__name__}: {e}"
    live.append(proc)
    out, err, timed_out = "", "", False
    try:
        out, err = proc.communicate(timeout=budget)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc, float(fcfg.get("kill_grace_s", 5)))
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = "", ""
    except BaseException:
        _kill_group(proc, float(fcfg.get("kill_grace_s", 5)))
        raise
    finally:
        try:
            live.remove(proc)
        except ValueError:
            pass
    return proc.returncode, timed_out, out, err


def _run_child(spec, fcfg, workspace, cycle, collective, parent_role, prior,
               pool, depth, deadline, live, logger=None, situation="",
               transcript_dir=None, spare_processes=None):
    """One child, start to finish, with at most one format repair.

    Never raises: it returns its verdict. A reply that cannot be parsed is
    worth exactly one more process — models fail at JSON far more often than
    they fail at thinking, and losing a whole cycle's deliberation to a stray
    sentence is a bad trade. Two bad replies is a failed child, and a failed
    verifier is still a failed cycle.
    """
    say = logger or (lambda msg: None)
    started = time.monotonic()
    ws = Path(workspace) / "children" / spec["name"]
    ws.mkdir(parents=True, exist_ok=True)
    result = {"role": spec["name"], "wave": spec["wave"],
              "verifier": bool(spec.get("verifier")), "ok": False,
              "error": "", "timed_out": False, "exit_code": None,
              "elapsed_s": 0.0, "report": None, "argv": [], "pid": None,
              "attempts": 0, "processes": 0, "transcripts": [], "repaired": False}

    try:
        env = confined_env(fcfg, ws, depth)
    except AuthUnavailable as e:
        result["error"] = str(e)
        return result

    prompt = child_prompt(spec, fcfg, cycle, collective, parent_role,
                          situation, prior, pool)
    max_repairs = max(0, int(fcfg.get("format_repair_attempts", 1)))
    last_content, last_error = "", ""

    for attempt in range(1, max_repairs + 2):
        remaining = deadline - time.monotonic()
        per_child = float(fcfg.get("child_timeout_s", 600))
        budget = min(per_child, remaining)
        if budget <= 1:
            result["error"] = (result["error"]
                               or "no time left in the cycle's fan-out budget")
            break
        if attempt > 1:
            if spare_processes is not None and not spare_processes.take():
                result["error"] = (f"{last_error} (no process left in the "
                                   f"cycle's cap for a format repair)")
                break
            say(f"  {spec['name']}: unreadable reply, one format repair")
            prompt = repair_prompt(spec, fcfg, cycle, last_error, last_content)

        argv = sandbox_wrap(
            confined_argv(prompt, fcfg.get("model") or fcfg.get("_parent_model")
                          or "claude-sonnet-4.6", ws, tools=CHILD_TOOLS,
                          secret_vars=secret_vars_for(fcfg),
                          log_dir=ws / "copilot-logs", json_output=True),
            [ws], bool(fcfg.get("sandbox_exec")))
        result["argv"] = argv
        result["attempts"] = attempt
        result["processes"] += 1

        code, timed_out, out, err = _spawn_child(argv, ws, env, budget, fcfg,
                                                 live)
        result["exit_code"] = code
        path = _write_transcript(transcript_dir, cycle, spec["name"], attempt,
                                 out, err, fcfg)
        if path:
            result["transcripts"].append(path)

        if timed_out:
            result["timed_out"] = True
            result["error"] = f"timed out after {budget:.0f}s"
            break
        if code is None:
            result["error"] = err or "the child could not be started"
            break
        if code != 0:
            detail = (err or out or "").strip().splitlines()
            result["error"] = (f"exited {code}"
                               + (f": {detail[-1][:160]}" if detail else ""))
            break
        try:
            content = extract_assistant_message(
                out, int(fcfg.get("max_transcript_bytes", 262144)) * 16)
            last_content = content
            result["report"] = validate_report(content, spec, fcfg, cycle)
            result["ok"] = True
            result["repaired"] = attempt > 1
            result["error"] = ""
            break
        except ChildError as e:
            last_error = str(e)
            result["error"] = str(e)

    result["elapsed_s"] = round(time.monotonic() - started, 1)
    return result


class SpareProcesses:
    """The cycle's remaining process allowance, shared across children."""

    def __init__(self, count):
        self.remaining = max(0, int(count))
        self._lock = threading.Lock()

    def take(self):
        with self._lock:
            if self.remaining <= 0:
                return False
            self.remaining -= 1
            return True


def run_children(specs, fcfg, workspace, cycle, collective, parent_role, prior,
                 depth, logger=None, situation="", transcript_dir=None,
                 spare_processes=None):
    """Run the cast wave by wave, bounded in count, time and blast radius."""
    say = logger or (lambda msg: None)
    results, pool, live = [], [], []
    deadline = time.monotonic() + float(fcfg.get("total_timeout_s", 1200))
    concurrency = max(1, int(fcfg.get("concurrency", 3)))
    try:
        for wave in sorted({s["wave"] for s in specs})[:MAX_WAVES]:
            batch = [s for s in specs if s["wave"] == wave]
            if not batch:
                continue
            say(f"fan-out wave {wave}: {', '.join(s['name'] for s in batch)}")
            with ThreadPoolExecutor(max_workers=min(concurrency, len(batch))) as ex:
                futures = [ex.submit(_run_child, spec, fcfg, workspace, cycle,
                                     collective, parent_role, prior, list(pool),
                                     depth, deadline, live, say, situation,
                                     transcript_dir, spare_processes)
                           for spec in batch]
                wave_results = [f.result() for f in futures]
            for res in wave_results:
                results.append(res)
                if res["ok"]:
                    pool.extend({"id": f"{res['role']}#{c['id']}",
                                 "premise": c["premise"],
                                 "from": res["role"]}
                                for c in res["report"]["candidates"])
                    say(f"  {res['role']}: {len(res['report']['candidates'])} "
                        f"candidates in {res['elapsed_s']}s")
                else:
                    say(f"  {res['role']}: FAILED — {res['error']}")
    finally:
        # Whatever happens above — an exception, a KeyboardInterrupt, the
        # launchd ceiling — nothing this function started outlives it.
        for proc in list(live):
            _kill_group(proc, float(fcfg.get("kill_grace_s", 5)))
    return results


# ── deterministic aggregation ───────────────────────────────────────────────

RECORD_FIELDS = ("id", "from", "premise", "rationale", "scores",
                 "evidence_digest")


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def evidence_digest(evidence):
    """One hash for a child's whole evidence list, bound into its candidates."""
    rows = [{"claim": e.get("claim", ""), "source": e.get("source", "")}
            for e in (evidence or [])]
    return hashlib.sha256(_canonical(rows).encode("utf-8")).hexdigest()


def canonical_record(candidate, from_role, evidence_hash):
    """The finalist as a checkable object, not a label.

    A finalist is its premise, its rationale, all six scores, which
    sub-sentinel produced it, and the evidence that sub-sentinel offered. Bind
    the id alone and a maker can publish "r1c3" with any content it likes and
    still pass — the deliberation becomes a name-check (#2).
    """
    return {
        "id": str(candidate["id"]),
        "from": str(from_role),
        "premise": str(candidate["premise"]),
        "rationale": str(candidate.get("rationale") or ""),
        "scores": {d: round(float(candidate["scores"][d]), 4)
                   for d in SCORE_DIMENSIONS},
        "evidence_digest": str(evidence_hash),
    }


def record_digest(record):
    """sha256 over the canonical record. Recomputable by anyone, from the
    published meta.json alone."""
    try:
        payload = {
            "id": str(record["id"]),
            "from": str(record["from"]),
            "premise": str(record["premise"]),
            "rationale": str(record.get("rationale") or ""),
            "scores": {d: round(float(record["scores"][d]), 4)
                       for d in SCORE_DIMENSIONS},
            "evidence_digest": str(record["evidence_digest"]),
        }
    except (KeyError, TypeError, ValueError) as e:
        raise FanoutError(f"cannot digest a malformed finalist record: {e}")
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def expected_round1(finalists):
    """{id: digest} — what the published round one must reproduce exactly."""
    return {f["id"]: f["digest"] for f in finalists}


def aggregate(results, fcfg):
    """Exactly ten finalists, or an explicit failure. Never a quiet fallback.

    Ranking is deterministic and reproducible from the reports alone: mean
    score, minus half a point per medium-severity critique against that
    candidate, ties broken by namespaced id. High-severity critiques are
    vetoes — that is the adversarial verifier's actual authority, and it is
    exercised by code rather than by persuasion.
    """
    healthy = [r for r in results if r["ok"]]
    failures = [f"{r['role']}: {r['error']}" for r in results if not r["ok"]]
    minimum = int(fcfg.get("min_healthy_children", 2))
    if len(healthy) < minimum:
        raise FanoutError(
            f"only {len(healthy)} healthy child(ren), {minimum} required"
            + (f" — failures: {'; '.join(failures)}" if failures else ""))

    # The critic is load-bearing. A cycle whose adversarial verifier crashed
    # produced candidates nobody attacked, and publishing those while calling
    # it deliberation is the failure this whole fan-out exists to prevent (#7).
    if fcfg.get("require_verifier", True):
        seated = [r for r in results if r.get("verifier")]
        if not seated:
            raise FanoutError("no adversarial verifier ran; a cycle without a "
                              "critic is not a deliberation")
        broken = [r for r in seated if not r["ok"]]
        if broken:
            raise FanoutError(
                "the adversarial verifier failed and cannot be skipped: "
                + "; ".join(f"{r['role']}: {r['error']}" for r in broken))

    candidates, vetoes, mediums = {}, {}, {}
    for res in healthy:
        digest_of_evidence = evidence_digest(res["report"]["evidence"])
        for cand in res["report"]["candidates"]:
            key = f"{res['role']}#{cand['id']}"
            record = canonical_record({**cand, "id": key}, res["role"],
                                      digest_of_evidence)
            record["digest"] = record_digest(record)
            record["mean"] = round(sum(cand["scores"].values())
                                   / len(SCORE_DIMENSIONS), 4)
            candidates[key] = record
    for res in healthy:
        for item in res["report"]["critique"]:
            target = item["target"]
            if target not in candidates:
                # Allow a child to name its own candidate unqualified.
                target = f"{res['role']}#{item['target']}"
            if target not in candidates:
                continue
            if item["severity"] == "high":
                vetoes.setdefault(target, []).append(
                    f"{res['role']}: {item['finding']}")
            elif item["severity"] == "medium":
                mediums[target] = mediums.get(target, 0) + 1

    survivors = [c for key, c in candidates.items() if key not in vetoes]
    if len(survivors) < FINALISTS:
        raise FanoutError(
            f"{len(survivors)} candidate(s) survived vetoes, exactly "
            f"{FINALISTS} finalists are required"
            + (f" — child failures: {'; '.join(failures)}" if failures else "")
            + (f" — vetoed: {', '.join(sorted(vetoes))}" if vetoes else ""))

    ranked = sorted(
        survivors,
        key=lambda c: (-(c["mean"] - 0.5 * mediums.get(c["id"], 0)), c["id"]))
    finalists = ranked[:FINALISTS]

    digest = {
        "children": [{"role": r["role"], "ok": r["ok"], "error": r["error"],
                      "elapsed_s": r["elapsed_s"], "verifier": r.get("verifier", False),
                      "candidates": len(r["report"]["candidates"]) if r["ok"] else 0}
                     for r in results],
        "healthy": len(healthy),
        "failures": failures,
        "pool": len(candidates),
        "vetoed": {k: v for k, v in sorted(vetoes.items())},
        "evidence": [e for r in healthy for e in r["report"]["evidence"]][:10],
        "critique": [c for r in healthy for c in r["report"]["critique"]
                     if c["severity"] != "high"][:10],
    }
    return finalists, digest


def round1_array(finalists):
    """Exactly what the maker must publish as round one of its cycle."""
    return [{**{k: f[k] for k in RECORD_FIELDS}, "digest": f["digest"]}
            for f in finalists]


def finalists_block(finalists, digest):
    """The bounded text the maker sees. The records are load-bearing: round
    one of the published cycle must reproduce them field for field."""
    lines = ["THE TEN FINALISTS YOUR SUB-SENTINELS PRODUCED",
             "Round 1 of your _dada_cycle MUST be exactly these ten records,",
             "copied field for field from round1.json in your workspace. The",
             "controller recomputes a sha256 over id, from, premise, rationale,",
             "all six scores and the evidence digest, and rejects the whole",
             "submission if one character moved.",
             ""]
    for c in finalists:
        lines.append(f'  - id "{c["id"]}" (from {c["from"]}, mean {c["mean"]}, '
                     f'digest {c["digest"][:12]}…): {c["premise"]}')
    if digest.get("evidence"):
        lines += ["", "EVIDENCE THEY CHECKED"]
        lines += [f"  - {e['claim']} ({e['source']})" for e in digest["evidence"]]
    if digest.get("critique"):
        lines += ["", "STANDING CRITIQUE (not vetoes, but read them)"]
        lines += [f"  - [{c['severity']}] {c['target']}: {c['finding']}"
                  for c in digest["critique"]]
    if digest.get("vetoed"):
        lines += ["", "VETOED BY THE ADVERSARIAL VERIFIER — do not resurrect these"]
        lines += [f"  - {k}: {'; '.join(v)}" for k, v in digest["vetoed"].items()]
    if digest.get("failures"):
        lines += ["", "CHILDREN THAT FAILED (their absence is a fact, not a gap)"]
        lines += [f"  - {f}" for f in digest["failures"]]
    return "\n".join(lines)
