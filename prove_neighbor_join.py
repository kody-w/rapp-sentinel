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

    # ── the universal pattern: any AIs join by config, not by editing code ──
    home = TMP / "home"
    home.mkdir()
    real_home = NB.HOME
    NB.HOME = home
    try:
        (home / "config.json").write_text(json.dumps({"neighbors": {
            "gemini": "google's model, on the wire like everyone else",
            "grok-4": "xai's model",
            "BadCaps": "must be rejected — not a valid §7 slug",
            "human-molly": "a person is a peer too",
        }}), encoding="utf-8")
        # The roster is read from _CONFIG, loaded once at import (each tick is
        # a fresh process, so "on the next tick" is exactly right in the
        # organism). A proof that swaps HOME must reload it the same way the
        # next tick would, or it measures the cache, not the code.
        NB._CONFIG = NB._load_config()
        roster = NB._load_roster()
        scenario("config-declared AIs join the roster (gemini, grok-4, a human)",
                 {"gemini", "grok-4", "human-molly"} <= set(roster),
                 f"roster now has {len(roster)}: {sorted(roster)}")
        scenario("the estate's default watch is never dropped by config",
                 set(_DEFAULT := NB._DEFAULT_NEIGHBORS) <= set(roster),
                 "all five defaults survive")
        scenario("a slug that cannot form a §7 kind is refused",
                 "BadCaps" not in roster, "invalid slug rejected")
        # A malformed config must never leave fewer watchers than we started.
        (home / "config.json").write_text("{ not json", encoding="utf-8")
        NB._CONFIG = NB._load_config()   # same reload the next tick would do
        scenario("a broken config falls back to the defaults, never fewer",
                 set(NB._load_roster()) == set(NB._DEFAULT_NEIGHBORS),
                 "defaults intact on bad config")
    finally:
        NB.HOME = real_home
        NB._CONFIG = NB._load_config()
finally:
    NB.NBHD, NB.IDENTITY = real["NBHD"], real["IDENTITY"]
    NB.NEIGHBORS = real["NEIGHBORS"]

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
