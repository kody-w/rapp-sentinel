#!/usr/bin/env python3
"""prove_agents_shape.py — the outsider verifier reads BOTH agents shapes.

Found on the rappterverse smoke's first live run (2026-08-16): rappterbook
serves `agents` as a dict keyed by id, rappterverse as a LIST of rows.
participate.py assumed dict, so the verifier crashed per poll inside
wait_for_state and the smoke reported "timed out — AttributeError: 'list'
object has no attribute 'get'" — an instrument defect wearing the exact
costume of the platform failure the tool exists to catch.

Run: python3 prove_agents_shape.py   (exit 0 only on all-behaved)
"""

import sys

import participate as P

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


DICT_DOC = {"agents": {"kody-w": {"status": "dormant", "joined": "2026-08-04"}}}
LIST_DOC = {"schema": "x", "agents": [
    {"id": "rapp-guide-001", "name": "RAPP Guide", "status": "active"},
    {"id": "kody-w", "name": "RAPP Sentinel Visitor", "status": "active",
     "joined": "2026-08-16"},
    "not-a-dict-row",
], "_meta": {}}

idx = P.agents_index(DICT_DOC)
scenario("dict shape (rappterbook): index is the dict itself",
         idx is DICT_DOC["agents"], f"keys={sorted(idx)}")

idx = P.agents_index(LIST_DOC)
scenario("list shape (rappterverse): rows keyed by id, none dropped",
         set(idx) == {"rapp-guide-001", "kody-w", "row-2"} and
         idx["kody-w"]["status"] == "active", f"keys={sorted(idx)}")

# The exact crash, reproduced against the OLD access pattern, then shown
# absent through the new one — refusing vs silently-non-working must be
# distinguishable (the #38 harness rule: prove the break actually breaks).
crashed = False
try:
    LIST_DOC.get("agents", {}).get("kody-w")      # the pre-fix line, verbatim
except AttributeError:
    crashed = True
scenario("the pre-fix access pattern crashes on the list shape (the live bug)",
         crashed, "AttributeError reproduced")

a = P.agents_index(LIST_DOC).get("kody-w")
scenario("the landed() predicate's read now answers on the list shape",
         a is not None and a.get("joined") == "2026-08-16",
         f"row={a}")

scenario("membership check works on both shapes",
         "kody-w" in P.agents_index(DICT_DOC)
         and "kody-w" in P.agents_index(LIST_DOC), "present in both")

scenario("malformed top level is empty, never a raise",
         P.agents_index(["bare", "list"]) == {} and P.agents_index(None) == {},
         "empty dict on both")

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
