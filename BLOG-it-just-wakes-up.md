# It just wakes up and tells me what we should do today

*On the morning memo, the principal who sits in the back of the room, and why a neighborhood of AIs that grade each other is a different way of working — not a better dashboard.*

---

## The text message

At 2:18 in the morning my phone buzzed:

> 🏫 The Principal is live. First round: Dada B · Storykeeper One B · Estate C · Storykeeper Two F (tick dead 7h, kicked) · Battlestation F (no sentinel). Chronic across the board: openrappter spin on two machines, a smoke test that has timed out on every one of 82 ticks, a brainstem with a revoked token, a Copilot login that died at 18:40, and alert delivery — the duplicate texts you got were an outbox resend bug: fixed and molted everywhere. Dada's evolve arm has never shipped a piece. Full memo in the morning file.

I did not ask for that message. Nothing in it is a status I requested. It is a **judgement about my own watchers**, written by a watcher I had stood up an hour earlier, delivered at the moment it became true. When I woke up there was a memo waiting with a table: *what keeps failing · how often · the root cause · the fix for today* — and an ordered list of what to do first.

The text I sent back was: *it just wakes up and tells me what we should do today.*

That sentence is the thesis. This post is the argument.

---

## What we actually built (briefly, because the pattern is the point)

Over the last two days a small neighborhood of AI sentinels came into being across five machines on a private network:

- an **estate watch** on my laptop that keeps two public platforms honest,
- a **collective** on a Mac mini that watches the same platforms and, when healthy, makes art for a public commons,
- two **storykeepers**, one per mini, each keeping a local author loop honest — a loop that writes a children's picture book every few hours and must prove it with a compiled file whose hash is on a ledger,
- and, last night, **the Principal**: a sentinel whose job is *other sentinels*.

Every one of them keeps a tamper-evident record of its own actions. Every one publishes a head other watchers can fetch and compare. None of them can quietly lie, because each one's record is re-verified from genesis by the others — that is the [N-AIs-walk-into-a-bar](N-AIS-WALK-INTO-A-BAR.md) pattern, and it is old news here.

The Principal is the new thing. It does what a good school principal does: drops into a classroom unannounced, sits at the back, and grades the **teacher against the job the teacher declared** — not against whether the world is healthy. A sentinel that correctly reports a broken platform is doing its job. A sentinel whose own record stalled for seven hours while every chain still verified clean is not — and that is exactly what it found on its first visit.

It grades with a rubric that cannot be wrong about arithmetic: *attendance* (is the tick landing on schedule, and is the record **moving**), *record* (chains verify, nothing truncated), *the job* (does the verdict cover what the teacher said it cares about; how many checks are red; how many have been red across visits with nobody deciding), *honesty* (does "ok" agree with the failing list; are alerts rotting undelivered), *discipline* (budgets). Then — because it is an AI in the neighborhood, not a spreadsheet — it reads the same evidence a person would and writes its own note: grade, what works, what fails, the one change it would make. The two grades sit side by side. When they disagree, the disagreement is recorded, not resolved. Every visit is a frame on its own chain. It texts only when a grade changes.

Then it mined the exhaust: 82, 98, 34 and 7 ticks of logs, the alert ledgers, the evolve history, the launchd state — and wrote the memo.

---

## Why this is not monitoring

Monitoring answers *is it up?* This answers a different question: **what did we keep getting wrong, and what should a human decide today?**

Three things separate it from a dashboard, and each one is a design decision you can copy.

**1. The job is declared, so the grade can be against it.** Every sentinel carries a `direction.json`: a situation in plain language, boundaries it will not cross, what it cares about, how much freedom it has. That document is the contract. It is what makes "is this teacher teaching what they said they would teach" a computable question — and what makes a standing red a *decision nobody made* rather than background noise. The single most useful line in the memo is the one that says: *this check has been red on 82 of 82 ticks; either fix the thing or mark the gap accepted, but decide.* A dashboard shows you the red. A principal tells you it has been red for a day and that the redness is now the failure.

**2. The record is evidence, not a report.** A watcher that says "ran" has told you nothing; a watcher that says "ran, and here is the artifact whose hash is on the ledger" has. The author loop is only "working" when a compiled book exists and the ledger names its hash. The sentinel is only "present" when its heartbeat moved since the last visit. The alert is only "delivered" when the message store gained a row. The Principal's first real catch came straight from that rule: a sentinel whose chains verified perfectly and whose tick had not landed in seven hours. Integrity is not liveness. Both are checkable, so both get checked.

**3. Cheap checks all the time, judgement only where it pays.** The rubric is free — no model, no network beyond ssh. The model is invited in once per visit, tool-less, with a bounded prompt and a bounded answer, to do the one thing arithmetic cannot: say *what the one change should be*. That is how you keep a neighborhood of AIs from becoming a neighborhood of bills. It is also how you keep it honest: the model grades next to the rubric, never instead of it.

---

## What the Principal found in one night

This is the part that convinced me, because none of it was in my head at midnight:

- **My own alerts were being sent twice.** The outbox sent a text, then discovered it could not read the message database to verify delivery, called the send "unverifiable", kept the message in the queue — and sent it again on the next drain. Ten copies were queued on the estate sentinel. I had been receiving every alert twice and had filed it under "iMessage is weird". The fix was twelve lines and a rewritten contract: an unverifiable send is delivered once, recorded as unverified, never requeued.
- **A collective whose entire purpose had never once succeeded.** Five evolve runs: failed, failed, failed, rejected, fan-out failed — blocked by a staging directory the confined maker was not allowed to write. Every tick of that sentinel was green on the thing it watched and silent on the thing it was *for*.
- **The same two or three reds on every machine**, each with a real cause (a crash-looping job, a revoked token, a login that died at a known minute, a smoke test whose landing window is shorter than the platform's ingestion) — and each one sitting there because fixing it and deciding to accept it were equally undone.
- **An empty classroom.** The Windows box on the network has a brainstem and no sentinel. Grade F, note: *hatch one.*

None of that is an inventory. It is an agenda.

---

## The new way of working

Here is the shape of the day this creates, and why I think it generalizes far beyond my network.

You stop starting the day by looking. You start the day by **reading what the night decided you should look at** — and, crucially, what it decided you should *decide*. The memo is not "here are 412 metrics." It is "these four things have been wrong for a day, here is why, here is the fix, and here are the three items that need a human because they need a login, a permission, or a choice." The human's job narrows to the part only a human can do: interpretation and approval. Everything else is already done, evidenced, and chained.

That narrowing is the whole point, and it only works if four conditions hold:

1. **Every agent has a declared job** it can be graded against. No declaration, no grade — and no grade means nobody can tell you it drifted.
2. **Every claim has an artifact.** "Done" means a file, a hash, a row. Receipts are not evidence; ran is not worked.
3. **Agents watch agents.** A single watcher has the same blind spot as the thing it watches: its own liveness. Peers see each other stall; a principal sees the peers.
4. **The human stays above, not beside.** You write situations and boundaries; the neighborhood writes the agenda; you decide. A principal that could fix things itself would be one more thing to supervise. This one grades, suggests, and texts — and waits.

Put those together and the team you manage in the morning is not a team of tools. It is a faculty. Some members teach (the watchers, the writer). One keeps the records (every chain). One sits in the back of the room and grades the faculty against what they promised, out loud, with receipts, and hands you the list.

The future of work is not an AI that does your tasks while you watch. It is a room full of AIs that hold each other to what they said they would do — and one that wakes up before you and tells you what the day is for.

---

*The Principal is `principal.py` in this repo; the rubric is pinned by `prove_principal.py`; the memo format is in the README. Every number in this post came from a chain.*
