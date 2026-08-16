"""Reproduction for #1 (ask 3) — first sight of a peer is not evidence it moved.

`advancing` was computed as `not same`, with
`same = prev.get("heads") == info["heads"] and prev.get("heads") is not None`.
With no prior observation, `prev.get("heads") is not None` is False, so
same=False and advancing=True. A twin born stalled — serving the same dead
head forever — read healthy at the exact moment someone first looked at it,
which is right after setup, which is when everyone looks.

The fix is that the first observation says what it knows: `advancing` is None
with `advancing_basis: "first-sight"`. But None alone would swap one lie for
another — sentinel.py classified stalled peers with `not v.get("advancing")`,
and `not None` is True, so every peer would read STALLED on first sight
instead. Hence stalled_peers(): valid AND `advancing is False`, an identity
check, never truthiness. Both halves are proved here, because shipping the
first without the second is the same bug wearing the other sign.

The control leg is the molt: the live organism already has a peers-seen.json
written by OLD code ({"slug": {"heads": {...}, "at": "..."}}). Those records
carry "heads", so the first tick after the upgrade must judge them — False or
True, never None. An upgrade that reset every known peer to "first sight"
would blind the watch for exactly one tick, and one tick is all a stall needs
to be missed.

The fixture is fed through the REAL fetch_peer (urlopen stubbed, not
fetch_peer itself), so the head doc must genuinely pass head_violations() —
a fixture that bypasses validation cannot prove `valid` peers are the ones
being classified.
"""
import io
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

import neighborhood as NB
import rapp

R = dict(urlopen=urllib.request.urlopen, nbhd=NB.NBHD, peers=NB.PEERS)


def restore():
    urllib.request.urlopen = R["urlopen"]
    NB.NBHD, NB.PEERS = R["nbhd"], R["peers"]


# ── a head doc the real validator accepts ───────────────────────────────────
SLUGS = ("openrappter", "brainstem", "copilot")
RIDS = {s: rapp.mint_rappid("prover", f"watcher-{s}") for s in SLUGS}


def frame_hash(slug, epoch):
    """Deterministic 64-hex per (slug, epoch) — epoch bump = the head moved."""
    return rapp.H("rapp/1:particle", {"prove": "first-sight", "slug": slug,
                                      "epoch": epoch})


def head_doc(epochs):
    """A rapp-sentinel-head/1.0 doc shaped like publish_head() writes it."""
    utc = NB.utc_now()
    return {
        "schema": "rapp-sentinel-head/1.0",
        "neighborhood": "peer-under-test",
        "name": "Peer Under Test",
        "utc": utc,
        "heads": {s: {"rappid": RIDS[s], "seq": 7 + epochs.get(s, 0),
                      "frame_hash": frame_hash(s, epochs.get(s, 0)),
                      "utc": utc}
                  for s in SLUGS},
    }


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def serve(doc):
    urllib.request.urlopen = lambda req, timeout=None: _Resp(
        json.dumps(doc).encode())


def fresh_paths(seen=None):
    """Point NBHD / PEERS / peers-seen at a throwaway dir, one peer listed."""
    d = Path(tempfile.mkdtemp(prefix="prove-first-sight-"))
    NB.NBHD = d
    NB.PEERS = d / "peers.json"
    NB.PEERS.write_text(json.dumps(
        {"peer-a": "https://example.invalid/sentinel-head.json"}) + "\n",
        encoding="utf-8")
    if seen is not None:
        (d / "peers-seen.json").write_text(
            json.dumps(seen, indent=2) + "\n", encoding="utf-8")
    return d


def seen_heads(epochs):
    """What fetch_peer records per peer: frame_hash truncated to 12 hex."""
    return {s: frame_hash(s, epochs.get(s, 0))[:12] for s in SLUGS}


CASES = []


def case(name, got, want):
    CASES.append((got == want, name, want, got))


# ── the fixture must be a live instrument before anything is measured ───────
_doc = head_doc({})
case("fixture: head doc passes the real head_violations()",
     NB.head_violations(_doc), [])

try:
    # ── the growing-up path: three ticks of one fresh peer ──────────────────
    fresh_paths()

    serve(head_doc({}))
    t1 = NB.peer_roll_call()["peer-a"]
    case("tick 1 fetches valid", t1.get("valid"), True)
    case("THE BUG: tick 1 advancing is None, not True", t1.get("advancing"), None)
    case("tick 1 basis is first-sight", t1.get("advancing_basis"), "first-sight")
    case("tick 1 peer is NOT classified stalled (None is not False)",
         NB.stalled_peers({"peer-a": t1}), [])

    serve(head_doc({}))                       # identical heads, fresh utc
    t2 = NB.peer_roll_call()["peer-a"]
    case("tick 2 same heads: advancing is False", t2.get("advancing"), False)
    case("tick 2 basis is compared", t2.get("advancing_basis"), "compared")
    case("tick 2 peer IS classified stalled", NB.stalled_peers({"peer-a": t2}),
         ["peer-a"])

    serve(head_doc({"brainstem": 1}))         # one frame_hash moved
    t3 = NB.peer_roll_call()["peer-a"]
    case("CONTROL tick 3 moved heads: advancing is True", t3.get("advancing"), True)
    case("CONTROL tick 3 peer is not stalled", NB.stalled_peers({"peer-a": t3}), [])

    # ── the molt: seen-file written by OLD code, first tick after upgrade ───
    fresh_paths(seen={"peer-a": {"heads": seen_heads({}),
                                 "at": "2026-08-15T00:00:00.000Z"}})
    serve(head_doc({}))
    m1 = NB.peer_roll_call()["peer-a"]
    case("MOLT known peer, unmoved heads: False — never None",
         m1.get("advancing"), False)
    case("MOLT unmoved known peer is stalled on the very first new-code tick",
         NB.stalled_peers({"peer-a": m1}), ["peer-a"])

    fresh_paths(seen={"peer-a": {"heads": seen_heads({"copilot": 3}),
                                 "at": "2026-08-15T00:00:00.000Z"}})
    serve(head_doc({}))
    m2 = NB.peer_roll_call()["peer-a"]
    case("MOLT known peer, moved heads: True — never None",
         m2.get("advancing"), True)
    case("MOLT basis is compared, not first-sight",
         m2.get("advancing_basis"), "compared")
finally:
    restore()

bad = 0
for good, name, want, got in CASES:
    bad += not good
    print(f"  [{'ok' if good else 'XX'}] {name}\n"
          f"        expected {want!r}  got {got!r}")
print(f"\n{len(CASES)-bad}/{len(CASES)} scenarios behaved as specified")
sys.exit(1 if bad else 0)
