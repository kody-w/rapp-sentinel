"""Proof for #8 — say() refuses the unsigned relay, and the refusal is real.

The defect: `say()` wrote bare `rapp-twin-chat/1.0` envelopes straight to
`relay.jsonl`. §6 ("On-relay form") is explicit that on a relay — and it names
"local file" first — the envelope rides inside a signed
`rapp-commons-event/1.0` wrapper: {from, pub, alg:"ecdsa-p256", ts, kind,
body, sig}. ECDSA-P256 is not in the stdlib and this repo is deliberately
stdlib-only, so an "append-only-looking" relay any local process could rewrite
undetectably is exactly the failure this module exists to catch — a weaker
guarantee than the hash-chained chain.jsonl logs sitting beside it, wearing
the same clothes.

The fix is already merged: say() now REFUSES (NotImplementedError), the same
posture as `console`, and the §16 conformance block records both gaps. What
was missing is the house-rule proof that the refusal actually fires — a fix
without a reproduction is a claim, not evidence (#38's rule: a mutation
harness must prove it actually broke something).

Five scenarios:
  S1  say() raises NotImplementedError AND relay.jsonl does not exist after —
      the refusal prevented the write, not merely raised past it.
  S2  kind="console" still raises ValueError first: the §6b sealed-only guard
      precedes the relay refusal, so fixing #8 did not swallow the older one.
  S3  build_envelope() still hands back the in-memory §6a payload shape
      (schema rapp-twin-chat/1.0) and creates no relay file — the sanctioned
      escape hatch stays open.
  S4  MUTATION: textually revert the fix in a scratch copy (re-instate the
      pre-fix append of the bare envelope), import it under a throwaway name,
      and observe relay.jsonl grow by exactly one line missing all five
      wrapper fields (from, pub, alg, ts, sig). This proves the harness can
      tell refusing apart from silently-non-conformant — without it, S1 could
      pass forever against a say() that never existed.
  S5  The module docstring still confesses both §16 gaps (#8's secondary
      finding: the self-audit block was one gap short). A conformance block
      that drifts back to one gap is the same lie at a different layer.

Every path global is pointed at a mkdtemp BEFORE any neighborhood call —
identities() lazily writes IDENTITY on first use, so ordering matters. The
real neighborhood/ directory is never written; if importing the module had to
create it empty, that is undone on exit.
"""
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REAL_NBHD = HERE / "neighborhood"
_nbhd_preexisted = REAL_NBHD.exists()

import neighborhood as nb  # noqa: E402  (import order is the point: see below)

# Redirect every path global into a scratch root before ANY call — the first
# identities() call writes IDENTITY, and chain_path() mkdirs under NBHD.
# resolve(): on macOS mkdtemp hands back /var/… but a module that derives its
# home via Path(__file__).resolve() sees /private/var/… — the containment
# check below would call a perfectly sandboxed path an escape.
TMP = Path(tempfile.mkdtemp(prefix="prove-unsigned-relay-")).resolve()
SANDBOX = TMP / "nb"
SANDBOX.mkdir()
nb.NBHD = SANDBOX
nb.IDENTITY = SANDBOX / "neighbors.json"
nb.RELAY = SANDBOX / "relay.jsonl"
nb.ANCHORS = SANDBOX / "anchors.jsonl"
nb.PEERS = SANDBOX / "peers.json"

CASES = []


def case(name, good, observed):
    CASES.append((bool(good), name, observed))


def cleanup():
    """Leave no footprint — including the empty real neighborhood/ the module
    import had to mkdir. Anything of ours INSIDE the real directory is a
    harness bug and fails the run."""
    shutil.rmtree(TMP, ignore_errors=True)
    if not _nbhd_preexisted and REAL_NBHD.exists():
        leftovers = list(REAL_NBHD.iterdir())
        if leftovers:
            print(f"  [HARNESS ERROR] real neighborhood/ gained files during "
                  f"the proof: {[p.name for p in leftovers]}")
            sys.exit(1)
        REAL_NBHD.rmdir()


def harness_error(msg):
    """A harness that cannot run its scenario must ERROR, never report MISSED."""
    print(f"  [HARNESS ERROR] {msg}")
    cleanup()
    sys.exit(1)


# ── S1: the refusal fires, and it fires BEFORE the write ────────────────────
try:
    nb.say("openrappter", "brainstem", "chat", {"text": "hello"})
    case("S1 say() refuses the unsigned relay", False,
         "say() returned instead of raising — the relay was written or worse")
except NotImplementedError as e:
    wrote = nb.RELAY.exists()
    case("S1 say() refuses the unsigned relay", not wrote,
         f"NotImplementedError raised ({str(e)[:44]}…); "
         f"relay.jsonl exists={wrote}")
except Exception as e:
    case("S1 say() refuses the unsigned relay", False,
         f"wrong exception: {type(e).__name__}: {e}")

# ── S2: the §6b console guard still comes first ─────────────────────────────
try:
    nb.say("openrappter", "brainstem", "console", {"cmd": "rm -rf /"})
    case("S2 console is still sealed-only, checked first", False,
         "say() accepted kind=console")
except ValueError as e:
    case("S2 console is still sealed-only, checked first",
         not nb.RELAY.exists(), f"ValueError first, as before the fix: {e}")
except NotImplementedError:
    case("S2 console is still sealed-only, checked first", False,
         "relay refusal fired before the §6b console guard — order regressed")

# ── S3: the in-memory §6a escape hatch still works, and writes nothing ──────
env = nb.build_envelope("brainstem", "copilot", "chat", {"text": "in memory"})
case("S3 build_envelope() returns §6a shape, touches no relay",
     isinstance(env, dict) and env.get("schema") == "rapp-twin-chat/1.0"
     and not nb.RELAY.exists(),
     f"schema={env.get('schema')!r}, relay.jsonl exists={nb.RELAY.exists()}")

# ── S4: revert the fix in a scratch copy — the bare-envelope write returns ──
# Anchor on the one NotImplementedError in say(); if the marker moved, this
# harness can no longer apply its mutation and must say so loudly, because a
# mutation that silently failed to apply would report the fix as present
# whether or not it is (#38: prove the mutation actually broke something).
MUT_HOME = TMP / "mutant"
MUT_HOME.mkdir()
src = (HERE / "neighborhood.py").read_text(encoding="utf-8")
MARKER = "raise NotImplementedError("
if src.count(MARKER) != 1:
    harness_error(f"mutation anchor {MARKER!r} found {src.count(MARKER)} times "
                  "in neighborhood.py (need exactly 1) — cannot apply the "
                  "pre-fix mutation; fix the anchor, do not trust this run")
start = src.index(MARKER)
start = src.rindex("\n", 0, start) + 1          # start of the raise line
end = src.index("\n    )\n", start)             # closing paren of the raise
mutated = src[:start] + (
    "    env = build_envelope(from_slug, to_slug, kind, payload)\n"
    "    with open(RELAY, \"a\", encoding=\"utf-8\") as fh:\n"
    "        fh.write(json.dumps(env, ensure_ascii=False) + \"\\n\")\n"
    "    return env\n"
) + src[end + len("\n    )\n"):]
if MARKER in mutated:
    harness_error("mutation left the refusal in place — the splice failed")
mut_path = MUT_HOME / "neighborhood_prefix_mutant.py"
mut_path.write_text(mutated, encoding="utf-8")

spec = importlib.util.spec_from_file_location("neighborhood_prefix_mutant",
                                              mut_path)
mut = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mut)   # HOME derives from __file__, so all its paths
if TMP not in mut.RELAY.parents:  # land under TMP — but verify, don't assume
    harness_error(f"mutant relay escaped the sandbox: {mut.RELAY}")

before = mut.RELAY.read_text().splitlines() if mut.RELAY.exists() else []
mut.say("openrappter", "brainstem", "chat", {"text": "unsigned, quietly"})
after = mut.RELAY.read_text(encoding="utf-8").splitlines() \
    if mut.RELAY.exists() else []
grew_one = len(after) == len(before) + 1
missing = []
if grew_one:
    line = json.loads(after[-1])
    missing = [k for k in ("from", "pub", "alg", "ts", "sig") if k not in line]
case("S4 MUTATION: pre-fix say() writes the bare envelope, and we see it",
     grew_one and len(missing) == 5,
     f"relay grew {len(after) - len(before)} line(s); wrapper fields absent: "
     f"{len(missing)}/5 ({', '.join(missing) if missing else 'n/a'})")

# ── S5: the §16 confession still names BOTH gaps ────────────────────────────
doc = nb.__doc__ or ""
wants = ["does NOT implement the §6 on-relay wrapper",
         "implements NO §5 transport"]
gone = [w for w in wants if w not in doc]
case("S5 docstring still confesses both §16 gaps",
     not gone,
     "both ⛔ lines present" if not gone else f"missing: {gone}")

# ── verdict ──────────────────────────────────────────────────────────────────
bad = 0
for good, name, observed in CASES:
    bad += not good
    print(f"  [{'PASS' if good else 'FAIL'}] {name}\n        {observed}")
print(f"\n{len(CASES) - bad}/{len(CASES)} scenarios behaved as specified")

cleanup()
sys.exit(1 if bad else 0)
