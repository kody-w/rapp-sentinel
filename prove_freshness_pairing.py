#!/usr/bin/env python3
"""prove_freshness_pairing.py — R2's mechanical enforcement fires, and an
accepted gap is a visible decision, not a silence.

The five-day rappterbook outage (#3) happened in a domain that had run-status
coverage and no output-freshness coverage, and nothing surfaced that gap
until it had already cost the five days. w_freshness_paired reads the kinds
map in required_checks.json and fails — at warn, since the repair arm cannot
write a freshness check but a human can — any domain with run-status checks
and no output-freshness check, unless the manifest's unpaired_accepted map
names the gap as a deliberate decision. Deleting the acceptance re-arms the
check: that is proven here, not assumed.

Run: python3 prove_freshness_pairing.py   (exit 0 only on all-behaved)
"""

import json
import sys
import tempfile
from pathlib import Path

import health as H

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


REAL_CODE = H.CODE
COMMITTED = json.loads((REAL_CODE / "required_checks.json").read_text(
    encoding="utf-8"))


def with_manifest(doc_or_bytes):
    d = Path(tempfile.mkdtemp(prefix="prove-pairing-"))
    p = d / "required_checks.json"
    if isinstance(doc_or_bytes, bytes):
        p.write_bytes(doc_or_bytes)
    else:
        p.write_text(json.dumps(doc_or_bytes), encoding="utf-8")
    H.CODE = d
    return d


try:
    # (1) The committed manifest passes, and the accepted gap is SAID, not
    # hidden — silently reclassifying ecosystem would break this assertion.
    H.CODE = REAL_CODE
    r = H.check_freshness_pairing()
    scenario("(1) committed manifest: ok, and the ecosystem acceptance is named",
             r["ok"] and "accepted-unpaired" in r["detail"]
             and "ecosystem" in r["detail"], r["detail"])

    # (2) Remove BOTH rappterbook freshness entries -> fires naming the domain.
    doc = json.loads(json.dumps(COMMITTED))
    del doc["kinds"]["rb_content_moving"]
    del doc["kinds"]["rb_shards"]
    with_manifest(doc)
    r = H.check_freshness_pairing()
    scenario("(2) a domain stripped of freshness coverage fires, at WARN, naming it",
             (not r["ok"]) and r["severity"] == H.C.WARN
             and "rappterbook" in r["detail"] and "R2" in r["detail"],
             f"severity={r['severity']} {r['detail']}")

    # (3) One freshness check suffices — pairing needs one, not all.
    doc = json.loads(json.dumps(COMMITTED))
    del doc["kinds"]["rb_shards"]
    with_manifest(doc)
    r = H.check_freshness_pairing()
    scenario("(3) removing one of two freshness checks does not fire", r["ok"],
             r["detail"])

    # (4) No kinds map: pairing unknown is a warn, never green (#45).
    doc = json.loads(json.dumps(COMMITTED))
    del doc["kinds"]
    with_manifest(doc)
    r = H.check_freshness_pairing()
    scenario("(4) kinds map missing -> warn 'pairing unknown', not green",
             (not r["ok"]) and r["severity"] == H.C.WARN
             and "kinds map missing" in r["detail"],
             f"severity={r['severity']} {r['detail']}")

    # (5) Deleting the acceptance re-arms the check for that domain.
    doc = json.loads(json.dumps(COMMITTED))
    doc["unpaired_accepted"] = {}
    with_manifest(doc)
    r = H.check_freshness_pairing()
    scenario("(5) deleting the ecosystem acceptance re-arms: fires naming ecosystem",
             (not r["ok"]) and "ecosystem" in r["detail"],
             f"severity={r['severity']} {r['detail']}")

    # (6) An unclassified required id is reported, never failed — mirroring
    # w_checks_complete's unlisted convention.
    doc = json.loads(json.dumps(COMMITTED))
    doc["required"] = list(doc["required"]) + ["mystery_check"]
    with_manifest(doc)
    r = H.check_freshness_pairing()
    scenario("(6) an unclassified id rides in the detail without failing",
             r["ok"] and "unclassified" in r["detail"]
             and "mystery_check" in r["detail"], r["detail"])

    # (7) An unreadable manifest is a warn about the instrument.
    with_manifest(b"{ not json")
    r = H.check_freshness_pairing()
    scenario("(7) unreadable manifest -> warn, never green",
             (not r["ok"]) and r["severity"] == H.C.WARN
             and "unreadable" in r["detail"],
             f"severity={r['severity']} {r['detail']}")
finally:
    H.CODE = REAL_CODE

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
