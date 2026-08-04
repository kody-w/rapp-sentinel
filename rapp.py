"""rapp.py — reference implementation of the RAPP protocol suite (rev-5).

Stdlib only (json, hashlib, uuid, re, base64). Implements the primitives that the
spec claims are byte-for-byte interoperable, so the conformance suite can PROVE the
standard is implementable and self-consistent — and so it can be run against real
estate artifacts to see where reality conforms and where reality is the drift RAPP fixes.

Scope note: §4 canonicalization here is JCS restricted to the string/int/bool/null/
array/object domain (no floats) — exactly the profile RAPP §4 allows for payloads.
Full IEEE-754 number serialization (RFC 8785) is the production requirement; the
reference vectors use exact-integer payloads so the hashes are reproducible anywhere.
"""
import hashlib
import json
import re
import uuid
import io
import zipfile

SPEC = "rapp/1"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_LCLABEL = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_RAPPID = re.compile(r"^rappid:@([a-z0-9]+(?:-[a-z0-9]+)*)/([a-z0-9]+(?:-[a-z0-9]+)*):([0-9a-f]{64})$")

FRAME_KEYS = {"spec", "kind", "stream_id", "seq", "utc", "payload",
              "payload_hash", "frame_hash", "prev", "prev_wave", "sig"}


# ---------- §4 canonicalization ----------
def canonical(v):
    """RFC 8785 JCS over the exact-value domain (no floats). Returns UTF-8 str."""
    if v is None or isinstance(v, bool):
        return json.dumps(v)
    if isinstance(v, int):
        return json.dumps(v)               # exact integers only in this profile
    if isinstance(v, float):
        raise ValueError("floats require full-JCS number serialization; use ints/strings")
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ",".join(canonical(x) for x in v) + "]"
    if isinstance(v, dict):
        keys = sorted(v.keys())
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate keys")
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + canonical(v[k]) for k in keys) + "}"
    raise ValueError(f"non-I-JSON value: {type(v)}")


# ---------- §5 domain-separated content addressing ----------
def H(space, v):
    return hashlib.sha256(space.encode() + b"\x0a" + canonical(v).encode("utf-8")).hexdigest()

def Hb(space, b):
    return hashlib.sha256(space.encode() + b"\x0a" + b).hexdigest()


# ---------- §6 identity ----------
def mint_rappid(owner, slug, spki_der=None):
    """§6.2 mint-once. keyless = Hb(uuid4); keyed = Hb(SPKI). NEVER a name-hash."""
    if spki_der is not None:
        tail = Hb("rapp/1:rappid", spki_der)
    else:
        tail = Hb("rapp/1:rappid", uuid.uuid4().bytes)
    return f"rappid:@{owner}/{slug}:{tail}"

def rappid_valid(s):
    return bool(_RAPPID.match(s))


# ---------- §7 the frame ----------
def build_frame(kind, stream_id, seq, utc, payload, prev, prev_wave=None, sig=None):
    """Construct an 11-key frame, computing particle then wave (§7.3)."""
    payload_hash = H("rapp/1:particle", payload)
    frame = {
        "spec": SPEC, "kind": kind, "stream_id": stream_id, "seq": seq, "utc": utc,
        "payload": payload, "payload_hash": payload_hash,
        "prev": prev, "prev_wave": prev_wave, "sig": sig,
    }
    pre = {k: frame[k] for k in frame if k not in ("frame_hash", "sig")}
    frame["frame_hash"] = H("rapp/1:wave", pre)
    # canonical key set / ordering is by JCS at hash time; store all 11:
    frame = {**frame, "frame_hash": frame["frame_hash"]}
    return frame


def verify_frame(frame, head=None, stream_id_of_record=None):
    """§7.5 consumer checklist. Returns (ok, failing_step_or_None, reason)."""
    # 1 shape & types
    if set(frame.keys()) != FRAME_KEYS:
        return False, "1", f"key set != 11 ({sorted(frame.keys())})"
    if frame["spec"] != SPEC:
        return False, "1", "spec != rapp/1"
    if not (isinstance(frame["kind"], str) and re.match(r"^[a-z0-9]+(-[a-z0-9]+)*\.[a-z0-9]+(-[a-z0-9]+)*$", frame["kind"])):
        return False, "1", "kind grammar"
    if not isinstance(frame["stream_id"], str):
        return False, "1", "stream_id type"
    if not (isinstance(frame["seq"], int) and not isinstance(frame["seq"], bool) and 0 <= frame["seq"] <= 2**53 - 1):
        return False, "1", "seq not uint53"
    if not (isinstance(frame["utc"], str) and _UTC.match(frame["utc"])):
        return False, "1", "utc not fixed form"
    if not isinstance(frame["payload"], dict):
        return False, "1", "payload not object"
    for k in ("payload_hash", "frame_hash"):
        if not (isinstance(frame[k], str) and _HEX64.match(frame[k])):
            return False, "1", f"{k} not 64hex"
    for k in ("prev", "prev_wave"):
        if not (frame[k] is None or (isinstance(frame[k], str) and _HEX64.match(frame[k]))):
            return False, "1", f"{k} not null|64hex"
    # 1a stream binding
    if stream_id_of_record is not None and frame["stream_id"] != stream_id_of_record:
        return False, "1a", "stream_id mismatch (cross-stream replay)"
    # 2 particle
    if frame["payload_hash"] != H("rapp/1:particle", frame["payload"]):
        return False, "2", "payload_hash mismatch"
    # 3 wave
    pre = {k: frame[k] for k in frame if k not in ("frame_hash", "sig")}
    if frame["frame_hash"] != H("rapp/1:wave", pre):
        return False, "3", "frame_hash mismatch"
    # 4 chain
    if head is None:
        if not (frame["seq"] == 0 and frame["prev"] is None):
            return False, "4", "genesis must be seq=0 prev=null"
    else:
        if frame["seq"] != head["seq"] + 1:
            return False, "4", "seq not contiguous"
        if frame["prev"] != head["payload_hash"]:
            return False, "4", "prev != head payload_hash"
        if frame["utc"] < head["utc"]:
            return False, "4", "utc < head utc"
    # 5 wire
    is_swarm = frame["stream_id"].startswith("net:")
    if is_swarm and frame["seq"] > 0:
        if head is not None and frame["prev_wave"] != head["frame_hash"]:
            return False, "5", "prev_wave != head frame_hash"
    else:
        if frame["prev_wave"] is not None:
            return False, "5", "prev_wave must be null off swarm"
    # 6 signature: (crypto-dependent; verified elsewhere) — refuse unsigned swarm
    if is_swarm and frame["sig"] is None:
        return False, "6", "swarm frame must be signed"
    return True, None, "ok"


# ---------- §9 the egg (L5) — the one egg spec of record ----------
EGG_VARIANTS = {"organism", "rapplication", "session", "invite", "neighborhood", "estate"}
_EGG_JSON_VARIANTS = {"session", "invite"}          # JSON object eggs (no packed files)
_EGG_MANIFEST_KEYS = {"schema", "variant", "rappid", "created_utc", "contents", "payload", "sig"}


def egg_address(manifest):
    """§9.1 the egg's one §5 address: H('rapp/1:egg-manifest', manifest \\ {sig})."""
    return H("rapp/1:egg-manifest", {k: v for k, v in manifest.items() if k != "sig"})


def _egg_contents(files):
    """§9.1 contents: {path: Hb('rapp/1:egg', octets)}, sorted ascending by UTF-8 bytes of path."""
    items = [{"path": p, "hash": Hb("rapp/1:egg", octets)} for p, octets in files.items()]
    items.sort(key=lambda c: c["path"].encode("utf-8"))
    return items


def pack_egg(variant, rappid, created_utc, files=None, payload=None, sig=None):
    """Build a byte-reproducible §9 `rapp/1-egg`. Returns bytes.

    files: {relative_posix_path: octets} for ZIP (tree) variants; MUST be empty for
    JSON variants (session/invite). Two conformant packers of the same manifest value
    emit byte-identical eggs (ZIP stored, manifest.json first, timestamps 1980-01-01)."""
    if variant not in EGG_VARIANTS:
        raise ValueError(f"unknown variant: {variant}")
    files = dict(files or {})
    payload = {} if payload is None else payload
    is_json = variant in _EGG_JSON_VARIANTS
    if is_json and files:
        raise ValueError(f"{variant} is a JSON variant — no packed files")
    manifest = {
        "schema": "rapp/1-egg", "variant": variant, "rappid": rappid,
        "created_utc": created_utc,
        "contents": [] if is_json else _egg_contents(files),
        "payload": payload, "sig": sig,
    }
    man_octets = canonical(manifest).encode("utf-8")
    if is_json:
        return man_octets                                  # JSON egg serialized == canonical(manifest)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        def _w(name, data):
            zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_STORED
            zi.flag_bits |= 0x800                          # UTF-8 filename flag
            z.writestr(zi, data)
        _w("manifest.json", man_octets)                    # manifest.json first
        for c in manifest["contents"]:                     # then contents order
            _w(c["path"], files[c["path"]])
    return buf.getvalue()


def read_egg(blob):
    """Parse a rapp/1-egg → (manifest_dict, files_dict). files={} for JSON variants."""
    if blob[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(blob))
        manifest = json.loads(z.read("manifest.json"))
        files = {n: z.read(n) for n in z.namelist() if n != "manifest.json"}
        return manifest, files
    return json.loads(blob), {}


def _egg_variant_ok(v, m, files):
    p = m["payload"]
    if v == "organism":
        if not {"rappid.json", "soul.md"} <= set(files):
            return "organism contents MUST include rappid.json + soul.md"
    elif v == "rapplication":
        if "rappid.json" not in files:
            return "rapplication MUST include rappid.json"
        root_py = [n for n in files if "/" not in n and n.endswith(".py")]
        if len(root_py) != 1:
            return "rapplication MUST have exactly one root agent.py"
    elif v == "session":
        if set(p.keys()) != {"runtime", "transcript"}:
            return "session payload MUST be {runtime, transcript}"
    elif v == "invite":
        if set(p.keys()) != {"target_rappid", "target_url", "target_kind"}:
            return "invite payload MUST be {target_rappid, target_url, target_kind}"
        if m["sig"] is None:
            return "invite sig is REQUIRED"
    elif v == "neighborhood":
        if set(p.keys()) != {"members"}:
            return "neighborhood payload MUST be {members}"
    elif v == "estate":
        if set(p.keys()) != {"neighborhoods"}:
            return "estate payload MUST be {neighborhoods}"
    return None


def verify_egg(blob):
    """§9.3 consumer verify — integrity then viability. Returns (ok, failing_step, reason)."""
    try:
        manifest, files = read_egg(blob)
    except Exception as e:
        return (False, "parse", str(e))
    if not isinstance(manifest, dict) or set(manifest.keys()) != _EGG_MANIFEST_KEYS:
        return (False, "§9.1", "manifest must have exactly the 7 members")
    if manifest["schema"] != "rapp/1-egg":
        return (False, "§9.1", f"schema != rapp/1-egg ({manifest.get('schema')})")
    v = manifest["variant"]
    if v not in EGG_VARIANTS:
        return (False, "§9.2", f"unknown variant {v}")
    if not rappid_valid(manifest["rappid"]):
        return (False, "§6.1", f"bad rappid {manifest['rappid']}")
    if not (isinstance(manifest["created_utc"], str) and _UTC.match(manifest["created_utc"])):
        return (False, "§7.4", "created_utc not the fixed millisecond form")
    contents = manifest["contents"]
    if not isinstance(contents, list):
        return (False, "§9.1", "contents not a list")
    paths = [c["path"] for c in contents]
    for p in paths:
        if p.startswith("/") or "\\" in p or any(seg in ("", ".", "..") for seg in p.split("/")):
            return (False, "§9.1", f"bad path grammar: {p}")
    if paths != sorted(paths, key=lambda x: x.encode("utf-8")):
        return (False, "§9.1", "contents not sorted by path bytes")
    if len(paths) != len(set(paths)):
        return (False, "§9.1", "duplicate path")
    if v in _EGG_JSON_VARIANTS:
        if contents != []:
            return (False, "§9.1", "JSON variant contents MUST be []")
        if blob != canonical(manifest).encode("utf-8"):
            return (False, "§9.1", "JSON egg serialized form != canonical(manifest)")
    else:
        if set(files.keys()) != set(paths):                # zip-slip defense
            return (False, "§9.1", "archive entry set != contents")
        for c in contents:
            if Hb("rapp/1:egg", files[c["path"]]) != c["hash"]:
                return (False, "§5", f"content hash mismatch: {c['path']}")
    why = _egg_variant_ok(v, manifest, files)
    if why:
        return (False, "§9.2", why)
    return (True, None, "ok")
