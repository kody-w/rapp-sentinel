#!/usr/bin/env python3
"""prove_method_change.py — a repeat escalation carries its own history, and
attempt 1 stays clean.

#4's incident: the escalation prompt was byte-identical on attempts 1
through 3, so the same wrong diagnosis got three confident retries and the
third one texted the owner a false alarm. method_change_block() is the pure,
model-free piece: from attempt 2 it injects the count, the prior result
verbatim, and the instruction to measure (diagnose.py) instead of re-infer.
Absent from attempt 1 on purpose — diluted into every prompt it would be
noise, and noise trains the reader to scroll past.

Run: python3 prove_method_change.py   (exit 0 only on all-behaved)
"""

import sys

import sentinel as S

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


b = S.method_change_block(1, None)
scenario("attempt 1: block ABSENT (no dilution to noise)", b == "", repr(b[:40]))

b = S.method_change_block(2, "BLOCKED — token lacks scope")
scenario("attempt 2: block present with the prior result quoted verbatim",
         "ATTEMPT 2" in b and "1 PRIOR" in b
         and "BLOCKED — token lacks scope" in b,
         b.strip().splitlines()[0] if b else "empty")
scenario("attempt 2: the block names the measurement, not another inference",
         "diagnose.py" in b and "DIAGNOSIS is wrong" in b,
         "diagnose.py named" if "diagnose.py" in b else "MISSING")

b = S.method_change_block(3, "PARTIAL — patched agent_heartbeat.py")
scenario("attempt 3: counts 2 prior attempts",
         "ATTEMPT 3" in b and "2 PRIOR" in b,
         b.strip().splitlines()[0] if b else "empty")

b = S.method_change_block(2, None)
scenario("attempt 2 with no recorded result: block absent rather than vacuous",
         b == "", repr(b[:40]))

# The block actually reaches the prompt: build via escalate()'s own assembly
# by inspecting the source contract — the f-string embeds
# method_change_block(attempt, last_result). Assert the call site exists so a
# refactor cannot silently orphan the block.
import inspect
src = inspect.getsource(S.escalate)
scenario("escalate() embeds method_change_block in the prompt",
         "method_change_block(attempt, last_result)" in src,
         "call site present" if "method_change_block" in src else "MISSING")

src_main = inspect.getsource(S.main)
scenario("main() reads the issue record BEFORE escalating and passes history",
         "attempt=rec[\"attempts\"] + 1" in src_main
         and "last_result=rec.get(\"last_result\")" in src_main,
         "history threaded" if "last_result=rec.get" in src_main else "MISSING")

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
