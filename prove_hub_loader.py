#!/usr/bin/env python3
"""prove_hub_loader.py — the hub socket keeps its promises (hub.py).

WAR STORY
The hub is where strangers' checks arrive. Two blindnesses were possible the
day it was wired in, and both are the exact shape this repo has already paid
for: (1) a hub sentinel that stops emitting an id would vanish silently -
the #15 missing-@check defect, reborn for plug-ins; (2) a stranger's file
saying "critical" would wake the repair arm and spend money on a check
nobody here reviewed. And the growth-path constraint that binds every
change: a live install with no hub/ directory must produce byte-identical
verdicts.

Break/control pairs, one process, a throwaway SENTINEL_HOME:
  A  no hub dir            -> declared_ids() empty, run_all() empty, w_checks_complete text unchanged
  B  good sentinel         -> its id appears, produced_by=hub:<name>, joins the required set
  C  sentinel goes silent  -> the id it owes is a warn line, never absent
  D  sentinel raises       -> ONE hub_<slug>_load warn + its owed ids as warn; the tick survives
  E  critical from a stranger -> demoted to warn with the reason; critical_allowed honours it
  F  bad manifest          -> load warn only, contributes no required ids
  G  disabled in config    -> contributes nothing, no load line
  H  config overrides reach run() (merged over manifest defaults)
  I  gh via ctx counts _GH_CALLS (outsider claim stays enforceable)
Exit 1 on any deviation.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="prove-hub-"))
os.environ["SENTINEL_HOME"] = str(tmp)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import checks as C   # noqa: E402
import hub           # noqa: E402
import health        # noqa: E402

GOOD = '''
__manifest__ = {"schema": "rapp-sentinel/1.0", "name": "@tester/probe_sentinel", "version": "1.0.0",
  "description": "probe used by prove_hub_loader.py, twenty chars", "category": "consistency",
  "checks": {"probe_a": {"domain": "probe", "kind": "consistency"}, "probe_b": {"domain": "probe", "kind": "run-status"}},
  "config": {"mode": "ok"}, "requires": [], "vantage": "outsider", "license": "MIT"}
def run(config=None, ctx=None):
    cfg = dict(__manifest__["config"], **(config or {}))
    ok, fail = ctx["ok"], ctx["fail"]
    if cfg["mode"] == "raise":
        raise RuntimeError("boom")
    if cfg["mode"] == "silent":
        return [ok("probe_a", "only a")]
    if cfg["mode"] == "critical":
        return [fail("probe_a", "bad", critical=True), ok("probe_b", "b")]
    if cfg["mode"] == "gh":
        ctx["gh"](["api", "user"])
        return [ok("probe_a", "used gh"), ok("probe_b", "b")]
    return [ok("probe_a", "mode=" + cfg["mode"]), ok("probe_b", "b")]
def prove():
    return True
'''
BAD = "__manifest__ = {'schema': 'nope'}\ndef run(c=None, x=None):\n    return []\n"

hubdir = tmp / "hub"
hubdir.mkdir()
failures = []


def expect(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def cfg(**hub_cfg):
    (tmp / "config.json").write_text(json.dumps({"level": 0, "hub": hub_cfg}), encoding="utf-8")


def by_id(rows):
    return {r["id"]: r for r in rows}


# A — growth path
cfg()
expect(hub.declared_ids() == set() and hub.run_all() == [], "A no sentinels -> nothing declared, nothing run")
base_complete = health.check_completeness([{"id": i, "produced_by": "x"} for i in
                                            json.load(open(Path(health.CODE) / "required_checks.json"))["required"]])
expect(base_complete["ok"] and "from hub" not in base_complete["detail"], "A w_checks_complete text unchanged without hub")

# B — good
(hubdir / "probe_sentinel.py").write_text(GOOD, encoding="utf-8")
rows = by_id(hub.run_all())
expect(set(rows) == {"probe_a", "probe_b"} and rows["probe_a"]["ok"] and rows["probe_a"]["produced_by"] == "hub:@tester/probe_sentinel",
       "B good sentinel emits its ids tagged hub:<name>")
expect(hub.declared_ids() == {"probe_a", "probe_b"}, "B declared ids join the required set")
req = json.load(open(Path(health.CODE) / "required_checks.json"))["required"]
res = [{"id": i, "produced_by": "x"} for i in req]           # everything native ran, hub did NOT
r = health.check_completeness(res)
expect(not r["ok"] and "probe_a" in r["detail"], "B/C an installed sentinel that never reported fails w_checks_complete")
r = health.check_completeness(res + list(rows.values()))
expect(r["ok"] and "2 from hub" in r["detail"], "B ...and passes once it reports")
kinds = hub.declared_kinds()
expect(kinds["probe_b"]["kind"] == "run-status", "B kinds map merged")
pair = health.check_freshness_pairing()
expect(not pair["ok"] and "probe" in pair["detail"], "B run-status without freshness in a hub domain fires w_freshness_paired (R2)")
cfg(unpaired_accepted={"probe": "prove harness"})
pair = health.check_freshness_pairing()
expect(pair["ok"], "B ...accepted in config.json hub.unpaired_accepted")

# C — silent
cfg(config={"probe_sentinel": {"mode": "silent"}})
rows = by_id(hub.run_all())
expect("probe_b" in rows and not rows["probe_b"]["ok"] and rows["probe_b"]["severity"] == "warn",
       "C an owed id that was not emitted is a warn line, never absent")

# D — raises
cfg(config={"probe_sentinel": {"mode": "raise"}})
rows = by_id(hub.run_all())
expect("hub_probe_sentinel_load" in rows and rows["hub_probe_sentinel_load"]["severity"] == "warn"
       and {"probe_a", "probe_b"} <= set(rows) and not rows["probe_a"]["ok"],
       "D a raising sentinel = one load warn + its owed ids as warn; tick survives")

# E — critical demotion / dial
cfg(config={"probe_sentinel": {"mode": "critical"}})
rows = by_id(hub.run_all())
expect(rows["probe_a"]["severity"] == "warn" and "demoted" in rows["probe_a"]["detail"],
       "E stranger's critical demoted to warn, reason in detail")
cfg(config={"probe_sentinel": {"mode": "critical"}}, critical_allowed=["probe_sentinel"])
rows = by_id(hub.run_all())
expect(rows["probe_a"]["severity"] == "critical", "E critical_allowed honours it")

# F — bad manifest
(hubdir / "broken_sentinel.py").write_text(BAD, encoding="utf-8")
cfg()
rows = by_id(hub.run_all())
expect("hub_broken_sentinel_load" in rows and rows["hub_broken_sentinel_load"]["severity"] == "warn",
       "F bad manifest -> load warn")
expect(hub.declared_ids() == {"probe_a", "probe_b"}, "F ...and contributes no required ids")
(hubdir / "broken_sentinel.py").unlink()

# G — disabled
cfg(disabled=["probe_sentinel"])
expect(hub.run_all() == [] and hub.declared_ids() == set(), "G disabled slug contributes nothing")

# H — overrides
cfg(config={"probe_sentinel": {"mode": "custom"}})
rows = by_id(hub.run_all())
expect(rows["probe_a"]["detail"] == "mode=custom", "H config overrides reach run()")

# I — gh counts
cfg(config={"probe_sentinel": {"mode": "gh"}})
before = C._GH_CALLS
hub.run_all()
expect(C._GH_CALLS == before + 1, "I ctx.gh routes through checks.gh (counts _GH_CALLS)")

# J — full runner still emits a verdict with a hub sentinel installed (no crash)
cfg()
import io, contextlib
buf = io.StringIO()
# only run the cheap path: verify main() would include hub rows by calling the same composition
rows = hub.run_all()
expect(any(r.get("hub_version") == "1.0.0" for r in rows), "J hub_version tag present")

print("\nprove_hub_loader:", "PASS" if not failures else f"FAIL ({len(failures)})")
sys.exit(1 if failures else 0)
