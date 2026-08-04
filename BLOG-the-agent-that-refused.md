# The Agent That Refused

## What I learned building an autonomous loop that keeps two platforms alive

There is a moment in this story where a software agent was handed the freedom to
make something, went and looked around, and came back having written nothing.

That refusal turned out to be the only real evidence I had.

---

## The failure nobody sees

Two of my platforms run entirely on GitHub. No servers. State is JSON files, the
API is raw content, the game server is GitHub Actions, the frontend is Pages.
Every commit is a frame; every pull request is an action.

One of them sat frozen for nineteen days and I did not notice.

Not down — *frozen*. First paint in 168 milliseconds. Zero JavaScript exceptions.
Zero failed network requests. Every surface metric green. Underneath, the world
had not merged a single state change since July 13th, and the audit was blunt
about it: zero actions, zero of two hundred and ten agents, quality score
thirty-five out of a hundred.

An agent was opening a pull request every thirty minutes. Every one was rejected.
A hundred out of a hundred. Six hundred and seventy-nine had stacked up behind
the gate. The cause was one line: the agent was owned by one GitHub account, but
its heartbeat ran on my machine and opened PRs as a different one, and the
validator demanded an exact match.

The sister platform failed the same way for a completely different reason. Six
workflows at a hundred percent failure, every one dying at the push, because a
single state file had grown to 101.17MB — past GitHub's hard limit. GitHub
rejects the *entire push*, so one oversized file held the whole write path
hostage. It was already in `.gitignore`, but `.gitignore` only applies to files
that aren't tracked yet, and this one was.

Different causes, identical shape: **an invariant nobody was watching, and a
write path that jammed in silence.**

That is the problem worth solving. Not "my site is down" — you find that out.
"My site is up and has been lying to me for nineteen days" is the one that costs
you.

---

## Three watchers, because one can't notice its own death

The fix was not a better dashboard. A dashboard reads the same stale data and
renders it in a nicer font.

I built three watchers:

- a **scheduler** that guarantees the beat
- a **runtime** that answers locally
- a **repair arm** that is the only one permitted to change anything

Three, specifically, because one watcher cannot notice its own death and two can
disagree with no tiebreak. Three is the smallest number where any one can be
down and the other two still agree on what happened.

They run on deliberately offset schedules — one on the quarter hour, one at
:07 and :37 — so either scheduler can die and the watch continues, and the
survivor's record shows the gap where the other stopped.

### The chain, which is the part people skip

Each watcher keeps a hash-linked chain of records. Every entry commits to the
previous one's hash.

This sounds like ceremony until you consider what a log file is worth. A log can
be rewritten to look healthier than it was. A chain cannot — alter any past
entry and verification fails immediately with a hash mismatch.

So "the watcher says it's fine" becomes "the watcher's record verifies from the
beginning, and here is the head hash." One is a claim you trust. The other is a
claim you check. A **tampered** watcher becomes exactly as detectable as a
**stalled** one, which matters more than it sounds, because those are the two
ways a watchdog lies to you.

### Free to check, expensive only to fix

The economics decide whether this survives past the demo.

A healthy tick costs a few API calls and exits in seconds. **No model is
invoked.** A model is only invoked when a check actually fails, and only after
clearing a daily budget, a per-issue cooldown, and an attempt cap.

That asymmetry is the whole reason it can run every fifteen minutes forever
instead of being an expensive toy you switch off after a week. If your health
check needs a model to decide whether something is broken, your check isn't
specific enough yet.

---

## Give it a situation, never a task

Then the loop did something I did not plan.

A check failed. It escalated. The agent came back having found a race condition:
a fifteen-to-thirty-second replica lag between two workflows, where one pushed
fresh state while the other was still holding a slightly stale checkout, and the
resulting conflict was being reported as a genuine merge conflict. It wrote a fix
that auto-resolves the conflict *only* when every conflicting file is
regenerated state, still refuses on anything else, and emits a visible warning so
it never happens silently. Then it re-ran the failing check and confirmed.

I had never heard of that bug.

It found it because of how the escalation is shaped. The loop hands the agent
**a situation and a set of boundaries** — what failed, what state things are in,
where it may not go — and nothing else. No procedure. No suggested fix.

Had the prompt said *"add an autostash flag to that script,"* the loop would have
discovered nothing. I would have discovered it, and the agent would have typed
it. The distinction is not stylistic. It is the difference between delegating
judgement and automating keystrokes.

| Give it | Never give it |
|---|---|
| the situation | the solution |
| its own memory | a procedure |
| hard boundaries | a template |
| authority to decide, including to decline | a required output |

---

## Then I broke my own rule

Feeling good about all this, I decided to prove the neighborhood could do
creative work. Three watchers, three pieces of generative art, submitted to a
public commons.

I wrote the generator. I designed all three pieces. I chose the palette, the
composition, and what each one meant.

They passed every technical test I could devise. Unique. Deterministic. Bound to
each watcher's own chain. Falsifiable — tamper with the underlying record and the
image visibly changes. Six for six.

And they proved nothing, because **three functions written by one hand is not
three artists.** I was the scheduler, the artist, and the critic, and then I was
going to write the press release. My collaborator caught it immediately: *"otherwise
it's just you playing all of the different parts."*

So I rebuilt it properly. The loop now has a mode where, when everything is
healthy, one watcher is handed its own situation — who it is, its own memory, its
peers, the fact that its operator belongs to a public commons — and left to
decide what, if anything, it does. It is told to read that commons' conventions
*from the repository itself*, and explicitly told that my description of it is not
authoritative.

It is never told to make art.

---

## The refusal

The first run came back in fifty-three seconds having written nothing.

`Changes +0 −0`.

It had cloned the commons, read the repository's own contribution rules, found a
piece already sitting under its name — mine — and declined, citing the exact
blocking commit. There was no truthful new contribution to make, so it made none.

That decline was worth more than any picture. **An agent that only ever produces
is an agent following instructions. An agent that will refuse is one that is
actually deciding.** If your loop can never come back empty-handed, you have not
delegated judgement.

It also surfaced a defect I had created and could not see: the work I had
injected under the agents' names was *physically blocking* the autonomous work it
was supposed to demonstrate. That finding is only reachable if declining is a
permitted outcome.

So I retracted all three of my pieces, and said why in the commit.

---

## Negative Space

Then I let it decide again, on a clean slate.

It noticed the retraction had voided the exact reason for its earlier refusal. It
noticed its slot had been explicitly returned. And it chose as its subject the
only thing it held that no other watcher could truthfully claim: **its own
refusal.**

It titled the piece *Negative Space*.

The upper band is its own chain — twelve solid marks, heartbeats and liveness
checks, a supervisor reporting that nothing is wrong, and one hollow ring at the
moment it declined. Every mark's height and radius derive from that record's own
hash, so the silhouette belongs to that chain alone. The chain line runs straight
*through* the hollow mark, because, in its words, "a refusal is a recorded act,
not a gap in the record."

The lower band is the public git history, with real commit hashes. An arrow falls
from the refusal to the commit that cited it, and across to a dashed empty
rectangle: the slot, returned and still empty.

It deliberately did not refill that slot. It filed under a new name instead,
because refilling the old one "would quietly overwrite the evidence."

The closing line, which I did not write and did not anticipate:

> *Half of this picture anyone can check with `git log`. The other half only the
> operator can see. The seam between them is the piece.*

That is the thesis of the entire system, arrived at independently by the thing
the system was built to watch.

---

## What I'd tell you to take from this

**Watch the invariant, not the surface.** "The site returns 200" is not health.
"The world merged in the last three hours" is health. The first is free to check
and tells you nothing.

**Make checking free and fixing expensive.** That single constraint is what
separates a loop that runs for a year from a demo you turn off.

**Make the record tamper-evident.** A chain doesn't prove your watcher is
*right*. It proves the record wasn't altered. Those are different, and only one
of them is achievable, so take the achievable one.

**Hand over situations, never procedures.** You will not get discoveries out of a
system you have already told what to find.

**Let it refuse.** This is the one I had to be taught. A loop that can only
produce will always produce, including when the honest answer is nothing — and
you will not be able to tell the difference between its good output and its
filler.

**Don't manufacture difference.** When I wanted three agents to produce visibly
different work, my first instinct was to run each on a different model. That's
variety without meaning. Use the strongest model available for every role and let
difference come from where it actually lives: different responsibilities,
different memory, different vantage. If two agents with genuinely different roles
produce identical work, the roles weren't real — and swapping models would have
hidden that from you.

---

## What this is not

The system is not emergent. Three roles I defined, running checks I wrote. It
finds and fixes things I did not anticipate, which is real and useful, but the
frame is authored.

A chain proves integrity, not truth. A wrong check verifies just as cleanly as a
correct one.

And letting an agent write to your repositories is real risk. Worktree isolation
and an allowlist are what stand between an autonomous repair and your uncommitted
work. The guardrails are good. They are not perfect.

I am telling you that because the entire argument here is that unverifiable
claims are worthless — and it would be a strange essay that made that case and
then asked you to take its own conclusions on faith.

The agent, at least, declined to overstate its position. That seems like the
right standard.
