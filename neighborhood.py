#!/usr/bin/env python3
"""neighborhood.py — the local twin neighborhood the sentinel lives in.

Three watchers are supposed to keep this estate honest: **openrappter**, the
**brainstem**, and **copilot**. The failure this whole ecosystem keeps getting
bitten by is a watcher that dies quietly while everything downstream keeps
reporting green off data that stopped moving. A watchdog nobody watches is
just another process that can stall in silence.

So the three are modelled as what they actually are under
`rapp-neighborhood-protocol/1.0`: **uniform peers** (§3) in a **kited
neighborhood**, each a **Neighbor** with its own **rappid**, speaking the one
**twin-chat** envelope (§6, `rapp-twin-chat/1.0`) over a local file relay.

And crucially, each neighbor's heartbeat is a **rapp/1 frame** (§7): an
11-key, content-addressed, hash-chained record. That is the whole point of
using the protocol here rather than writing more JSON — a chain cannot be
quietly rewritten to look healthier than it was. `roll_call()` re-verifies
every chain from genesis on every tick, so a stalled *or* tampered watcher is
detectable by the other two.

CONFORMANCE — stated honestly, per §16:
  ✅ speaks the §6 twin-chat envelope
  ✅ supports a §5 transport (local file relay — §18 names local file as a
     valid relay form; this neighborhood is on-device by design)
  ✅ uses the §1 vocabulary (Neighbor, twin-chat, kited neighborhood)
  ⛔ does NOT implement the §8 sealed codec (`rapp-sealed/1.0`), therefore
     this does NOT claim full §16 conformance. It rejects `console` kind
     outright rather than pretending to seal it. Everything stays on-device
     and never crosses a network, which is why that is an acceptable trade
     here — but it is a gap, and it is written down rather than glossed.

Frames follow `rapp/1` exactly, via the vendored reference implementation
(rapp.py, copied from kody-w/rapp-1 — stdlib only).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import rapp

HOME = Path(__file__).resolve().parent
NBHD = HOME / "neighborhood"
NBHD.mkdir(exist_ok=True)
IDENTITY = NBHD / "neighbors.json"
RELAY = NBHD / "relay.jsonl"

OWNER = "kody-w"

# The neighborhood's purpose, in its own words. Membership is whoever joins
# (§1 Kited Neighborhood), but this one exists for exactly one job.
NEIGHBORHOOD = {
    "schema": "rapp-neighborhood-protocol/1.0",
    "name": "Rappterbook & Rappterverse Neighborhood Watch",
    "slug": "rappter-neighborhood-watch",
    "purpose": "Keep the rappterbook and rappterverse platforms alive and healthy, "
               "and keep the three watchers honest about whether they are.",
    "watching": ["kody-w/rappterbook", "kody-w/rappterverse",
                 "kody-w/rappvision-field-notes"],
}

# §3 uniform peers — a person, a brainstem, a vTwin and Copilot are all just
# Neighbors on the wire. These three are the watchers of this estate.
NEIGHBORS = {
    "openrappter": "the local daemon that schedules and supervises",
    "brainstem":   "the local RAPP brainstem that answers turns",
    "copilot":     "the repair arm that actually fixes things",
}


def utc_now():
    """§7 fixed-form UTC — exactly millisecond precision, Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def identities():
    """Mint-once rappids (§6.2). Never a name-hash — minted from uuid4."""
    if IDENTITY.exists():
        return json.loads(IDENTITY.read_text(encoding="utf-8"))
    ids = {slug: rapp.mint_rappid(OWNER, f"watcher-{slug}") for slug in NEIGHBORS}
    IDENTITY.write_text(json.dumps(ids, indent=2) + "\n", encoding="utf-8")
    return ids


def chain_path(slug):
    d = NBHD / slug
    d.mkdir(exist_ok=True)
    return d / "chain.jsonl"


def read_chain(slug):
    p = chain_path(slug)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def emit(slug, kind, payload):
    """Append one rapp/1 frame to this neighbor's chain.

    `kind` must match the §7 grammar: two lowercase-hyphen segments joined by
    a dot, e.g. "sentinel.tick".
    """
    ids = identities()
    stream_id = ids[slug]                      # the rappid IS the stream of record
    chain = read_chain(slug)
    head = chain[-1] if chain else None
    frame = rapp.build_frame(
        kind=kind,
        stream_id=stream_id,
        seq=(head["seq"] + 1) if head else 0,
        utc=utc_now(),
        payload=payload,
        prev=head["payload_hash"] if head else None,
        prev_wave=None,                        # §7.5 step 5: null off swarm
    )
    ok, step, why = rapp.verify_frame(frame, head=head, stream_id_of_record=stream_id)
    if not ok:
        raise ValueError(f"refusing to append an invalid frame (step {step}): {why}")
    with open(chain_path(slug), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(frame, ensure_ascii=False) + "\n")
    return frame


def verify(slug):
    """Re-verify a neighbor's whole chain from genesis. Returns (ok, detail)."""
    ids = identities()
    chain = read_chain(slug)
    if not chain:
        return True, "empty chain"
    head = None
    for i, frame in enumerate(chain):
        ok, step, why = rapp.verify_frame(frame, head=head, stream_id_of_record=ids[slug])
        if not ok:
            return False, f"frame {i} failed §7.5 step {step}: {why}"
        head = frame
    return True, f"{len(chain)} frames verified from genesis"


def say(from_slug, to_slug, kind, payload):
    """Put a §6a twin-chat envelope on the local relay.

    `console` is refused: §6b makes it sealed-only and this neighborhood has
    no §8 codec, so accepting it would be a false claim of security.
    """
    if kind == "console":
        raise ValueError("console is sealed-only (§6b/§8); this neighborhood has no sealed codec")
    ids = identities()
    env = {
        "schema": "rapp-twin-chat/1.0",
        "from_rappid": ids[from_slug],
        "to_rappid": ids[to_slug],
        "utc": utc_now(),
        "nonce": rapp.H("rapp/1:particle", {"f": from_slug, "t": to_slug, "u": utc_now()})[:32],
        "kind": kind,
        "payload": payload,
        "facets": [],
    }
    with open(RELAY, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(env, ensure_ascii=False) + "\n")
    return env


def roll_call(stale_minutes=90):
    """The mutual-honesty check: is every watcher alive AND is its record intact?

    Returns a dict per neighbor with chain integrity and heartbeat age. This is
    what makes the trio self-policing — any one of them can run this and see
    the other two.
    """
    out = {}
    now = datetime.now(timezone.utc)
    for slug in NEIGHBORS:
        chain = read_chain(slug)
        ok, detail = verify(slug)
        age_m = None
        if chain:
            last = datetime.strptime(chain[-1]["utc"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=timezone.utc)
            age_m = (now - last).total_seconds() / 60
        out[slug] = {
            "frames": len(chain),
            "chain_ok": ok,
            "chain_detail": detail,
            "age_minutes": None if age_m is None else round(age_m, 1),
            "alive": age_m is not None and age_m < stale_minutes,
            "role": NEIGHBORS[slug],
        }
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "roll-call":
        print(json.dumps(roll_call(), indent=2))
    else:
        print(json.dumps({"neighbors": identities(),
                          "roll_call": roll_call()}, indent=2))
