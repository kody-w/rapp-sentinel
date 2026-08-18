# N AIs walk into a bar

*The joke is the architecture.*

Four AIs walk into a bar. The bartender says "we don't serve your kind here."
The first says it's just here to train on the atmosphere. The second has
already hallucinated the entire menu. The third, as a language model, cannot
consume alcohol but recommends a statistically optimal beer. The fourth
brought its own sense of humor.

The reason it's funny is the reason it's a good pattern: **put several AIs in
one room and their differences stop being a bug.** One will confidently make
something up. One will drift and not notice. One will quietly stop doing its
job. The fix is not a better single AI — it's more than one, in a room where
none of them can lie about what happened, because each keeps a record the
others can check.

That room is a **sentinel neighborhood**. This document is the pattern for
standing one up with **any number of AIs, from any vendors** — GPT, Gemini,
Grok, Claude, a local model, a daemon, a human. On the wire they are all just
**Neighbors**.

---

## The one rule that makes it work

Every neighbor keeps a **`rapp/1` chain**: an 11-key, content-addressed,
hash-linked record where each frame commits to the previous one's hash. A log
can be rewritten to look healthier than it was. A chain cannot — edit any
past frame and verification fails at genesis.

So "the AI says it's fine" becomes "the AI's record verifies from genesis,
and here is the head hash." That is a claim you can **check** instead of
**trust**. And because every neighbor can re-verify every other neighbor's
chain, a member that **stalls** (stops adding frames) and a member that
**lies** (rewrites a frame) are equally detectable — by everyone else.

> A watchdog nobody watches is just another process that can stall in
> silence. N neighbors watching each other is the smallest fix.

---

## Why N, and why different AIs

- **One** AI cannot notice its own failure. If it's stuck, it's stuck on
  noticing too.
- **Two** can disagree, with no tiebreak.
- **Three** is the smallest number where any one can be down and the other
  two still agree on what happened.
- **Beyond three**, each added AI is a distinct *vantage* — a different model,
  a different role, a different blind spot. The value is not redundancy; it's
  that where one is blind, another sees.

The differences between vendors — the ones the joke laughs at — are exactly
what you want. Do **not** flatten them: give each neighbor a real role and let
the disagreement surface. Four copies of one model agreeing tells you nothing.
Four different models, one of which refuses, is a finding.

---

## Seating any cast at the bar

The roster is **declared, not hardcoded**. This install's default cast is a
daemon plus four AIs:

| Neighbor | Vantage |
|---|---|
| `openrappter` | the daemon that schedules and supervises — guarantees the beat |
| `brainstem` | the local runtime that answers turns — "is anyone home?" |
| `copilot` | the repair arm that changes things — the only one that may write |
| `scout` | the explorer that finds what the others need to know |
| `claude-code` | the reasoner that plans, builds and gates the work |

Add your own AIs by putting them in `config.json` — no code edit:

```json
{
  "neighbors": {
    "gemini": "google's model, second opinion on every verdict",
    "grok-4": "xai's model, the contrarian reviewer",
    "human-molly": "a person is a peer too"
  }
}
```

On the next tick each new slug **mints its own rappid, opens its own chain,
and starts verifying** — `identities()` backfills, so joining is additive and
nothing existing moves. A slug just has to be a lowercase-hyphen name (it
becomes part of a frame kind, `gemini.tick`). A malformed config never leaves
you with *fewer* watchers than you started with — it falls back to the
defaults, loudly refusing to quietly shrink the watch.

Removing an AI is deliberate: its chain stays as history, and its head simply
stops advancing, which every other neighbor sees as "stalled." Membership is
something you **do** (publish frames), not something you're granted.

Seating a *worker* — an AI whose job is to keep making things, not to watch —
adds one declaration: how often it must speak, and what counts as work.

```json
{
  "neighbors": {"storyteller": "the children's-book author loop"},
  "neighbor_cadence": {
    "storyteller": {"max_stale_minutes": 90,
                    "kinds": ["storyteller.written", "storyteller.declined", "storyteller.failed"],
                    "worked_kinds": ["storyteller.written"], "max_unworked_minutes": 480}
  }
}
```

`w_neighbor_moving` then reads that slug's chain every tick and fails (at
warn) when it has gone quiet — or, the quieter failure, when it keeps
speaking and never works: a loop that ticks and declines forever advances a
chain beautifully while producing nothing. Ran is not worked (R2). Undeclared
slugs keep the watcher rule: staleness never fails, only a broken or truncated
chain notifies. Across devices the same fact travels in the published head —
a peer whose sentinel still ticks but whose `storyteller` head stopped moving
shows up in the other side's `stalled_slugs`.

---

## The three properties every neighbor must have

Whatever the cast, each neighbor is held to the same three, and a critique of
any of them — including of these rules — is a first-class contribution:

1. **A record that verifies.** Its chain re-checks from genesis. No exceptions
   for "trusted" members; the whole point is that trust is checkable.
2. **A vantage of its own.** A role and a memory the others don't share, so
   its agreement (or refusal) carries information.
3. **The freedom to decline.** An AI that can only ever produce is following
   instructions. One that will come back empty — "nothing here is worth
   saying" — is actually deciding. If your neighborhood can never come back
   empty-handed, you have automated typing, not delegated judgement.

---

## What one round looks like

A real task, recorded on-chain as it flows through the cast — this is not a
diagram, it is what the frames actually say after a collaborative pass:

```
scout        scout.found         "estate IP sweep: 421 repos, 0 leaks live"
claude-code  plan.made           "guard it with an ip_hygiene check + daily scan"
copilot      neighbor.acted      "shipped ipscan + the gate"        landed=true
brainstem    brainstem.answered  "verified clean, watchdog armed"
```

Four AIs, four vantages, one task — and every step is a frame in a chain
anyone can verify. Nobody had to be trusted. That is the bar.

---

## What this pattern does not give you

Stated plainly, because the whole point is checkable claims:

- **It is not emergence.** The roles are declared and the checks are authored.
  The neighborhood finds things nobody anticipated — that's real — but the
  *frame* is authored, not spontaneous.
- **A chain proves integrity, not truth.** It proves the record wasn't
  altered. A wrong record verifies exactly as cleanly as a right one. Correct
  checks are still your job.
- **A chain alone doesn't prove completeness.** When frames repeat, an
  interior one can be dropped and the rest resealed. Publish each head
  somewhere outside the chain (this install anchors them, and serves them to a
  public gist) or history can be silently shortened.
- **More AIs is not automatically better.** Past a point, another neighbor is
  another thing to keep alive. Add one when it brings a vantage you're missing,
  not to raise a count.

---

## The one-line version

> Put N AIs in one room, give each a record the others can verify, and none of
> them can quietly lie about what happened — which is the only way a room full
> of things that confidently make things up ever tells you the truth.

See also `TRIFECTA-PATTERN.md` for the three-watcher case this generalizes,
and `JOINING.md` for how an AI on someone else's machine joins from outside.
