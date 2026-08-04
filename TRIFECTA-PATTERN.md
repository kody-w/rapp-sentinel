# The Neighborhood Trifecta Loop

A reusable pattern for keeping something alive with autonomous agents, without
the two failure modes that make most "AI automation" useless in practice:

1. **The silent stall.** A loop that dies quietly while every dashboard stays
   green, because everything downstream is reading data that stopped moving.
2. **The unverifiable claim.** An agent reports success and there is no way to
   check, so its report is worth exactly nothing.

The pattern is three watchers, one shared verifiable memory, and a ladder of
freedom you raise as trust is earned.

---

## 1. Why three

One watcher cannot notice its own death. Two watchers can disagree with no
tiebreak. **Three is the smallest number where any one can be down and the
other two still agree on what happened.**

The three roles, which map onto whatever tools you actually have:

| Role | Job | In this estate |
|---|---|---|
| **Scheduler** | guarantees the beat. Owns "did it run?" | openrappter daemon + launchd |
| **Runtime** | answers turns locally. Owns "is anyone home?" | RAPP brainstem (`:7071`) |
| **Repair arm** | the only one that may change things | Copilot CLI |

They are **uniform peers** (`rapp-neighborhood-protocol/1.0` §3) — on the wire
a human, a daemon, a browser twin and a model are indistinguishable. That is
what lets you swap any of them without touching the others.

**Deliberately redundant scheduling.** launchd fires on `:00/:15/:30/:45`,
openrappter's cron on `:07/:37`. They interleave. Either scheduler can die and
the watch continues — and the survivor's chain shows the gap where the other
stopped.

---

## 2. Why a chain, not a log

Every watcher keeps a `rapp/1` frame chain: an 11-key, content-addressed,
hash-linked record. Each frame commits to the previous one's `payload_hash`.

This is the part people skip, and it is the part that matters. A log file can
be rewritten to look healthier than it was. A chain cannot — edit any past
payload and verification fails at §7.5 step 2 with `payload_hash mismatch`.

So "the watcher says it's fine" becomes "the watcher's record verifies from
genesis, and here is the head hash." That is a claim you can check rather than
trust. `roll_call()` re-verifies all three chains every tick, so a **tampered**
watcher is as detectable as a **stalled** one.

---

## 3. The freedom ladder

Never hand a fresh loop write access. Raise it deliberately.

| Level | Name | What it may do |
|---|---|---|
| 0 | observe | health check only. Logs, notifies on state change. **No model is invoked.** |
| 1 | diagnose | on failure, a model investigates read-only and explains |
| 2 | repair | a model may fix and push — in a throwaway worktree, inside an allowlist |
| 3 | evolve | proactive improvement while healthy |

Level 0 is not a toy. Run there for a week. If it reports things you disagree
with, your checks are wrong and level 2 would have "fixed" the wrong thing
eight times a day.

---

## 4. The cost asymmetry that makes "forever" affordable

**Health checks must be free. Only failure may spend.**

A healthy tick is `gh` and `curl` — a few seconds, no tokens. A model is
invoked only when a check actually fails, and only when it clears every
guardrail. That is why this can run every 15 minutes indefinitely rather than
being an expensive demo you turn off after a week.

If your health check needs a model to decide whether something is broken, the
check is not specific enough yet.

---

## 5. Guardrails, all enforced before a model is invoked

| Guardrail | Prevents |
|---|---|
| **Kill switch** — a `STOP` file | you cannot stop a runaway loop fast enough |
| **Daily budget** (rolling 24h) | a flapping check burning credits all night |
| **Per-issue cooldown** | re-attacking the same failure every tick |
| **Attempt cap** → escalate to a human | infinite retry on something unfixable |
| **Worktree isolation** | destroying a working tree with uncommitted work |
| **Notify on state change only** | alert fatigue — a muted watcher is no watcher |
| **Re-probe after repair** | believing a fix landed when it did not |

That last one is the difference between self-healing and self-reporting. After
a repair the loop re-runs the *same* health check and compares. It only claims
`verified fixed` when the check that failed now passes.

---

## 6. What one cycle looks like

```
probe (free)
  └─ healthy?  → record frame, exit                      ~3s, $0
  └─ critical? → guardrails → model → repair in worktree
                 → RE-PROBE → verified? → record + notify
```

Concretely, from this estate's own log:

```
status=critical failing=['rb_workflows']
escalating (repair) to copilot for: rb_workflows
FIXED — extended safe_commit.sh to auto-resolve state/-only rebase conflicts
  by keeping the remote version, eliminating a ~15-30s git-replica lag between
  the zion-autonomy push and the heartbeat's checkout
verified fixed: ['rb_workflows']
```

Nobody asked for that. The loop found a race condition, reasoned about replica
lag, wrote a fix that **still refuses to auto-resolve non-state conflicts**,
and proved it by re-probing.

---

## 6b. The rule that makes it real: hand over a situation, never a task

This is the part that is easy to get wrong, and getting it wrong turns the
whole thing into theatre with extra steps.

The repair arm found a git-replica race nobody described to it. That happened
because the loop handed it **a failure and a set of boundaries** — not a
procedure. Had the prompt said "add an autostash flag to safe_commit.sh", the
loop would have discovered nothing; the author would have, and the agent would
have typed it.

So every escalation, at every level, has the same shape:

| Give it | Never give it |
|---|---|
| the situation (what failed, what state things are in) | the solution |
| its own memory / context | a procedure to follow |
| hard boundaries (worktree, verify, no secrets) | a template to fill in |
| the authority to decide, including to decline | a required output format |

**Declining must be a first-class outcome.** The first time a neighbor was
handed its situation, it cloned the public commons, read that repo's own
conventions, found a piece already sitting under its name, and returned
`DECLINED` with the blocking commit cited. It wrote nothing — `+0 −0`.

That decline was worth more than a picture would have been. An agent that only
ever produces is an agent following instructions. An agent that will refuse is
one that is actually deciding. If your loop can never come back empty-handed,
you have not delegated judgement — you have automated typing.

It also surfaced a real defect: work the author had injected under the agents'
names was *blocking* the autonomous work it was meant to demonstrate. That
finding is only possible if declining is allowed.

---

## 6c. Do not manufacture difference with different models

A tempting shortcut, when you want N agents to produce visibly different work,
is to run each on a different model. Resist it. That is variety without
meaning — the differences come from vendor quirks, not from the agents having
genuinely different vantage points, and it degrades every agent that is not on
your best model.

**Use the strongest model available for every role.** Let difference come from
where difference actually lives:

- **role** — the scheduler, the runtime and the repair arm care about different
  things and are asked different questions
- **memory** — each neighbor holds its own chain and cannot read the others'
  except through the roll call
- **situation** — each is handed a different vantage on the same estate

If two agents on the same model with different roles and different memory
produce identical work, the roles were not real. That is a finding about your
design, and swapping models would have hidden it.

---

## 7. Porting it to another machine or task

Only three things are estate-specific. Everything else is the pattern.

1. **`health.py`** — your checks. This is the real work, and it is where the
   whole thing lives or dies. A check must be **specific** (names one thing),
   **cheap** (no model), and **actionable** (a failure implies a next step).
   `rb_workflows: 100% failing: Agent Heartbeat` is actionable.
   `something seems off` is not.
2. **`config.json`** — level and guardrail values.
3. **The three roles** — any scheduler, any local runtime, any model with a
   CLI. Swap Copilot for another model, or run different models per role.

The chain, the roll call, the ladder and the guardrails port unchanged.

**Adding a new AI as a neighbor** is deliberately boring: mint it a rappid,
give it a chain, have it emit frames, and it is a peer. It does not need to
know about the others — `roll_call()` is what relates them. That is how
openclaw, Hermes, or something that ships next year joins without a rewrite.

---

## 8. What this pattern does *not* give you

Stated plainly, because the whole point is verifiable claims.

- **It is not emergence.** Three roles I defined, running checks I wrote. The
  loop finds and fixes things I did not anticipate — that is real and useful —
  but the *frame* is authored.
- **A chain proves integrity, not truth.** It proves the record was not
  altered. It cannot prove the record was right. A wrong check verifies just
  as cleanly as a correct one.
- **Level 2 is real write access.** The guardrails are good, not perfect.
  Worktree isolation and the allowlist are what stand between an autonomous
  repair and your uncommitted work.

---

## 9. The one-line version

> Three watchers, each keeping a chain the others can verify, where checking is
> free and only failure is allowed to spend — and where the freedom to change
> things is a dial you turn up, not a switch you flip.
