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

## Growing it from the hub

Checks you did not write live on the **[RAPP Sentinel Hub](https://kody-w.github.io/rapp-sentinel-hub/)** —
single-file sentinels (`rapp-sentinel/1.0`) posted the way RAR posts `agent.py`s. Drop one into
`hub/` and the next tick runs it; its check ids join the required set so it can never silently
stop. Trust is a dial (`hub.critical_allowed` in config.json), not a switch.

```bash
curl -O https://raw.githubusercontent.com/kody-w/rapp-sentinel-hub/main/sentinel_sdk.py
python3 sentinel_sdk.py install @kody-w/output_moving_sentinel --home ~/rapp-sentinel
python3 health.py | grep -A3 hub:      # produced_by=hub:@kody-w/output_moving_sentinel
```

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

Proactive art (level 3) can run as its own job so it never blocks the
15-minute health tick — opt-in, see [the evolve worker](#level-3-in-its-own-job-the-evolve-worker):

```bash
./install-launchd.sh --with-evolve-worker          # or --home DIR --with-evolve-worker
```

### One instance per `SENTINEL_HOME`

One checkout can serve several instances. Set `SENTINEL_HOME` to a directory
and everything an instance owns — `config.json`, `direction.json`, `state/`,
`logs/`, `neighborhood/`, `public/`, `dashboard/`, `STOP` — lives there
instead of beside the code:

```bash
SENTINEL_HOME=~/vision-court python3 health.py     # a second neighborhood
./install-launchd.sh --home ~/vision-court         # …or under launchd
```

`--home` stamps `SENTINEL_HOME` into every job it loads, including
`com.rapp.evolve-worker`, so the art arm and the tick always serve the same
instance.

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

Level 3 can be specialized without weakening repair safety. `instance_name`
brands reports and chain metadata, `evolve_brief` supplies a standing
structured directive, and `evolve_interval_hours` sets a recurring global
cadence. Evolution has its own rolling daily budget and does not inherit the
repair arm's lifetime attempt cap. Set `repair_enabled: false` when an instance
may contribute to its allowlisted commons but must only diagnose watched
platforms.

### Level 3 in its own job: the evolve worker

launchd **serialises** a `StartInterval` job. A 15-30 minute model call inside
the 15-minute tick is therefore 15-30 minutes with nobody measuring the estate,
and the next tick does not start early to make up for it. So proactive art can
move out of the tick entirely:

```jsonc
"evolve_worker": {
  "enabled": true,
  "repo": "kody-w/public-art-collective",
  "degraded_allowlist": ["w_openrappter_spin"]
}
```

```bash
./install-launchd.sh --with-evolve-worker        # or just set enabled:true and rerun
python3 evolve_worker.py --dry-run               # what would it decide right now?
```

Enabled, the tick logs `evolve delegated to evolve_worker.py` and spends **no**
model on art — while still diagnosing or repairing a critical failure on the
same tick, still honouring `repair_enabled`. Absent or `false`, nothing about
an existing install changes.

The worker (`evolve_worker.py`, `com.rapp.evolve-worker`, every 30 min):

| Guard | What it means |
|---|---|
| nonblocking `flock` | two passes never overlap; a killed pass leaves no stale lock |
| global cadence + rolling daily budget | shared across roles, in its own ledger, never repair's |
| fail-closed ledgers | a corrupt or truncated history **stops the pass**; it is never read as "no spend" |
| health at start, before the push, before the merge | any **critical** check aborts; degraded proceeds only when *every* failing id is in `degraded_allowlist` — `evolve_on_degraded` is ignored here, and `alert_delivery` / `health_runtime` refuse to be allowlisted at all |
| confined model | **no `--allow-all`**: the maker gets bounded file tools rooted at `--add-dir` and no shell, git, gh, MCP or network tool; built-in MCPs, custom instructions, `BASH_ENV`, the system temp dir, remote control and auto-update are off; HOME/XDG/TMPDIR/gh/git config live in a runtime directory the tools cannot reach, behind a strict env allowlist; inference auth is one `--secret-env-vars` variable |
| sanitized staging | the maker never sees a repository — its root holds only its read context and a **precreated** `out/submission/`, with no `.git` and no clone metadata. It writes three files into paths that already exist (it has file tools and no shell, so it cannot create a directory); the slug lives in `meta.json`, and the controller materialises `submissions/<slug>/` in its own private clone from the gated bytes |
| whole-tree staging check | the controller hashes the entire prepared staging tree before the model runs, and afterwards every baseline path must be byte- and mode-identical, with the only new paths allowed being `out/submission/meta.json`, one `piece.<ext>` and `state-out.json` — no new directories, no hidden files, no drafts, no rewritten context |
| pinned git binary | git is resolved to a trusted absolute path (`/usr/bin/git`, validated as a regular, executable, non-group/world-writable file under a system root — symlinks resolved first), never through PATH. `PATH` itself is **set, not inherited**: trusted system directories only, so an attacker's directory holding a fake `git` cannot be consulted |
| config-isolated git | **every** controller git call runs in an environment built from nothing: an allowlist carries only locale and existing cert paths — so `GIT_EXEC_PATH`, `LD_PRELOAD`, `DYLD_INSERT_LIBRARIES`, `GIT_SSH_COMMAND`, `GIT_CONFIG_PARAMETERS`, proxies and the rest cannot arrive at all. The controller then sets an isolated HOME/XDG/TMPDIR, no system config, a global config it writes (at most a validated credential helper), no prompts, and only https/file protocols. The clone is built by `init` + configured remote + fetch — never `git clone`, which reads global config *before* it resolves the URL, so a `url.<attacker>.insteadOf` rewrite produced a flawless clone of the wrong repository |
| validated repo URL | the canonical repo is checked for shape before git sees it: https on an allowed host with plain `owner/name` segments, or an explicit existing absolute path for local test repos. `ext::`, `ssh://`, `git://`, embedded credentials and dash-prefixed "hosts" are refused |
| remote integrity | **every** network git call — fetch, push, and branch cleanup — goes through one chokepoint that first rejects any `remote.origin.pushurl`, unexpected local config (`core.hooksPath`, `url.*.insteadOf`, `credential.*`, …), `objects/info/alternates` and executable hooks, then resets and re-reads origin's fetch and push URLs |
| honest cleanup | if the clone no longer verifies, an abandoned branch is deleted from the canonical URL through a fresh repository with global/system git config neutralised, then confirmed with `ls-remote` — an injected remote is never contacted, and a real branch is never left orphaned |
| tracked process tree | maker and children each get their own process group; a timeout or SIGTERM kills the tree, then the workspace is deleted, and only then is the lock released |
| verified index | the git **index** is checked before the push — exactly two `100644` blobs whose bytes are the bytes that passed the gate |
| reconciliation | a cycle killed between `gh pr merge` and the ledger write is finished (or its PR closed) on the next pass, from the PR and `origin/main` |
| continuity that migrates | the creative ledger's current cycle is read canonically — `cycle`, else `last_cycle`, else a validated `cycles[]` — and fields that disagree fail closed rather than guessing. History is a strictly ordered contiguous run: a prefix from cycle 1, or a bounded tail that must carry an explicit counter, be exactly `creative_history_limit` long and end at that counter. Reader and writer share one constant, so the state written after cycle 50 is state the worker can still read. A rejected attempt is a failed spend that leaves public continuity alone |
| liveness | every pass writes a heartbeat, and `w_evolve_worker` reports enabled-but-never-loaded or stale |
| bounded sub-sentinel fan-out | optional: 3-5 read-only children in separate processes with no repo, no token and no ability to spawn children, aggregated deterministically into exactly 10 finalists — see below |
| deterministic gate | exactly **two root-level regular files** (`lstat`: no symlink, no hardlink, no fifo, not executable, nothing nested) in one new `submissions/<slug>/`, valid slug/schema/kind/extension/license, piece ≤ 50 KB, SVG parses with no script, no `on*` handler and no external reference (including CSS), and `_dada_cycle` proving 1-5 rounds of **exactly 10** scored candidates whose round one reproduces the finalist records by digest |
| one repository, two names | the configured `repo` is normalised once: a validated transport URL for git, and `[HOST/]OWNER/REPO` for gh. `owner/name`, a full `https://` URL and a `.git` suffix all describe the same repository — before this, a URL config passed the auth preflight and then died at `gh pr create --repo https://…` |
| one controller per pass | the pinned git and gh binaries, the sanitized git environment with its generated credential helper, the gh environment and both repository forms are resolved **once** and threaded through every call — preflight, clone, push, PR, merge, cleanup and reconciliation. No module-level cache, so a pass cannot inherit a choice made by an earlier one |
| publish auth, checked first | before any child process, the controller asks GitHub whether this account may push to the configured repo and asks `git credential fill` whether credentials resolve — lengths logged, never values. Its credential helper is generated, not inherited: a fixed `gh auth git-credential` with the real HOME and validated gh config given to that process alone, never to git's environment and never to a model's |
| child replies are typed | children run with `--output-format=json` and are read only from the final `assistant.message` event — never reasoning, which contains the same JSON. One unparseable reply earns exactly one format-repair process if the deadline and process cap allow (`format_repair_attempts` is 0 or 1 — anything else is a configuration error, not a clamp); it is debited as a spend, recorded, and every attempt's transcript is kept outside the disposable workspace |
| controller-owned publish | the branch, commit, PR, PR **file scope as GitHub reports it**, squash merge, and the re-read of `origin/main` and the merge commit afterwards are all done by code |
| dual public deployment | optional `rapp_vision` mirrors the exact gated bytes into a RAPP Vision channel after the canonical collective merge; success stays pending until both GitHub Pages experiences answer, and reconciliation retries without spending another model |
| Azure visual studio | optional `azure_image` turns a maker-authored visual brief into a local GPT Image PNG, attaches the actual pixels to a tool-less Copilot multimodal art director, regenerates rejected images, archives the accepted file on-device, and publishes only after the score clears the configured bar |
| honest outcomes | only a re-read merge sends a 🎨; a timeout, failure, rejection or decline is recorded as what it was |
| one text per deployment | a verified dual deployment sends exactly one iMessage: title, one sentence, the Public Art Collective Pages experience, and the RAPP Vision watch experience — no private report or LAN URL |

`SENTINEL_RESULT: CONTRIBUTED` is a claim, not a receipt. The creative ledger,
the cadence history, the chain frame and the notification move only after the
merge commit has been fetched back and the merged bytes match the bytes that
passed the gate. The temporary clone is removed on every path out.

Honest limit: the deterministic gate encodes the submission protocol *as it
was read* — a repo that changes its protocol needs the gate updated with it,
on purpose, so a PR cannot relax the rules that judge it.

#### The one text a dual deployment earns

A verified final deployment — and *only* that deployment — sends one iMessage to
`report_number` (falling back to `notify_handle`):

```
🎨 Dada Collective: “Nine Sworn Assurances” is merged.

Nine consecutive attestations so identical that each one's prev equals its own payload_hash.

Public Art Collective: https://kody-w.github.io/public-art-collective/submissions/nine-sworn-assurances/piece.svg
RAPP Vision: https://kody-w.github.io/rapp-vision/#/watch/nine-sworn-assurances
```

- **Public Art Collective** is the GitHub Pages copy of the canonical piece.
  **RAPP Vision** is its watch route in the actual player. Both are derived
  deterministically and probed before the ledger can move.
- **Concept** is one sentence taken from the piece's own record: `_concept`,
  else the first sentence of `_artist_statement`, else the premise of the
  candidate that actually won its cycle. Nothing is summarised on the piece's
  behalf, because a summary nobody wrote is a claim nobody made.
- `notification_mode: "art-only"` suppresses nightwatch, health transitions,
  diagnostics, and private static-report links while retaining this one final
  deployment receipt.

Nothing else sends it. `SENTINEL_RESULT: CONTRIBUTED` does not; a PR that was
opened does not; an abort after the PR does not. The message is built inside
the same branch that already re-read the merge commit from `origin/main` and
compared the merged bytes to the gated bytes — so if the text arrives, the art
is live at the URL in it.

Both Pages deployments are probed after their merges and before the message is
queued. Pages publishes on its own schedule, so a bounded retry runs first; if
either experience is not ready, the transaction remains pending and the next
worker pass retries deployment without spending another model. A triumphant
404 teaches the reader to ignore the next one.

It is queued through the ordinary outbox exactly once, like every other alert,
and the delivery layer classifies it: sent, unverified, or dead-lettered after
its retries. This worker never asserts delivery — it hands the message over and
stops talking. That matters because the **art arm refuses to run at all while
`alert_delivery` is failing**: unverified or dead-lettered alerts mean the
estate cannot reach a human, and a system that makes paintings while its own
alarm is broken is the exact silence this repo exists to refuse. Naming
`alert_delivery` in `degraded_allowlist` does not help; it is one of two ids
(with `health_runtime`) the gate refuses to silence.

Set `commons_repo` and `evolve_worker.rapp_vision` to enable the two-link final
receipt. A dual deployment with either URL missing does not send.

#### Sub-sentinels: a bounded fan-out before the maker

One model deciding alone what to make is one model's blind spot, published. So
a cycle can start by spending a small, capped cast of **sub-sentinels** —
separate `copilot` processes with separate contexts:

```jsonc
"fanout": {
  "enabled": true,
  "children": 3,          // max_children caps it; the maker counts too
  "max_depth": 1,         // children may never spawn children
  "daily_child_budget": 24
}
```

The default cast is three, and one of them exists to argue:

| Role | Wave | Job |
|---|---|---|
| `novelty-archaeologist` | 1 | reads every prior submission and vetoes repeats |
| `execution-designer` | 1 | thinks in the medium — can this actually be built under 50 KB? |
| `adversarial-verifier` | 2 | sees wave one's candidates and attacks them |

Roles, briefs and waves are configurable; a critic that never sees the
candidates would be decoration, which is why waves exist.

**Neither the maker nor a sub-sentinel can reach a repository.** The maker's
tool root is a staging directory holding its read context and an empty `out/`;
the controller's clone lives outside it, so there is no `.git` to write a
`pushurl` into. That was a real finding — with the clone inside the tool root a
probe wrote `clone/.git/probe.txt`, and a bounded repro set
`remote.origin.pushurl` and got the controller to push to an attacker's remote.
The fix is structural (no repository in reach), with the integrity check before
every git call as the second lock.

**A sub-sentinel cannot publish anything.** Not by instruction — by
construction: it gets no git clone (the parent hands it `prior.json`, read
from the clone), no GitHub token (`GH_TOKEN`, `GITHUB_TOKEN`, `SSH_AUTH_SOCK`
are stripped and `gh` is pointed at an empty config dir), its own temporary
workspace, its own process group, a hard timeout, and `RAPP_SENTINEL_DEPTH`
set — which the worker checks and refuses to run under, so a child cannot
start a cycle inside a cycle. The maker inherits the same marker.

Each child writes exactly one file: a `rapp-subsentinel-report/1.0` JSON
report, parsed strictly — bounded candidates, six numeric score dimensions,
bounded evidence and critique, no unknown keys, size-capped. Then the parent
aggregates **deterministically**: high-severity critiques are vetoes, medium
ones demote, and the survivors are ranked by mean score into **exactly ten
finalists** — which the gate then requires to *be* round one of the published
`_dada_cycle`. A maker that ignores its sub-sentinels is rejected.

Failure is explicit at every step. A child that times out, crashes, writes
nothing, or breaks one bound is a named failure carried into the ledger, the
chain frame and the maker's own prompt. Enough healthy children may continue —
but only if ten finalists still survive; nine is a failed cycle, never a
rounded-up ten. And if the fan-out is enabled but cannot run (spent child
credit, no slots), the cycle is **skipped**, not quietly made alone: "the
collective deliberated" and "one model had a think" are different claims.

Everything the fan-out produces is still just text. The parent controller
remains the only thing in this system that touches git or GitHub, after the
same deterministic gates and the same health re-probe.

Verified against the real CLI, not just asserted: `RAPP_CLI_PROBE=1 python3 -m
unittest test_worker_liveness` spends a few model calls to check that a
zero-tool child still answers, that the maker can write inside `--add-dir` but
reports no shell, and that it cannot create a file outside it. The argv/env
unit tests assert the strings; those probes assert reality.

Honest limits:

- Inference auth is a GitHub token, so an isolated HOME needs
  `COPILOT_GITHUB_TOKEN` (or `fanout.auth_env_var`) set for the job. Unset,
  with `isolated_home: true`, the cycle fails **explicitly** rather than
  falling back to the operator's real `~/.copilot` — that direction is
  deliberate.
- `sandbox_exec` (macOS `sandbox-exec` file-write confinement) is available
  and off by default: tool and path restriction is the mandatory layer, and a
  second belt that silently strangled inference would be worse than none. Its
  profile permits writes to exactly two roots — the sanitized staging
  directory and the sibling runtime directory holding the isolated
  HOME/XDG/TMPDIR — and never the controller's clone or the operator's real
  HOME. It therefore **requires `isolated_home`** (and so the inference
  credential): a shared `~/.copilot` is outside every writable root, and that
  combination is refused up front instead of failing later as a bare
  PermissionError.

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
| `BLOG-it-just-wakes-up.md` | the Principal, the morning memo, and the new way of working |

MIT. The `rapp/1` reference implementation is vendored from [kody-w/rapp-1](https://github.com/kody-w/rapp-1) under its own terms.

## The pattern, generalized

Three watchers is the smallest case. For the general one — **any number of AIs, from any vendors, as mutually-verifying peers** — see [N-AIS-WALK-INTO-A-BAR.md](N-AIS-WALK-INTO-A-BAR.md). Seat your own cast in `config.json`'s `neighbors` map; no code edit.

## The Principal — a sentinel that sits in on sentinels

`principal.py` is a watcher whose classrooms are *other* sentinels, anywhere on the tailnet
(local paths or ssh, bash or PowerShell hosts). Like a school principal it drops in unannounced
(random visits, everyone within a window), sits at the back, and grades the **teacher against the
job it declared** — not the world it watches: attendance (tick on schedule and *moving*), record
(chains verify, nothing truncated), the job (verdict covers `cares_about`; standing reds that
nobody decided; criticals), honesty (status agrees with the failing list; alerts not rotting
undelivered), discipline (budgets). That rubric is the deterministic floor; then the principal —
an AI in the neighborhood — reads the same evidence and writes its own note (grade, what works,
what fails, one change). Disagreement between the two is recorded, not resolved.

Every visit is a `principal.visited` frame on its own chain, a row in `state/observations.jsonl`,
a line on `state/report-card.json`, and `dashboard/principal.html`. It texts only when a grade
changes. Instance: `SENTINEL_HOME=~/.principal` with `classrooms` in config.json; launchd
template `com.rapp.principal.plist.template`. Proof: `prove_principal.py`.

**It heals, it explains, it reorients.** A grade nobody acts on is decoration, so the Principal does
three more things on a schedule of its own:

| command | schedule | what it does |
|---|---|---|
| `principal.py heal` | `:20`, `:50` | Fixes the two ways a sentinel goes useless on its own machine: a **hung** tick (a slow network read wedges the job — killed) and an **absent** one (launchd's `StartInterval` stops firing after sleep — rewritten to `StartCalendarInterval` and kickstarted). Then it **re-visits to prove the fix took**; a heal that isn't verified is a hope. |
| `principal.py relay` | `:05`, `:35` | Is the mouth for a classroom that can't speak. A sentinel whose iMessage send is blocked queues alerts forever — the finding exists, nobody hears it. The Principal moves those messages out under the classroom's own outbox lock, marks them `relayed_by`, and sends them on its own working channel. |
| `principal.py memo` | 07:15 daily | Writes the morning memo: every classroom's grade, what's chronic (the same finding three visits running), what has no classroom yet, and the decisions waiting on you. |

**Feedback, not just a letter.** Every visit now files its reasons *inside the classroom* at
`state/principal-feedback.json` (+ `.jsonl` history): the rubric breakdown of which points were lost
and why, the principal's note (what works, what fails, the one change), and — the important part —
a proposed **reorientation**. Most sentinels don't need new code; they need their declared job
pointed at the right thing. The proposal is written to `state/principal-reorientation.json` as a
diff against `direction.json` and is *never applied* unless the principal's config says
`"reorient": "apply"` — and even then the owner's `boundaries` are copied through untouched. A
machine with no sentinel is marked `pending_hatch`: an empty room is a decision to make, not a
teacher to fail every twenty minutes. The relay is a **two-phase hand-off**: it takes a classroom's queued alerts by renaming the queue
aside (atomic — the bytes are never in flight), holds them in its own locked outbox, and only then
tells the classroom to forget them. If anything fails in between, the messages are still in that
classroom's `outbox.relaying.jsonl` and the next relay recovers them. At-least-once, never
at-most-once: a duplicate alert is survivable, a destroyed finding is not. It also refuses to empty
a queue at all when the Principal's own mouth is unconfigured — carrying a message out of a
classroom and dropping it into a silent outbox is a deletion with extra steps.

Healing identifies a hung tick by **ownership, not by a name in someone's argv**: it asks launchd
which pid belongs to this job. These machines run two sentinels each, and matching on `sentinel.py`
would reach across homes and kill a healthy neighbour's tick. The bar for "hung" never falls below
the tick's own sanctioned ceiling (`SENTINEL_TICK_LIMIT`, 3000s), because killing a legitimate
evolve tick and reporting a successful heal is the Principal manufacturing the hang it claims to
have cured. Proof: `prove_principal_heal.py`.

**The hub runs only what it agreed to run.** `hub.py` imports and executes every file in `HOME/hub/`
on every tick; the only gate was a well-formed `__manifest__` — a check of *shape*, never of identity.
Nothing recorded which bytes were accepted, so a hub sentinel could be rewritten in place and the next
tick would execute the new code under the old name, silently. `w_hub_integrity` compares every installed
file against an explicit acceptance ledger (`state/hub-integrity.json`):

```bash
python3 hub.py integrity          # what is installed vs what was accepted
python3 hub.py accept <slug>      # record the bytes now on disk, after reading them
python3 hub.py forget <slug>      # drop the record of an uninstalled sentinel
```

Changed bytes under an accepted name is **critical** — that is the case that must never pass quietly.
An installed-but-never-accepted file is a **warn**, because that is the ordinary state between
installing and accepting and the loop should say so rather than break. Acceptance is never automatic:
an organism that accepts whatever it finds has recorded a habit, not a decision. The digest is taken
*before* the manifest is parsed, so a file rewritten into something that no longer even loads is still
reported as changed rather than downgraded to a load warning. Proof: `prove_hub_integrity.py`.

**Hatching on a Windows machine.** A neighbourhood only one OS can join is smaller than it claims.
The sentinel now runs on Windows: `filelock.py` replaces the `fcntl` import that made
`import outbox` fail outright, `paths.app_support()` stops building a macOS-shaped path inside a
Windows profile, and the world-writable guard is skipped where NTFS has no mode bits (it still
catches `/tmp` on POSIX). Schedule the tick with Task Scheduler instead of launchd:

```bat
schtasks /create /tn RAPPSentinel /tr "%USERPROFILE%\.rapp-sentinel\run-sentinel.cmd" /sc minute /mo 15 /f
```

Such a sentinel has **no mouth** — there is no `osascript` — so it queues its alerts and the
Principal carries them out over the tailnet on its next `relay`. That is not a degraded mode: a
finding delivered by a neighbour is delivered. Mac-only checks (auditing launchd jobs) report that
they *cannot* audit rather than passing, because a check that cannot run is not a pass.
