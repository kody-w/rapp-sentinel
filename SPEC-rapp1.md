# The RAPP Protocol Suite
### Unified normative specification of identity, canonicalization, the frame, the wire, and the egg

**Status:** Draft standard for ratification (Kody, estate owner). **rev-5.** **Obsoletes / consolidates:**
`rapp-frame/2.0`, `rapp-frame/2.1`, `rapp-rappid-spec/2.0`, `rapp-protocol/1.0`, all scattered egg specs
(§9 subsumes them), and `OSI.md`. On ratification this is the single living standard; the consolidated
specs become retired historical record (Federal Constitution Art. X).

**Rides existing standards; invents nothing:** requirement terms [RFC 2119]/[RFC 8174]; JSON restricted to
I-JSON [RFC 7493] over [RFC 8259]; canonicalization [RFC 8785] (JCS); hashing SHA-256 [FIPS 180-4] with
git-style domain separation; identifiers on the [RFC 3986] URI model; case-sensitive grammar [RFC 7405]
over [RFC 5234] ABNF; keyless entropy UUIDv4 [RFC 9562]; keyed identity X.509 SPKI [RFC 5280]; signatures
detached unencoded JWS [RFC 7515]/[RFC 7797], EdDSA [RFC 8037] / ES256 [RFC 7518]/[RFC 6979]. RAPP is a
*profile* over these, as HTTP profiles TCP/URIs/MIME.

---

## 1. Introduction
RAPP is a content-addressed distributed organism. Its integrity rests on one invariant: **the same
concept has the same bytes everywhere.** This document specifies, normatively and completely, five
load-bearing primitives so any two independent implementations interoperate **byte-for-byte with no
out-of-band agreement**: canonicalization (§4), content addressing (§5), identity (§6), the frame (§7),
the egg (§9) — all riding one wire (§8): `POST /chat`, or a signed append-only frame. Implementations add
agents, cartridges, and registered `kind`s — never new endpoints, never new envelopes.

### 1.1 The layered model
```
  L5  EGG        cartridge packaging (§9)          — MIME-multipart analogue
  L4  FRAME      universal event envelope (§7)     — the IP packet of RAPP
  L3  WIRE       transport: /chat + frames (§8)    — HTTP-analogue single method
  L2  IDENTITY   rappid namespace + trust (§6,§10) — URI + PKI analogue
  L1  ADDRESS    canonicalization + hash (§4,§5)   — the git object model
```
A higher layer **MUST NOT** redefine a lower one. Every layer names exactly one canonical form.

## 2. Requirements language
The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHALL**, **SHOULD**, **MAY** are as in [RFC 2119]/
[RFC 8174] when in **bold caps**. (The Federal Constitution adopts the same clause.)

## 3. Terminology
**organism** — a running brainstem with persistent identity. **rappid** — the eternal content-addressed
name of an organism/door/object (§6). **frame** — one immutable content-addressed event (§7). **stream**
— an append-only hash-chained frame sequence sharing one `stream_id`. **particle / wave** — a frame's two
domain-separated addresses: payload-hash / whole-frame-hash (§7.3). **canonical form** — the one [RFC 8785]
byte string for a value (§4). **legacy form** — any other historical encoding; legacy is drift and **MUST**
be migrated out (Fed. Const. Art. III), except sealed re-genesis history (§12.1).

## 4. Canonicalization (L1)
`canonical(v)` is the UTF-8 byte string produced by **[RFC 8785] JCS** for the value `v`, defined **only**
over I-JSON [RFC 7493]. JCS fixes member-name ordering (UTF-16 code-unit), string escaping, and number
serialization (ECMAScript `Number::toString`, [ECMA-262]); there is no insignificant whitespace and no
byte-order mark.

**RAPP input-domain profile (parse-side interoperability — this is a RAPP rule, not a JCS mandate).** An
implementation **MUST** refuse (never repair) any JSON value that, at any depth, contains: (a) duplicate
member names in one object; (b) an unpaired UTF-16 surrogate in any string; (c) a number token that does
not survive the binary64 round-trip — let `d` be the token's nearest binary64 value under `roundTiesToEven`
(the IEEE-754 default; ±∞ are admissible results); the token is refused iff `d` is not finite, or the
[RFC 8785] serialization of `d` (ECMA-262 `Number::toString`) denotes a different mathematical value than
the token (so `0.1` is accepted — it round-trips — while `9007199254740993` and `1e999` (→ +∞) are refused); (d) canonical form exceeding 1 MiB or JSON nesting depth
exceeding 64 (the root value is depth 1; each nested object/array adds 1). Refusal is whole (§7.5-style),
never partial.

**No normalization.** RAPP applies **no** Unicode normalization when hashing, storing, or re-emitting an
existing value; strings are code-point sequences preserved verbatim and equality everywhere is code-point
equality (no canonical-equivalence matching). A producer creating a **new** human-or-identifier string
(slug, kind, label, `payload` object key) **MUST** emit it in Unicode NFC.

> **Migration note (drift C4):** the `twin`/`rapp-body` `_frame.mjs::canonicalize()` (sorted-key
> `JSON.stringify`) coincides with JCS only for string-only payloads; it **MUST** be replaced by the JCS
> implementation of record, imported (not re-typed) by every repo that content-addresses.

## 5. Content addressing (L1) — domain-separated
Every hash is **domain-separated** (git's `type\0`, Nix's tagged store — collisions across address spaces
are made unconstructible):
```
H(space, v) = lowercase_hex( SHA-256( utf8(space) || 0x0A || canonical(v) ) )     ; v a value (§4)
Hb(space, b) = lowercase_hex( SHA-256( utf8(space) || 0x0A || b ) )                ; b raw octets
```
`space` is an exact ASCII tag, none containing `0x0A`: `"rapp/1:particle"`, `"rapp/1:wave"`, `"rapp/1:egg"`,
`"rapp/1:egg-manifest"`, `"rapp/1:rappid"`, `"rapp/1:seal"`. A tag is used by either `H` or `Hb`, never
both. Output is always exactly 64 lowercase hex, **never truncated or uppercased**. Two values
are treated as the same object iff their same-space hashes are equal; SHA-256 collision resistance
[FIPS 180-4] is a security assumption of this standard (§14). A `name/X.Y` label is never identity — only
a hash is. A bare 64-hex is meaningful **only** within its space; an implementation **MUST NOT** dereference
a hash from one space as an object of another, and content-addressed stores **MUST** key by `(space, hash)`.

## 6. Identity — the rappid (L2)
### 6.1 Grammar (case-sensitive, [RFC 7405])
```abnf
rappid    = %s"rappid:@" owner "/" slug ":" hash
owner     = lclabel                       ; the lowercase GitHub login (1-39 chars)
slug      = lclabel                        ; 1-100 chars
lclabel   = lcalnum *( ["-"] lcalnum )     ; no leading/trailing/adjacent hyphen
lcalnum   = LCALPHA / DIGIT
LCALPHA   = %x61-7A                         ; a-z
hash      = 64HEXDIGLC
HEXDIGLC  = DIGIT / %x61-66                 ; 0-9 a-f
```
`owner` **MUST** be the lowercase form of the GitHub login (logins are case-insensitive; display casing is
presentation, never identity). Lengths are normative: `owner` 1–39, `slug` 1–100; an implementation
**MUST** refuse longer. This self-locating form is the **only** conformant rappid; `rappid:<slug>:<hash>`,
`rappid:v2:…`, bare UUIDs, `moment:`/name-hash derivations are legacy and **MUST** be migrated out
(Art. III), not read forever.

### 6.1.1 stream_id and kind grammar
```abnf
stream_id   = memory-stream / body-stream / swarm-stream
memory-stream = rappid ":" instance      ; one organism instance's memory
body-stream   = rappid                    ; an organism's biography
swarm-stream  = %s"net:" lclabel          ; a planetary-wire stream
instance      = lclabel                   ; 1-64 chars
kind          = lclabel "." lclabel       ; each label 1-64 chars
```
A `kind` string carries **no intrinsic family**; the §13 registry binds each registered `kind` to exactly
one family. Membership is tested by exact-match against the registry — never prefix inference, never wildcards.

### 6.2 Minting (mint-once)
The 64-hex tail is minted **exactly once** per identity, then immutable:
- **keyless:** `tail = Hb("rapp/1:rappid", uuid4_octets)`, where `uuid4_octets` is the 16-octet binary
  UUIDv4 [RFC 9562] §5.4 (field/byte order per §4 of that RFC).
- **keyed:** `tail = Hb("rapp/1:rappid", SPKI_DER)`, the DER `SubjectPublicKeyInfo` [RFC 5280] of the master key.

A producer **MUST NOT** derive the tail from owner/slug or any name (`sha256("owner/slug")` is prohibited —
drift ID-01/C3). On read of an existing `rappid.json` an implementation **MUST** reuse the stored tail
(canonicalize-on-read) and **MUST NOT** re-mint — with exactly one mechanism: the owner-authorized
**re-anchor** (§6.3), which mints a fresh 64-hex tail once per authorization and records the superseded id
in `_migrated_from`. Re-anchor is lawful in exactly three cases: (a) a 128→256-bit provisional upgrade
(§6.3); (b) §10 key rotation or compromise; (c) migrating a pre-rev-3 keyed tail minted with the un-tagged
`sha256(SPKI)` formula (which fails §10 discovery and **MUST** be re-anchored like a provisional identity).

### 6.3 Canonicalization on read; provisional identifiers
`canonicalize_rappid(s)` restructures any legacy form into §6.1, **preserving the existing hash** (never
inventing one). A restructured identifier whose tail is not exactly 64 lowercase hex (e.g. a legacy 32-hex
tail) is **provisional**: it exists only inside the reading process and **MUST NOT** appear in any emitted
frame, `stream_id`, egg, or registry entry. The one-time owner-authorized 128→256-bit re-anchor mints a
fresh 64-hex, records the old id in `_migrated_from`, and is the only way a provisional identity becomes
usable. A provisional identifier found in a stored artifact is a drift finding (Art. III). Re-anchor is the
single re-mint mechanism (§6.2), lawful only in the enumerated cases: provisional 128→256-bit upgrade,
§10 key rotation/compromise, and pre-rev-3 un-tagged-`sha256(SPKI)` keyed-tail migration.

**A re-anchor is valid only with a verifiable authorization** — a self-asserted `_migrated_from` is
insufficient (it would let anyone hijack an identity). A re-anchor **MUST** be recorded as an owner-signed
§13.3 **re-anchor record** `{old_rappid, new_rappid, case, utc, sig, old_key_sig?}`; a consumer **MUST**
refuse a `new_rappid` (and treat `_migrated_from` as drift) unless that record is present and:
- `case:"rotation"` (uncompromised): `old_key_sig` (a §10 JWS by the **old** key) verifies — proof of
  continuity;
- `case:"compromise"`: `old_key_sig` is waived but a §10 **tombstone** for `old_rappid` is registered in the
  same append;
- `case:"tag-migrate"` (pre-rev-3 keyed tail): the verifier checks `lowercase_hex(SHA-256(SPKI_DER_old))` ==
  the old tail;
- `case:"upgrade"` (provisional 128→256): the old provisional id resolved to this owner at read time.
Each mints one fresh tail. The **estate_owner's own** re-anchor record **MUST** be signed by the outgoing
`estate_owner` key (§13.2); root-key compromise is recovered only by out-of-band re-anchoring (§13.1).

## 7. The Frame (L4)
### 7.1 The envelope — exactly eleven keys
```json
{
  "spec":         "rapp/1",
  "kind":         "<klabel.klabel>",
  "stream_id":    "<stream_id>",
  "seq":          <uint53>,
  "utc":          "YYYY-MM-DDTHH:MM:SS.mmmZ",
  "payload":      { },
  "payload_hash": "<64hex>",
  "frame_hash":   "<64hex>",
  "prev":         "<64hex|null>",
  "prev_wave":    "<64hex|null>",
  "sig":          "<jws|null>"
}
```
- **`spec` MUST be the exact string `"rapp/1"`** in every frame. `rapp-frame/2.0`/`2.1` are legacy tokens
  and **MUST NOT** be emitted. Any revision changing the key set, any field's grammar, or either hash rule
  **MUST** change this token and land an Art. III total migration; revisions adding only new registered
  `kind`s/registry entries keep the token (Fed. Const. Art. II).
- **Exactly these eleven keys, always present**, none missing, none extra. A field that does not apply is
  present with value `null` (`prev`/`prev_wave` at genesis and on non-swarm streams; `sig` when unsigned) — never omitted,
  because [RFC 8785] hashes `null` and an absent key differently. Extra or missing keys are refused (§7.5).
- **`payload` MUST be a JSON object** (possibly empty `{}`); never `null`, array, string, number, or bool.
- `seq` is `uint53` (§7.4). A producer **MUST NOT** emit a frame whose canonical form exceeds 1 MiB or
  nesting depth 64 (§4).

### 7.2 Kind families (one envelope, registry-bound families)
| family | example registered kinds | `stream_id` form | logs |
|---|---|---|---|
| `memory` | `memory.chat-turn`, `memory.tool-call`, `memory.save`, `memory.reconstructed` | memory-stream | one organism's life |
| `swarm`  | `swarm.guidance`, `swarm.echo`, `swarm.telemetry`, `swarm.reconstructed` | swarm-stream | the planetary wire |
| `body`   | `body.pulse`, `body.twin-pulse`, `body.reconstructed`, `body.re-genesis` | body-stream | an organism's biography |
Each family also has a `*.re-genesis` kind (`memory.re-genesis`, `swarm.re-genesis`, `body.re-genesis`)
used only by §12.1. The family is **not** the kind's prefix — it is the §13 registry binding (so
`body.twin-pulse` is family `body`). Adding a family or event is a new registered `kind` on the **same** envelope (Art. IV), never a
new frame type. A frame's `kind` family **MUST** be compatible with its `stream_id` form (table column 3).

### 7.3 Particle and wave (the unification)
A frame carries **both** of its domain-separated addresses; a reader collapses it to whichever it needs.
Computed in order:
- **particle** — `payload_hash = H("rapp/1:particle", payload)`. The **worldline identity and chain link**.
- **wave** — `frame_hash = H("rapp/1:wave", frame \ {frame_hash, sig})` — the frame with **exactly** the
  `frame_hash` and `sig` keys removed, all nine remaining keys (including `payload_hash`) present.
Because `payload_hash` is in the wave pre-image, `frame_hash` attests the particle; because only
`frame_hash` (cannot hash itself) and `sig` (signs the result) are removed, the pre-image is unambiguous
and non-circular. Both hashes are always present (never `null`).

### 7.4 Chaining, time, and merge order
- **`utc`** **MUST** be exactly the 24-byte form `YYYY-MM-DDTHH:MM:SS.mmmZ` — uppercase `T`/`Z`, exactly
  three fractional digits, no numeric offset; the seconds field **MUST NOT** be `60` (a leap second clamps
  to `59.999`). All `utc` comparisons are **bytewise** over this fixed form (identical to chronological order).
- **Worldline chain (particle):** the **genesis** frame has `seq`=0 and `prev`=null; every later frame has
  `seq` = predecessor's `seq`+1 (contiguous) and `prev` = predecessor's `payload_hash`. `seq` is `uint53`
  (JSON integer, 0 ≤ seq ≤ 2^53−1, no fraction/exponent; a stream nearing 2^53−1 converges by re-genesis).
- **Wire chain (wave):** `prev_wave` **MUST** be non-null **iff** `stream_id` is a swarm-stream **and**
  `seq` > 0, in which case it equals the predecessor's `frame_hash`; in every other frame (all memory/body
  streams, every genesis) it **MUST** be `null`. (Presence is a function of stream family, not transport.)
- A frame is **immutable**: a new state is a new frame at a new hash; the head pointer (§7.6) re-points.
- **Cross-stream merge order** (Dream-Catcher) is the total order: ascending `utc` (bytewise), ties broken
  by ascending `frame_hash` (bytewise); no further ties are possible (§5).

### 7.5 Verification (the complete consumer checklist)
Before accepting a frame, a consumer **MUST**, in order, **refuse** (never repair/reparent) on any failure:
1. **Shape & types:** exactly the eleven §7.1 keys; `spec`==`"rapp/1"`; `kind` a string matching §6.1.1
   ABNF and registered (§13); `stream_id` a string matching §6.1.1; `seq` a `uint53`; `utc` matching the
   §7.4 fixed form **and** a calendar-valid [RFC 3339] `date-time` (so `2026-13-45T25:61:61.999Z` is
   refused); `payload` a JSON object; `payload_hash`/`frame_hash` `64HEXDIGLC`; `prev`/`prev_wave` each
   `null` or `64HEXDIGLC`; `sig` `null` or a §10 JWS string.
1a. **Stream binding:** `frame.stream_id` **MUST** byte-equal the identifier of the stream being extended
   or read (its declared `stream_id` at the head pointer / repo path of record). A genesis frame is
   accepted only for the stream it names. (Defeats cross-stream genesis/segment replay — §14.)
2. **Particle:** `payload_hash` == `H("rapp/1:particle", payload)`.
3. **Wave:** `frame_hash` == `H("rapp/1:wave", frame \ {frame_hash, sig})`.
4. **Chain:** `seq`==head.`seq`+1 and `prev`==head.`payload_hash` (or `seq`==0 ∧ `prev`==null at genesis);
   `utc` ≥ head.`utc` (bytewise).
5. **Wire:** if `stream_id` is a swarm-stream and `seq`>0, `prev_wave`==head.`frame_hash`; else
   `prev_wave`==null.
6. **Signature:** if `sig`≠null, verify per §10 (present-but-invalid is refusal); a swarm-stream frame with
   `sig`==null is refused (§8/§10).
Steps 1–5 are **time-independent** (kind lookups only append, §13), so a frame passing them passes forever;
step 6 alone may flip pass→fail when a §10 tombstone with `revoked_utc` ≤ the frame's `utc` is later
registered — the one time-dependent check.

### 7.6 Heads and forks
A stream's **head** is the frame with the greatest verified `seq` descending from its registered genesis
(§13). A consumer **MUST** persist, per stream, the highest `(seq, frame_hash)` it verified and **MUST**
refuse any presented head with lower `seq`, or a different `frame_hash` at an already-verified `seq` (no
rollback, no silent reorg). Two distinct valid frames with equal `stream_id`, `seq`, and `prev` are a
**fork**: a consumer **MUST** refuse both branches past the fork point and surface a drift finding; only
the owner resolves a fork (Art. X), by re-genesis (§12.1) if needed.

**Exception (re-genesis head reset).** The no-rollback rule above refuses a lower-`seq` head — but a
legitimate §12.1 re-genesis presents a new genesis at `seq`=0. When (and only when) the §13 registry maps a
`stream_id` to a new genesis `frame_hash` (§12.1 step 3), a consumer **MUST** verify that registered
genesis (§7.5) and then **reset** its persisted head for that `stream_id` to it. Only a registry-published
genesis authorizes a reset; any other lower-`seq` head remains a refused rollback.

## 8. The Wire (L3)
All interaction rides one of exactly two forms:
1. **Synchronous — `POST /chat`, `application/json` both ways.** Request: `user_input` (string, REQUIRED);
   `session_id` (string, OPTIONAL — omit to start a session); `idempotency_key` (string, OPTIONAL — a repeat
   with the same key returns the original response, not a new turn or duplicate session; scoped to
   `session_id` when present, else to the key alone so session-creation is also de-duplicated); unrecognized members **MUST** be
   ignored, never refused. Success: HTTP 200 with **exactly** `{response:string, agent_logs:[string],
   session_id:string}` (no extra members). An unknown `session_id`, a refusal, or a malformed request
   **MUST** be HTTP 422, `{error:{code:string, step:string|null}}` where `code` is a §13-registered error
   code (e.g. `"unknown-session"`) and `step` is the failing §7.5 step as a string — one of
   `"1","1a","2","3","4","5","6"` — or `null`. No other shape is conformant. New capability is a new agent
   behind `/chat`, never a sibling REST route.
2. **Asynchronous — an append-only frame (§7) published to a stream** (a repo path, an `events/` log). A
   frame on a **swarm-stream MUST** carry `sig`≠null (§10); memory/body-stream frames **MAY** be unsigned.
   Any *history* is safe to read given a trusted head (§14); the hash chain (§5) makes tampering
   detectable.

## 9. The Egg (L5) — the single egg spec of record
An **egg** is a cartridge packing a unit of the estate. **RAPP §9 is the one egg spec of record** (it
subsumes and retires `EGG_FAMILY.md`, `NEIGHBORHOOD_EGG_SPEC.md`, `ESTATE_SPEC.md`, `rappterbook/EGG_SPEC.md`,
and the rest — drift C7). No other document may re-specify eggs; they cite this section.

### 9.1 Container, manifest, and egg address
An egg is either a JSON object (`invite`/`session` variants) or a ZIP whose root is `manifest.json` (tree
variants). The manifest is a §4 value with exactly these members:
```json
{ "schema": "rapp/1-egg", "variant": "<variant>", "rappid": "<§6.1 rappid>",
  "created_utc": "<§7.4 utc>", "contents": [ {"path":"<rel>","hash":"<64hex>"}, … ], "payload": { },
  "sig": "<jws|null>" }
```
- `contents` **MUST** list every packed file **except `manifest.json` itself**, exactly once each, with
  `hash = Hb("rapp/1:egg", file_octets)` (§5) over the raw stored octets. `contents` is **always present**;
  for JSON (pointer/session) variants it **MUST** be exactly `[]`.
- `path` **MUST** be a relative POSIX path: `/`-separated NFC UTF-8 segments, no `.`/`..` segment, no
  leading `/`, no backslash, no duplicate `path` in one manifest. `contents` **MUST** be sorted ascending
  by the UTF-8 bytes of `path`.
- `payload` is a §4 object (variant-specific). `sig` is a §10 JWS over `canonical(manifest \ {sig})`, or
  `null`. For the `invite` variant `sig` is REQUIRED and **MUST** verify with `kid` in the §13.2 estate-owner
  succession (invites are estate-issued; a `sig` by any other key, even a validly registered one, is
  refused — otherwise an attacker mints a fresh rappid and forges invites). For other variants a non-null
  `sig` **MUST** verify per §10 with `kid` == a keyed rappid the consumer resolves via §13; a consumer
  presented a signed egg **MUST** verify it.
- **The egg's one §5 address is** `egg_hash = H("rapp/1:egg-manifest", manifest \ {sig})` — the manifest with
  exactly the `sig` key removed (mirroring §7.3's wave rule: `sig` authenticates the egg, `egg_hash` names
  it, so re-signing never changes identity). Stores key eggs by `("rapp/1:egg-manifest", egg_hash)`.
  (`"rapp/1:egg"` addresses file octets; `"rapp/1:egg-manifest"` addresses the egg as a whole.)
- **Container determinism.** A ZIP variant **MUST** use compression method `stored` (0) for **every** entry
  — no deflate in any variant (deflate is library-dependent, so it cannot be byte-reproducible; transport
  compression, if any, wraps the egg and is not the egg). Entries appear in `contents` order with
  `manifest.json` first; the `manifest.json` entry's octets **MUST** be exactly `canonical(manifest)`; all
  timestamps `1980-01-01 00:00:00`; no extra fields; UTF-8 filename flag set; `contents[].hash` is over the
  file octets (identical to the archive octets under method 0). A JSON-variant egg's serialized form
  **MUST** be exactly `canonical(manifest)`. Two conformant packers of the **same manifest value** thus emit
  byte-identical eggs.

### 9.2 Variants (the ratified set — closes EGG-01)
| variant | container | packs | required members |
|---|---|---|---|
| `organism` | ZIP | a full brainstem instance | contents (sorted) MUST include `rappid.json`, `soul.md`; MAY include `agents/*`, `organs/*`, memory files |
| `rapplication` | ZIP | one rapp | contents MUST include `rappid.json` and exactly one `agent.py` at the root (the agent of record); MAY include one `ui.html` and files under `state/` |
| `session` | JSON | one runtime + transcript | `payload` = `{runtime:<string>, transcript:[<object>]}`; contents `[]` |
| `invite` | JSON | a QR-sized pointer (**no packed files**) | `payload` = `{target_rappid:<rappid>, target_url:<string>, target_kind:("neighborhood" / "estate")}`; contents `[]`; `sig` REQUIRED |
| `neighborhood` | ZIP | several organisms meant to live together | `payload` = `{members:[<rappid>,…]}`; contents = one sub-egg per member, named `<owner>--<slug>.egg` at the root, matched by the sub-egg manifest's `rappid` == the `payload.members[]` entry |
| `estate` | ZIP | several neighborhoods | `payload` = `{neighborhoods:[<rappid>,…]}`; contents = one sub-egg per neighborhood, named `<owner>--<slug>.egg` at root, matched by sub-egg `rappid` |

The QR-sized invite that caused EGG-01 is the **`invite`** variant: a signed pointer object, not a
member-packing `neighborhood` egg. The banned legacy stamps (`brainstem-egg/2.3-neighborhood`,
`neighborhood-egg/1.0`) migrate to `{schema:"rapp/1-egg", variant:"invite" / "neighborhood"}` (Art. III).

### 9.3 Conformance
- **Producer** **MUST** emit only `schema:"rapp/1-egg"` with a variant from §9.2, a §6.1 rappid, and, for
  ZIP variants, a `contents` list whose every hash verifies. It **MUST NOT** emit any legacy egg schema.
- **Consumer** **MUST** read every §9.2 variant, dispatch on `variant`, verify **integrity then viability** —
  (0) the manifest is a §4 value satisfying **every §9.1 rule** — exact member set, `path` grammar (no `..`,
  no leading `/`, no backslash), no duplicate paths, sort order, and for ZIP variants the archive entry set
  equals `contents` ∪ {`manifest.json`} in the §9.1 deterministic order (this is the zip-slip defense; an
  unenforced path grammar defends nothing); (1) every `contents[].hash` recomputes per §5; (2) the variant's
  §9.2 structural requirements hold — and refuse whole on any failure; it **MUST NOT** reparent on transport.

## 10. Trust and signatures (L2)
`sig` is OPTIONAL on memory/body streams and REQUIRED on swarm streams (§8). Chain integrity comes from the
hash links (§5), not signatures. When present, `sig` **MUST** be JWS Compact Serialization with **detached,
unencoded payload** ([RFC 7515] App. F + [RFC 7797]):
- protected header members are **exactly** `alg` (`"EdDSA"` or `"ES256"`), `b64` (`false`), `crit`
  (`["b64"]`), `kid` (signer's §6.1 rappid), no others; the header octets **MUST** be `canonical(header)`
  (§4 — JCS orders them `alg`, `b64`, `crit`, `kid`, no whitespace);
- the `sig` string is the detached compact form `BASE64URL(canonical(header)) || ".." || BASE64URL(signature)`;
- JWS signing input = `BASE64URL(canonical(header)) || "." || canonical(frame \ {sig})`;
- `alg`: `EdDSA`/Ed25519 [RFC 8037] or `ES256` [RFC 7518]; ES256 signers **SHOULD** sign deterministically
  [RFC 6979] (Ed25519 is deterministic by construction) so signed frame files stay byte-reproducible.

**Key discovery.** A keyed rappid's tail is one-way (`Hb("rapp/1:rappid",SPKI)`). A verifier resolves the
signer's SPKI (DER) **from the §13 registry** entry (`rappid` → `spki_der_b64`); the door-of-record
`rappid.json` is the publication venue the registry entry is generated from, not itself a verification
source. The verifier **MUST** check `Hb("rapp/1:rappid", SPKI_DER)` == the rappid's tail and refuse on
mismatch or registry absence.

**Key lifecycle.** Rotation is an identity re-anchor (§6.3, case `rotation`) with new `tail =
Hb("rapp/1:rappid", newSPKI)` and a §13.3 re-anchor record. A re-anchor **deprecates** the superseded
rappid's §13 `spki` entry: a verifier **MUST** refuse a `sig` whose `kid` is a superseded rappid on any
frame with `utc` ≥ the re-anchor record's `utc` (rotation gives forward security; earlier frames verify as
before). Compromise is declared by an owner-signed **tombstone** in the §13 registry `{rappid, revoked_utc}`; a verifier **MUST** refuse any `sig` by a
tombstoned key on a frame whose `utc` ≥ `revoked_utc`, checking tombstones at verification time (§7.5 step
6). "Owner-signed" means a `sig` verifying with `kid` == the registry's designated `estate_owner` rappid
(§13). A consumer **MUST NOT** infer authorship from an unsigned frame (keyless rappids assert location,
not authorship).

## 11. Conformance classes
- **Producer:** emits only §4 JCS/I-JSON bytes, §5 domain-separated full-SHA-256 addresses, §6 rappids
  minted per §6.2, §7.1 eleven-key frames, §9 `rapp/1-egg` variants — and **no legacy form**.
- **Consumer:** runs the full §7.5 checklist (incl. 1a binding), §9.3 egg verification, §10 signature +
  key-discovery + tombstone checks, canonicalizes legacy ids on read (§6.3), refuses on any failure, never
  repairs/reparents/rolls back (§7.6).
- **Router/Mirror:** invents no endpoints (§8), declares subordination to `kody-w/RAPP` (Fed. Const.
  Art. VII), serves only provenance-stamped hash-matching mirrors (Art. VIII).

## 12. Versioning, evolution, no-legacy
RAPP is a **living standard** (WHATWG): revised in place, never forked into parallel versions; a `name/X.Y`
label **MUST NOT** ever denote two shapes (Art. II) — a shape change moves the token (§7.1). Published
content-addressed artifacts are **immutable** (SemVer/crates). Because the estate has **no uncontrolled
userspace**, there is **no perpetual backward compatibility** (Art. III): a change to a canonical form is a
**total migration** of every instance + **deletion** of the old form. Sealed re-genesis history (§12.1) is
the one retained exception and is not "legacy compatibility."

### 12.1 Re-genesis (converging an immutable chain — one owner-authorized operation)
1. **Terminal seal:** `seal = Hb("rapp/1:seal", head_octets)`. `head_octets` is the exact octets of the old
   head's record **as retained under `legacy/`** (step 4): for a one-frame-per-file store, the retired
   file's full octets; for a line-oriented log, the head's line **excluding** its trailing terminator. The
   retained `legacy/` artifact is the verification reference for the seal, and retirement **MUST** preserve
   those octets bit-exact. The step-3 `genesis` registry entry **SHOULD** record the `legacy/` artifact's
   repo+path so a consumer **MAY** verify the seal against it. (Defined for every legacy shape, including
   ones that cannot be §7.3-hashed.)
2. **New genesis:** emit `seq`=0, `prev`=null, `kind` = the registered re-genesis kind **of the stream's
   family** — `memory.re-genesis`, `swarm.re-genesis`, or `body.re-genesis` (three §13 kinds, used only
   here) so the frame satisfies §7.2 family↔stream compatibility for any stream — `sig`≠null owner-signed
   (§10, §13 `estate_owner`), `payload` = `{"migrated_from":{"stream_id":<old>,"terminal_seal":<seal>,
   "terminal_seq":<n>}}` and no other members. A consumer **MUST** treat any `*.re-genesis` kind as the sole
   re-genesis marker for its family and refuse an unsigned/non-owner one.
3. **Register (the linearization point):** append a §13.3 `genesis` entry mapping the `stream_id` to the new
   genesis's `frame_hash`, **and flag every prior `genesis` entry for that `stream_id` `deprecated`** — the
   first convergence included (it deprecates the creation-time genesis), so exactly one non-deprecated entry
   always remains. A consumer resolves the current genesis **only** via that sole non-deprecated entry. A
   **concurrent** second registration fails closed (the append is the linearization point, Art. IX); a
   later fork/brick (§7.6/§14) is a fresh owner-authorized convergence that appends again and re-deprecates.
4. **Retire:** move old frames under `legacy/` — retained as immutable sealed history, never served as
   current, never extended, never read as live chain. No live frame may set `prev`/`prev_wave` to a retired
   hash (a dangling ref is a drift finding).
5. Keep the old `stream_id` unless the identity itself re-anchored (§6.3), in which case the registry entry
   also records `old_stream_id → new_stream_id`.
6. Two frames with equal `stream_id`+`seq` from different eras are disambiguated **solely** by descent from
   the current registered genesis. Re-genesis is one-time per convergence; a repeat *of the same
   convergence* is the concurrent case (step 3, fails closed).

## 13. The registry — the estate's signed root of trust (append-only)
`rapp-map/ecosystem-spec.json` (`canonical_source` `kody-w/RAPP`) is the estate's IANA. Because §7.6 head
resets, §10 key discovery, tombstone revocation, and ownership all resolve through it, it is the **root of
trust** and is itself authenticated (an unsigned mutable file at the root of the trust graph would forge the
whole estate).

### 13.1 Trust anchor and registry authentication
- The one bootstrap axiom is the **`estate_owner` rappid string** itself: since a keyed tail is
  `Hb("rapp/1:rappid", SPKI_DER)`, the rappid **is** a self-certifying key fingerprint, distributed
  out-of-band exactly once (QR, invite, docs) the way a root-CA certificate is.
- The registry document **MUST** carry a top-level `registry_seq` (uint53) and a detached §10 JWS `sig` over
  `canonical(registry \ {sig})` with `kid` = the `estate_owner` rappid. A consumer **MUST** verify this
  signature against an SPKI whose `Hb("rapp/1:rappid", SPKI_DER)` equals the anchor rappid's tail (the SPKI
  may travel alongside the registry — the tail check authenticates it) and **MUST** refuse an unsigned or
  non-verifying registry.
- **No rollback:** a consumer persists the highest `registry_seq` it verified and refuses any registry with
  a lower one (mirrors §7.6). **Freshness:** a consumer **MUST** obtain the registry from `canonical_source`
  or a provenance-stamped (Art. VIII) mirror of it, **SHOULD** refresh before any §7.5-step-6 or §7.6
  head-reset decision, and **MUST** report a verification made against a registry older than its staleness
  policy as *stale*, not *clean*.

### 13.2 Owner succession (time-scoped authority)
"Owner-signed" means: the `sig` verifies per §10 **and** `kid` is the estate-owner **in effect at the
artifact's `utc`** — the current `estate_owner` or any predecessor reachable through the registry's
re-anchor records (§13.3), with the artifact's `utc` inside that owner's tenure `[record.utc, successor.utc)`.
Verification uses the owner in effect at the artifact's time, **never only the current one** (so a routine
owner key rotation never invalidates historical re-genesis frames or tombstones). Estate-owner **root-key
compromise** is recovered only by re-distributing a new trust anchor out-of-band (§13.1) — it cannot be
expressed inside the registry it signs.

### 13.3 Entry types (each a §4 value; document `schema:"rapp/1-registry"`)
The registry is an I-JSON document; every entry is append-only (never removed/renamed; retirement is a
`deprecated:true` flag). Entry types and their exact members:
- **protocol** `{type:"protocol", name, spec_repo, spec_path, spec_hash, deprecated}`
- **kind** `{type:"kind", kind, family, deprecated}` (incl. the three `*.re-genesis` kinds)
- **egg-variant** `{type:"egg-variant", variant, deprecated}` · **error-code** `{type:"error-code", code}`
  (both closed namespaces; unregistered value = not conformant)
- **genesis** `{type:"genesis", stream_id, frame_hash, deprecated, old_stream_id?, new_stream_id?}` — **every**
  stream registers its creation genesis; re-genesis appends a new one and deprecates all prior (§12.1 step 3);
  §7.6's "registered genesis" is the sole non-deprecated `genesis` for a `stream_id`.
- **spki** `{type:"spki", rappid, spki_der_b64, deprecated}` — the §10 key-discovery source.
- **tombstone** `{type:"tombstone", rappid, revoked_utc, sig}`, `sig` owner-signed over `canonical(entry \ {sig})`.
- **re-anchor** `{type:"re-anchor", old_rappid, new_rappid, case:("upgrade"|"rotation"|"compromise"|"tag-migrate"),
  utc, sig, old_key_sig?}` — `sig` owner-signed; `old_key_sig` a §10 JWS by the **old** key over
  `canonical(entry \ {sig,old_key_sig})`, REQUIRED for `case:"rotation"`. This is the normative succession record (§13.2).
- **estate_owner** `{type:"estate_owner", rappid}` (exactly one non-deprecated) · **master-plan**
  `{type:"master-plan", repo, path}` (Fed. Const. Art. VII).

§7.5 steps 1–5 are time-independent (append-only lookups); **only** step 6 (tombstones) and §13.2 owner
tenure are time-scoped, and both are monotone given the §13.1 no-rollback rule.

## 14. Security considerations
- **Integrity:** every object is domain-separated content-addressed (§5); a hostile mirror cannot alter
  bytes without breaking the hash, so *history is safe given a trusted head*.
- **Head freshness is not self-certifying:** the chain authenticates history, not which head is current; a
  hostile mirror may serve a stale/forked head. Consumers counter with the §7.6 monotonic-head rule; swarm
  heads **SHOULD** be owner-signed.
- **Cross-stream replay:** without §7.5 step 1a, any genesis/segment of stream A replays as stream B (seq=0,
  prev=null always pass). Step 1a's stream binding is mandatory.
- **Address-space confusion:** §5 domain tags make a particle, wave, egg, or rappid tail with equal hex
  non-interchangeable; stores key by `(space, hash)`.
- **Canonicalization attacks:** the §4 I-JSON input-domain profile (no duplicate keys, no lone surrogates,
  exact binary64, no normalization ambiguity) removes hash-splitting and NFC-twin vectors.
- **Identity forgery / key compromise:** authorship requires a keyed rappid + valid §10 `sig`; rotation is
  §6.3 re-anchor (verifiable authorization, §6.3/§13.3 — a self-asserted `_migrated_from` is refused);
  compromise is a §13 tombstone enforced at verify time. Because a tombstone gates on the frame's
  producer-controlled `utc`, a compromised key can still emit frames stamped just below `revoked_utc`; after
  a compromise the owner **SHOULD** advance affected stream heads (or re-genesis) past `revoked_utc`.
- **Root of trust:** the registry is the estate's signed root (§13.1); it is authenticated by an owner
  signature anchored to the out-of-band `estate_owner` rappid fingerprint, `registry_seq`-monotonic against
  rollback, and freshness-checked (a stale registry silently un-revokes keys and hides re-geneses).
- **Producer-controlled `utc` (DoS/merge bias):** a future-dated head can brick a stream (successors refused
  as earlier) and bias UTC-first merges. A consumer **SHOULD** refuse a frame whose `utc` exceeds receipt
  time by >300 s, and adversarial-scope merges **SHOULD** rank by `min(utc, first-seen)`; a bricked stream
  converges by re-genesis (§12.1).

## 15. References
[RFC 2119] [RFC 8174] requirement terms · [RFC 8259] JSON · [RFC 7493] I-JSON · [RFC 8785] JCS ·
[FIPS 180-4] SHA-256 · [RFC 3986] URI · [RFC 5234] ABNF · [RFC 7405] case-sensitive ABNF · [RFC 9562] UUID
(obsoletes RFC 4122) · [RFC 5280] X.509 SPKI · [RFC 7515] JWS · [RFC 7797] unencoded JWS payload ·
[RFC 7518] JWA/ES256 · [RFC 8037] EdDSA in JOSE · [RFC 6979] deterministic ECDSA · [RFC 3339] timestamps ·
[ECMA-262] ECMAScript.

---

### Revision log
- **rev-5 (war-game round 3 fold)** — folded 5 blockers + 7 majors + 7 minors, all clustered on the trust
  model that rev-4's fixes made load-bearing: the **registry is now a signed root of trust** (§13.1) —
  owner-signed, anchored to the out-of-band `estate_owner` rappid fingerprint, `registry_seq`-monotonic,
  freshness-checked (B-1); **re-anchor requires a verifiable §13.3 authorization record** with old-key
  continuity proof / tombstone / SPKI-tail check (B-2, mint-once now enforceable); **owner-succession is
  time-scoped** so a key rotation never invalidates historical signatures (B-3); **eggs are `stored`-only**
  (deflate is non-deterministic) with `canonical(manifest)` bytes (B-4); **invites sign under the
  estate-owner succession** not the egg's own rappid (B-5); `egg_hash` excludes `sig` (M-1); first
  re-genesis deprecates the creation genesis (M-2); full **registry entry schema** §13.3 (M-3); registry
  freshness rule (M-4); egg consumer enforces §9.1 (zip-slip, M-5); rotated key refused on new frames (M-6);
  `rapplication` exact `agent.py` (M-7); rev label, `invite` naming, sub-egg collision, rounding rule,
  idempotency, compromise-window, seal-path (m-1…m-7).
- **rev-4 (war-game round 2 fold)** — folded 6 blockers + 14 majors + 8 minors: domain-tagged mint
  reconciled across Constitution/ledger (B1); §4(c) binary64 round-trip test so `0.1` is accepted (B2);
  re-genesis head-reset exception so it isn't refused as rollback (B3); family-matched `*.re-genesis` kinds
  so memory/swarm streams can converge (B4); whole-egg address `H("rapp/1:egg-manifest",…)` + signed invites
  + manifest self-reference resolved + deterministic ZIP/`contents` ordering (B5, M2–M4); re-anchor
  enumerated three cases incl. key rotation (B6, M-rotation); `head_octets` pinned to the retained `legacy/`
  artifact (M6); subsequent-convergence via `deprecated` (M7); every stream registers its genesis (M8); JWS
  header canonical bytes + registry key-discovery + `estate_owner` designation (M9–M11); `/chat` error `step`
  as string incl. `"1a"` + code namespace + session semantics (M12, m5); tombstone as the one time-dependent
  verify check (M13); egg draft-artifact + dangling `§4` reference removed (M1, M5); calendar-valid `utc`,
  depth convention, kind dedup/bounds (m2, m7, m8).
- **rev-3 (war-game round 1 fold)** — folded 7 blockers + 19 majors + 12 minors from the Fable adversarial last-call:
  I-JSON input domain + no-normalization/NFC (§4); **domain-separated hashing** (§5, the stronger option);
  fixed `utc` byte form (§7.4); `prev_wave` by stream family not transport (§7.4); **stream-binding**
  anti-replay (§7.5.1a); `spec` token pinned to `rapp/1` (§7.1); full JWS profile + key discovery + rotation/
  tombstone (§10); hardened re-genesis with raw-byte terminal seal + registry linearization (§12.1); heads &
  forks (§7.6); `/chat` fully specified (§8); **egg variants ratified into the standard**, killing the
  6-spec collision and closing EGG-01 (§9); registry append-only (§13); type-validated verify (§7.5.1);
  cross-stream merge tie-break (§7.4); provisional-identifier rule (§6.3); all references added (§15).
- **rev-2** — first last-call tightening (7 self-review defects).
- **rev-1** — initial unified draft.

*Drafted, not merged. Belongs at `kody-w/RAPP/specs/RAPP-1.md`; governed by the Federal Constitution.*
