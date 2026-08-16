#!/usr/bin/env python3
"""prove_attests_for.py — a head can carry the subject it stands witness for,
a malformed claim cannot travel, and absent config changes nothing.

Issue #1 ask 4: a watcher mints its own rappid, so its published head
attests that a watcher is alive — never that the twin it watches is, and a
reader had to trust prose linking them. attests_for is the machine-readable
link: a VALIDATED claim (publish refuses a malformed rappid; head_violations
rejects one arriving from a peer) that remains only the operator's
assertion — validated, never verified.

Run: python3 prove_attests_for.py   (exit 0 only on all-behaved)
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


TMP = Path(tempfile.mkdtemp(prefix="prove-attests-"))
(TMP / "nbhd").mkdir()
real = dict(HOME=NB.HOME, NBHD=NB.NBHD, IDENTITY=NB.IDENTITY, PUBLIC=NB.PUBLIC)
NB.HOME = TMP
NB.NBHD = TMP / "nbhd"
NB.IDENTITY = NB.NBHD / "neighbors.json"
NB.PUBLIC = TMP / "public"

SUBJECT = rapp.mint_rappid("kody-w", "rock-tumbler")

try:
    NB.emit("copilot", "sentinel.tick", {"probe": 1})   # a chain to publish

    # (1) No config: byte-shape identical to today — no attests_for key.
    doc = NB.publish_head()
    scenario("(1) absent config: no attests_for key anywhere (growth path)",
             all("attests_for" not in h for h in doc["heads"].values()),
             f"keys={sorted(doc['heads'].get('copilot', {}))}")

    # (2) A valid claim travels in the head entry.
    (TMP / "config.json").write_text(json.dumps(
        {"attests_for": {"copilot": SUBJECT}}), encoding="utf-8")
    doc = NB.publish_head()
    scenario("(2) valid claim: the head carries the subject rappid",
             doc["heads"]["copilot"].get("attests_for") == SUBJECT,
             doc["heads"]["copilot"].get("attests_for", "ABSENT")[:60])
    scenario("(2b) the published head passes the real validator",
             NB.head_violations(doc) == [], str(NB.head_violations(doc)))

    # (3) A malformed claim FAILS THE PUBLISH — an operator typo must not
    # travel as a trust link.
    (TMP / "config.json").write_text(json.dumps(
        {"attests_for": {"copilot": "sha256-of-a-name"}}), encoding="utf-8")
    raised = False
    try:
        NB.publish_head()
    except ValueError as e:
        raised = "refusing to publish" in str(e)
    scenario("(3) malformed claim: publish_head refuses outright", raised,
             "ValueError raised" if raised else "published anyway")

    # (4) A peer's head carrying a malformed attests_for is rejected by
    # head_violations — the field cannot smuggle arbitrary strings.
    (TMP / "config.json").unlink()
    doc = NB.publish_head()
    doc["heads"]["copilot"]["attests_for"] = "not-a-rappid"
    bad = NB.head_violations(doc)
    scenario("(4) peer head with malformed attests_for is flagged",
             any("attests_for" in b for b in bad), str(bad)[:100])

    # (5) A peer's WELL-FORMED claim passes — validated, not verified, and
    # the validator's job stops at well-formedness.
    doc["heads"]["copilot"]["attests_for"] = SUBJECT
    scenario("(5) well-formed peer claim passes validation",
             NB.head_violations(doc) == [], str(NB.head_violations(doc)))
finally:
    NB.HOME, NB.NBHD, NB.IDENTITY, NB.PUBLIC = (
        real["HOME"], real["NBHD"], real["IDENTITY"], real["PUBLIC"])

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
