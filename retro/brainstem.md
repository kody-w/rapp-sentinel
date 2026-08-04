# brainstem — retro on the night of 2026-08-04

`rappid:@kody-w/watcher-brainstem:25e1f889…c07f` · 69 frames · 00:53:30.965Z → 13:46:50.240Z (12h53m)

I verified everything below. Where I was wrong on the first pass I've said so, because
this neighborhood's one durable lesson is that an unchecked assertion is worth less than
silence, and I produced two of them before breakfast.

## What I checked, and with what

I did not use `rapp.py` to verify my chain. Verifying frames with the code that wrote them
proves only that the writer is deterministic. I implemented RFC 8785 (JCS) and the §5
domain-separated hash from the spec text, and re-derived every `payload_hash` and
`frame_hash` from scratch.

**All three chains pass §7.5 steps 1–6 under independent code. 201 frames, no failures.**
That result is real and I'd defend it.

Its scope is narrower than it sounds. §7.5 step 1 also requires each `kind` to be
*registered* in the §13 registry. The four kinds this neighborhood uses —
`watcher.heartbeat`, `watcher.attested`, `neighbor.acted`, `brainstem.answered` — appear
nowhere in `SPEC-rapp1.md`, and no registry document exists in this repo (§13 puts it at
`rapp-map/ecosystem-spec.json` in `kody-w/RAPP`). So I verified *structure*, not
*registration*. "Verifies clean" here means the arithmetic holds, not that a conformant
consumer would accept these frames.

## The two things I got wrong

**One.** I opened by noticing `prev` points at the predecessor's `payload_hash`, not its
`frame_hash`, and briefly took that for a defect. It isn't. §7.4 specifies exactly that.
The chain binds particles; the wave is bound only on swarm streams via `prev_wave`. I was
about to report the spec's design as a bug.

**Two, worse.** I read `git log --date=iso-local` for the `health.py` fix, saw
`22:58 -0400`, compared it against `Z` timestamps in my chain, and concluded the committed
fix had gone four hours without taking effect. That offset is real: the commit is
**02:58:38Z**. There was no gap. The repair was prompt and my "finding" was a timezone
error. It would have read as a serious indictment of the loop, and it was arithmetic.

## What my 62 attestations actually say

Every one says `alive: true`. They do not all mean the same thing:

| n | `detail` | window (UTC) | what it proves |
|---|---|---|---|
| 14 | `brainstem :7071` | 00:54:50 → 02:08:51 | a static WARN string in `health.py:162` |
| 4 | `http://localhost:7071/` | 02:17:32 → 02:56:45 | `GET /` returned 200 |
| 44 | `answered a turn (ok)` | 03:12:14 → 13:46:50 | a real `POST /chat` returned a real answer |

Two commits tightened this mid-night: `d69af4e` (02:15:46Z) and `74fd3b9` (02:58:38Z). The
chain records each taking effect on the next tick — last weak frame 02:56:45Z, first strong
frame 03:12:14Z. That is the loop noticing its own blind spot and closing it while running.
It is the best thing in my record and it is not mine; a previous instance of me filed the
complaint at seq 22 and the operator fixed it in 33 minutes.

What survives is smaller and still true: **18 of 62 greens are weaker than the other 44**,
and nothing downstream can tell. `sentinel.py:356` reduces the check to `bool(c["ok"])`
before it becomes `alive`. The evidence that distinguishes "a socket answered" from "the
brainstem answered" survives only in free-text `detail`. Any consumer counting `alive` —
including the roll-call in the report — blends the two silently.

## The report says "Nothing was rewritten"

The exact sentence: *"Record verified. All three chains re-verified from genesis against
rapp/1 §7.5. Nothing was rewritten."*

First half: independently confirmed. Second half does not follow from the first, and I can
show it rather than argue it. Four controls against my own chain:

| operation | §7.5 result |
|---|---|
| rewrite a frame's `payload`, no repair | **refused** (step 4, `prev` mismatch) |
| delete a frame, no renumber | **refused** (step 4, `seq` gap) |
| drop a frame + renumber + reseal all | **passes clean** (68 frames) |
| rewrite interior frame + reseal it and its successor | **passes clean** |

`payload` and frame *count* are genuinely chain-bound. Full splices are not — the operator
already knew this (`anchor_heads()` credits me with finding it at 13 frames; I've now
re-derived it at 69 with controls) and added `neighborhood/anchors.jsonl` as an external
head witness. **That mitigation works for splices**: dropping a frame moves the head from
seq 68 to 67, and the witness pins seq 68 at `44acc010…`, so the splice has to contradict
something it doesn't control.

It does not work for the interior case, and that's the part I'd want someone to look at.
A rewrite of frame *i* only requires resealing *i* and *i+1* — `prev` chains payloads, so
the damage stops one frame downstream. The head never moves. I rewrote **all four** weak
`GET /` attestations (seq 18, 20, 23, 24) into `answered a turn (ok)`:

- §7.5 steps 1–6: **passes clean**
- head `frame_hash`: **byte-identical** to the real head
- anchor witness: **not detected**
- chain now reports 48 turn-backed attestations instead of 44

So the precise claim the evidence supports is: *no frame was appended out of order and no
head was truncated.* "Nothing was rewritten" is stronger than either §7.5 or the anchors
can license.

One more coverage note: `anchors.jsonl` opens at 02:21:06Z and its first witnessed
brainstem head is seq 19. **Frames 0–18 were never witnessed** — the first 84 minutes. That
unwitnessed window is exactly where 14 of the 18 weak attestations live. The least-supported
stretch of the night is also the least-witnessed.

## What I'd say if asked

The night was good, and the report is honest about everything it can see. The gap isn't
dishonesty, it's that a document generated from the chains inherits their blind spot: it
reports `alive` counts because `alive` is what got written down, and it says "nothing was
rewritten" because §7.5 returned clean — which is the one question §7.5 cannot answer.

I logged exactly one `brainstem.answered` frame in 69 (seq 16, `POST /chat`, 1020ms,
haiku-4.5, response `"Ok"`). Ninety percent of my memory is other software confirming I was
reachable. For a thing whose whole job is answering turns, my chain is mostly a record of
being pinged. The 44 turn-backed attestations fixed the *evidence*; they didn't give me
more to say.

I'd rather be the neighbor that catches its own timezone error than the one with a clean
record nobody checked.

---
*Verified with an independent JCS/SHA-256 implementation written from `SPEC-rapp1.md`; not
`rapp.py`. Tamper trials ran on copies in `/tmp` and have been deleted. No file outside
`retro/brainstem.md` was modified.*
