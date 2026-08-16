# Mutation ledger — every check broken on purpose, and what the verdict said

*The exercise was directed in #38 ("T1 is the only tier with no proof harness:
mutation-test all 18 checks and publish the ledger", opened 2026-08-06) and run
in 2026-08 against the then-19 required checks — the manifest as of #42, when
the target had moved 18 → 19 mid-exercise: `rb_content_moving`'s claim changed
under #39 and `rb_rollup_coverage` was added by #41. The manifest now lists 22:
`gh_status` landed with `e61710c` (#49), `rb_derived_truth` and
`rv_meaningful_activity` with `5a4538e` (#62), all after the exercise closed.
The scope line, stated honestly: **19 of the then-19 were proven by mutation;
ids added since are covered by their own committed proof artifacts, and any id
with neither is marked UNPROVEN in the coverage table below.** Issue #16's rule
is why this is a file and not an issue comment: findings do not transfer,
ledgers do.*

Every check was mutated by breaking the condition it claims to defend, and
every mutation was paired with a control asserting a healthy world still
passes. **Result: 19/19 DETECTED their failure case. 0 false positives on
controls. 1 MISSED class found.**

## The 19 verdicts

| check | mutation | verdict |
|---|---|---|
| `w_sentinel_fresh` | last_run.json moved 9h into the past | DETECTED |
| `w_checks_complete` | removed an @check decorator | DETECTED |
| `w_anchor_ledger` | dropped 6 frames from a witnessed chain | DETECTED |
| `alert_delivery` | queued an alert to an unroutable handle | DETECTED |
| `rv_world_merging` | no state merges; then the real 19-day freeze | DETECTED |
| `sites` | both 404; then a SINGLE platform 503 | DETECTED |
| `rb_json_parses` | git conflict markers in served state | DETECTED |
| `rv_validation` | gate rejecting 10/10; 50% below the bar | DETECTED |
| `rv_pr_queue` | 679 open PRs; exact-40 boundary | DETECTED |
| `rb_workflows` | 2 workflows failing every run | DETECTED |
| `rb_wf_starved` | static-api cancelled 3/3 | DETECTED |
| `channel` | full 404; then a PARTIAL outage | DETECTED |
| `rb_shards` | shard 404 | DETECTED |
| `w_openrappter` | nothing LISTENING on :18790 | DETECTED |
| `w_brainstem` | 503; unreachable; responds-but-blank | DETECTED |
| `eco_sweep` | a swept repo given a 9-run red streak | DETECTED |
| `rb_public_surface` | public state 404 to an outsider | DETECTED |
| `rb_content_moving` | stalled 13h; dead 120h; zero posts | DETECTED |
| `rb_rollup_coverage` | 73.8% live; 50.8% scrape cap | DETECTED |

`rb_content_moving` and `rb_rollup_coverage` are proven by committed
reproductions (`prove_content_moving.py`, `prove_rollup_coverage.py`) rather
than ad-hoc mutation, which is stronger — the proof ships with the check.

## Controls: zero false positives, and two boundary cases

A control is not optional. Three checks would have looked "sensitive" on
failure cases alone; the controls are what prove they are not simply
always-red. The two boundary cases: **`rv_validation` at exactly 60%** and
**`rv_pr_queue` at exactly 39** both pass, so the thresholds are real rather
than decorative.

One case was recorded as INFO, not MISSED: `w_openrappter` with a process
serving but no launchd job claiming it returns ok with the detail "no launchd
job claims it" — deliberate per its docstring, because the previous version
false-alarmed on a daemon supervised by a system LaunchDaemon the query could
not see.

## The one MISSED class — absence-blindness, filed as #43

The batches covered "the subject is failing". They never covered "the subject
is **absent**". Probing that produced the only miss:

```
rv_validation,  ZERO runs    -> ok  'no recent runs'
rb_workflows,   no history   -> ok  'no run history'
rb_wf_starved,  no history   -> ok  'no run history'
```

Every one asks *is this failing?* and none asks *is this running at all?*
Filed as #43, fixed in `5b1aa1f` ("Absence is not health: distinguish
no-workflows from workflows-stopped", PR #44), with the committed reproduction
`prove_absence.py`. The follow-on probe — what does a check say when its own
*instrument* is dead? — became #45, fixed in `dfe0400` ("A dead instrument
must never read as green", PR #46), with `prove_blind_green.py`.

## A mutation harness must prove it actually broke something

Three harness errors occurred across the exercise, and every one produced a
plausible, alarming, **false** finding:

1. **Guessed function names.** Batch 4 reported `w_openrappter` MISSED twice
   because the harness guessed the function names in `health.py` and got
   `n/a` back. Re-run against the real names (`_openrappter_supervision`,
   `_brainstem_answers_turns`) it detects 6/6.
2. **Patched the wrong read path (T2).** The first rapp-overwatch run reported
   5 blind-green, including `l_chains_verify` — chain integrity, the one thing
   worth waking someone for. All five were wrong: the harness had patched
   `_load`, and none of those five use it. Breaking nothing, they returned
   their genuine healthy answers.
3. **Patched the wrong predicate (T3).** `f_subject_reachable` uses
   `is_dir()`, not `exists()`, so it too was untouched by the patch. Patching
   `is_dir` directly, it fails closed.

Every one was caught the same way: reading the source before believing the
output. The transferable rule: **a verdict of MISSED is only meaningful if the
mutation demonstrably took effect — otherwise "the check didn't notice" and
"there was nothing to notice" are indistinguishable, and they look identical
in the report.**

## Extending the probe upward: T2 and T3 are clean

The same probes applied to the watchers above this repo, breaking every read
path (`_load`, `Path.read_text`, `Path.exists`, `Path.glob`,
`subprocess.run`):

- **rapp-overwatch (T2), 13 checks**: fails closed 1, raised 12,
  **blind-green 0**.
- **rapp-ratchet (T3), 9 checks**: same result, **blind-green 0**, same runner
  guarantee.

A raise is not a leak there: `run_checks()` converts it deliberately —

> *A check that raises is a broken check, not a broken subject, and is
> reported as such — conflating the two is how a monitoring bug becomes an
> outage report at three in the morning.*

```python
except Exception as exc:
    r = C.fail(fn.__name__, f"check raised {type(exc).__name__}: {exc}")
```

Both tiers were built with the discipline T1 lacked. The blindness class was
specific to this repo.

## Coverage — every required id and the artifact that proves it

*This table is LIVING: `test_ledger_coverage.py` fails the suite if an id in
`required_checks.json` has no row here, so a new check cannot land without at
least an explicit UNPROVEN entry. Rows may outlive the manifest — a retired
check keeps its history.*

| id | proof artifact | status |
|---|---|---|
| `alert_delivery` | ledger row (unroutable handle) | proven |
| `channel` | ledger row; `prove_required_id_survives_outage.py` | proven |
| `eco_sweep` | ledger row; `prove_blind_green.py` | proven |
| `gh_status` | `prove_github_status.py` (post-exercise id, #49) | proven |
| `rails_fresh` | `prove_rails_fresh.py` | lands with #6's PR |
| `rb_content_moving` | ledger row; `prove_content_moving.py`; `prove_transport_failure_is_not_a_content_stall.py` | proven |
| `rb_derived_truth` | none committed — unit branch tests only (`RappterbookDerivedTruthTests` in `test_static_delivery.py`); no mutation row, no prove file | UNPROVEN |
| `rb_json_parses` | ledger row; `prove_starved_colors.py`; `prove_blind_green.py` | proven |
| `rb_public_surface` | ledger row; `prove_starved_colors.py` | proven |
| `rb_rejection_rate` | `prove_rejection_rate.py` | lands with #6's PR |
| `rb_rollup_coverage` | ledger row; `prove_rollup_coverage.py` | proven |
| `rb_shards` | ledger row; `prove_shards_regenerating.py` | proven |
| `rb_wf_starved` | ledger row; `prove_absence.py`; `prove_starvation_confirm.py`; `prove_starved_colors.py`; `prove_unreadable_is_not_absence.py` | proven |
| `rb_workflows` | ledger row; `prove_absence.py` | proven |
| `rv_meaningful_activity` | none committed — unit branch tests only (`MeaningfulActivityTests` in `test_static_delivery.py`); no mutation row, no prove file | UNPROVEN |
| `rv_pr_queue` | ledger row; `prove_blind_green.py` | proven |
| `rv_validation` | ledger row; `prove_absence.py`; `prove_undecided.py`; `prove_resolved_burst.py`; `prove_validation_staleness.py` | proven |
| `rv_world_merging` | ledger row; `prove_unreadable_history_is_not_a_frozen_world.py` | proven |
| `sites` | ledger row; `prove_required_id_survives_outage.py` | proven |
| `w_anchor_ledger` | ledger row (6 frames dropped from a witnessed chain) | proven |
| `w_brainstem` | ledger row (503 / unreachable / responds-but-blank) | proven |
| `w_checks_complete` | ledger row; `CompletenessRegressionTests` in `test_static_delivery.py` | proven |
| `w_openrappter` | ledger row (nothing LISTENING on :18790) | proven |
| `w_sentinel_fresh` | ledger row (last_run.json moved 9h into the past) | proven |
| `w_openrappter_spin` | `prove_spinning_job.py` | landed in #76 |
| `w_freshness_paired` | `prove_freshness_pairing.py` | landed in #76 |
| `w_outsider_coverage` | `prove_outsider_coverage.py` | proven |
| `page_fetch` | `prove_page_fetch.py` (ships with the check, #12) | proven |
| `cadence_honest` | `prove_cadence_honest.py` (ships with the check, #12) | proven |
| `w_test_baseline` | `prove_test_baseline.py` (ships with the check, #13) | proven |

The two UNPROVEN rows are the honest debt: both ids landed in #62 with unit
tests that exercise their fail and pass branches, but neither has been through
a mutation run or ships a `prove_*.py`, and this ledger does not upgrade a
unit test to a proof by relabeling it.
