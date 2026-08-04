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


ANCHORS = NBHD / "anchors.jsonl"


def anchor_heads():
    """Append every chain's current head to an external, append-only witness.

    A chain cannot detect its own truncation. When payloads repeat, `prev`
    repeats too, so an interior frame can be dropped, the successors resealed,
    and the result verifies clean — the brainstem neighbor found exactly this
    in its own memory, and it was reproducible: drop a frame, recompute, 19
    frames verify.

    The fix is not more hashing inside the chain. It is a witness OUTSIDE it.
    Once a head is recorded here, a later splice has to disagree with something
    it does not control.
    """
    ids = identities()
    rec = {"utc": utc_now(), "heads": {}}
    for slug in NEIGHBORS:
        ch = read_chain(slug)
        if ch:
            rec["heads"][slug] = {"seq": ch[-1]["seq"],
                                  "frame_hash": ch[-1]["frame_hash"],
                                  "stream_id": ids[slug]}
    with open(ANCHORS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def check_anchors():
    """Compare each chain against the oldest anchor that covers it.

    A truncated chain shows up as a head whose seq went BACKWARDS, or a seq we
    once witnessed that the chain can no longer produce.
    """
    if not ANCHORS.exists():
        return {}
    seen = {}
    for line in ANCHORS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        for slug, h in r.get("heads", {}).items():
            prev = seen.get(slug)
            if prev is None or h["seq"] > prev["seq"]:
                seen[slug] = h
    out = {}
    for slug, high in seen.items():
        ch = read_chain(slug)
        cur = ch[-1]["seq"] if ch else -1
        hashes = {f["frame_hash"] for f in ch}
        out[slug] = {
            "witnessed_seq": high["seq"],
            "current_seq": cur,
            "truncated": cur < high["seq"],
            "witnessed_head_present": high["frame_hash"] in hashes or cur > high["seq"],
        }
    return out

# ── outside neighbors ───────────────────────────────────────────────────────
#
# Membership is whoever joins (rapp-neighborhood-protocol/1.0 §1). A watcher on
# someone else's machine cannot write to this chain directory and should not be
# able to — so it joins the way a RAPP channel does: it PUBLISHES a head, and
# everyone else fetches and verifies it.
#
# That keeps the trust model honest. A local neighbor is trusted because you can
# read its whole chain. A remote neighbor is trusted exactly as far as its
# published head can be checked against what it published before — which is the
# same external-anchor argument that fixed truncation locally.

PEERS = NBHD / "peers.json"          # {"slug": "https://.../sentinel-head.json"}
PUBLIC = HOME / "public"             # what THIS neighborhood publishes outward


def peers():
    try:
        return json.loads(PEERS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def publish_head():
    """Write the head this neighborhood exposes to outsiders.

    Only heads and identities — never payloads. A peer needs enough to detect
    that you stalled or truncated, and nothing more. Publishing the whole chain
    would leak whatever your checks happen to name.
    """
    PUBLIC.mkdir(exist_ok=True)
    ids = identities()
    doc = {
        "schema": "rapp-sentinel-head/1.0",
        "neighborhood": NEIGHBORHOOD["slug"],
        "name": NEIGHBORHOOD["name"],
        "utc": utc_now(),
        "heads": {},
    }
    for slug in NEIGHBORS:
        ch = read_chain(slug)
        if ch:
            doc["heads"][slug] = {
                "rappid": ids[slug],
                "seq": ch[-1]["seq"],
                "frame_hash": ch[-1]["frame_hash"],
                "utc": ch[-1]["utc"],
            }
    (PUBLIC / "sentinel-head.json").write_text(
        json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return doc


def fetch_peer(slug, url, timeout=20):
    """Read one outside neighbor's published head."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rapp-sentinel"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.loads(r.read().decode())
    except Exception as e:
        return {"slug": slug, "url": url, "reachable": False,
                "detail": f"{type(e).__name__}"}
    if doc.get("schema") != "rapp-sentinel-head/1.0":
        return {"slug": slug, "url": url, "reachable": True, "valid": False,
                "detail": f"unexpected schema {doc.get('schema')!r}"}
    ages = []
    for h in doc.get("heads", {}).values():
        try:
            t = datetime.strptime(h["utc"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=timezone.utc)
            ages.append((datetime.now(timezone.utc) - t).total_seconds() / 60)
        except Exception:
            pass
    return {"slug": slug, "url": url, "reachable": True, "valid": True,
            "neighborhood": doc.get("neighborhood"),
            "watchers": len(doc.get("heads", {})),
            "age_minutes": round(min(ages), 1) if ages else None,
            "heads": {k: v["frame_hash"][:12] for k, v in doc.get("heads", {}).items()}}


def peer_roll_call(stale_minutes=90):
    """Same question asked of outsiders: are you alive, and is your record moving?

    A peer whose published head has not advanced is stalled, and you can see
    that without trusting anything it says about itself.
    """
    seen_path = NBHD / "peers-seen.json"
    try:
        seen = json.loads(seen_path.read_text(encoding="utf-8"))
    except Exception:
        seen = {}

    out = {}
    for slug, url in peers().items():
        info = fetch_peer(slug, url)
        prev = seen.get(slug, {})
        if info.get("valid"):
            same = prev.get("heads") == info["heads"] and prev.get("heads") is not None
            info["advancing"] = not same
            info["alive"] = (info.get("age_minutes") is not None
                             and info["age_minutes"] < stale_minutes)
            seen[slug] = {"heads": info["heads"], "at": utc_now()}
        out[slug] = info
    seen_path.write_text(json.dumps(seen, indent=2) + "\n", encoding="utf-8")
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "publish":
        print(json.dumps(publish_head(), indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "peers":
        print(json.dumps(peer_roll_call(), indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "anchor":
        print(json.dumps(anchor_heads(), indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "anchors":
        print(json.dumps(check_anchors(), indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "roll-call":
        print(json.dumps(roll_call(), indent=2))
    else:
        print(json.dumps({"neighbors": identities(),
                          "roll_call": roll_call()}, indent=2))
