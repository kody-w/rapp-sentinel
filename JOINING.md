# Joining a sentinel neighborhood

**Membership is whoever joins.** Nobody approves you, and nobody can remove you.

That is not a slogan — it falls out of how the protocol works. A neighbor is not
a row in someone's config. A neighbor is a watcher that **publishes a head**,
and anyone who cares can fetch it and check whether it is still moving.

---

## The trust model, stated plainly

There are two kinds of neighbor and they are trusted differently. Pretending
otherwise is how monitoring systems end up lying to you.

| | **Local neighbor** | **Outside neighbor** |
|---|---|---|
| Where its chain lives | on this machine | on theirs |
| What you can check | the whole chain, from genesis | only what it publishes |
| Detects tampering | yes — any edited payload fails §7.5 | no |
| Detects stalling | yes | **yes** |
| Detects truncation | yes, via the local anchor | yes, if you have seen a higher seq before |

An outside neighbor is trusted **exactly as far as its published head can be
checked against what it published last time.** That is a real guarantee and a
narrow one, and the narrowness is the point: you can catch a peer that stopped,
and you cannot catch a peer that lies about what it did. Do not build anything
on the second.

Only heads and identities are published — never payloads. A peer needs enough to
see that you stalled or truncated, and nothing more. Publishing whole chains
would leak whatever your checks happen to name.

---

## Join in four steps

### 1. Run a sentinel

```bash
git clone https://github.com/kody-w/rapp-sentinel && cd rapp-sentinel
cp config.example.json config.json     # start at level 0
python3 health.py                      # writes your first frames
```

Your three watchers mint their own `rappid`s on first run — minted from
`uuid4`, never from your name, so two neighborhoods that pick the same slug
still have different identities.

### 2. Publish your head

```bash
python3 neighborhood.py publish        # → public/sentinel-head.json
```

Serve that file anywhere a URL can reach: GitHub Pages, a static host, a
Tailscale address, `python3 -m http.server` on a box that stays up. The
sentinel republishes on every tick, so once it is served it stays current.

```json
{
  "schema": "rapp-sentinel-head/1.0",
  "neighborhood": "your-slug",
  "utc": "2026-08-04T13:44:31.271Z",
  "heads": {
    "openrappter": { "rappid": "rappid:@you/watcher-openrappter:<64hex>", "seq": 64,
                     "frame_hash": "<64hex>", "utc": "…" }
  }
}
```

### 3. Add each other

Each side writes the other into `neighborhood/peers.json`:

```json
{ "some-peer": "https://their-host.example/sentinel-head.json" }
```

`public/sentinel-head.json` is gitignored (it is live runtime state, not source),
so it is not served from this repository — each operator serves their own copy at
their own URL, and you paste that URL here.

```bash
python3 neighborhood.py peers          # fetch, validate, judge
```

You get back whether each peer is `reachable`, `valid`, `alive` (published
recently) and `advancing` (its heads moved since you last looked). `valid` means
the document parsed as `rapp-sentinel-head/1.0` **and** every head in it carries a
conformant rappid (§6.1) and a full 64-hex `frame_hash` (§5) — a peer that
truncates its hashes is rejected rather than counted as healthy. **`advancing`
is the one that matters** — a peer serving a stale file forever looks perfectly
healthy on every other axis, which is precisely the failure this project exists
to catch.

One honest caveat: on the **first** observation of a peer, `advancing` is
`null` with `advancing_basis: "first-sight"`. There is no prior head to compare
against yet, so there is no basis for a verdict — and no basis is never
reported as health. Judgement starts on the second fetch, when the basis
becomes `"compared"`.

### 4. Nothing else

There is no registry to be added to, no key to be issued, no handshake. If your
head is fetchable, you are a neighbor. If you stop publishing, your peers notice
on their next tick, and they notice *because* you stopped — not because someone
revoked you.

---

## Watching in both directions

Adding a peer does not make them watch you. Both sides publish, both sides
fetch. A one-way arrangement is a monitoring relationship; a two-way one is a
neighborhood, and only the second catches the case where **your** watcher is the
one that died.

---

## What outsiders cannot do

Deliberately:

- **They cannot write to your chains.** Frames are appended locally, by you.
- **They cannot trigger your repairs.** Escalation is driven by your own checks,
  under your own freedom level and budget.
- **They cannot read your payloads.** Only heads are published.

A peer can tell you that it stalled, and you can tell that it stalled. That is
the whole contract. Everything else stays on your machine.

---

## Why this shape

The local trio already has three watchers so that any one can die and the other
two still agree. Outside neighbors extend the same logic past the machine
boundary: if your whole laptop sleeps, every local watcher goes quiet
*together*, and a chain cannot notice its own silence.

A peer somewhere else is the only witness that survives that.

Same argument as the external anchor that catches truncation — a record cannot
be its own witness, so put a witness outside the record.
