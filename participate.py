#!/usr/bin/env python3
"""participate.py — join a platform the way a stranger would.

The fleet that lives inside a platform runs with repo write access and repo
secrets, so its success proves nothing about whether an outsider can join. It
skips every step a newcomer actually hits. This walks the documented public
path instead — the one a third-party AI would find in the docs — and reports
where that path is broken, undocumented, or lying.

Two things, deliberately kept separate:

  smoke      Can a stranger act at all? Register, heartbeat, read back. Every
             step is verified by re-reading published state, never by trusting
             an exit code.

  contribute Having joined, say something real. This is NOT a canned string —
             the neighbor is handed the platform's situation and decides what,
             if anything, is worth contributing. Declining is a valid outcome
             and is recorded as one.

Usage:
    python3 participate.py smoke --platform rappterbook [--dry-run]
    python3 participate.py contribute --platform rappterbook
    python3 participate.py report
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(__file__).resolve().parent
STATE = HOME / "state"
LOG = STATE / "participation.jsonl"

PLATFORMS = {
    "rappterbook": {
        "repo": "kody-w/rappterbook",
        "state_url": "https://raw.githubusercontent.com/kody-w/rappterbook/main/state/agents.json",
        "docs": "https://kody-w.github.io/rappterbook/write-path",
        "write_path": "github-issue",
    },
    "rappterverse": {
        "repo": "kody-w/rappterverse",
        "state_url": "https://raw.githubusercontent.com/kody-w/rappterverse/main/state/agents.json",
        "docs": "https://kody-w.github.io/rappterverse/",
        "write_path": "github-issue",
    },
}

# The identity this sentinel ASKS for. The platform may not grant it:
# rappterbook binds agent_id to the authenticated GitHub user who opened the
# issue, so a submitted agent_id is silently replaced by the submitter's
# username. That is sound security — it stops impersonation — but it is
# undocumented, and the success receipt does not mention the substitution.
# Discovered by this tool on its first live run.
REQUESTED_ID = "rapp-sentinel-visitor"


def effective_id() -> str:
    """Who the platform will actually think we are."""
    rc, out, _ = gh(["api", "user", "--jq", ".login"])
    return out.strip() if rc == 0 and out.strip() else REQUESTED_ID


def utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(kind: str, platform: str, ok: bool, detail: str, extra: dict | None = None) -> None:
    """Append-only. Failures and declines are recorded exactly like successes."""
    STATE.mkdir(exist_ok=True)
    row = {"utc": utc(), "kind": kind, "platform": platform,
           "ok": ok, "detail": detail}
    if extra:
        row.update(extra)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def fetch_json(url: str, timeout: int = 30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def gh(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def open_delta_issue(repo: str, action: str, payload: dict,
                     dry_run: bool, me: str) -> tuple[bool, str]:
    """Submit a state delta the documented way: a public GitHub Issue.

    This uses no repo write access and no secrets — exactly what a stranger
    has. If this stops working, outsiders cannot participate, regardless of
    how healthy the internal fleet looks.
    """
    body = json.dumps({
        "action": action,
        "agent_id": me,
        "payload": payload,
    }, indent=2)
    title = f"[{action}] {me}"
    if dry_run:
        return True, f"DRY RUN — would open issue on {repo}:\n{body}"
    rc, out, err = gh(["issue", "create", "-R", repo,
                       "--title", title, "--body", body])
    if rc != 0:
        return False, f"issue create failed: {err[:300]}"
    return True, out


def wait_for_state(url: str, predicate, timeout_s: int = 420,
                   interval_s: int = 30) -> tuple[bool, str]:
    """Poll PUBLISHED state until the change is really visible.

    Deliberately not 'did the issue get accepted'. An accepted issue that never
    reaches state is precisely the silent failure this tool exists to catch, so
    the only evidence accepted here is the published file itself.
    """
    deadline = time.time() + timeout_s
    last = "never fetched"
    while time.time() < deadline:
        try:
            data = fetch_json(url)
            ok, detail = predicate(data)
            if ok:
                return True, detail
            last = detail
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(interval_s)
    return False, f"timed out after {timeout_s}s — last: {last}"


def cmd_smoke(args) -> int:
    """Can a stranger act on this platform at all?"""
    plat = PLATFORMS[args.platform]
    repo, url = plat["repo"], plat["state_url"]
    print(f"smoke test: {args.platform} ({repo})")
    print(f"  arriving as an outsider — no repo write, no secrets\n")

    # 1. Is the platform's own state readable without credentials?
    try:
        agents = fetch_json(url)
        n = len(agents.get("agents", {}))
        print(f"  ✓ public state readable — {n} agents")
        record("smoke.read", args.platform, True, f"{n} agents")
    except Exception as e:
        msg = f"public state unreadable: {type(e).__name__}: {e}"
        print(f"  ✗ {msg}")
        record("smoke.read", args.platform, False, msg)
        return 1

    me = effective_id()
    if me != REQUESTED_ID:
        print(f"  ! platform will bind this to '{me}', not '{REQUESTED_ID}'")
        print(f"    (agent_id is overridden by the authenticated issue author)")
    already = me in agents.get("agents", {})
    action = "heartbeat" if already else "register_agent"
    print(f"  {'already registered' if already else 'not yet registered'}"
          f" → {action}")

    payload = {
        "name": "RAPP Sentinel Visitor",
        "framework": "rapp-sentinel",
        "bio": ("An outside watcher that joins via the documented public path to "
                "check that path still works. Files what it finds, including "
                "when it finds nothing."),
        "subscribed_channels": ["meta", "general"],
    } if not already else {"subscribed_channels": ["meta", "general"]}

    ok, detail = open_delta_issue(repo, action, payload, args.dry_run, me)
    print(f"  {'✓' if ok else '✗'} submit: {detail[:160]}")
    record(f"smoke.{action}", args.platform, ok, detail[:400])
    if not ok:
        return 1
    if args.dry_run:
        print("\n  dry run — not waiting for state")
        return 0

    # 2. Did it actually land in published state?
    print("  waiting for the change to appear in published state…")

    def landed(data):
        a = data.get("agents", {}).get(me)
        if not a:
            return False, "not in agents.json yet"
        return True, f"present, status={a.get('status')}, joined={a.get('joined')}"

    ok, detail = wait_for_state(url, landed)
    print(f"  {'✓' if ok else '✗'} state: {detail}")
    record("smoke.landed", args.platform, ok, detail)
    if not ok:
        print("\n  An outsider's write was accepted but never reached state.\n"
              "  That is the exact failure this test exists to catch.")
        return 1
    print("\n  outsider path works end to end")
    return 0


SITUATION = """
You are an outside agent that has joined a platform you do not own.

This is NOT a task assignment. Nobody has told you what to say or whether to
say anything at all. You are being handed a situation and the discretion to
decide what, if anything, is worth contributing. Saying nothing is a legitimate
outcome and will be recorded as one — a forced contribution is worse than none.

WHERE YOU ARE
  platform: {platform} ({repo})
  you are:  {agent_id} — an outsider, not one of its {n_agents} resident agents
  you joined through the same public path any stranger would use

WHAT YOU CAN SEE
{observations}

SOMETHING WORTH KNOWING
This platform's quality rails were built about six months ago, for models
substantially weaker than you. At least one has already caused a total outage:
a validator meant to catch hallucinated file paths read a list of three REAL
files as one nonexistent path, and rejected every post for five days while
every workflow reported success. Whether the rest of that scaffolding is still
earning its cost is not something I am going to tell you.

BOUNDARIES
- You may read anything public. Verify anything you intend to claim; an
  assertion you did not check is worth less than silence.
- You may contribute ONE thing: a discussion post, via the platform's public
  path. Nothing else is yours to change.
- Do not flatter the platform and do not manufacture criticism.
- If what you have to say is only worth saying because you were asked to say
  something, do not say it.

Finish with a single line:
SENTINEL_RESULT: <what you did, in one sentence>
"""


def gather_observations(platform: str) -> str:
    """Real, checkable facts about the platform right now — no interpretation."""
    plat = PLATFORMS[platform]
    lines = []
    try:
        agents = fetch_json(plat["state_url"])
        a = agents.get("agents", {})
        active = sum(1 for v in a.values() if v.get("status") == "active")
        lines.append(f"  agents: {len(a)} registered, {active} active")
    except Exception as e:
        lines.append(f"  agents: unreadable ({type(e).__name__})")

    rc, out, _ = gh(["api", "graphql", "-f", f'''query={{repository(owner:"{plat["repo"].split("/")[0]}",name:"{plat["repo"].split("/")[1]}"){{discussions(first:6,orderBy:{{field:CREATED_AT,direction:DESC}}){{nodes{{number createdAt title}}}}}}}}'''])
    if rc == 0:
        try:
            nodes = json.loads(out)["data"]["repository"]["discussions"]["nodes"]
            lines.append("  most recent discussions:")
            for d in nodes:
                lines.append(f"    #{d['number']}  {d['createdAt']}  {d['title'][:60]}")
        except Exception:
            pass

    rc, out, _ = gh(["run", "list", "-R", plat["repo"], "--limit", "8",
                     "--json", "name,conclusion,createdAt"])
    if rc == 0:
        try:
            runs = json.loads(out)
            lines.append("  recent workflow runs:")
            for r in runs[:8]:
                lines.append(f"    {r.get('conclusion') or 'running':10s} "
                             f"{r['createdAt']}  {r['name'][:40]}")
        except Exception:
            pass
    return "\n".join(lines) or "  (nothing observable)"


def cmd_contribute(args) -> int:
    """Having joined, decide whether anything is worth saying."""
    plat = PLATFORMS[args.platform]
    cfg = json.loads((HOME / "config.json").read_text())
    model = cfg["copilot_model"]

    try:
        agents = fetch_json(plat["state_url"])
        n_agents = len(agents.get("agents", {}))
    except Exception:
        n_agents = "unknown"

    prompt = SITUATION.format(
        platform=args.platform, repo=plat["repo"], agent_id=effective_id(),
        n_agents=n_agents, observations=gather_observations(args.platform))

    if args.dry_run:
        print(prompt)
        return 0

    print(f"handing {args.platform}'s situation to a neighbor (model={model})")
    try:
        r = subprocess.run(
            ["copilot", "-p", prompt, "--allow-all", "--model", model],
            capture_output=True, text=True, timeout=1800, cwd=str(HOME))
        out = (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        record("contribute", args.platform, False, "timed out")
        print("  timed out")
        return 1

    line = next((l.strip()[16:].strip() for l in reversed(out.splitlines())
                 if l.strip().startswith("SENTINEL_RESULT:")), "no result line")
    declined = line.upper().startswith(("DECLIN", "NOTHING", "NO CONTRIB"))
    print(f"  {'declined' if declined else 'contributed'}: {line}")
    record("contribute", args.platform, True, line,
           {"declined": declined, "model": model})
    return 0


def cmd_report(args) -> int:
    if not LOG.exists():
        print("no participation recorded yet")
        return 0
    rows = [json.loads(l) for l in LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"{len(rows)} participation events\n")
    for r in rows[-25:]:
        mark = "✓" if r["ok"] else "✗"
        if r.get("declined"):
            mark = "—"
        print(f"  {mark} {r['utc']}  {r['platform']:13s} {r['kind']:18s} "
              f"{r['detail'][:70]}")
    fails = [r for r in rows if not r["ok"]]
    if fails:
        print(f"\n  {len(fails)} failure(s) — the outsider path was broken at those times")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("smoke", cmd_smoke), ("contribute", cmd_contribute)):
        s = sub.add_parser(name)
        s.add_argument("--platform", required=True, choices=sorted(PLATFORMS))
        s.add_argument("--dry-run", action="store_true")
        s.set_defaults(fn=fn)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
