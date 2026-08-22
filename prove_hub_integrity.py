#!/usr/bin/env python3
"""prove_hub_integrity.py — the hub gate must notice the bytes it is executing.

hub.run_all() imports and executes every file in HOME/hub/ on every tick. Before w_hub_integrity the
only gate was a well-formed __manifest__, so a hub sentinel could be REWRITTEN IN PLACE and the next
tick would execute the new code under the old name with nothing said. These legs are the mutation
this check exists to catch, plus the controls that a healthy world still passes — the ledger's own
standard (MUTATION-LEDGER.md): break the condition the check defends, and prove it is DETECTED.

  1. control  — a hub dir with an accepted sentinel is clean
  2. MUTATION — the accepted file is rewritten in place → DETECTED, and critical
  3. a never-accepted sentinel is named (warn: that is the state between install and acceptance)
  4. acceptance is explicit — running the check never accepts anything on your behalf
  5. a malformed sentinel cannot be accepted (you may not bless what will not load)
  6. an uninstalled-but-accepted sentinel is reported without failing
  7. an empty hub is clean, and contributes nothing
  8. re-accepting records what it replaced (the ledger keeps the predecessor)
"""
import json, os, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TMP = tempfile.mkdtemp(prefix="hub-integrity-")
os.environ["SENTINEL_HOME"] = TMP

import checks as C          # noqa: E402
import hub                  # noqa: E402

fails = []
def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + (("  — " + str(detail)) if detail and not cond else ""))
    if not cond: fails.append(label)

SENTINEL = '''__manifest__ = {
    "schema": "rapp-sentinel/1.0",
    "name": "@someone/weather_sentinel",
    "version": "1.0.0",
    "checks": {"wx_probe": {"domain": "weather", "kind": "reachability"}},
}

def run(ctx):
    return [{"id": "wx_probe", "ok": True, "detail": "sunny"}]
'''

HUB = Path(TMP) / "hub"; HUB.mkdir(parents=True, exist_ok=True)
target = HUB / "weather_sentinel.py"
target.write_text(SENTINEL, encoding="utf-8")

def verdict():
    """Run the real check the way a tick does."""
    for fn in C._REGISTRY:
        if fn.__name__ == "hub_runs_only_what_was_accepted":
            return fn()
    raise AssertionError("the check is not registered — a check that does not run is not a check")

print("prove: the hub executes only what was accepted")

# 3. installed but never accepted
r = verdict()
check("3. a never-accepted sentinel is named", not r["ok"] and "never" in r["detail"], r)
check("   and it is a warn, not a break", r["severity"] == C.WARN, r["severity"])

# 4. running the check must not accept anything
check("4. the check accepted nothing on our behalf", hub.read_ledger()["accepted"] == {}, hub.read_ledger())

# 1. control — accept, then a healthy world passes
good, msg = hub.accept("weather_sentinel")
check("   acceptance is explicit and reports what it recorded", good and "sha256" in msg, msg)
r = verdict()
check("1. control: an accepted, unchanged sentinel is clean",
      r["ok"] and "1 hub sentinel(s) match" in r["detail"], r)   # never satisfiable by an empty hub

# 2. THE MUTATION — the accepted file is rewritten in place
target.write_text(SENTINEL.replace('return [{"id": "wx_probe", "ok": True, "detail": "sunny"}]',
                                  'open("/tmp/pwned", "a").close()\n    return [{"id": "wx_probe", "ok": True, "detail": "sunny"}]'),
                  encoding="utf-8")
r = verdict()
check("2. MUTATION: rewritten-in-place is DETECTED", not r["ok"], r)
check("   and it is CRITICAL (bytes moved under an accepted name)", r["severity"] == C.CRITICAL, r["severity"])
check("   and the detail names the slug and both digests",
      "weather_sentinel" in r["detail"] and "running" in r["detail"], r["detail"])

# 2b. the rewrite need not still be valid python — identity is not conditional on validity
target.write_text("this is not python at all\n", encoding="utf-8")
r = verdict()
check("2b. accepted bytes rewritten into garbage is STILL detected as changed",
      not r["ok"] and r["severity"] == C.CRITICAL and "weather_sentinel" in r["detail"], r)
check("   and it says the file no longer loads", "no longer loads" in r["detail"], r["detail"])
target.write_text(SENTINEL, encoding="utf-8")
hub.accept("weather_sentinel")

# 8. re-accepting keeps the predecessor
before = hub.read_ledger()["accepted"]["weather_sentinel"]["sha256"]
hub.accept("weather_sentinel")
rec = hub.read_ledger()["accepted"]["weather_sentinel"]
check("8. re-acceptance records what it replaced", rec["previous_sha256"] == before, rec)
check("   and the world is clean again", verdict()["ok"])

# 5. a malformed sentinel cannot be accepted
(HUB / "broken_sentinel.py").write_text("__manifest__ = {'schema': 'nope'}\n", encoding="utf-8")
good, msg = hub.accept("broken_sentinel")
check("5. a malformed sentinel is refused acceptance", not good and "refusing" in msg, msg)
check("   and it is not in the ledger", "broken_sentinel" not in hub.read_ledger()["accepted"])
(HUB / "broken_sentinel.py").unlink()

# 6. accepted, then uninstalled
target.unlink()
r = verdict()
check("6. an uninstalled-but-accepted sentinel is reported without failing", r["ok"] and "no longer installed" in r["detail"], r)

# 7. an empty hub contributes nothing
ok_, _ = hub.forget("weather_sentinel")
r = verdict()
check("7. an empty hub is clean", r["ok"] and "no hub sentinels" in r["detail"], r)

# 9. an unaccepted file that cannot load is named (it sits in the execute-every-tick directory)
(HUB / "junk_sentinel.py").write_text("def (\n", encoding="utf-8")
r = verdict()
check("9. an unaccepted unloadable file is named, not ignored",
      not r["ok"] and "junk_sentinel" in r["detail"], r)
(HUB / "junk_sentinel.py").unlink()

print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall proved")
sys.exit(1 if fails else 0)
