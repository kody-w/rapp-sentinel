# Tactical plan — every open issue, addressed or deliberately deferred

*Drafted 2026-08-16 from all 14 open issues (#1–#38), six design passes and one
adversarial synthesis, every claim spot-checked against source. This file is a
living ledger: boxes get checked as PRs land, and a deferral here is a decision
with a reason, not a silence.*

## The constraint that governs everything: the organism must survive the molt

A sentinel is a running digital organism. The live install carries state
written by OLD code — hash chains verified from genesis, anchors, the external
ledger, `peers-seen.json`, its own `config.json` — and it picks up new code by
`git pull` between ticks. Every change below therefore ships a **growth path**:

1. New config keys always default to current behavior; the live config won't have them.
2. New code reads state written by old code; missing fields are tolerated, never fatal.
3. A new check and its `required_checks.json` row land in the **same commit** (atomic via pull).
4. Check ids are never renamed; frame kinds are additive; chain history is never rewritten.
5. New launchd behavior must work under the already-installed plists.
6. After each merge: pull the live install between ticks, run `health.py` there,
   and watch the next scheduled tick advance `last_run.json` with chains verifying.
   A tranche is done when the **live organism** has ticked green on it, not when CI has.

## Tranche 1 — framework bedrock (#2, #3, #11, #8, #1-part)

- [ ] **TRIFECTA-PATTERN.md §6d** — three numbered, incident-backed invariants:
  R1 *a receipt is not evidence* (#2), R2 *ran is not worked* (#3),
  R3 *require known-good, never enumerate known-bad* (#11). One-line forms also
  appended to the checks.py docstring, where authors actually look at 2am.
- [ ] **`moving()` + `require_success()` + `UNDECIDED`** — helpers that make the
  right check shape the cheap default. `moving()` separates *blind* (warn) from
  *stampless* and *stale* (critical). `require_success()` is colour-blind about
  failure: cancelled, skipped, timed_out are all equally not-success.
- [ ] **Four audited live defects fixed**: `rb_wf_starved` misses all-skipped /
  all-timed-out (only counts `cancelled`); `rb_public_surface` passes on a
  zero-agent roster; `rb_json_parses` silently skips unreachable files and can
  report ok on 1-of-5; `rb_shards` is a bare 200-check on bytes it never parses.
  Plus **CHECK-AUDIT.md**: the full 22-id ledger of what each check passes on
  today and its positive-evidence inversion, so the findings transfer (#16's rule).
- [ ] **prove_unsigned_relay_refusal.py** — the #8 fix is already merged
  (`say()` refuses, §16 block records both gaps); ship the house-rule proof
  that the refusal fires, then close #8.
- [ ] **`advancing` is null on first sight** (#1 ask 3) — plus a
  `stalled_peers()` classifier, because `sentinel.py` tests truthiness and a
  bare `None` would swap "born-stalled reads healthy" for "first-sight reads stalled".

## Tranche 2 — instance & runner infrastructure (#1, #23, #3, #38, #16)

- [ ] **SENTINEL_HOME** via one shared `paths.py`, honored by every runtime
  module (all 10 derive HOME independently today — a one-file fix would split-brain).
  Unset ⇒ byte-identical behavior.
- [ ] **HTTP status codes in every fetch-failure detail** (#1 ask 5) —
  `fetch_peer` and the other bare `type(e).__name__` sites; 404 and 503 demand
  opposite responses.
- [ ] **launchd truth** (#23): fix the missing `import os` in health.py (the
  gui-domain query silently falls back to uid 501 today), enumerate both
  domains for the socket-owning pid, three-state supervision answer.
- [ ] **`w_openrappter_spin`** (#23): a loaded job with runs≥3, last exit ≠ 0,
  never running is a spinning wheel — its own id, manifest row, prove file.
- [ ] **`w_freshness_paired`** (#3): a watched domain with run-status checks but
  no output-freshness check becomes a per-tick finding.
- [ ] **MUTATION-LEDGER.md** — the 19/19 T1 mutation results, the three harness
  errors, and the transferable rule ("a mutation harness must prove it actually
  broke something"), with a coverage-guard test. Closes #38; names where the
  chain of watchers ends (#16, weakness 5).

## Tranche 3 — diagnose-and-measure + the outsider's vantage (#4, #5, #1-part, #12)

- [ ] **`sentinel diagnose`** (#4): identity / scope / reachability of every
  credential and endpoint the checks depend on, values never printed.
- [ ] **Escalation prompts carry attempt history** (#4): repeated failure of the
  same repair means the *diagnosis* is wrong — the prompt says to change method.
- [ ] **`@outsider_check` vantage marker + `w_outsider_coverage`** (#5): every
  watched platform must have ≥1 unauthenticated check, enforced per tick.
- [ ] **probe_watchers targets from config.json** (#1 ask 2): the brainstem POST
  and launchd labels stop being hardcoded estate facts; a disabled probe is
  declared in the verdict, never silently green.
- [ ] **`page_fetch` + `cadence_honest`** (#12): fetch the served HTML, assert
  every first-party request it makes returns 2xx (pollers loudest); and a served
  doc whose declared refresh cadence contradicts its own newest timestamp fails.
  Stdlib regex extraction, honestly labeled, with a CDP slot-in path.

## Tranche 4 — rails, rejection rates, real-write smoke, baselines (#6, #5, #7-part, #13, #1-part)

- [ ] **Scaffolding registry + `rails_fresh`** (#6): each quality rail declares
  when it was written and what it guarded against; unreviewed rails surface.
- [ ] **`rb_rejection_rate`** (#6): on the live slop-cop ledger; ≥90% rejection
  pages critically — the check that would have caught the July-30 outage day one.
- [ ] **participate.py outcome honesty** (#5/#7): decline first-class, a silent
  model failure never recorded as success, exit codes never believed.
- [ ] **Outsider smoke in the tick** (#5): budgeted like evolve (writes a real
  issue — 72h interval, daily cap 1), verified by re-reading `participation.jsonl`,
  with read-only `w_outsider_smoke` watching that the front door stays exercised.
- [ ] **Test baselines** (#13): dated, environment-stamped, set-based —
  `baseline.py` recorder (refuses shallow clones and world-writable dirs, the
  two measured phantom-failure sources) + `w_test_baseline` comparing the named
  set, reporting `newly_failing` / `newly_passing`. Enrolls rapp-sentinel only
  at landing.
- [ ] **Low-noise head hosting** (#1 ask 6): JOINING.md recipe (gist first),
  optional throttled `head_publish_cmd` hook, and a live example head the first
  outside neighbor can actually point at.

## Deferred — decisions, not silences

- **`critique.py` resident-agent platform critique (#7)**: designed and
  prove-able, but it is recurring model spend (2 calls/day), files issues under
  the owner's login "on behalf of" agents, and `--allow-all` hands the model far
  more than "file one issue". Parked behind an explicit owner decision on
  budget, sandbox, and representation. The safer v2 (model emits a structured
  block; critique.py files the issue itself) should be v1 when approved.
- **`attests_for` head field (#1 ask 4)**: approved as designed; purely
  additive; rides any later neighborhood.py PR at near-zero cost.
- **P2 inversion backlog** (eco_sweep colour-blindness, rv_* inversions,
  alert_delivery chat.db-verified SENT, sites body markers): each recorded in
  CHECK-AUDIT.md with its inversion; each lands as its own small PR after the
  tranche-1 helpers exist. alert_delivery's rewrite waits for the outbox
  ordering fixes (#70–#72) to soak.

## Issue → disposition map

| issue | disposition |
|---|---|
| #1 | six asks split: T1 (advancing), T2 (SENTINEL_HOME, status codes), T3 (config probes), T4 (head hosting); attests_for deferred-approved |
| #2 | T1 rules + helpers; closes with T1 |
| #3 | T1 `moving()` + T2 `w_freshness_paired`; closes with T2 |
| #4 | T3 diagnose + prompt change |
| #5 | T3 vantage marker + T4 smoke wiring |
| #6 | T4 scaffolding + rejection rate |
| #7 | deferred pending owner decision (see above); outcome-honesty part lands in T4 |
| #8 | fix already merged; T1 ships the proof, closes |
| #11 | T1 helpers + four fixes + CHECK-AUDIT.md; closes with T1 |
| #12 | T3 page_fetch + cadence_honest |
| #13 | T4 baselines |
| #16 | stays open as the method record; MUTATION-LEDGER.md (T2) and CHECK-AUDIT.md (T1) give its findings a durable home |
| #23 | T2 launchd truth + spin check; closes with T2 |
| #38 | ledger complete in-issue; T2 publishes MUTATION-LEDGER.md, closes |
