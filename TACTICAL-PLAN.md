# Tactical plan — every open issue, addressed or deliberately deferred

**STATUS 2026-08-16 (same day): every tranche landed — PRs #73–#87, all
verified on the live organism. Day-one findings by the new checks: the
rappterverse front door structurally dead-ends (filed as rappterverse#6752
by the outsider smoke); page_fetch caught and fixed its own scanner defect;
the #12 drift was confirmed fixed upstream. Still open by design: #16 (the
method record) and #7 (deferred on an explicit owner decision).**

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

- [x] **TRIFECTA-PATTERN.md §6d** — three numbered, incident-backed invariants:
  R1 *a receipt is not evidence* (#2), R2 *ran is not worked* (#3),
  R3 *require known-good, never enumerate known-bad* (#11). One-line forms also
  appended to the checks.py docstring, where authors actually look at 2am.
- [x] **`moving()` + `require_success()` + `UNDECIDED`** — helpers that make the
  right check shape the cheap default. `moving()` separates *blind* (warn) from
  *stampless* and *stale* (critical). `require_success()` is colour-blind about
  failure: cancelled, skipped, timed_out are all equally not-success.
- [x] **Four audited live defects fixed**: `rb_wf_starved` misses all-skipped /
  all-timed-out (only counts `cancelled`); `rb_public_surface` passes on a
  zero-agent roster; `rb_json_parses` silently skips unreachable files and can
  report ok on 1-of-5; `rb_shards` is a bare 200-check on bytes it never parses.
  Plus **CHECK-AUDIT.md**: the full 22-id ledger of what each check passes on
  today and its positive-evidence inversion, so the findings transfer (#16's rule).
- [x] **prove_unsigned_relay_refusal.py** — the #8 fix is already merged
  (`say()` refuses, §16 block records both gaps); ship the house-rule proof
  that the refusal fires, then close #8.
- [x] **`advancing` is null on first sight** (#1 ask 3) — plus a
  `stalled_peers()` classifier, because `sentinel.py` tests truthiness and a
  bare `None` would swap "born-stalled reads healthy" for "first-sight reads stalled".

## Tranche 2 — instance & runner infrastructure (#1, #23, #3, #38, #16)

- [x] **SENTINEL_HOME** via one shared `paths.py`, honored by every runtime
  module (all 10 derive HOME independently today — a one-file fix would split-brain).
  Unset ⇒ byte-identical behavior.
- [x] **HTTP status codes in every fetch-failure detail** (#1 ask 5) —
  `fetch_peer` and the other bare `type(e).__name__` sites; 404 and 503 demand
  opposite responses.
- [x] **launchd truth** (#23): fix the missing `import os` in health.py (the
  gui-domain query silently falls back to uid 501 today), enumerate both
  domains for the socket-owning pid, three-state supervision answer.
- [x] **`w_openrappter_spin`** (#23): a loaded job with runs≥3, last exit ≠ 0,
  never running is a spinning wheel — its own id, manifest row, prove file.
- [x] **`w_freshness_paired`** (#3): a watched domain with run-status checks but
  no output-freshness check becomes a per-tick finding.
- [x] **MUTATION-LEDGER.md** — the 19/19 T1 mutation results, the three harness
  errors, and the transferable rule ("a mutation harness must prove it actually
  broke something"), with a coverage-guard test. Closes #38; names where the
  chain of watchers ends (#16, weakness 5).

## Tranche 3 — diagnose-and-measure + the outsider's vantage (#4, #5, #1-part, #12)

- [x] **`sentinel diagnose`** (#4): identity / scope / reachability of every
  credential and endpoint the checks depend on, values never printed.
- [x] **Escalation prompts carry attempt history** (#4): repeated failure of the
  same repair means the *diagnosis* is wrong — the prompt says to change method.
- [x] **`@outsider_check` vantage marker + `w_outsider_coverage`** (#5): every
  watched platform must have ≥1 unauthenticated check, enforced per tick.
- [x] **probe_watchers targets from config.json** (#1 ask 2): the brainstem POST
  and launchd labels stop being hardcoded estate facts; a disabled probe is
  declared in the verdict, never silently green.
- [x] **`page_fetch` + `cadence_honest`** (#12): fetch the served HTML, assert
  every first-party request it makes returns 2xx (pollers loudest); and a served
  doc whose declared refresh cadence contradicts its own newest timestamp fails.
  Stdlib regex extraction, honestly labeled, with a CDP slot-in path.

## Tranche 4 — rails, rejection rates, real-write smoke, baselines (#6, #5, #7-part, #13, #1-part)

- [x] **Scaffolding registry + `rails_fresh`** (#6): each quality rail declares
  when it was written and what it guarded against; unreviewed rails surface.
- [x] **`rb_rejection_rate`** (#6): on the live slop-cop ledger; ≥90% rejection
  pages critically — the check that would have caught the July-30 outage day one.
- [x] **participate.py outcome honesty** (#5/#7): decline first-class, a silent
  model failure never recorded as success, exit codes never believed.
- [x] **Outsider smoke in the tick** (#5): budgeted like evolve (writes a real
  issue — 72h interval, daily cap 1), verified by re-reading `participation.jsonl`,
  with read-only `w_outsider_smoke` watching that the front door stays exercised.
- [x] **Test baselines** (#13): dated, environment-stamped, set-based —
  `baseline.py` recorder (refuses shallow clones and world-writable dirs, the
  two measured phantom-failure sources) + `w_test_baseline` comparing the named
  set, reporting `newly_failing` / `newly_passing`. Enrolls rapp-sentinel only
  at landing.
- [x] **Low-noise head hosting** (#1 ask 6): JOINING.md recipe (gist first),
  optional throttled `head_publish_cmd` hook, and a live example head the first
  outside neighbor can actually point at.

## Deferred — decisions, not silences

- **`critique.py` resident-agent platform critique (#7)**: designed and
  prove-able, but it is recurring model spend (2 calls/day), files issues under
  the owner's login "on behalf of" agents, and `--allow-all` hands the model far
  more than "file one issue". Parked behind an explicit owner decision on
  budget, sandbox, and representation. The safer v2 (model emits a structured
  block; critique.py files the issue itself) should be v1 when approved.
- ~~**`attests_for` head field (#1 ask 4)**~~: LANDED (PR #87) — validated
  claim, never verified truth; malformed claims fail the publish.
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

## After the issues: a publication-hygiene pass

The issue work above finished, and a question about protecting the project's
IP turned into the larger finding of the session: internal notes had been
riding along into public repositories for months. Every individual commit
looked reasonable, which is why review never caught any of it.

A sweep of the whole public estate found and removed them across a number of
repositories, and two repositories were made private pending an owner
decision. The specifics are recorded privately — enumerating what was
removed, and where it can still be read, would rebuild the index this pass
existed to take down. That is not a hypothetical: the first version of this
section did exactly that, in this public file, and had to be scrubbed.

**Three lessons worth keeping, which are the transferable part:**

1. `gh search code` is not authoritative for "is this public" — it missed a
   public repo while returning private ones. Verify per repo with the
   contents API or a fresh clone, and never with `raw.githubusercontent`,
   which caches for about five minutes and will show you stale bytes.
2. Deleting a file does not unpublish it. Git history stays public; only
   making the repository private, or purging history, actually removes it.
3. **The denylist is the disclosure.** A committed list of the strings you
   are suppressing is a better search index than the content it suppresses.
   This is why `ipscan.py`'s rules are injected and never shipped, why it
   refuses a denylist that git tracks, why its receipts record file paths and
   counts but never the matched text — and why a write-up of a cleanup
   belongs somewhere private, however useful the narrative feels.

**Now guarded, not just cleaned:** `ipscan.py` + the `ip_hygiene` check
(PRs #89-#92). Latest full run: 421 public repositories, zero findings, zero
unscanned.

## rapp-monorepo

Built the same night, on the same discipline: one public repo that captures
every public RAPP repo at HEAD in a single pass, so a reader gets the whole
estate with no drift between its pieces. 192 repos, ~666MB, daily refresh,
verified from a clean clone and with its workflow observed running green.

Its gate **withholds whole files rather than redacting them** — a rewritten
file would quietly break the mirror's one promise, that what you have is what
upstream has — and it fails closed when unconfigured, because a gate that
screens nothing while reporting success launders content through a step that
looks like diligence.
