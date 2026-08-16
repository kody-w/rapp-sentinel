#!/usr/bin/env python3
"""prove_contribute_outcomes.py — cmd_contribute records what happened, not
what would be convenient (#5 ask 4, groundwork for #7).

The pre-fix code recorded a model run that produced NO SENTINEL_RESULT line
as ok=True/declined=False — a silent model failure filed as a successful
contribution — and never examined the subprocess's returncode at all. That
is R1's mistake (a receipt is not evidence) with the sign flipped: not even
the receipt was read.

Every assertion here is made by READING the participation.jsonl rows the
tool wrote — the tool's own evidence rule applied to the tool. The old
behavior is demonstrated first, against a throwaway copy of the pre-fix
function spliced from the current source at a named marker; if the marker
is gone the harness ERRORS loudly rather than silently proving nothing.

Run: python3 prove_contribute_outcomes.py   (exit 0 only on all-behaved)
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

import participate as P

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


# ── a temp HOME so the real participation.jsonl is never touched ────────────

TMP = Path(tempfile.mkdtemp(prefix="prove-contribute-"))
(TMP / "state").mkdir()
(TMP / "config.json").write_text(
    json.dumps({"copilot_model": "canned-model"}), encoding="utf-8")

P.HOME = TMP
P.STATE = TMP / "state"
P.LOG = P.STATE / "participation.jsonl"

# No network, no gh: the situation-building reads are stubbed to constants.
P.fetch_json = lambda url, timeout=30: {"agents": {}}
P.gather_observations = lambda platform: "  (stubbed observations)"
P.effective_id = lambda: "prover"


class _Run:
    def __init__(self, returncode, stdout):
        self.returncode, self.stdout, self.stderr = returncode, stdout, ""


def canned(returncode, stdout):
    """A subprocess.run stand-in that replays one transcript."""
    def fake_run(cmd, **kwargs):
        return _Run(returncode, stdout)
    return fake_run


def rows():
    if not P.LOG.exists():
        return []
    return [json.loads(l) for l in
            P.LOG.read_text(encoding="utf-8").splitlines() if l.strip()]


ARGS = argparse.Namespace(platform="rappterbook", dry_run=False)
REAL_RUN = P.subprocess.run


def run_with(fn, returncode, stdout):
    """Call `fn` under a canned transcript; return (rc, newest_row)."""
    before = len(rows())
    P.subprocess.run = canned(returncode, stdout)
    try:
        rc = fn(ARGS)
    finally:
        P.subprocess.run = REAL_RUN
    after = rows()
    return rc, (after[before] if len(after) > before else None)


# ── splice the PRE-FIX function out of the current source, marker-anchored ──

MARKER = "outcome honesty (#5/#7)"
OLD_TAIL = '''
    line = next((l.strip()[16:].strip() for l in reversed(out.splitlines())
                 if l.strip().startswith("SENTINEL_RESULT:")), "no result line")
    declined = line.upper().startswith(("DECLIN", "NOTHING", "NO CONTRIB"))
    print(f"  {'declined' if declined else 'contributed'}: {line}")
    record("contribute", args.platform, True, line,
           {"declined": declined, "model": model})
    return 0
'''

import inspect
src = inspect.getsource(P.cmd_contribute)
if MARKER not in src:
    print(f"ERROR: marker {MARKER!r} not found in cmd_contribute — the fix "
          f"moved and this harness can no longer reconstruct the pre-fix "
          f"function. Re-anchor the splice before trusting anything below.")
    sys.exit(1)
head = src[:src.index(src.splitlines()[
    next(i for i, l in enumerate(src.splitlines()) if MARKER in l)])]
old_src = head.replace("def cmd_contribute(", "def old_cmd_contribute(", 1) \
    + OLD_TAIL
ns = dict(vars(P))
exec(compile(old_src, "<pre-fix cmd_contribute>", "exec"), ns)
old_cmd_contribute = ns["old_cmd_contribute"]

NO_RESULT = "I looked around and wrote three paragraphs but no verdict.\n"

rc, row = run_with(old_cmd_contribute, 0, NO_RESULT)
scenario("OLD behavior, demonstrated: exit 0 + no SENTINEL_RESULT line was "
         "recorded ok=True/declined=False and exited 0 — a silent model "
         "failure filed as success",
         rc == 0 and row is not None and row["ok"] is True
         and row.get("declined") is False,
         f"rc={rc} row={row}")

# ── the fix, judged by the rows it writes ────────────────────────────────────

rc, row = run_with(P.cmd_contribute, 0, NO_RESULT)
scenario("exit 0 + no result line -> ok=False, detail names the missing "
         "line, exit 1",
         rc == 1 and row is not None and row["ok"] is False
         and row["detail"] == "copilot exit 0, no SENTINEL_RESULT line",
         f"rc={rc} row={row}")

rc, row = run_with(P.cmd_contribute, 0,
                   "thinking...\nSENTINEL_RESULT: DECLINED nothing here is "
                   "worth an outsider's noise\n")
scenario("DECLINED -> ok=True, declined=True, exit 0 (declining is a valid "
         "outcome, recorded as one)",
         rc == 0 and row is not None and row["ok"] is True
         and row.get("declined") is True
         and row["detail"].startswith("DECLINED"),
         f"rc={rc} row={row}")

rc, row = run_with(P.cmd_contribute, 0,
                   "SENTINEL_RESULT: BLOCKED the issue form rejected an "
                   "unauthenticated submitter\n")
scenario("BLOCKED -> ok=False, exit 1 (a blocked outsider path is a finding "
         "about the front door, never a decline)",
         rc == 1 and row is not None and row["ok"] is False
         and row.get("declined") is False
         and row["detail"].startswith("BLOCKED"),
         f"rc={rc} row={row}")

rc, row = run_with(P.cmd_contribute, 0,
                   "SENTINEL_RESULT: CONTRIBUTED posted one comment on the "
                   "validator discussion\n")
scenario("CONTRIBUTED -> ok=True, declined=False, exit 0",
         rc == 0 and row is not None and row["ok"] is True
         and row.get("declined") is False
         and row["detail"].startswith("CONTRIBUTED"),
         f"rc={rc} row={row}")

rc, row = run_with(P.cmd_contribute, 3,
                   "SENTINEL_RESULT: CONTRIBUTED posted a comment\n")
scenario("nonzero exit is never believed away by a pretty result line -> "
         "ok=False, detail carries the exit AND the line, exit 1",
         rc == 1 and row is not None and row["ok"] is False
         and row["detail"].startswith("copilot exit 3:")
         and "CONTRIBUTED" in row["detail"],
         f"rc={rc} row={row}")

scenario("the prompt's closing instruction names the three verdicts",
         "CONTRIBUTED, DECLINED or BLOCKED" in P.SITUATION,
         "grammar present" if "CONTRIBUTED, DECLINED or BLOCKED" in P.SITUATION
         else "MISSING from SITUATION")

scenario("no row ever landed outside the temp HOME "
         "(the real participation.jsonl was never written)",
         all(str(P.LOG).startswith(str(TMP)) for _ in [0]) and P.LOG.exists()
         and len(rows()) == 6,
         f"log={P.LOG} rows={len(rows())}")

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
