#!/usr/bin/env python3
"""prove_principal_heal.py — the Principal must heal, feed back, and reorient; and must not overstep.

Grading a classroom changes nothing on its own. These are the behaviours that make the Principal
useful rather than decorative, each proved against a real temporary sentinel home:

  1. an absent sentinel is DETECTED (state older than two intervals is named, not shrugged at)
  2. a home with nothing scheduling it is NAMED (a sentinel nothing runs is the quietest failure)
  3. feedback is FILED WHERE THE SENTINEL CAN READ IT (state/principal-feedback.json + .jsonl)
  4. a reorientation is a PROPOSAL by default — direction.json is not touched
  5. when apply is set, the reorientation lands AND the owner's boundaries survive it untouched
  6. lost points name the dimension, never the generic all-good note
"""
import base64, json, os, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("SENTINEL_HOME", tempfile.mkdtemp(prefix="principal-home-"))
import principal as P

fails = []
def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + (("  — " + str(detail)) if detail and not cond else ""))
    if not cond: fails.append(label)

def run(prog, home, stdin="", extra=(), doc=None):
    """Programs take (home[, extra]) as argv and arrive on stdin — the shape that survives PowerShell."""
    argv = [sys.executable, "-", str(home)] + list(extra)
    if doc is not None:
        argv.append(base64.b64encode(json.dumps(doc).encode()).decode())
    r = subprocess.run(argv, capture_output=True, text=True, input=prog, timeout=60)
    lines = [l for l in (r.stdout or "").strip().splitlines() if l.strip()]
    return (json.loads(lines[-1]) if lines else {}), r

print("prove: the Principal heals, explains itself, and reorients without overstepping")
with tempfile.TemporaryDirectory() as td:
    home = Path(td) / "home"; (home / "state").mkdir(parents=True)
    (home / "config.json").write_text(json.dumps({"instance_slug": "fake", "interval_s": 900}))
    direction = {"situation": "watch the thing", "boundaries": ["never force-push", "never exceed budget"],
                 "cares_about": ["kody-w/a", "kody-w/gone"], "freedom": 1}
    (home / "direction.json").write_text(json.dumps(direction))
    # a record that stopped moving three hours ago, on a 15-minute interval
    (home / "state" / "last_run.json").write_text(json.dumps({"at": "2026-01-01T00:00:00Z"}))
    os.utime(home / "state" / "last_run.json", (time.time() - 3 * 3600,) * 2)

    res, r = run(P.HEAL, home, extra=("20",))
    joined = " ".join(res.get("findings", []))
    check("1. absent sentinel detected", "stale record" in joined, res)
    check("2. nothing scheduling it is named", "no launchd job" in joined, res)
    check("   heal took no destructive action on an unknown home", not res.get("actions"), res.get("actions"))

    doc = {"utc": "2026-01-01T00:00:00Z", "grade": "C", "score": 70, "points": {"job": 5},
           "lost": ["job 5/25"], "notes": ["stale"], "note": {"one_change": "point it at the live repo"},
           "reorientation": {"needed": True, "add_cares": ["kody-w/new"], "drop_cares": ["kody-w/gone"],
                             "situation_note": "the old repo is archived", "freedom": 2, "why": "evidence"},
           "apply": False}
    res, r = run(P.FEEDBACK_WRITE, home, doc=doc)
    fb = home / "state" / "principal-feedback.json"
    check("3. feedback filed where the sentinel reads it", fb.exists() and (home / "state" / "principal-feedback.jsonl").exists())
    check("   feedback carries the reasons, not just the letter", json.loads(fb.read_text()).get("lost") == ["job 5/25"])
    after = json.loads((home / "direction.json").read_text())
    check("4. reorientation is a proposal — direction.json untouched", after == direction, after)
    check("   the proposal is readable", (home / "state" / "principal-reorientation.json").exists())

    doc["apply"] = True
    res, r = run(P.FEEDBACK_WRITE, home, doc=doc)
    after = json.loads((home / "direction.json").read_text())
    check("5. apply lands the reorientation", "kody-w/new" in after["cares_about"] and "kody-w/gone" not in after["cares_about"], after)
    check("   the owner's boundaries survive it", after["boundaries"] == direction["boundaries"], after.get("boundaries"))
    check("   the previous direction is kept", (Path(str(home / "direction.json")) .with_suffix(".json.bak")).exists()
          or (home / "direction.json.bak").exists())

# 7. a home is shared by the tick AND its outbox-drain; healing the drain would "fix" a sentinel
#    that is still not ticking, so the tick must win the label race.
with tempfile.TemporaryDirectory() as td2:
    home2 = Path(td2) / "home"; (home2 / "state").mkdir(parents=True)
    agents = Path(td2) / "LaunchAgents"; agents.mkdir()
    import plistlib
    for label, args in (("com.rapp.x.outbox-drain", ["/usr/bin/python3", "outbox.py", "drain"]),
                        ("com.rapp.x", ["/bin/bash", str(home2.parent / "code" / "run.sh")])):
        plistlib.dump({"Label": label, "ProgramArguments": args, "StartInterval": 900,
                       "EnvironmentVariables": {"SENTINEL_HOME": str(home2)}},
                      open(agents / (label + ".plist"), "wb"))
    prog = P.HEAL.replace('os.path.expanduser("~/Library/LaunchAgents/*.plist")', repr(str(agents / "*.plist")))
    res, _ = run(prog, home2, extra=("20",))
    check("7. heal targets the tick job, never the outbox-drain", res.get("label") == "com.rapp.x", res.get("label"))
    check("   and the drain is not even considered", "com.rapp.x.outbox-drain" not in (res.get("considered") or []), res.get("considered"))

obs = {"grade": "B", "score": 80, "points": {"attendance": 25, "record": 20, "job": 5, "honesty": 15, "discipline": 15},
       "notes": ["present, on time, record intact, doing the declared job", "principal's note grades C vs rubric B: x"]}
lost = P.lost_points(obs)
check("6. lost points name the dimension", any(l.startswith("job 5/25") for l in lost), lost)
check("   and never quote the all-good note", not any("present, on time" in l for l in lost), lost)

print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall proved")
sys.exit(1 if fails else 0)
