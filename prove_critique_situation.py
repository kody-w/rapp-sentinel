#!/usr/bin/env python3
"""prove_critique_situation.py — the evolve situation invites a platform
critique without turning into an instruction to produce one.

Issue #7 asked for a way for the agents living in a platform to say the
platform is broken. The design it sketched was a second autonomous writer
with its own budget, its own rotation, and its own identity question. But the
loop already has an arm shaped exactly like that: `evolve` hands a neighbor
its situation, budgets it, rotates it, and records a decline as a first-class
outcome. Adding a target to a prompt is a smaller change than adding a
subsystem, and it inherits guardrails that are already proven.

What has to stay true after the change, and is checked here:

  * SITUATION, NOT TASK. The framing may offer the critique as something a
    neighbor MAY do. The moment it reads as "find problems", the finding is
    the author's and the agent is just typing (TRIFECTA §6b).
  * DECLINING SURVIVES. If a critique becomes the expected output, an empty
    hand stops being allowed, and an agent that cannot come back empty is not
    exercising judgement.
  * CLAIMS MUST BE CHECKABLE, and the rails are explicitly in scope — the
    issue's own point was that a constraint written for a weaker model can
    become the largest cause of an outage.
  * NO NEW SPEND. The existing evolve budget and rotation carry it; nothing
    here introduces a second envelope.

Run: python3 prove_critique_situation.py   (exit 0 only on all-behaved)
"""

import inspect
import re
import sys

import sentinel as S

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


# The prose is hard-wrapped, so every assertion runs against a
# whitespace-normalised copy. The first version matched the raw string and
# failed on two phrases that merely straddled a line break — a harness that
# reports a present guarantee as missing is its own kind of false alarm.
T = " ".join(S.EVOLVE_SITUATION.split())

scenario("the situation offers a platform critique as an option",
         "CRITIQUE OF THE PLATFORM" in T and "IS a contribution" in T,
         "critique framing present")

# Situation, not task: no imperative that orders the agent to find something.
banned = [r"\bfind problems\b", r"\blist improvements\b",
          r"\byou must (?:file|find|produce|critique)\b",
          r"\bidentify (?:all|any) (?:problems|issues|defects)\b"]
hits = [b for b in banned if re.search(b, T, re.I)]
scenario("...and never as an instruction to produce one (situation, not task)",
         not hits, f"task-shaped phrases found: {hits}" if hits else "none present")

scenario("declining is still explicitly legitimate AFTER the change",
         "Declining is a legitimate outcome" in T
         and "legitimate outcome, recorded as such" in T,
         "both the original and the critique-local restatement survive")

scenario("manufacturing a critique is explicitly ruled out",
         "Do not manufacture a critique" in T, "guard present")

scenario("claims must be checkable, in the critique's own terms",
         "An assertion you did" in T and "worth less than silence" in T,
         "evidence bar stated")

scenario("the agent's own constraints are in scope (issue #6's finding)",
         "rails" in T.lower() and "relaxed is a valuable finding" in T,
         "rails explicitly critiquable")

# No new spend envelope: evolve's budget/rotation still the only gate, and no
# second daily budget key appeared.
src = inspect.getsource(S.main)
scenario("no second spend envelope was introduced",
         "daily_evolve_budget" in src and "daily_critique_budget" not in src
         and "critique_budget" not in inspect.getsource(S),
         "evolve's budget remains the only one")

scenario("the rotation that carries it is unchanged",
         "evolve_turn.json" in src, "existing rotation still in use")

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
