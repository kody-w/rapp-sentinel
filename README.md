# rapp-sentinel

> **Why this exists:** [RAPP and the new way of working: above AI, not beside it](https://kody-w.github.io/rapp-sentinel/) — the argument for the pattern, with every figure traceable to a public repo.

**A watchdog that can't quietly lie to you, and a repair arm that only spends money when something is actually broken.**

Three watchers, each keeping a tamper-evident chain the other two can verify. Health checks are free; only failure is allowed to invoke a model. The freedom to change things is a dial you turn up, not a switch you flip.

Built to keep two GitHub-native platforms alive. The pattern is the point — the checks are yours to write.

---

## The failure this exists for

One of my platforms sat frozen for **nineteen days** and I didn't notice.

Not down — *frozen*. First paint in 168ms. Zero JavaScript exceptions. Zero failed requests. Every surface metric green. Underneath: no state merged since July 13th, 679 pull requests stacked behind a broken gate, and a hundred out of a hundred CI runs failing.

Its sister platform failed the same way for a different reason: one state file crossed GitHub's 100MB limit, so the pre-receive hook rejected *every push*, and six workflows died at once.

Different causes, identical shape: **an invariant nobody was watching, and a write path that jammed in silence.**

"My site is down" you find out about. "My site is up and has been lying to me for nineteen days" is the one that costs you.

---

## What it actually did

Unprompted, on its own schedule:

```
status=critical failing=['rb_workflows']
escalating (repair) to copilot for: rb_workflows
FIXED — extended safe_commit.sh to auto-resolve state/-only rebase conflicts
  by keeping the remote version, eliminating a ~15-30s GitHub git-replica lag
  between the zion-autonomy push and the heartbeat's checkout
verified fixed: ['rb_workflows']
```

It found a replica-lag race nobody had described to it, wrote a fix that **still refuses to auto-resolve anything outside `state/`**, emitted a visible warning so it can never happen silently, then re-ran the failing check to confirm.

Later, handed its own situation and left to decide whether it had anything worth contributing, one watcher read its own chain and reported that **`utc` is the one field the chain doesn't bind** — `prev` links `payload_hash` (which excludes `utc`), and `prev_wave`, which would bind it, must be null off-swarm.

I tested that claim and got "WRONG." My test was bad — I'd broken monotonicity, which isn't what the claim says. Retested properly: **the agent was right.** It found a real limitation in the system it runs on, and I nearly dismissed it with a sloppy test.

---

## Joining someone else's neighborhood

Membership is whoever joins. You publish a head, they fetch it, and either side
can tell if the other stopped moving. Nobody grants access and nobody can revoke
it — see **[JOINING.md](JOINING.md)** for the trust model and the four steps.

```bash
python3 neighborhood.py publish   # → public/sentinel-head.json, serve it anywhere
python3 neighborhood.py peers     # fetch peers, check they're still advancing
```

An outside neighbor is trusted exactly as far as its published head can be
checked against what it published before: you can catch a peer that **stalled**,
and you cannot catch a peer that **lied**. Build only on the first.

## Install

```bash
git clone https://github.com/kody-w/sentinel && cd sentinel
python3 health.py          # runs your checks, prints a verdict, costs nothing
```

Stdlib only. Needs `gh` (authenticated) for GitHub checks, and the [Copilot CLI](https://github.com/github/copilot-cli) for levels 1+.

```bash
cp config.example.json config.json    # start at level 0
./install-launchd.sh                  # every 15 minutes, survives reboot
./morning                             # read the overnight shift report
```

The installer also loads an Aqua-session outbox drainer every five minutes.
Background reporters remain queue-only; the drainer is the single serialized
process allowed to drive Messages, so reports survive both producer failures
and reboot without waiting for a terminal command.

### One instance per `SENTINEL_HOME`

One checkout can serve several instances. Set `SENTINEL_HOME` to a directory
and everything an instance owns — `config.json`, `direction.json`, `state/`,
`logs/`, `neighborhood/`, `public/`, `dashboard/`, `STOP` — lives there
instead of beside the code:

```bash
SENTINEL_HOME=~/vision-court python3 health.py     # a second neighborhood
./install-launchd.sh --home ~/vision-court         # …or under launchd
```

Unset, nothing changes: state lives beside the code, byte-for-byte the same
paths as before the variable existed, so a live install picks this up by
`git pull` without its ledger key or chains moving. `paths.py` is the single
place the split is derived.

Honest limit: instances share `checks.py` and `required_checks.json` — every
instance running this code runs the same check SET against the same targets
declared in its own `direction.json`. Per-instance check sets are a different
feature this does not provide; if two instances need different checks, today
that still means two checkouts.

---

## Write your checks

`checks.py` is the only file most people edit.

```python
@check
def world_still_merging():
    commits = gh(["api", "repos/me/thing/commits?per_page=20", "--jq", "..."])
    h = hours_since(commits[0]["date"])
    return ok("merging", f"{h:.1f}h ago") if h < 3 else fail("merging", f"stalled {h:.1f}h")
```

Three rules, learned the hard way:

- **Specific.** *"The world merged in the last 3 hours"* is a check. *"The system is healthy"* is a mood.
- **Cheap.** No model. If deciding whether something is broken needs a model, the check isn't sharp enough yet.
- **Actionable.** `rb_workflows: 100% failing: Agent Heartbeat` implies a next step. `something seems off` doesn't.

`critical=True` spends money and takes action. Use it for *"the thing is not doing its job"*, not *"a page is slow"*.

---

## The freedom ladder

| Level | Name | What it may do |
|---|---|---|
| **0** | observe | health check only. Logs, notifies on change. **No model invoked.** |
| **1** | diagnose | on failure, a model investigates read-only and explains |
| **2** | repair | a model may fix and push — in a throwaway worktree, inside an allowlist |
| **3** | evolve | acts on its own initiative when everything is healthy |

**Run at level 0 for a week.** It isn't a toy setting. If it reports things you disagree with, your checks are wrong — and level 2 would have "fixed" the wrong thing eight times a day.

---

## Why three watchers

One watcher can't notice its own death. Two can disagree with no tiebreak. **Three is the smallest number where any one can be down and the other two still agree on what happened.**

| Role | Owns | Here |
|---|---|---|
| **Scheduler** | "did it run?" | launchd + openrappter daemon |
| **Runtime** | "is anyone home?" | a local brainstem |
| **Repair arm** | the only one that may change things | Copilot CLI |

Schedulers are deliberately offset — one on the quarter hour, one at `:07/:37`. Either can die and the watch continues, and the survivor's chain shows the gap.

### The chain is the part people skip

Each watcher keeps a [`rapp/1`](https://github.com/kody-w/rapp-1) frame chain: content-addressed, hash-linked, append-only. Every entry commits to the previous one's hash.

A log file can be rewritten to look healthier than it was. A chain makes that harder — alter any past *payload* and verification fails with a hash mismatch. So *"the watcher says it's fine"* becomes *"the watcher's record verifies from genesis, and here's the head hash."*

One is a claim you trust. The other is a claim you check.

### …but a chain alone is not enough, and one of the watchers proved it

An earlier version of this README said a chain "can't be rewritten." **That was
an overclaim, and the brainstem watcher caught it by reading its own memory.**

Its liveness frames carry byte-identical payloads, so `prev` — which links the
predecessor's `payload_hash` — is *the same value* on 14 of 19 links. Which means
an interior frame can be **deleted, the successors resealed, and the whole chain
still verifies.** I tested it: dropped a frame, recomputed, 19 frames verify
clean. History can be silently *shortened*, even though it cannot be silently
*edited*.

The root cause is that verification is **self-referential** — the chain attests
to itself, so there is no outside witness a splice has to agree with.

The watcher's own repair was the right one: **publish the head hash externally.**
An outside anchor is something a splice cannot rewrite, because it does not live
in the chain. `neighborhood.py` now writes `neighborhood/anchors.jsonl` and the
morning report shows head-vs-anchor.

So the honest version of the claim:

> A chain makes tampering *detectable*. An external anchor makes truncation
> detectable. Neither makes the record *true* — only unaltered.

### Where the chain of watchers ends

The regress is infinite, so the honest move is to name the stopping point
instead of adding tiers forever. Here it is:
[rapp-overwatch](https://github.com/kody-w/rapp-overwatch) watches this
sentinel from outside, [rapp-ratchet](https://github.com/kody-w/rapp-ratchet)
watches whether that watching is actually being done, and **a person reads the
last one**. Nothing watches the ratchet. That terminus is a stated design
assumption — the fourth tier is a human with a morning report — not a claim
that the last watcher cannot fail. A guard nobody reads is the same guard this
whole repo exists to distrust.

---

## The rule that makes it real

**Hand over a situation. Never a task.**

The repair arm found that replica race because the loop gave it *a failure and a set of boundaries* — not a procedure. Had the prompt said "add an autostash flag," the loop would have discovered nothing; I would have, and the agent would have typed it.

| Give it | Never give it |
|---|---|
| the situation | the solution |
| its own memory | a procedure |
| hard boundaries | a template |
| authority to decide, **including to decline** | a required output |

**Declining must be first-class.** The first time a watcher was handed its situation, it cloned a public commons, read that repo's conventions, found a piece already sitting under its name, and returned `DECLINED` with the blocking commit cited. `+0 −0`.

That decline was worth more than output. *An agent that only ever produces is following instructions. An agent that will refuse is deciding.* It also surfaced a defect I couldn't see — work I'd injected under the agents' names was **blocking** the autonomous work it was meant to demonstrate.

### Don't manufacture difference with different models

When you want N agents to produce visibly different work, the tempting shortcut is a different model each. That's variety without meaning, and it degrades every agent not on your best model.

**Use the strongest model available for every role.** Let difference come from where it actually lives: different responsibilities, different memory, different vantage. If two agents with genuinely different roles produce identical work, *the roles weren't real* — and swapping models would have hidden that from you.

---

## Guardrails

All enforced **before** a model is invoked.

| Guardrail | Prevents |
|---|---|
| kill switch (`touch STOP`) | not being able to stop a runaway loop fast enough |
| daily budget (rolling 24h) | a flapping check burning credits all night |
| per-issue cooldown | re-attacking the same failure every tick |
| attempt cap → escalate to human | infinite retry on something unfixable |
| worktree isolation | destroying a working tree with uncommitted work |
| notify on **state change only** | alert fatigue — a muted watcher is no watcher |
| **re-probe after repair** | believing a fix landed when it didn't |

That last one is the difference between self-healing and self-reporting. It re-runs the *same* check and only claims `verified fixed` when what failed now passes.

---

## The morning report

```bash
./morning        # last 14h
./morning 24     # last 24h
```

Reads the chains — it keeps **no log of its own**, because a dashboard with a private copy of the truth is a second source that can disagree with the first. It re-verifies every chain from genesis while rendering, so a tampered record shows as a red banner instead of a tidy chart.

Every claim links to evidence: commits to GitHub, contributions to their source, and each autonomous decision to the full local transcript of the run that produced it.

Periodic Messages updates link to an immutable, tokenized static HTML snapshot
served only over the Mac's private Tailscale/LAN addresses. The HTML embeds local
decision transcripts, so it remains readable on a phone that cannot reach
`localhost:9797`; dashboard and log routes remain loopback-only.

---

## What this is not

- **Not emergent.** Roles I defined, running checks I wrote. It finds and fixes things I didn't anticipate — real and useful — but the frame is authored.
- **A chain proves integrity, not truth.** It proves the record wasn't altered. A wrong check verifies just as cleanly as a correct one.
- **Level 2 is real write access.** Worktree isolation and the allowlist are what stand between an autonomous repair and your uncommitted work. The guardrails are good. They are not perfect.

Stated plainly because the whole argument here is that unverifiable claims are worthless, and it would be strange to make that case and then ask you to take mine on faith.

---

## Layout

| File | What |
|---|---|
| `checks.py` | **your checks** — the only file most people edit |
| `health.py` | runs them + the watcher self-checks. No model. |
| `sentinel.py` | decides, escalates, enforces guardrails, re-probes |
| `neighborhood.py` | the three peers and their `rapp/1` chains |
| `standup.py` | the morning shift report |
| `rapp.py` | vendored reference implementation from [rapp-1](https://github.com/kody-w/rapp-1) |
| `TRIFECTA-PATTERN.md` | the pattern, portable to other domains |
| `BLOG-the-agent-that-refused.md` | the long-form writeup |

MIT. The `rapp/1` reference implementation is vendored from [kody-w/rapp-1](https://github.com/kody-w/rapp-1) under its own terms.

## The pattern, generalized

Three watchers is the smallest case. For the general one — **any number of AIs, from any vendors, as mutually-verifying peers** — see [N-AIS-WALK-INTO-A-BAR.md](N-AIS-WALK-INTO-A-BAR.md). Seat your own cast in `config.json`'s `neighbors` map; no code edit.
