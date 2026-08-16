"""Reproduction for #1 (ask 1) — one checkout, many instances, and the molt.

`HOME = Path(__file__).resolve().parent` in every module meant an "instance"
was necessarily a copy of the code. The fix is one shared derivation
(paths.py): SENTINEL_HOME set → instance state lives there; unset → byte-
identical to before, which is the constraint the live install survives its
upgrade by. Four legs, every one measured through subprocesses with a
controlled environment, because the thing under test IS the environment:

  1. paths.HOME resolves to the code dir with the env unset, and to the
     scratch dir with it set.
  2. `neighborhood.py publish` under SENTINEL_HOME writes neighbors.json and
     public/sentinel-head.json into the SCRATCH dir and adds NOTHING to the
     code tree — the failure this catches is one module missing the switch
     and quietly writing a second instance into the checkout. The code tree
     is snapshotted before/after including gitignored names; only bytecode
     caches (a code artifact the interpreter owns) are excluded.
  3. Two scratch instances mint different rappids and different external-
     ledger install keys — the isolation the key exists for.
  4. With the env unset, the install key still equals
     rapp.H("rapp/1:install", {"path": str(CODE)})[:16] — the LIVE ledger
     key did not move. A moved key would make the running organism read an
     empty ledger and report its own streams vanished.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

CODE = Path(__file__).resolve().parent
PY = sys.executable

BASE_ENV = {k: v for k, v in os.environ.items() if k != "SENTINEL_HOME"}


def run_py(code, home=None):
    env = dict(BASE_ENV)
    if home is not None:
        env["SENTINEL_HOME"] = str(home)
    r = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                       env=env, cwd=str(CODE), timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"subprocess failed: {r.stderr[:400]}")
    return r.stdout.strip()


def snapshot(root):
    """Every path under the code tree, gitignored names included.

    __pycache__/*.pyc are excluded: bytecode caches are an artifact of the
    interpreter importing CODE, not instance state, and they appear whether
    or not any module misbehaves. .git churn is likewise not state.
    """
    out = set()
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        parts = rel.parts
        if ".git" in parts or "__pycache__" in parts or rel.suffix == ".pyc":
            continue
        out.add(str(rel))
    return out


CASES = []


def case(name, got, want):
    CASES.append((got == want, name, want, got))


# ── 1. the derivation itself ────────────────────────────────────────────────
case("env unset: paths.HOME is the code directory",
     run_py("import paths; print(paths.HOME)"), str(CODE))

scratch1 = Path(tempfile.mkdtemp(prefix="prove-home-1-")).resolve()
case("env set: paths.HOME is the scratch directory",
     run_py("import paths; print(paths.HOME)", home=scratch1), str(scratch1))

# ── 2. publish under SENTINEL_HOME touches only the scratch dir ─────────────
# Import alone mkdirs HOME/neighborhood — with the env set that must be the
# scratch dir, so the code tree gains nothing, not even the directory. The
# snapshot is taken immediately before, so anything the run adds to the code
# tree — a chain, a neighborhood/, a public/ — fails the comparison.
scratch2 = Path(tempfile.mkdtemp(prefix="prove-home-2-")).resolve()
before = snapshot(CODE)
run_py("import neighborhood; neighborhood.publish_head()", home=scratch2)
after = snapshot(CODE)
case("publish under SENTINEL_HOME adds nothing to the code tree",
     sorted(after - before), [])
case("scratch instance minted its identities",
     (scratch2 / "neighborhood" / "neighbors.json").is_file(), True)
case("scratch instance published its head",
     (scratch2 / "public" / "sentinel-head.json").is_file(), True)

# ── 3. two instances are two organisms ──────────────────────────────────────
PROBE = ("import json, neighborhood as NB; "
         "print(json.dumps({'ids': NB.identities(), 'key': NB._INSTALL_KEY}))")
a = json.loads(run_py(PROBE, home=scratch1))
b = json.loads(run_py(PROBE, home=scratch2))
case("two instances mint different rappids",
     any(a["ids"][s] == b["ids"][s] for s in a["ids"]), False)
case("two instances get different external-ledger keys",
     a["key"] == b["key"], False)

# ── 4. the molt: the live ledger key did not move ───────────────────────────
unset = json.loads(run_py(
    "import json, neighborhood as NB, paths, rapp; "
    "print(json.dumps({'key': NB._INSTALL_KEY, "
    "'expected': rapp.H('rapp/1:install', {'path': str(paths.CODE)})[:16]}))"))
case("MOLT env unset: install key still keyed on the code directory",
     unset["key"], unset["expected"])

bad = 0
for good, name, want, got in CASES:
    bad += not good
    print(f"  [{'ok' if good else 'XX'}] {name}\n"
          f"        expected {want!r}  got {got!r}")
print(f"\n{len(CASES)-bad}/{len(CASES)} scenarios behaved as specified")
sys.exit(1 if bad else 0)
