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
| `rb_shards` | bare HTTP 200 — the full #11 triple in one line (reachable ≠ parseable ≠ current) | bytes must parse, `_meta.count` must be positive, and the path's newest commit must be younger than 15h (3× the worst measured regeneration gap, 4.9h, 2026-08-15/16) | **fixed** |
| `gh_status` | GitHub's self-report, fail-closed | acceptable: warn-level attribution only, bounded claim | bounded |
| `sites` | 200 only; an error page served 200, or an empty body, passes | positive marker in the body (the /ship marker discipline) | P2 |
| `channel` | 200 only, including channel.json | parse channel.json + `_generated` freshness via `moving()` — issue #1's `channel_fresh` is the worked inversion | P2 |
| `alert_delivery` | outbox.status() self-report; empty queue + dead drainer = green with zero evidence anything ever delivered | positive: last chat.db-verified SENT within N days, else "delivery unverified for Nd" warn — waits for the #70–#72 outbox ordering fixes to soak | P2 |
| `w_brainstem` | POSITIVE — answers a turn. R1's worked example | none needed | exemplar |
| `w_openrappter` | positive LISTENING pid; honest UNVERIFIED branch. Gap: #23's spinning wheel — a loaded label with runs=27, last exit 1, never up, is invisible while another process serves the port | new id `w_openrappter_spin` (tranche 2): a loaded, never-running, nonzero-exit job is a failure with its own manifest row and prove file | tranche 2 |
| `w_anchor_ledger` | positive external comparison | exemplar | ok |
| `w_sentinel_fresh` | self-written stamp, judged by peers per design | keep — the peers are the check on it | by design |
| `w_checks_complete` | positive required-vs-ran comparison | keep; the self-removal limit is documented and held by rapp-overwatch from outside | ok |
