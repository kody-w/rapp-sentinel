# Check audit — what each check passes on today, and its positive-evidence inversion

*Audited 2026-08-16 against every `@check` in checks.py and every probe in
health.py, under §6d R3: a check should require positive evidence of the good
state, not the absence of an enumerated bad one. Issue #16's rule is why this
is a file and not a comment thread: findings do not transfer, ledgers do.*

Four rows marked **fixed** landed with this audit. The **P2** rows are the
recorded backlog — each future inversion lands as its own small PR with its
own `prove_*.py`, per house rule. Ids are never renamed.

| id | passes today on | inversion | status |
|---|---|---|---|
| `eco_sweep` | absence of a `conclusion==failure` streak; cancelled/skipped/timed-out streaks invisible in the generic sweep — the rb_wf_starved lesson never carried across | extend `workflows_currently_broken` to judge non-success streaks via `require_success()` (wide: multiplies gh calls across ~9 repos, needs its own API-budget design) | P2 |
| `rv_world_merging` | mostly positive (output age), but keys on the commit MESSAGE prefix `[state] apply PR` — a producer's receipt; a commit claiming a merge that touched nothing passes | assert the newest such commit actually changed `state/` paths | P2 |
| `rv_meaningful_activity` | freshness from `_meta.lastUpdate` self-stamp — the stamp-advances-while-content-freezes shape rb_rollup_coverage was built for | cross-check newest `messages[]` entry against the stamp | P2 |
| `rv_validation` | run conclusions — receipts (R1); a gate degrading to no-op and exiting 0 reads 10/10 passing | downstream evidence: validated PRs actually merge — join with rv_pr_queue throughput (needs design) | P2 |
| `rv_pr_queue` | "empty" passes forever; a queue steady at 39 with zero merges passes; the id claims DRAINING, the check enumerates pile-up shapes | positive throughput: PRs closed in the last 6h > 0 whenever the queue is non-empty | P2 |
| `rb_workflows` | pure receipt check — the #3 check | kept BY DESIGN as run-status; must be freshness-paired (`w_freshness_paired`, tranche 2) | by design |
| `rb_wf_starved` | had require-success bones, but the candidate filter `cancelled > 0` re-enumerated one bad colour: ok==0 with all runs skipped/timed_out was invisible — the enumerate-known-bad shape inside the very check written against it | candidates = enough decided runs with zero successes, colour-blind, via `require_success()`; all-skipped reported at WARN ("never ran the job — trigger or path-filter suspect") since a path-filtered workflow can legitimately skip forever | **fixed** |
| `rb_json_parses` | `except Exception: pass` on fetch — 4/5 unreachable + 1 parsed read as ok "1/5" | parsed < total → warn fail naming the unread files | **fixed** |
| `rb_content_moving` | producer's own `materialized_at` (receipt-adjacent), mitigated by the rb_rollup_coverage cross-check | keep; the pairing is the design | ok |
| `rb_derived_truth` | three enumerated contradictions; anything unnamed passes — inherent to consistency checks | documented as a bounded-claim check; the detail names what it compared | bounded |
| `rb_public_surface` | readable + parses, but a zero-agent roster returned ok — an empty world reading healthy; the id also oversells (proves readability, not joinability — join evidence is participate.py's smoke, #5). Second defect, 2026-08-17: the retry-exhausted branch kept `fail()`'s default severity, so an unreachable surface paged at CRITICAL while the observed zero-roster arm was warn — the weaker evidence carrying the louder alarm | n==0 → fail "readable but lists zero agents" (warn: the repair arm cannot restore a roster it did not delete); exhausted fetch → warn, matching `rb_content_moving` against this same host (`prove_unreachable_is_not_unjoinable.py`) | **fixed** |
| `rb_rollup_coverage` | positive independent-corpus join | exemplar | ok |
| `rb_shards` | bare HTTP 200 — the full #11 triple in one line (reachable ≠ parseable ≠ current) | the generator's manifest `state/cache_shards/index.json` must parse, the newest shard it names must actually be served and report a positive `_meta.count`, and the manifest's own `_meta.generated_at` must be younger than 15h (3× the worst measured regeneration gap, 4.9h, 2026-08-15/16) | **fixed** |
| `gh_status` | GitHub's self-report, fail-closed | acceptable: warn-level attribution only, bounded claim | bounded |
| `sites` | 200 only; an error page served 200, or an empty body, passes | positive marker in the body (the /ship marker discipline) | P2 |
| `channel` | 200 only, including channel.json | parse channel.json + `_generated` freshness via `moving()` — issue #1's `channel_fresh` is the worked inversion | P2 |
| `alert_delivery` | outbox.status() self-report; empty queue + dead drainer = green with zero evidence anything ever delivered | positive: last chat.db-verified SENT within N days, else "delivery unverified for Nd" warn — waits for the #70–#72 outbox ordering fixes to soak | P2 |
| `w_brainstem` | POSITIVE — answers a turn. R1's worked example | none needed | exemplar |
| `w_openrappter` | positive LISTENING pid; honest UNVERIFIED branch. Gap: #23's spinning wheel — a loaded label with runs=27, last exit 1, never up, is invisible while another process serves the port | new id `w_openrappter_spin` (tranche 2): a loaded, never-running, nonzero-exit job is a failure with its own manifest row and prove file | tranche 2 |
| `w_anchor_ledger` | positive external comparison | exemplar | ok |
| `w_sentinel_fresh` | self-written stamp, judged by peers per design | keep — the peers are the check on it | by design |
| `w_checks_complete` | positive required-vs-ran comparison | keep; the self-removal limit is documented and held by rapp-overwatch from outside | ok |

## The alert gate (added 2026-08-25 from measured field behavior)

Three watchers ran in the wild for three days and taught us what the design missed.
Observed: the same findings re-sent for **69+ hours** with only the age counter moving;
the majority of alert text was the watcher's own blindness ("cannot read the PR queue",
"cannot audit launchd jobs"); the repair arm wrote no state for six days; and 11 of the
watchers' own launchd services sat crashed. Nobody could answer *did any of this cause
an action?* — because alerting left no record.

Every alert now passes one gate in `sentinel.notify()`, and **every decision is a rapp/1
frame** on `state/alerts.jsonl` (`alert_ledger.py`):

1. **Blindness never pages.** If every finding is the watcher failing to observe, it is
   recorded as `alert.blind` and no human is woken. That is a defect in the watcher, and
   it surfaces in the digest as one — where it can actually be fixed.
2. **Identity is the failing-check SET, not the prose.** A re-measured age (68.6h →
   69.1h) is the same alarm. Repeats inside the window record `alert.suppressed`.
3. **A page records why it was allowed** (`alert.paged`), so noise is countable and
   silence is provable.

Two bugs this fixed, both invisible without the field run:
- `cooldown.py` wrote its state to `~/rapp-sentinel/state/` — a directory that does not
  exist on any deployed instance (they run from `~/Documents/GitHub/rapp-sentinel`), so
  suppression state was never read back and every alarm looked new. It now derives from
  `paths.HOME`, the one place HOME is supposed to come from.
- `cooldown` was never wired into `notify()` at all. The module existed; nothing called it.

Ask the ledger anything: `python3 alert_ledger.py <instance>` prints the 24h digest —
paged / suppressed / blind, and which checks are blind most often.

## Identity: why a sentinel must be rapp/1 compliant (2026-08-25)

A full inventory of the estate found **six sentinel installs across four devices at five
different code versions**, and nothing could tell them apart. The alerts carried a
human-typed display name from a config file — so when three days of noise arrived, nobody
could answer the three questions that matter: *which instance sent this, what code was it
running, and had my fix ever reached it?* Two of the noisiest were running code that was
six days and twenty days stale. That stayed invisible because **a display name is not an
identity**.

`identity.py` fixes it with the estate's existing standard rather than a private scheme:

- `rappid.json` — minted ONCE per rapp/1 §6.2 (uuid-entropy tail, never a name-hash), so
  two instances both called "Storykeeper One" on different machines cannot collide.
- `stamp()` — identity + running commit + host, attached to every alert and every ledger
  frame, so any message traces back to the exact instance and commit that produced it.

Why the open standard instead of something homegrown: every other chain in this estate
already verifies under one envelope, so a sentinel frame pools, travels, and gate-checks
alongside world data, brains, and films with no special case. A private identity scheme
would need its own tooling forever. Compliance is what makes an instance legible to a
system it has never met — which is the entire point of running many of them.
