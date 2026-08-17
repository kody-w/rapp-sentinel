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

import json
import os
import re
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

CHILD_SCHEMA = "rapp-subsentinel-report/1.0"
REPORT_NAME = "report.json"
DEPTH_ENV = "RAPP_SENTINEL_DEPTH"

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
    if len(chosen) < int(fcfg.get("min_healthy_children", 2)):
        return [], (f"only {len(chosen)} child slot(s) available, "
                    f"min_healthy_children is {fcfg.get('min_healthy_children', 2)}")
    return chosen, f"{len(chosen)} children ({used}/{fcfg.get('daily_child_budget', 24)} spent today)"


# ── the child prompt ────────────────────────────────────────────────────────

CHILD_PROMPT = """
You are a SUB-SENTINEL of {collective}, working for the {parent_role} neighbor
on cycle {cycle}. Your role is: {role}

{brief}

WHAT YOU MAY DO
Read {workspace}/brief.json (the situation), {workspace}/prior.json (every
submission the collective has already published){pool_note}. Think. Then write
exactly one file: {workspace}/{report} — and nothing else, anywhere.

WHAT YOU MAY NOT DO — THIS IS ABSOLUTE
You have NO publishing authority of any kind. Do not run git or gh. Do not
clone, commit, push, open a pull request, comment, or merge. Do not call any
network API that writes. Do not start another agent, sentinel or sub-sentinel;
you are already the deepest layer this system permits. Do not write outside
{workspace}. You have no repository and no GitHub credentials on purpose: a
parent controller — code, not a model — owns every operation that touches the
world, and it will discard everything you produce if you overstep.

WHAT TO WRITE — {report}, strictly this shape:
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
    {{"target": "<a candidate id from pool.json, or your own, or \\"pool\\">",
      "finding": "what is wrong with it, <= {max_text} characters",
      "severity": "low|medium|high"}}
    ... at most {max_critique} ...
  ]
}}

Every score is a number from 0 to 10 on all six dimensions
({dimensions}). A "high" severity critique VETOES that candidate — it will be
removed from the cycle deterministically, so use it when you can defend it.

Bounds are enforced by a parser, not by goodwill: more than
{max_candidates} candidates, a missing dimension, a non-numeric score, an
unknown key, or a file over {max_bytes} bytes makes your whole report a named
failure. Write valid JSON. No prose outside the file. No markdown fences.

If you genuinely cannot do the work, still write {report} with an empty
candidates list and a critique saying why. Silence is the one thing that
cannot be read.
"""


def child_prompt(spec, fcfg, workspace, cycle, collective, parent_role, pooled):
    example = ", ".join(f'"{d}": 7' for d in SCORE_DIMENSIONS)
    return CHILD_PROMPT.format(
        collective=collective,
        parent_role=parent_role,
        cycle=cycle,
        role=spec["name"],
        brief=spec.get("brief") or "Decide from your role.",
        workspace=str(workspace),
        pool_note=(", and {}/pool.json (the candidates the earlier wave "
                   "proposed)".format(workspace) if pooled else ""),
        report=REPORT_NAME,
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


def validate_report(path, spec, fcfg, cycle):
    """Parse one child's report, or say exactly what is wrong with it."""
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        raise ChildError(f"{spec['name']} wrote no {REPORT_NAME}")
    size = p.stat().st_size
    limit = int(fcfg.get("max_report_bytes", 65536))
    if size > limit:
        raise ChildError(f"{REPORT_NAME} is {size} bytes, over the {limit} cap")
    if size == 0:
        raise ChildError(f"{REPORT_NAME} is empty")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ChildError(f"{REPORT_NAME} is not valid json: {e}")
    if not isinstance(doc, dict):
        raise ChildError(f"{REPORT_NAME} is not an object")

    known = {"schema", "role", "cycle", "candidates", "evidence", "critique"}
    extra = [k for k in doc if k not in known and not str(k).startswith("_")]
    if extra:
        raise ChildError(f"{REPORT_NAME} carries unknown keys: {', '.join(sorted(extra))}")
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

def child_env(fcfg, workspace, depth, env=None):
    """The environment a child gets: no tokens, no gh config, one step deeper."""
    base = dict(env if env is not None else os.environ)
    for key in fcfg.get("strip_env") or []:
        base.pop(str(key), None)
    gh_config = os.path.join(str(workspace), "gh-config")
    git_config = os.path.join(str(workspace), "gitconfig")
    os.makedirs(gh_config, exist_ok=True)
    if not os.path.exists(git_config):
        with open(git_config, "w", encoding="utf-8") as fh:
            fh.write("# intentionally empty: children have no git identity\n")
    base["GH_CONFIG_DIR"] = gh_config
    base["GIT_CONFIG_GLOBAL"] = git_config
    base["GIT_CONFIG_SYSTEM"] = os.devnull
    base["GIT_TERMINAL_PROMPT"] = "0"
    base["GIT_ASKPASS"] = "/usr/bin/false"
    base[DEPTH_ENV] = str(depth + 1)
    return base


def _child_argv(spec, fcfg, prompt):
    model = fcfg.get("model") or fcfg.get("_parent_model") or "claude-sonnet-4.6"
    return ["copilot", "-p", prompt, "--allow-all", "--model", model]


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


def _run_child(spec, fcfg, workspace, cycle, collective, parent_role, prior,
               pool, depth, deadline, live, logger=None):
    """One child, start to finish. Never raises: it returns its verdict."""
    from pathlib import Path
    started = time.monotonic()
    ws = Path(workspace) / "children" / spec["name"]
    ws.mkdir(parents=True, exist_ok=True)
    result = {"role": spec["name"], "wave": spec["wave"], "ok": False,
              "error": "", "timed_out": False, "exit_code": None,
              "elapsed_s": 0.0, "report": None}

    remaining = deadline - time.monotonic()
    per_child = float(fcfg.get("child_timeout_s", 600))
    budget = min(per_child, remaining)
    if budget <= 1:
        result["error"] = "no time left in the cycle's fan-out budget"
        return result

    (ws / "brief.json").write_text(json.dumps({
        "cycle": cycle, "collective": collective, "for_neighbor": parent_role,
        "role": spec["name"], "brief": spec.get("brief", ""),
    }, indent=2), encoding="utf-8")
    (ws / "prior.json").write_text(json.dumps(prior, indent=2), encoding="utf-8")
    if pool:
        (ws / "pool.json").write_text(json.dumps(pool, indent=2), encoding="utf-8")

    prompt = child_prompt(spec, fcfg, ws, cycle, collective, parent_role, bool(pool))
    env = child_env(fcfg, ws, depth)
    try:
        proc = subprocess.Popen(_child_argv(spec, fcfg, prompt), cwd=str(ws),
                                env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
    except FileNotFoundError:
        result["error"] = "copilot CLI not found on PATH"
        return result
    except OSError as e:
        result["error"] = f"could not start the child: {type(e).__name__}: {e}"
        return result

    live.append(proc)
    out = ""
    try:
        out, _ = proc.communicate(timeout=budget)
        result["exit_code"] = proc.returncode
    except subprocess.TimeoutExpired:
        result["timed_out"] = True
        _kill_group(proc, float(fcfg.get("kill_grace_s", 5)))
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out = ""
        result["exit_code"] = proc.returncode
        result["error"] = f"timed out after {budget:.0f}s"
    finally:
        result["elapsed_s"] = round(time.monotonic() - started, 1)
        try:
            live.remove(proc)
        except ValueError:
            pass
        try:
            (ws / "child.log").write_text(out or "", encoding="utf-8")
        except OSError:
            pass

    if result["timed_out"]:
        return result
    if result["exit_code"] != 0:
        result["error"] = f"exited {result['exit_code']}"
        return result
    try:
        result["report"] = validate_report(ws / REPORT_NAME, spec, fcfg, cycle)
        result["ok"] = True
    except ChildError as e:
        result["error"] = str(e)
    return result


def run_children(specs, fcfg, workspace, cycle, collective, parent_role, prior,
                 depth, logger=None):
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
                                     depth, deadline, live, say)
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

    candidates, vetoes, mediums = {}, {}, {}
    for res in healthy:
        for cand in res["report"]["candidates"]:
            key = f"{res['role']}#{cand['id']}"
            candidates[key] = {**cand, "id": key, "from": res["role"],
                               "mean": round(sum(cand["scores"].values())
                                             / len(SCORE_DIMENSIONS), 4)}
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
                      "elapsed_s": r["elapsed_s"],
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


def finalists_block(finalists, digest):
    """The bounded text the maker sees. Ids are load-bearing: the submission's
    round one must contain exactly these."""
    lines = ["THE TEN FINALISTS YOUR SUB-SENTINELS PRODUCED",
             "Round 1 of your _dada_cycle MUST be exactly these ten ids.",
             ""]
    for c in finalists:
        lines.append(f'  - id "{c["id"]}" (from {c["from"]}, mean {c["mean"]}): '
                     f'{c["premise"]}')
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
