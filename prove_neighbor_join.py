#!/usr/bin/env python3
"""prove_neighbor_join.py — a neighbor joins by being declared, and no
existing identity moves when it does.

The neighborhood grew from three watchers to five — the four AIs of the
demo (scout, copilot, claude-code, brainstem) plus the openrappter daemon
underneath. Adding a peer has to be additive on a LIVE install: the three
running chains, with their thousands of verified frames, cannot be
disturbed, and the new peers must actually mint, chain and verify.

The trap this proves closed: identities() cached the first identity file it
wrote and returned it forever, so a neighbor declared after that file
existed would never get a rappid — and emit()/publish_head() would KeyError
on it. A membership promise defeated by a cache. The fix backfills only the
missing slugs and re-mints nothing.

Run: python3 prove_neighbor_join.py   (exit 0 only on all-behaved)
"""

import json
import sys
import tempfile
from pathlib import Path

import neighborhood as NB
import rapp

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


TMP = Path(tempfile.mkdtemp(prefix="prove-join-"))
real = dict(NBHD=NB.NBHD, IDENTITY=NB.IDENTITY, NEIGHBORS=dict(NB.NEIGHBORS))
NB.NBHD = TMP
NB.IDENTITY = TMP / "neighbors.json"

try:
    # The four AIs are all present in the shipped roster.
    for who in ("scout", "copilot", "claude-code", "brainstem"):
        scenario(f"{who} is a declared neighbor", who in NB.NEIGHBORS,
                 NB.NEIGHBORS.get(who, "MISSING"))

    # Simulate a LIVE install: an identity file that predates the new slugs.
    NB.NEIGHBORS = {"openrappter": "d", "brainstem": "r", "copilot": "f"}
    old = NB.identities()
    scenario("a pre-existing install has exactly its three identities",
             set(old) == {"openrappter", "brainstem", "copilot"}, sorted(old))
    frozen = dict(old)

    # Now the code declares five. Backfill must mint the two new ones and
    # leave the three originals byte-for-byte unchanged.
    NB.NEIGHBORS = dict(real["NEIGHBORS"])
    grown = NB.identities()
    scenario("declaring two more neighbors mints exactly those two",
             set(grown) == set(real["NEIGHBORS"])
             and set(grown) - set(frozen) == {"scout", "claude-code"},
             f"gained {sorted(set(grown) - set(frozen))}")
    scenario("no existing rappid was re-minted (identities are mint-once)",
             all(grown[s] == frozen[s] for s in frozen),
             "three originals unchanged")
    scenario("the new rappids are conformant §6.1 rappids",
             all(rapp.rappid_valid(grown[s]) for s in ("scout", "claude-code")),
             "both valid")

    # The new neighbor can emit and its chain verifies from genesis.
    f = NB.emit("scout", "scout.hello", {"joined": True})
    ok, detail = NB.verify("scout")
    scenario("scout emits a genesis frame that verifies from genesis",
             f["seq"] == 0 and ok, detail)

    # The roll call now sees all five, each a valid chain.
    roll = NB.roll_call()
    scenario("roll_call reports all five neighbors",
             set(roll) == set(real["NEIGHBORS"]) and all(v["chain_ok"] for v in roll.values()),
             f"{len(roll)} neighbors, all chain_ok")

    # A published head carries the new neighbor without violating §5/§6.
    NB.PUBLIC = TMP / "public"
    doc = NB.publish_head()
    scenario("publish_head includes scout with no head violations",
             "scout" in doc["heads"] and NB.head_violations(doc) == [],
             str(NB.head_violations(doc)))
finally:
    NB.NBHD, NB.IDENTITY = real["NBHD"], real["IDENTITY"]
    NB.NEIGHBORS = real["NEIGHBORS"]

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
