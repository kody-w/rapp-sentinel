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
  ✅ builds the §6a twin-chat envelope (the payload layer)
  ✅ uses the §1 vocabulary (Neighbor, twin-chat, kited neighborhood)
  ⛔ implements NO §5 transport. §5 enumerates a closed set — 5a-http,
     5a-mcp, 5a-tether, 5a-kite, 5a-cloud, 5b-issues — and a local file is
     none of them. What §18 names is a *relay* ("an on-device file (local —
     the default)"), which is where the signed log lives, not a transport.
     This neighborhood is on-device by design and dials nobody.
  ⛔ does NOT implement the §8 sealed codec (`rapp-sealed/1.0`), therefore
     this does NOT claim full §16 conformance. It rejects `console` kind
     outright rather than pretending to seal it. Everything stays on-device
     and never crosses a network, which is why that is an acceptable trade
     here — but it is a gap, and it is written down rather than glossed.
  ⛔ does NOT implement the §6 on-relay wrapper. §6 requires that on a relay
     — and it names "local file" first — the envelope ride inside a signed
     `rapp-commons-event/1.0` event carrying {from, pub, alg:"ecdsa-p256",
     ts, kind, body, sig}. That needs ECDSA-P256; rapp.py and this repo are
     stdlib-only, so `say()` REFUSES rather than writing an unsigned relay
     line that would look conformant and not be. Same posture as `console`.

INTEGRITY — the two stores are NOT equally trustworthy, and the difference
matters. Each neighbor's `<slug>/chain.jsonl` is a hash-chained rapp/1 log
re-verified from genesis by verify(), so it cannot be quietly rewritten. The
`relay.jsonl` twin-chat log has no such property while it is unsigned, which
is exactly why say() will not write to it.

NOTE on kind registration: §7.5 step 1 also requires each `kind` be
registered in the §13 registry, and rapp.py checks grammar only. That is not
fixable here — §13's registry (rapp-map/ecosystem-spec.json) declares
"accepted_as_rapp1_registry": false and is an open owner action, so no kind
in the estate is registered. Inventing one locally is explicitly forbidden.

Frames follow `rapp/1` exactly, via the vendored reference implementation
(rapp.py, copied from kody-w/rapp-1 — stdlib only).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import rapp
from paths import HOME

NBHD = HOME / "neighborhood"
NBHD.mkdir(exist_ok=True)
IDENTITY = NBHD / "neighbors.json"
# Reserved for the §6 twin-chat relay. Nothing writes it today: see say() —
# a conformant relay line needs an ecdsa-p256 signature this repo cannot make.
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


def build_envelope(from_slug, to_slug, kind, payload):
    """Build a §6a twin-chat envelope. Does NOT write it anywhere.

    This is the *payload* layer only. Putting it on a relay additionally
    requires the §6 signing wrapper — see say().
    """
    if kind == "console":
        raise ValueError("console is sealed-only (§6b/§8); this neighborhood has no sealed codec")
    ids = identities()
    return {
        "schema": "rapp-twin-chat/1.0",
        "from_rappid": ids[from_slug],
        "to_rappid": ids[to_slug],
        "utc": utc_now(),
        "nonce": rapp.H("rapp/1:particle", {"f": from_slug, "t": to_slug, "u": utc_now()})[:32],
        "kind": kind,
        "payload": payload,
        "facets": [],
    }


def say(from_slug, to_slug, kind, payload):
    """Refused: writing this relay conformantly needs a signature we cannot make.

    §6 ("On-relay form") is explicit that a relay — and it names "local file"
    first, which is exactly what RELAY is — carries the envelope inside a
    signed `rapp-commons-event/1.0` wrapper: {schema, from, pub,
    alg:"ecdsa-p256", ts, kind, body, sig}, sig being an ECDSA-P256 signature
    over the canonicalized event.

    ECDSA-P256 is not in the stdlib, and rapp.py plus this repo are
    deliberately stdlib-only. Appending the bare envelope anyway would produce
    an append-only-looking log that any local process could rewrite
    undetectably — the precise failure this module exists to catch, and a
    weaker guarantee than the hash-chained chain.jsonl logs beside it.

    So this refuses, the same way `console` is refused rather than pretended
    sealed. Use emit() for the record of account; it is chained and verified.
    Build the unsigned envelope explicitly with build_envelope() if you need
    the §6a shape in memory for something that never touches a relay.
    """
    build_envelope(from_slug, to_slug, kind, payload)  # still enforces §6b console
    raise NotImplementedError(
        "refusing to write an unsigned relay record: §6 on-relay form requires a signed "
        "rapp-commons-event/1.0 wrapper (ecdsa-p256), which needs a non-stdlib crypto "
        "dependency this repo does not take. Use emit() for the hash-chained record, or "
        "build_envelope() for an in-memory §6a envelope."
    )


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


def chain_digest(frames):
    """A hash over EVERY frame_hash in order, not just the last one.

    The head alone is not enough, and the openrappter neighbor proved it by
    forging its own memory: `prev` binds the predecessor's *payload_hash*
    (§7.4), so rewriting an interior payload perturbs exactly that frame and
    its immediate successor, then stops. It never propagates to the head. The
    field that would carry it — `prev_wave` — is required to be null off-swarm.

    Reproduced independently: rewrite seq 11's payload, reseal seq 11 and 12,
    and all 67 frames verify clean under §7.5 with the head byte-identical.
    The one frame in that chain that recorded a DECLINE could be flipped to
    "CONTRIBUTED" and every guard stayed green.

    A digest over all frame_hashes has no such blind interior: move any frame
    and the digest moves, so the external witness sees it.
    """
    return rapp.H("rapp/1:wave", {"hashes": [f["frame_hash"] for f in frames]})


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
                                  "chain_digest": chain_digest(ch),
                                  "stream_id": ids[slug]}
    with open(ANCHORS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _update_external_ledger(rec)
    return rec


# The anchor file defeats a chain splice because it lives outside the chain.
# It does NOT defeat a splice of itself, because it lives inside the directory
# it attests. Demonstrated on a copy: drop six frames from a chain, remove the
# matching heads from anchors.jsonl, and roll-call reports "102 frames verified
# from genesis" with truncated=false. Both files moved together, so nothing
# disagreed with anything.
#
# So the high-water mark is also kept OUTSIDE the repository, where a working
# tree edit, a git checkout, or an agent rewriting files in its own checkout
# cannot reach it. This is a ratchet, not a vault: same machine, same user, so
# a determined actor with shell access can still edit both. It closes the
# accident and the in-tree rewrite, which are the cases that actually happen.
# Scoped to THIS install. The path is part of the key because the ledger lives
# outside the repository and would otherwise be shared by every checkout on the
# machine: a fresh clone has no chains, so it would read the live install's
# high-water mark and report every stream as vanished. Caught before shipping,
# by cloning the repo and running the check in it.
#
# Keyed on HOME deliberately, and this is load-bearing for the molt: with
# SENTINEL_HOME unset, HOME is the code directory and str(HOME) is
# byte-identical to what the old per-module derivation produced, so the LIVE
# install's ledger key does not move on upgrade — a moved key would make the
# running organism read an empty ledger and report its own streams vanished.
# With SENTINEL_HOME set, each instance gets its own key, which is exactly
# the isolation the key exists for.
_INSTALL_KEY = rapp.H("rapp/1:install", {"path": str(HOME)})[:16]
EXTERNAL_LEDGER = Path.home() / "Library" / "Application Support" / \
    "rapp-sentinel" / f"anchor-ledger-{_INSTALL_KEY}.json"


def _update_external_ledger(rec):
    """Remember the highest seq ever witnessed per stream. Never decreases."""
    try:
        EXTERNAL_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        led = {}
        if EXTERNAL_LEDGER.exists():
            led = json.loads(EXTERNAL_LEDGER.read_text(encoding="utf-8"))
        for slug, h in (rec.get("heads") or {}).items():
            prev = led.get(slug) or {}
            if h.get("seq", -1) >= prev.get("seq", -1):
                led[slug] = {"seq": h["seq"], "frame_hash": h["frame_hash"],
                             "utc": rec.get("utc")}
        EXTERNAL_LEDGER.write_text(json.dumps(led, indent=2) + "\n", encoding="utf-8")
    except Exception:
        # Never let bookkeeping break a tick. The check below reports a missing
        # ledger on its own terms.
        pass


def check_external_ledger():
    """Do the in-tree anchors still cover every seq the outside ledger saw?"""
    if not EXTERNAL_LEDGER.exists():
        return {"status": "absent", "detail": "no external ledger yet"}
    try:
        led = json.loads(EXTERNAL_LEDGER.read_text(encoding="utf-8"))
    except Exception as e:
        return {"status": "unreadable", "detail": f"{type(e).__name__}: {e}"}
    disputes = []
    for slug, remembered in led.items():
        ch = read_chain(slug)
        cur = ch[-1]["seq"] if ch else None
        if cur is None:
            disputes.append(f"{slug}: chain gone (ledger saw seq {remembered['seq']})")
        elif cur < remembered.get("seq", -1):
            disputes.append(f"{slug}: head {cur} < ledger high-water "
                            f"{remembered['seq']}")
    return {"status": "disputed" if disputes else "ok",
            "detail": "; ".join(disputes) or f"{len(led)} streams at or above high-water",
            "disputes": disputes}


def anchors_for(slug):
    """Every anchor record that covered this stream, oldest first."""
    if not ANCHORS.exists():
        return []
    out = []
    for line in ANCHORS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            h = json.loads(line).get("heads", {}).get(slug)
        except Exception:
            continue
        if h and "seq" in h:
            out.append(h)
    return out


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
        # Re-derive each historical digest over the prefix it covered. A
        # rewritten interior frame changes its own frame_hash, so every digest
        # recorded at or after that seq stops reproducing — even though the
        # chain still verifies and the head never moved.
        revised_at = None
        for a in anchors_for(slug):
            d = a.get("chain_digest")
            if not d or a["seq"] > cur:
                continue
            prefix = ch[:a["seq"] + 1]
            if len(prefix) == a["seq"] + 1 and chain_digest(prefix) != d:
                revised_at = a["seq"] if revised_at is None else min(revised_at, a["seq"])
        out[slug] = {
            "witnessed_seq": high["seq"],
            "current_seq": cur,
            "truncated": cur < high["seq"],
            "witnessed_head_present": high["frame_hash"] in hashes or cur > high["seq"],
            "revised": revised_at is not None,
            "revised_before_seq": revised_at,
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


def head_violations(doc):
    """RAPP §5/§6 violations in a published head, in the order a reader hits them.

    A peer's head is the only thing you can check it on, so it has to be checked.
    An identity that is not a conformant rappid, or a frame_hash that has been
    truncated below the 64 hex §5 requires, is exactly the drift this project
    exists to catch — accepting it because the schema string looked right would
    make a truncating peer indistinguishable from an honest one.
    """
    bad = []
    heads = doc.get("heads")
    if not isinstance(heads, dict):
        return ["'heads' is not an object"]
    for slug, h in heads.items():
        if not isinstance(h, dict):
            bad.append(f"{slug}: head is not an object")
            continue
        rid = h.get("rappid")
        if not isinstance(rid, str) or not rapp.rappid_valid(rid):
            bad.append(f"{slug}: rappid is not a conformant rappid (§6.1): {rid!r}")
        fh = h.get("frame_hash")
        if not isinstance(fh, str) or not rapp._HEX64.match(fh):
            n = len(fh) if isinstance(fh, str) else "n/a"
            bad.append(f"{slug}: frame_hash is not 64 lowercase hex (§5), len={n}")
        if not isinstance(h.get("seq"), int) or isinstance(h.get("seq"), bool):
            bad.append(f"{slug}: seq is not an integer")
    return bad


def fetch_peer(slug, url, timeout=20):
    """Read one outside neighbor's published head.

    Failure details carry the HTTP status when there is one (#1, ask 5): the
    bare word "HTTPError" cannot separate a 404 (misconfigured — fix the URL)
    from a 503 (transient — wait), and the two demand opposite responses.
    `reachable` stays False for HTTP errors on purpose: a peer whose head
    cannot be READ has not been observed, whatever the transport said, so the
    consumer's `gone` classification keeps its meaning. The `http_status`
    field is additive — nothing existing reads it.
    """
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rapp-sentinel"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"slug": slug, "url": url, "reachable": False,
                "detail": f"HTTP {e.code} {e.reason}",
                "http_status": int(e.code)}
    except Exception as e:
        return {"slug": slug, "url": url, "reachable": False,
                "detail": f"{type(e).__name__}: {str(e)[:60]}"}
    if doc.get("schema") != "rapp-sentinel-head/1.0":
        return {"slug": slug, "url": url, "reachable": True, "valid": False,
                "detail": f"unexpected schema {doc.get('schema')!r}"}
    bad = head_violations(doc)
    if bad:
        return {"slug": slug, "url": url, "reachable": True, "valid": False,
                "detail": "; ".join(bad[:4])}
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

    `advancing` is a COMPARISON, and a comparison needs two observations. On
    first sight there is no prior head to compare against, so the old
    `same = prev == cur and prev is not None` arithmetic made same=False and
    advancing=True — a twin born stalled read healthy at the exact moment
    someone first looked at it (#1, ask 3). So the first observation now says
    what it actually knows: `advancing` is None with
    `advancing_basis: "first-sight"` — no basis yet, which is not health and
    not stall. Judgement starts on the second fetch, where the basis is
    "compared". Additive only: the `advancing` field keeps its name, True and
    False keep their meanings, and consumers must classify with
    stalled_peers() rather than truthiness — `not None` is truthy, and a bare
    truthiness test would swap "born stalled reads healthy" for "first sight
    reads stalled".
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
            if prev.get("heads") is None:
                # Never seen this peer's heads before — including a live
                # install's peers-seen.json written by old code, whose records
                # always carry "heads", so existing peers are unaffected.
                info["advancing"] = None
                info["advancing_basis"] = "first-sight"
            else:
                info["advancing"] = prev["heads"] != info["heads"]
                info["advancing_basis"] = "compared"
            info["alive"] = (info.get("age_minutes") is not None
                             and info["age_minutes"] < stale_minutes)
            seen[slug] = {"heads": info["heads"], "at": utc_now()}
        out[slug] = info
    seen_path.write_text(json.dumps(seen, indent=2) + "\n", encoding="utf-8")
    return out


def stalled_peers(peer_report):
    """The peers KNOWN to be stalled — advancing is False, not merely un-True.

    This is the classifier consumers must use instead of `not v.get("advancing")`.
    `advancing` is three-valued: True (heads moved), False (heads did not move),
    None (first sight — no prior head to compare, so no verdict either way).
    Truthiness collapses None into False, which would report a peer as stalled
    the moment it is first observed — the mirror image of the bug this fixes.
    Only an identity check on False keeps "we watched it not move" distinct
    from "we have not watched it long enough to say".
    """
    return [k for k, v in peer_report.items()
            if v.get("valid") and v.get("advancing") is False]


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
