"""test_evolve_worker.py — the guarantees the art arm is not allowed to lose.

Every test here is a thing that, if it broke silently, would look exactly like
success: a second worker doubling the spend, a corrupt ledger handing back the
day's budget, a model committing its own work, a nine-candidate "cycle", a
timeout that texted a paintbrush. Mocks and temp git repos only — nothing here
touches the live instance, GitHub, or a model.
"""

import json
import os
import shutil
import subprocess
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import evolve_worker as EW
import sentinel
import subsentinels as SS

SCRATCH = Path(__file__).resolve().parent / ".tmp-evolve-worker-tests"
# Captured before any test patches it: ArtDeliveryTests puts the REAL notify
# back so it can watch the genuine path reach the genuine queue.
REAL_NOTIFY = sentinel.notify
NOW = datetime(2026, 8, 17, 22, 0, tzinfo=timezone.utc)


def _history_snapshot():
    """What the ledger held at the moment a notification was sent."""
    try:
        return json.loads(EW.HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def scratch_dir(name):
    SCRATCH.mkdir(exist_ok=True)
    path = SCRATCH / f"{name}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True)
    return path


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout


def git_bare(bare, *args):
    """This machine sets safe.bareRepository=explicit, so say --git-dir."""
    bare = Path(bare)
    return git(bare.parent, f"--git-dir={bare}", *args)


SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
       '<circle cx="5" cy="5" r="4"/></svg>')


def dada_cycle(slug, cycle=1, previous=None, rounds=1,
               candidates=EW.CANDIDATES_PER_ROUND,
               dimensions=EW.SCORE_DIMENSIONS, round1_ids=None):
    body = []
    for r in range(1, rounds + 1):
        ids = ([str(i) for i in round1_ids] if (r == 1 and round1_ids)
               else [f"r{r}c{i}" for i in range(1, candidates + 1)])
        cands = [{"id": cid, "premise": f"premise {r}.{n}",
                  "scores": {d: 5 for d in dimensions}}
                 for n, cid in enumerate(ids, start=1)]
        body.append({"round": r, "candidates": cands,
                     "selected": cands[0]["id"] if cands else None})
    return {
        "cycle": cycle,
        "previous_slug": previous,
        "rounds": body,
        "winner": {"round": rounds,
                   "candidate": body[-1]["selected"] if body else None,
                   "slug": slug},
    }


def meta_for(slug, cycle=1, previous=None, round1_ids=None, **overrides):
    meta = {
        "schema": EW.SUBMISSION_SCHEMA,
        "title": slug.replace("-", " ").title(),
        "slug": slug,
        "contributor": "kody-w",
        "kind": "svg",
        "submitted_at": "2026-08-17T22:00:00Z",
        "remix_of": None,
        "license": "CC0-1.0",
        "_dada_cycle": dada_cycle(slug, cycle=cycle, previous=previous,
                                  round1_ids=round1_ids),
    }
    meta.update(overrides)
    return meta


def write_submission(clone, slug, meta=None, piece=SVG, piece_name="piece.svg"):
    directory = Path(clone) / "submissions" / slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "meta.json").write_text(
        json.dumps(meta if meta is not None else meta_for(slug), indent=2),
        encoding="utf-8")
    (directory / piece_name).write_text(piece, encoding="utf-8")
    return directory


class ScratchCase(unittest.TestCase):
    """A worker whose entire durable footprint lives in a scratch directory."""

    def setUp(self):
        self.home = scratch_dir(self.__class__.__name__)
        self.state = self.home / "state"
        self.logs = self.home / "logs"
        self.state.mkdir()
        self.logs.mkdir()
        patches = {
            "HOME": self.home,
            "STATE": self.state,
            "LOGS": self.logs,
            "STOP": self.home / "STOP",
            "LOCK_PATH": self.state / "evolve-worker.lock",
            "HISTORY_PATH": self.state / "evolve-worker-history.json",
            "TURN_PATH": self.state / "evolve-worker-turn.json",
            "ALERT_PATH": self.state / "evolve-worker-alerts.json",
        }
        for name, value in patches.items():
            p = mock.patch.object(EW, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.notifications = []
        p = mock.patch.object(
            EW.sentinel, "notify",
            side_effect=lambda cfg, text, to=None, rebuild=False:
            self.notifications.append({"text": text, "to": to,
                                       "rebuild": rebuild,
                                       "history": _history_snapshot()}))
        p.start()
        self.addCleanup(p.stop)
        self.frames = []
        p = mock.patch.object(EW.NB, "emit",
                              side_effect=lambda slug, kind, payload:
                              self.frames.append((slug, kind, payload)))
        p.start()
        self.addCleanup(p.stop)

    def texts(self):
        return [n["text"] for n in self.notifications]

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)


# ── lock ────────────────────────────────────────────────────────────────────

class LockTests(ScratchCase):
    def test_a_second_worker_cannot_take_the_lock(self):
        first = EW.acquire_lock()
        self.addCleanup(EW.release_lock, first)
        self.assertIsNotNone(first)
        self.assertIsNone(EW.acquire_lock(),
                          "a second pass must not run while one is in flight")

    def test_the_lock_is_reusable_once_released(self):
        first = EW.acquire_lock()
        EW.release_lock(first)
        second = EW.acquire_lock()
        self.addCleanup(EW.release_lock, second)
        self.assertIsNotNone(second)

    def test_a_held_lock_skips_the_pass_without_spending(self):
        held = EW.acquire_lock()
        self.addCleanup(EW.release_lock, held)
        with mock.patch.object(EW, "run_model") as model:
            summary = EW.run_once(cfg=worker_cfg(), health=lambda phase: healthy())
        self.assertEqual("skipped", summary["outcome"])
        self.assertIn("lock", summary["reason"])
        model.assert_not_called()


# ── cadence and budget ──────────────────────────────────────────────────────

class CadenceBudgetTests(ScratchCase):
    def rows(self, *ages_h, **extra):
        return [{"at": (NOW - timedelta(hours=h)).isoformat(),
                 "mode": "evolve", **extra} for h in ages_h]

    def test_first_evolution_is_allowed(self):
        with mock.patch.object(EW.sentinel, "now", return_value=NOW):
            ready, why = EW.cadence_ready([], {"interval_hours": 4})
        self.assertTrue(ready)
        self.assertEqual("first evolution", why)

    def test_recent_evolution_waits_for_the_global_cadence(self):
        with mock.patch.object(EW.sentinel, "now", return_value=NOW):
            ready, why = EW.cadence_ready(self.rows(2), {"interval_hours": 4})
        self.assertFalse(ready)
        self.assertIn("2.0h of 4.0h", why)

    def test_cadence_is_global_across_roles(self):
        history = [{"at": (NOW - timedelta(hours=1)).isoformat(),
                    "mode": "evolve", "role": "scout"}]
        with mock.patch.object(EW.sentinel, "now", return_value=NOW):
            ready, _ = EW.cadence_ready(history, {"interval_hours": 4})
        self.assertFalse(ready, "another role's run still consumes the cadence")

    def test_budget_is_a_rolling_24h_window(self):
        with mock.patch.object(EW.sentinel, "now", return_value=NOW):
            ok, used, cap = EW.within_budget(self.rows(1, 5), {"daily_budget": 2})
            self.assertFalse(ok)
            self.assertEqual((2, 2), (used, cap))
            ok, used, _ = EW.within_budget(self.rows(1, 30), {"daily_budget": 2})
        self.assertTrue(ok)
        self.assertEqual(1, used, "a row older than 24h is out of the window")

    def test_skipped_rows_never_consume_the_budget(self):
        history = self.rows(1, 2, 3, skipped=True)
        with mock.patch.object(EW.sentinel, "now", return_value=NOW):
            ok, used, _ = EW.within_budget(history, {"daily_budget": 2})
        self.assertTrue(ok)
        self.assertEqual(0, used)

    def test_a_spent_budget_skips_before_the_model(self):
        EW.save_history([{"at": NOW.isoformat(), "mode": "evolve"}])
        with mock.patch.object(EW.sentinel, "now", return_value=NOW), \
             mock.patch.object(EW, "run_model") as model:
            summary = EW.run_once(cfg=worker_cfg(daily_evolve_budget=1),
                                  health=lambda phase: healthy())
        self.assertEqual("skipped", summary["outcome"])
        self.assertIn("budget spent", summary["reason"])
        model.assert_not_called()


# ── fail-closed ledgers ─────────────────────────────────────────────────────

class CorruptLedgerTests(ScratchCase):
    def test_corrupt_history_raises_instead_of_reading_as_no_spend(self):
        EW.HISTORY_PATH.write_text("{not json", encoding="utf-8")
        with self.assertRaises(EW.LedgerError):
            EW.load_history()

    def test_empty_history_is_unknown_not_zero(self):
        EW.HISTORY_PATH.write_text("   ", encoding="utf-8")
        with self.assertRaises(EW.LedgerError):
            EW.load_history()

    def test_a_row_with_an_unparseable_timestamp_fails_closed(self):
        EW.HISTORY_PATH.write_text(json.dumps([{"at": "yesterday", "mode": "evolve"}]),
                                   encoding="utf-8")
        with self.assertRaises(EW.LedgerError):
            EW.load_history()

    def test_a_missing_history_is_simply_zero_spend(self):
        self.assertEqual([], EW.load_history())

    def test_corrupt_history_stops_the_pass_and_preserves_the_bytes(self):
        raw = '[{"at": "2026-08-17T21:00:00+00:00", "mode": "evolve"'
        EW.HISTORY_PATH.write_text(raw, encoding="utf-8")
        with mock.patch.object(EW, "run_model") as model:
            summary = EW.run_once(cfg=worker_cfg(), health=lambda phase: healthy())
        self.assertEqual("fail-closed", summary["outcome"])
        model.assert_not_called()
        self.assertEqual(raw, EW.HISTORY_PATH.read_text(encoding="utf-8"),
                         "a corrupt ledger must never be rewritten as empty")
        self.assertTrue(self.notifications, "fail-closed must be said out loud")
        self.assertNotIn(EW.SUCCESS_PREFIX, self.texts()[0])

    def test_corrupt_creative_state_fails_closed_before_the_model(self):
        (self.state / "evolve-creative-state.json").write_text("{;", encoding="utf-8")
        with mock.patch.object(EW, "run_model") as model:
            summary = EW.run_once(cfg=worker_cfg(), health=lambda phase: healthy())
        self.assertEqual("fail-closed", summary["outcome"])
        model.assert_not_called()

    def test_atomic_write_leaves_no_partial_file_behind(self):
        target = self.state / "ledger.json"
        EW.atomic_write_json(target, [{"at": "now"}])
        self.assertEqual([{"at": "now"}], json.loads(target.read_text()))
        leftovers = [p.name for p in self.state.iterdir() if p.name.startswith(".")]
        self.assertEqual([], leftovers)


# ── health gates ────────────────────────────────────────────────────────────

class HealthGateTests(unittest.TestCase):
    def test_critical_always_aborts(self):
        ok, why = EW.health_gate({"degraded_allowlist": ["rb_workflows"]},
                                 {"status": "critical", "critical": ["rb_workflows"],
                                  "failed": ["rb_workflows"]})
        self.assertFalse(ok)
        self.assertIn("critical", why)

    def test_degraded_needs_every_failing_id_allowlisted(self):
        cfg = {"degraded_allowlist": ["w_openrappter_spin"]}
        ok, why = EW.health_gate(cfg, {"status": "degraded", "critical": [],
                                       "failed": ["w_openrappter_spin"]})
        self.assertTrue(ok)
        ok, why = EW.health_gate(cfg, {"status": "degraded", "critical": [],
                                       "failed": ["w_openrappter_spin", "rb_shards"]})
        self.assertFalse(ok)
        self.assertIn("rb_shards", why)

    def test_evolve_on_degraded_is_not_a_blanket_switch_here(self):
        ok, _ = EW.health_gate({"evolve_on_degraded": True, "degraded_allowlist": []},
                               {"status": "degraded", "critical": [],
                                "failed": ["rb_shards"]})
        self.assertFalse(ok, "the worker must ignore evolve_on_degraded")

    def test_a_blind_health_run_is_not_healthy(self):
        ok, _ = EW.health_gate({"degraded_allowlist": ["w_openrappter_spin"]},
                               {"status": "degraded", "critical": [],
                                "failed": ["health_runtime"]})
        self.assertFalse(ok)

    def test_healthy_passes(self):
        ok, _ = EW.health_gate({}, healthy())
        self.assertTrue(ok)

    def test_alert_delivery_cannot_be_allowlisted(self):
        # Since 232ce7e an unverifiable or dead-lettered send fails this check
        # instead of passing optimistically. Art must not outrun the channel
        # that would report it.
        verdict = {"status": "degraded", "critical": [],
                   "failed": ["alert_delivery"]}
        for allowlist in ([], ["alert_delivery"],
                          ["alert_delivery", "w_openrappter_spin"]):
            with self.subTest(allowlist=allowlist):
                ok, why = EW.health_gate({"degraded_allowlist": allowlist}, verdict)
                self.assertFalse(ok, "unverified/dead-letter alerts block new art")
                self.assertIn("alert_delivery", why)
                self.assertIn("cannot be allowlisted", why)

    def test_a_blind_health_run_cannot_be_allowlisted_either(self):
        ok, why = EW.health_gate({"degraded_allowlist": ["health_runtime"]},
                                 {"status": "degraded", "critical": [],
                                  "failed": ["health_runtime"]})
        self.assertFalse(ok)
        self.assertIn("cannot be allowlisted", why)

    def test_the_unskippable_set_is_exactly_what_it_claims(self):
        self.assertEqual({"alert_delivery", "health_runtime"},
                         set(EW.NEVER_ALLOWLISTABLE))

    def test_alert_delivery_blocks_even_beside_allowlisted_noise(self):
        ok, why = EW.health_gate(
            {"degraded_allowlist": ["w_openrappter_spin", "alert_delivery"]},
            {"status": "degraded", "critical": [],
             "failed": ["w_openrappter_spin", "alert_delivery"]})
        self.assertFalse(ok)
        self.assertIn("alert_delivery", why)


# ── the deterministic gate ──────────────────────────────────────────────────

class GateTests(ScratchCase):
    def setUp(self):
        super().setUp()
        self.clone = self.home / "clone"
        self.clone.mkdir()
        git(self.clone, "init", "-b", "main")
        git(self.clone, "config", "user.email", "t@example.com")
        git(self.clone, "config", "user.name", "t")
        write_submission(self.clone, "already-here")
        (self.clone / "submissions" / "index.json").write_text('{"submissions": []}',
                                                               encoding="utf-8")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-m", "seed")
        self.wcfg = EW.worker_config({})

    def gate(self, cycle=2, previous="already-here"):
        return EW.validate_submission(self.clone, self.wcfg, cycle, previous)

    def test_a_clean_submission_passes(self):
        write_submission(self.clone, "new-piece",
                         meta_for("new-piece", cycle=2, previous="already-here"))
        result = self.gate()
        self.assertEqual("new-piece", result["slug"])
        self.assertEqual("submissions/new-piece/piece.svg", result["piece_path"])

    def test_an_edited_existing_file_is_rejected(self):
        write_submission(self.clone, "new-piece",
                         meta_for("new-piece", cycle=2, previous="already-here"))
        (self.clone / "submissions" / "index.json").write_text('{"submissions": [1]}',
                                                               encoding="utf-8")
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("existing path", str(cm.exception))

    def test_a_deleted_existing_file_is_rejected(self):
        write_submission(self.clone, "new-piece",
                         meta_for("new-piece", cycle=2, previous="already-here"))
        (self.clone / "submissions" / "already-here" / "piece.svg").unlink()
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("existing path", str(cm.exception))

    def test_two_new_submissions_are_rejected(self):
        write_submission(self.clone, "one", meta_for("one", cycle=2, previous="already-here"))
        write_submission(self.clone, "two", meta_for("two", cycle=2, previous="already-here"))
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("more than one", str(cm.exception))

    def test_a_file_outside_submissions_is_rejected(self):
        write_submission(self.clone, "new-piece",
                         meta_for("new-piece", cycle=2, previous="already-here"))
        (self.clone / "notes.md").write_text("hi", encoding="utf-8")
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("outside submissions", str(cm.exception))

    def test_a_third_file_in_the_folder_is_rejected(self):
        directory = write_submission(self.clone, "new-piece",
                                     meta_for("new-piece", cycle=2, previous="already-here"))
        (directory / "notes.txt").write_text("extra", encoding="utf-8")
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("meta.json + piece", str(cm.exception))

    def test_no_changes_at_all_is_a_decline_shaped_rejection(self):
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("no new submission", str(cm.exception))

    def test_a_colliding_slug_is_rejected(self):
        # a new file dropped into a slug that already exists on the branch
        (self.clone / "submissions" / "already-here" / "piece2.svg").write_text(
            SVG, encoding="utf-8")
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("already exists", str(cm.exception))

    def test_a_moved_head_means_the_model_published_itself(self):
        write_submission(self.clone, "new-piece",
                         meta_for("new-piece", cycle=2, previous="already-here"))
        base = git(self.clone, "rev-parse", "HEAD").strip()
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-m", "mine now")
        with self.assertRaises(EW.GateError) as cm:
            EW.validate_submission(self.clone, self.wcfg, 2, "already-here",
                                   "main", base)
        self.assertIn("committed", str(cm.exception))

    def test_extension_must_match_kind(self):
        write_submission(self.clone, "new-piece",
                         meta_for("new-piece", cycle=2, previous="already-here",
                                  kind="md"), piece_name="piece.svg")
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("piece.md", str(cm.exception))

    def test_unknown_license_is_rejected(self):
        write_submission(self.clone, "new-piece",
                         meta_for("new-piece", cycle=2, previous="already-here",
                                  license="MIT"))
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("license", str(cm.exception))

    def test_wrong_schema_is_rejected(self):
        write_submission(self.clone, "new-piece",
                         meta_for("new-piece", cycle=2, previous="already-here",
                                  schema="rapp-art-submission/2.0"))
        with self.assertRaises(EW.GateError):
            self.gate()

    def test_unknown_top_level_meta_key_is_rejected(self):
        meta = meta_for("new-piece", cycle=2, previous="already-here")
        meta["price"] = 100
        write_submission(self.clone, "new-piece", meta)
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("unknown keys", str(cm.exception))

    def test_an_oversized_piece_is_rejected(self):
        big = ('<svg xmlns="http://www.w3.org/2000/svg">'
               + "<!--" + "x" * 60000 + "-->" + "</svg>")
        write_submission(self.clone, "new-piece",
                         meta_for("new-piece", cycle=2, previous="already-here"),
                         piece=big)
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("byte cap", str(cm.exception))

    def test_svg_with_a_script_is_rejected(self):
        write_submission(self.clone, "new-piece",
                         meta_for("new-piece", cycle=2, previous="already-here"),
                         piece='<svg xmlns="http://www.w3.org/2000/svg">'
                               '<script>alert(1)</script></svg>')
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("script", str(cm.exception))

    def test_svg_with_an_event_attribute_is_rejected(self):
        write_submission(self.clone, "new-piece",
                         meta_for("new-piece", cycle=2, previous="already-here"),
                         piece='<svg xmlns="http://www.w3.org/2000/svg">'
                               '<circle onclick="x()" r="1"/></svg>')
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("event attribute", str(cm.exception))

    def test_svg_with_an_external_reference_is_rejected(self):
        write_submission(self.clone, "new-piece",
                         meta_for("new-piece", cycle=2, previous="already-here"),
                         piece='<svg xmlns="http://www.w3.org/2000/svg">'
                               '<image href="https://example.com/a.png"/></svg>')
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("outside itself", str(cm.exception))

    def test_unparseable_svg_is_rejected(self):
        write_submission(self.clone, "new-piece",
                         meta_for("new-piece", cycle=2, previous="already-here"),
                         piece="<svg><circle></svg>")
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("parse", str(cm.exception))

    def test_svg_with_an_external_css_reference_is_rejected(self):
        write_submission(self.clone, "new-piece",
                         meta_for("new-piece", cycle=2, previous="already-here"),
                         piece='<svg xmlns="http://www.w3.org/2000/svg">'
                               '<style>@import url(https://evil.example/x.css);'
                               '</style><circle r="1"/></svg>')
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("@import", str(cm.exception))

    def test_svg_with_an_external_fill_url_is_rejected(self):
        write_submission(self.clone, "new-piece",
                         meta_for("new-piece", cycle=2, previous="already-here"),
                         piece='<svg xmlns="http://www.w3.org/2000/svg">'
                               '<circle r="1" fill="url(https://x.example/a.svg#g)"/>'
                               '</svg>')
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("outside itself", str(cm.exception))

    def test_a_fragment_url_reference_is_allowed(self):
        write_submission(self.clone, "new-piece",
                         meta_for("new-piece", cycle=2, previous="already-here"),
                         piece='<svg xmlns="http://www.w3.org/2000/svg">'
                               '<circle r="1" fill="url(#grad)"/>'
                               '<use href="#grad"/></svg>')
        self.assertEqual("new-piece", self.gate()["slug"])


class DadaCycleTests(unittest.TestCase):
    """The 10-candidate invariant and its neighbours, checked directly."""

    def test_a_ten_candidate_round_passes(self):
        EW.validate_dada_cycle(dada_cycle("s", cycle=3, previous="p"),
                               "s", 3, "p")

    def test_nine_candidates_is_a_rejection(self):
        with self.assertRaises(EW.GateError) as cm:
            EW.validate_dada_cycle(dada_cycle("s", candidates=9), "s", 1, None)
        self.assertIn("exactly 10", str(cm.exception))

    def test_eleven_candidates_is_a_rejection(self):
        with self.assertRaises(EW.GateError):
            EW.validate_dada_cycle(dada_cycle("s", candidates=11), "s", 1, None)

    def test_one_short_round_among_many_is_caught(self):
        cycle = dada_cycle("s", rounds=3)
        cycle["rounds"][1]["candidates"].pop()
        with self.assertRaises(EW.GateError) as cm:
            EW.validate_dada_cycle(cycle, "s", 1, None)
        self.assertIn("round 2", str(cm.exception))

    def test_rounds_must_be_between_one_and_five(self):
        with self.assertRaises(EW.GateError):
            EW.validate_dada_cycle(dada_cycle("s", rounds=6), "s", 1, None)
        with self.assertRaises(EW.GateError):
            EW.validate_dada_cycle(dada_cycle("s", rounds=0), "s", 1, None)
        EW.validate_dada_cycle(dada_cycle("s", rounds=5), "s", 1, None)

    def test_exactly_six_named_score_dimensions(self):
        five = EW.SCORE_DIMENSIONS[:5]
        with self.assertRaises(EW.GateError) as cm:
            EW.validate_dada_cycle(dada_cycle("s", dimensions=five), "s", 1, None)
        self.assertIn("scores", str(cm.exception))
        seven = EW.SCORE_DIMENSIONS + ("vibes",)
        with self.assertRaises(EW.GateError):
            EW.validate_dada_cycle(dada_cycle("s", dimensions=seven), "s", 1, None)

    def test_scores_must_be_numbers_in_range(self):
        cycle = dada_cycle("s")
        cycle["rounds"][0]["candidates"][3]["scores"]["craft"] = "high"
        with self.assertRaises(EW.GateError):
            EW.validate_dada_cycle(cycle, "s", 1, None)
        cycle = dada_cycle("s")
        cycle["rounds"][0]["candidates"][3]["scores"]["craft"] = 99
        with self.assertRaises(EW.GateError):
            EW.validate_dada_cycle(cycle, "s", 1, None)

    def test_duplicate_candidate_ids_are_rejected(self):
        cycle = dada_cycle("s")
        cycle["rounds"][0]["candidates"][2]["id"] = "r1c1"
        with self.assertRaises(EW.GateError) as cm:
            EW.validate_dada_cycle(cycle, "s", 1, None)
        self.assertIn("repeats", str(cm.exception))

    def test_the_winner_must_be_the_final_round_selection(self):
        cycle = dada_cycle("s", rounds=2)
        cycle["winner"]["candidate"] = "r2c9"
        with self.assertRaises(EW.GateError) as cm:
            EW.validate_dada_cycle(cycle, "s", 1, None)
        self.assertIn("final round selected", str(cm.exception))

    def test_the_winner_must_name_the_submission(self):
        cycle = dada_cycle("s")
        cycle["winner"]["slug"] = "some-other-thing"
        with self.assertRaises(EW.GateError):
            EW.validate_dada_cycle(cycle, "s", 1, None)

    def test_cycle_continuity_is_enforced_in_both_directions(self):
        with self.assertRaises(EW.GateError) as cm:
            EW.validate_dada_cycle(dada_cycle("s", cycle=1, previous=None),
                                   "s", 4, "prior-slug")
        self.assertIn("continuity", str(cm.exception))
        with self.assertRaises(EW.GateError):
            EW.validate_dada_cycle(dada_cycle("s", cycle=4, previous="wrong"),
                                   "s", 4, "prior-slug")
        EW.validate_dada_cycle(dada_cycle("s", cycle=4, previous="prior-slug"),
                               "s", 4, "prior-slug")

    def test_a_missing_cycle_block_is_a_rejection(self):
        with self.assertRaises(EW.GateError):
            EW.validate_dada_cycle(None, "s", 1, None)


# ── end to end, against temp repos ──────────────────────────────────────────

def healthy():
    return {"status": "healthy", "failed": [], "critical": [],
            "checks": [], "summary": "all good"}


def critical(*ids):
    return {"status": "critical", "failed": list(ids), "critical": list(ids),
            "checks": [], "summary": "on fire"}


def worker_cfg(**overrides):
    cfg = {
        "level": 3,
        "instance_name": "Dada Collective",
        "copilot_model": "test-model",
        "evolve_timeout_s": 60,
        "daily_evolve_budget": 10,
        "evolve_interval_hours": 0,
        "notify": True,
        "notify_handle": "+15550000001",
        "report_number": "+15550000002",
        "commons_repo": "kody-w/public-art-collective",
        "creative_state_file": "state/evolve-creative-state.json",
        "evolve_worker": {"enabled": True, "degraded_allowlist": ["w_openrappter_spin"]},
    }
    worker_block = overrides.pop("evolve_worker", None)
    cfg.update(overrides)
    if worker_block:
        cfg["evolve_worker"] = {**cfg["evolve_worker"], **worker_block}
    return cfg


class FakeGh:
    """Just enough GitHub to prove the controller believes only evidence.

    `pr view` answers from the bare origin repo, not from what the controller
    thinks it pushed, and `pr merge` really moves origin/main — so the
    post-merge re-read in publish() is exercised for real.
    """

    def __init__(self, origin, base="main"):
        self.origin = Path(origin)
        self.base = base
        self.calls = []
        self.extra_file = None      # to fake a PR that touches more than it should
        self.merged = False

    def __call__(self, *args, timeout=None):
        self.calls.append(args)
        if args[:2] == ("pr", "create"):
            self.branch = args[args.index("--head") + 1]
            return "https://github.com/kody-w/public-art-collective/pull/7\n"
        if args[:2] == ("pr", "view") and "files" in args[-1]:
            names = [line.split("\t") for line in git_bare(
                self.origin, "diff", "--name-status",
                f"{self.base}..{self.branch}").splitlines() if line.strip()]
            files = [{"path": path, "additions": 1, "deletions": 0}
                     for _, path in names]
            if self.extra_file:
                files.append({"path": self.extra_file, "additions": 1,
                              "deletions": 2})
            return json.dumps({"files": files, "state": "OPEN",
                               "baseRefName": self.base,
                               "headRefName": self.branch,
                               "isCrossRepository": False})
        if args[:2] == ("pr", "merge"):
            sha = git_bare(self.origin, "rev-parse", self.branch).strip()
            git_bare(self.origin, "update-ref", f"refs/heads/{self.base}", sha)
            self.merge_sha = sha
            self.merged = True
            return ""
        if args[:2] == ("pr", "view"):
            return json.dumps({"state": "MERGED" if self.merged else "OPEN",
                               "merged": self.merged,
                               "mergeCommit": {"oid": getattr(self, "merge_sha", "")}})
        if args[:2] == ("pr", "close"):
            return ""
        raise AssertionError(f"unexpected gh call: {args}")

    def called(self, *prefix):
        return any(call[:len(prefix)] == prefix for call in self.calls)


class WorkerEnv(ScratchCase):
    """A worker pointed at a temp origin repo with a scripted GitHub."""

    def setUp(self):
        super().setUp()
        self.origin = self.home / "origin.git"
        seed = self.home / "seed"
        seed.mkdir()
        git(seed, "init", "-b", "main")
        git(seed, "config", "user.email", "t@example.com")
        git(seed, "config", "user.name", "t")
        write_submission(seed, "already-here")
        (seed / "submissions" / "index.json").write_text('{"submissions": []}',
                                                         encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-m", "seed")
        git(self.home, "init", "--bare", "-b", "main", str(self.origin))
        git(seed, "remote", "add", "origin", str(self.origin))
        git(seed, "push", "-u", "origin", "main")
        self.gh = FakeGh(self.origin)
        for name, value in (("_gh", self.gh),):
            p = mock.patch.object(EW, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("identities", lambda: {s: f"rappid:test:{s}"
                                                    for s in EW.NB.NEIGHBORS}),
                            ("roll_call", lambda: {s: {"alive": True}
                                                   for s in EW.NB.NEIGHBORS}),
                            ("chain_path", lambda slug: self.home / f"{slug}.jsonl")):
            p = mock.patch.object(EW.NB, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.cfg = worker_cfg(evolve_worker={
            "repo": str(self.origin),
            "git_author_name": "test",
            "git_author_email": "t@example.com",
        })

    def model_that_submits(self, slug="new-piece", cycle=1, previous=None,
                           result="CONTRIBUTED it made a thing"):
        def fake(workspace, prompt, wcfg):
            self.prompt = prompt
            clone = Path(workspace) / "clone"
            write_submission(clone, slug, meta_for(slug, cycle=cycle,
                                                   previous=previous))
            (Path(workspace) / "state-out.json").write_text(json.dumps({
                "cycle": cycle, "last_slug": slug, "notes": "learned things",
            }), encoding="utf-8")
            return "ok", f"working...\nSENTINEL_RESULT: {result}\n"
        return fake

    def workspaces(self):
        root = self.home / "state" / "evolve-workspaces"
        return sorted(p.name for p in root.iterdir()) if root.exists() else []


class WorkerRunTests(WorkerEnv):
    # ── the verified success path ──
    def test_a_verified_merge_updates_every_ledger_exactly_once(self):
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())

        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"], summary)
        self.assertTrue(self.gh.merged)

        # the piece really is on the base branch now
        listed = git_bare(self.origin, "ls-tree", "--name-only", "-r", "main")
        self.assertIn("submissions/new-piece/piece.svg", listed)

        history = json.loads(EW.HISTORY_PATH.read_text())
        self.assertEqual(1, len(history))
        self.assertEqual(EW.OUTCOME_CONTRIBUTED, history[0]["outcome"])
        self.assertEqual(summary["receipts"]["merge_commit"],
                         history[0]["merge_commit"])

        state = json.loads((self.state / "evolve-creative-state.json").read_text())
        self.assertEqual(1, state["cycle"])
        self.assertEqual("new-piece", state["last_slug"])

        self.assertEqual(1, len(self.frames))
        self.assertEqual("neighbor.acted", self.frames[0][1])
        self.assertTrue(self.frames[0][2]["merged"])

        self.assertEqual(1, len(self.notifications))
        self.assertIn(EW.SUCCESS_PREFIX, self.texts()[0])
        self.assertEqual([], self.workspaces(), "the temp clone must be removed")

    def test_the_second_cycle_must_continue_the_first(self):
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        # a model that restarts the count at 1 is rejected, not published
        with mock.patch.object(EW, "run_model",
                               self.model_that_submits(slug="second-piece")):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_REJECTED, summary["outcome"])
        self.assertIn("continuity", summary["detail"])
        # and the honest continuation is accepted
        with mock.patch.object(EW, "run_model",
                               self.model_that_submits(slug="second-piece", cycle=2,
                                                       previous="new-piece")):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"], summary)

    def test_the_prompt_forbids_publishing_and_names_the_invariants(self):
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        for phrase in ("git commit", "git push", "gh pr create", "gh pr merge",
                       "Do not create branches", "UNCOMMITTED",
                       f"EXACTLY {EW.CANDIDATES_PER_ROUND}", "state-out.json"):
            self.assertIn(phrase, self.prompt)

    # ── the refusals ──
    def test_a_critical_check_before_the_merge_aborts_and_closes_the_pr(self):
        phases = {"start": healthy(), "pre-write": healthy(),
                  "pre-merge": critical("rb_workflows")}
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: phases[phase])

        self.assertEqual(EW.OUTCOME_ABORTED, summary["outcome"])
        self.assertIn("rb_workflows", summary["detail"])
        self.assertFalse(self.gh.called("pr", "merge"), "must not merge mid-outage")
        self.assertTrue(self.gh.called("pr", "close"), "the open PR must be closed")
        self.assertNotIn("submissions/new-piece",
                         git_bare(self.origin, "ls-tree", "--name-only", "-r", "main"))
        self.assertFalse(any(EW.SUCCESS_PREFIX in n for n in self.texts()))
        self.assertFalse((self.state / "evolve-creative-state.json").exists(),
                         "an aborted cycle must not advance the creative ledger")
        history = json.loads(EW.HISTORY_PATH.read_text())
        self.assertEqual(EW.OUTCOME_ABORTED, history[0]["outcome"])
        self.assertEqual([], self.workspaces())

    def test_a_critical_check_at_the_start_never_spends_the_model(self):
        with mock.patch.object(EW, "run_model") as model:
            summary = EW.run_once(cfg=self.cfg,
                                  health=lambda phase: critical("rv_validation"))
        self.assertEqual("skipped", summary["outcome"])
        model.assert_not_called()
        self.assertEqual([], EW.load_history())

    def test_degraded_runs_only_when_every_failing_id_is_allowlisted(self):
        allowed = {"status": "degraded", "critical": [],
                   "failed": ["w_openrappter_spin"], "checks": [], "summary": "x"}
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: allowed)
        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"], summary)

        unlisted = {"status": "degraded", "critical": [],
                    "failed": ["w_openrappter_spin", "rb_shards"],
                    "checks": [], "summary": "x"}
        with mock.patch.object(EW, "run_model") as model:
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: unlisted)
        self.assertEqual("skipped", summary["outcome"])
        self.assertIn("rb_shards", summary["reason"])
        model.assert_not_called()

    def test_a_pr_that_touches_more_than_the_submission_is_never_merged(self):
        self.gh.extra_file = "submissions/index.json"
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_REJECTED, summary["outcome"])
        self.assertIn("index.json", summary["detail"])
        self.assertFalse(self.gh.called("pr", "merge"))
        self.assertTrue(self.gh.called("pr", "close"))
        self.assertFalse(any(EW.SUCCESS_PREFIX in n for n in self.texts()))

    def test_a_failed_pr_creation_does_not_orphan_the_pushed_branch(self):
        def failing_create(*args, timeout=None):
            self.gh.calls.append(args)
            if args[:2] == ("pr", "create"):
                raise EW.CommandError("gh pr create exited 1: rate limited")
            raise AssertionError(f"unexpected gh call: {args}")
        with mock.patch.object(EW, "_gh", failing_create), \
             mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_FAILED, summary["outcome"])
        branches = git_bare(self.origin, "for-each-ref", "--format=%(refname)",
                            "refs/heads/")
        self.assertEqual(["refs/heads/main"], branches.split(),
                         "a pushed branch with no PR must be deleted again")
        self.assertFalse(any(EW.SUCCESS_PREFIX in n for n in self.texts()))

    def test_a_timeout_is_recorded_without_a_success_shaped_alert(self):
        def timing_out(workspace, prompt, wcfg):
            return EW.OUTCOME_TIMEOUT, "copilot timed out after 60s"
        with mock.patch.object(EW, "run_model", timing_out):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())

        self.assertEqual(EW.OUTCOME_TIMEOUT, summary["outcome"])
        history = json.loads(EW.HISTORY_PATH.read_text())
        self.assertEqual(1, len(history), "a timeout still consumed the budget")
        self.assertEqual(EW.OUTCOME_TIMEOUT, history[0]["outcome"])
        self.assertFalse(self.gh.calls, "nothing may reach GitHub after a timeout")
        self.assertEqual(1, len(self.notifications))
        self.assertNotIn(EW.SUCCESS_PREFIX, self.texts()[0])
        self.assertIn("timeout", self.texts()[0])
        self.assertFalse((self.state / "evolve-creative-state.json").exists())
        self.assertEqual([], self.workspaces(), "a timeout still cleans up")

    def test_the_real_timeout_path_maps_to_the_timeout_outcome(self):
        with mock.patch.object(EW.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("copilot", 60)):
            status, out = EW.run_model(self.home, "prompt", EW.worker_config({}))
        self.assertEqual(EW.OUTCOME_TIMEOUT, status)
        self.assertIn("timed out", out)

    def test_a_declined_cycle_is_recorded_without_a_paintbrush(self):
        def declining(workspace, prompt, wcfg):
            return "ok", "SENTINEL_RESULT: DECLINED nothing worth making today\n"
        with mock.patch.object(EW, "run_model", declining):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_DECLINED, summary["outcome"])
        self.assertFalse(self.gh.calls)
        self.assertEqual([], self.notifications, "declines are quiet by default")
        history = json.loads(EW.HISTORY_PATH.read_text())
        self.assertEqual(EW.OUTCOME_DECLINED, history[0]["outcome"])
        self.assertFalse((self.state / "evolve-creative-state.json").exists())

    def test_a_model_that_commits_its_own_work_is_rejected(self):
        def publishing(workspace, prompt, wcfg):
            clone = Path(workspace) / "clone"
            write_submission(clone, "new-piece", meta_for("new-piece"))
            git(clone, "config", "user.email", "t@example.com")
            git(clone, "config", "user.name", "t")
            git(clone, "add", "-A")
            git(clone, "commit", "-m", "I published myself")
            return "ok", "SENTINEL_RESULT: CONTRIBUTED and I merged it\n"
        with mock.patch.object(EW, "run_model", publishing):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_REJECTED, summary["outcome"])
        self.assertIn("committed", summary["detail"])
        self.assertFalse(self.gh.calls)
        self.assertFalse(any(EW.SUCCESS_PREFIX in n for n in self.texts()))

    def test_a_missing_next_state_file_is_rejected_before_any_remote_call(self):
        def no_state(workspace, prompt, wcfg):
            write_submission(Path(workspace) / "clone", "new-piece",
                             meta_for("new-piece"))
            return "ok", "SENTINEL_RESULT: CONTRIBUTED\n"
        with mock.patch.object(EW, "run_model", no_state):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_REJECTED, summary["outcome"])
        self.assertIn("state-out.json", summary["detail"])
        self.assertFalse(self.gh.calls)

    def test_roles_rotate_across_passes(self):
        seen = []

        def watcher(workspace, prompt, wcfg):
            seen.append(wcfg["role"])
            return EW.OUTCOME_FAILED, "no model here"
        with mock.patch.object(EW, "run_model", watcher):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(2, len(seen))
        self.assertNotEqual(seen[0], seen[1], "one neighbor must not dominate")

    def test_the_worker_stands_down_for_the_stop_file(self):
        EW.STOP.write_text("halt", encoding="utf-8")
        with mock.patch.object(EW, "run_model") as model:
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual("skipped", summary["outcome"])
        model.assert_not_called()

    def test_a_disabled_worker_does_nothing(self):
        cfg = dict(self.cfg, evolve_worker={"enabled": False})
        with mock.patch.object(EW, "run_model") as model:
            summary = EW.run_once(cfg=cfg, health=lambda phase: healthy())
        self.assertEqual("skipped", summary["outcome"])
        model.assert_not_called()


def child_result(role, n=6, ok=True, wave=1, error="", critique=()):
    """A sub-sentinel report in the shape subsentinels.run_children returns."""
    report = None
    if ok:
        report = {
            "role": role,
            "candidates": [
                {"id": f"c{i}", "premise": f"{role} premise {i}",
                 "rationale": "because",
                 "scores": {d: 5 + (i % 3) for d in SS.SCORE_DIMENSIONS}}
                for i in range(1, n + 1)],
            "evidence": [{"claim": "checked", "source": "prior.json"}],
            "critique": list(critique),
        }
    return {"role": role, "wave": wave, "ok": ok, "error": error,
            "timed_out": False, "exit_code": 0 if ok else 1, "elapsed_s": 2.0,
            "report": report}


class FanoutIntegrationTests(WorkerEnv):
    """The fan-out inside a real cycle: bounded, binding, and never silent."""

    def setUp(self):
        super().setUp()
        self.cfg = worker_cfg(evolve_worker={
            "repo": str(self.origin),
            "git_author_name": "test",
            "git_author_email": "t@example.com",
            "fanout": {"enabled": True, "children": 3},
        })

    def maker_using_finalists(self, slug="new-piece", cycle=1, previous=None,
                              honour=True):
        """A maker that reads the finalists the controller wrote for it."""
        def fake(workspace, prompt, wcfg):
            self.prompt = prompt
            data = json.loads((Path(workspace) / "finalists.json").read_text())
            self.finalists = [c["id"] for c in data["finalists"]]
            clone = Path(workspace) / "clone"
            write_submission(clone, slug, meta_for(
                slug, cycle=cycle, previous=previous,
                round1_ids=self.finalists if honour else None))
            (Path(workspace) / "state-out.json").write_text(json.dumps({
                "cycle": cycle, "last_slug": slug, "notes": "n"}), encoding="utf-8")
            return "ok", "SENTINEL_RESULT: CONTRIBUTED\n"
        return fake

    def patched_children(self, results):
        return mock.patch.object(EW.SS, "run_children", return_value=results)

    def test_the_finalists_bind_round_one_and_the_cycle_merges(self):
        results = [child_result("novelty-archaeologist"),
                   child_result("execution-designer"),
                   child_result("adversarial-verifier", wave=2)]
        with self.patched_children(results), \
             mock.patch.object(EW, "run_model", self.maker_using_finalists()):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())

        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"], summary)
        self.assertEqual(10, len(self.finalists))
        self.assertIn("THE TEN FINALISTS", self.prompt)
        self.assertIn("MUST be exactly these ten ids", self.prompt)
        for cid in self.finalists:
            self.assertIn(cid, self.prompt)
        row = json.loads(EW.HISTORY_PATH.read_text())[0]
        self.assertEqual(3, row["children"])
        self.assertEqual([], row["child_failures"])
        self.assertEqual(3, SS.children_spent([row]))

    def test_a_maker_that_ignores_its_sub_sentinels_is_rejected(self):
        results = [child_result("a"), child_result("b"),
                   child_result("c", wave=2)]
        with self.patched_children(results), \
             mock.patch.object(EW, "run_model",
                               self.maker_using_finalists(honour=False)):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())

        self.assertEqual(EW.OUTCOME_REJECTED, summary["outcome"])
        self.assertIn("exactly the ten finalists", summary["detail"])
        self.assertFalse(self.gh.calls, "nothing reaches GitHub")
        self.assertFalse(any(EW.SUCCESS_PREFIX in n for n in self.texts()))

    def test_a_partial_failure_that_still_yields_ten_continues(self):
        results = [child_result("a"), child_result("b"),
                   child_result("c", wave=2, ok=False, error="timed out after 600s")]
        with self.patched_children(results), \
             mock.patch.object(EW, "run_model", self.maker_using_finalists()):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())

        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"], summary)
        row = json.loads(EW.HISTORY_PATH.read_text())[0]
        self.assertEqual(["c: timed out after 600s"], row["child_failures"])
        self.assertIn("CHILDREN THAT FAILED", self.prompt,
                      "the maker is told what it did not get")

    def test_a_failed_fanout_never_reaches_the_maker(self):
        results = [child_result("a", ok=False, error="wrote no report.json"),
                   child_result("b", ok=False, error="exited 1"),
                   child_result("c", wave=2, ok=False, error="timed out")]
        with self.patched_children(results), \
             mock.patch.object(EW, "run_model") as maker:
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())

        self.assertEqual(EW.OUTCOME_FANOUT, summary["outcome"])
        maker.assert_not_called()
        self.assertFalse(self.gh.calls)
        self.assertFalse(any(EW.SUCCESS_PREFIX in n for n in self.texts()))
        self.assertEqual(1, len(self.notifications))
        self.assertIn("fanout-failed", self.texts()[0])
        row = json.loads(EW.HISTORY_PATH.read_text())[0]
        self.assertEqual(EW.OUTCOME_FANOUT, row["outcome"])
        self.assertEqual(3, row["children"], "children cost credit even so")
        self.assertEqual(3, len(row["child_failures"]))
        self.assertFalse((self.state / "evolve-creative-state.json").exists())
        self.assertEqual([], self.workspaces())

    def test_too_few_survivors_is_a_named_failure_not_nine_finalists(self):
        results = [child_result("a", n=5), child_result("b", n=4)]
        with self.patched_children(results), \
             mock.patch.object(EW, "run_model") as maker:
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_FANOUT, summary["outcome"])
        self.assertIn("9 candidate(s) survived", summary["detail"])
        maker.assert_not_called()

    def test_an_unavailable_fanout_skips_instead_of_making_art_alone(self):
        cfg = worker_cfg(evolve_worker={
            "repo": str(self.origin),
            "fanout": {"enabled": True, "children": 3, "daily_child_budget": 0},
        })
        with mock.patch.object(EW, "run_model") as maker, \
             mock.patch.object(EW.SS, "run_children") as children:
            summary = EW.run_once(cfg=cfg, health=lambda phase: healthy())

        self.assertEqual("skipped", summary["outcome"])
        self.assertIn("child budget spent", summary["reason"])
        maker.assert_not_called()
        children.assert_not_called()
        row = json.loads(EW.HISTORY_PATH.read_text())[0]
        self.assertTrue(row["skipped"], "a skipped cycle must not spend budget")
        self.assertEqual(0, len(EW.spend_rows(json.loads(
            EW.HISTORY_PATH.read_text()))))

    def test_a_nested_worker_run_refuses_itself(self):
        with mock.patch.dict(os.environ, {SS.DEPTH_ENV: "1"}), \
             mock.patch.object(EW, "run_model") as maker, \
             mock.patch.object(EW.SS, "run_children") as children:
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual("skipped", summary["outcome"])
        self.assertIn("nested run refused", summary["reason"])
        maker.assert_not_called()
        children.assert_not_called()

    def test_children_are_planned_but_never_spawned_by_a_child(self):
        fcfg = SS.fanout_config(EW.worker_config(self.cfg))
        self.assertEqual(3, len(SS.plan_children(fcfg, [], 0)[0]))
        self.assertEqual([], SS.plan_children(fcfg, [], 1)[0])

    def test_children_receive_every_prior_submission_but_no_repository(self):
        results = [child_result("a"), child_result("b"),
                   child_result("c", wave=2)]
        with self.patched_children(results) as children, \
             mock.patch.object(EW, "run_model", self.maker_using_finalists()):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())

        prior = children.call_args.args[6]
        self.assertEqual(["already-here"], [p["slug"] for p in prior])
        self.assertIn("title", prior[0])
        workspace = children.call_args.args[2]
        self.assertFalse((Path(workspace) / "children" / "a" / ".git").exists(),
                         "a child never gets a repository of its own")

    def test_the_dry_run_reports_the_planned_cast(self):
        summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy(),
                              dry_run=True)
        self.assertEqual("dry-run", summary["outcome"])
        self.assertEqual(0, summary["depth"])
        self.assertEqual(["novelty-archaeologist", "execution-designer",
                          "adversarial-verifier"], summary["children"])

    def test_a_solo_cycle_still_works_when_the_fanout_is_off(self):
        cfg = worker_cfg(evolve_worker={
            "repo": str(self.origin), "fanout": {"enabled": False}})
        with mock.patch.object(EW, "run_model", self.model_that_submits()), \
             mock.patch.object(EW.SS, "run_children") as children:
            summary = EW.run_once(cfg=cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"], summary)
        children.assert_not_called()
        self.assertNotIn("THE TEN FINALISTS", self.prompt)


class ArtNotificationTests(WorkerEnv):
    """The message a merge earns — and the silence everything else earns."""

    VIEW = ("https://kody-w.github.io/public-art-collective/"
            "submissions/new-piece/piece.svg")
    SOURCE = ("https://github.com/kody-w/public-art-collective/blob/main/"
              "submissions/new-piece/piece.svg")

    def test_exactly_one_message_on_a_verified_merge(self):
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"], summary)
        self.assertEqual(1, len(self.notifications),
                         "one merge, one message — not two, not zero")

        note = self.notifications[0]
        text = note["text"]
        self.assertIn(EW.SUCCESS_PREFIX, text)
        self.assertIn("New Piece", text, "the title a human recognises")
        self.assertIn(f"View: {self.VIEW}", text, "one tap to the artwork")
        self.assertIn(f"Source: {self.SOURCE}", text)
        self.assertIn(f"PR: {summary['receipts']['pr_url']}", text)
        self.assertEqual("+15550000002", note["to"],
                         "art news goes to the configured report number")

    def test_the_message_carries_a_one_sentence_concept(self):
        meta = meta_for("new-piece", _artist_statement=(
            "A clock that lies about the time. Then it goes on at length "
            "about clocks for several more sentences nobody will read on a "
            "phone."))

        def maker(workspace, prompt, wcfg):
            write_submission(Path(workspace) / "clone", "new-piece", meta)
            (Path(workspace) / "state-out.json").write_text(json.dumps(
                {"cycle": 1, "last_slug": "new-piece", "notes": "n"}),
                encoding="utf-8")
            return "ok", "SENTINEL_RESULT: CONTRIBUTED\n"

        with mock.patch.object(EW, "run_model", maker):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        text = self.texts()[0]
        self.assertIn("A clock that lies about the time.\n", text,
                      "one sentence, no trailing whitespace before the links")
        self.assertNotIn("nobody will read on a phone", text)

    def test_a_long_statement_is_truncated_not_pasted(self):
        self.assertEqual(80, len(EW.concept_sentence(
            {"_concept": "x" * 500}, limit=80)))
        self.assertTrue(EW.concept_sentence({"_concept": "x" * 500},
                                            limit=80).endswith("\u2026"))

    def test_the_concept_falls_back_to_the_winning_premise(self):
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertIn("premise 1.1", self.texts()[0],
                      "the premise that actually won its cycle")

    def test_the_static_report_is_rebuilt_after_the_merge_is_recorded(self):
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        note = self.notifications[0]
        self.assertTrue(note["rebuild"], "linked evidence must be current")
        self.assertEqual([EW.OUTCOME_CONTRIBUTED],
                         [r["outcome"] for r in note["history"]],
                         "the ledger is written before the message is built")

    def test_commons_repo_wins_over_the_worker_repo(self):
        cfg = dict(self.cfg, commons_repo="someone-else/other-commons")
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            EW.run_once(cfg=cfg, health=lambda phase: healthy())
        text = self.texts()[0]
        self.assertIn("https://someone-else.github.io/other-commons/"
                      "submissions/new-piece/piece.svg", text)
        self.assertIn("https://github.com/someone-else/other-commons/blob/main/"
                      "submissions/new-piece/piece.svg", text)

    def test_no_message_for_a_timeout(self):
        with mock.patch.object(EW, "run_model",
                               lambda ws, p, w: (EW.OUTCOME_TIMEOUT, "timed out")):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(1, len(self.notifications))
        self.assertNotIn("View:", self.texts()[0])
        self.assertNotIn(EW.SUCCESS_PREFIX, self.texts()[0])

    def test_no_message_for_a_declined_cycle(self):
        with mock.patch.object(
                EW, "run_model",
                lambda ws, p, w: ("ok", "SENTINEL_RESULT: DECLINED not today\n")):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual([], self.notifications)

    def test_a_model_claiming_success_without_a_merge_gets_no_art_message(self):
        # SENTINEL_RESULT is a claim; the health abort means nothing merged.
        phases = {"start": healthy(), "pre-write": healthy(),
                  "pre-merge": critical("rb_workflows")}
        with mock.patch.object(EW, "run_model", self.model_that_submits(
                result="CONTRIBUTED and it is live, I promise")):
            summary = EW.run_once(cfg=self.cfg,
                                  health=lambda phase: phases[phase])
        self.assertEqual(EW.OUTCOME_ABORTED, summary["outcome"])
        for text in self.texts():
            self.assertNotIn("View:", text)
            self.assertNotIn(EW.SUCCESS_PREFIX, text)

    def test_no_art_message_when_the_gate_rejects(self):
        with mock.patch.object(EW, "run_model", self.model_that_submits(
                slug="second-piece", cycle=9)):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_REJECTED, summary["outcome"])
        self.assertFalse(any("View:" in t for t in self.texts()))


class ArtUrlTests(unittest.TestCase):
    """URL derivation is deterministic, encoded, and per-extension correct."""

    def urls(self, piece_path, commons="kody-w/public-art-collective",
             base="main"):
        return EW.art_urls({"commons_repo": commons},
                           {"repo": "ignored/when-commons-set",
                            "base_branch": base},
                           {"piece_path": piece_path})

    def test_every_supported_extension_maps_to_its_own_path(self):
        for ext in sorted(EW.KIND_EXTENSIONS.values()):
            with self.subTest(ext=ext):
                view, source = self.urls(f"submissions/a-slug/piece{ext}")
                self.assertEqual(
                    f"https://kody-w.github.io/public-art-collective/"
                    f"submissions/a-slug/piece{ext}", view)
                self.assertEqual(
                    f"https://github.com/kody-w/public-art-collective/blob/main/"
                    f"submissions/a-slug/piece{ext}", source)

    def test_path_separators_survive_and_segments_are_encoded(self):
        view, source = self.urls("submissions/a b/piece.svg")
        self.assertIn("/submissions/a%20b/piece.svg", view)
        self.assertIn("/submissions/a%20b/piece.svg", source)
        self.assertEqual(4, view.count("/") - 2, "slashes are not encoded away")

    def test_a_non_default_branch_lands_in_the_source_url(self):
        _, source = self.urls("submissions/a/piece.md", base="trunk")
        self.assertIn("/blob/trunk/", source)

    def test_the_pages_host_is_lowercased_but_the_repo_is_not(self):
        view, source = self.urls("submissions/a/piece.svg",
                                 commons="Kody-W/Public-Art-Collective")
        self.assertTrue(view.startswith("https://kody-w.github.io/Public-Art-Collective/"))
        self.assertIn("github.com/Kody-W/Public-Art-Collective/", source)

    def test_a_full_url_or_git_remote_still_yields_owner_and_name(self):
        for commons in ("https://github.com/kody-w/public-art-collective",
                        "https://github.com/kody-w/public-art-collective.git",
                        "git@github.com:kody-w/public-art-collective.git"):
            with self.subTest(commons=commons):
                view, _ = self.urls("submissions/a/piece.svg", commons=commons)
                self.assertEqual("https://kody-w.github.io/public-art-collective/"
                                 "submissions/a/piece.svg", view)

    def test_a_local_path_is_not_a_public_url(self):
        view, source = EW.art_urls(
            {"commons_repo": ""}, {"repo": "/Users/someone/origin.git",
                                   "base_branch": "main"},
            {"piece_path": "submissions/a/piece.svg"})
        self.assertEqual(("", ""), (view, source))

    def test_the_worker_repo_is_used_when_commons_repo_is_unset(self):
        view, _ = EW.art_urls({}, {"repo": "kody-w/public-art-collective",
                                   "base_branch": "main"},
                              {"piece_path": "submissions/a/piece.txt"})
        self.assertEqual("https://kody-w.github.io/public-art-collective/"
                         "submissions/a/piece.txt", view)

    def test_a_message_without_a_derivable_url_says_so(self):
        text = EW.art_notification(
            {"instance_name": "Dada"}, {"repo": "/local/path"},
            {"title": "Thing", "piece_path": "submissions/a/piece.svg",
             "meta": {"title": "Thing"}}, {"pr_url": "https://example/pr/1"})
        self.assertIn("no public URL derivable", text)
        self.assertIn("PR: https://example/pr/1", text)

    def test_the_recipient_prefers_the_report_number(self):
        self.assertEqual("+1555", EW.art_recipient(
            {"report_number": "+1555", "notify_handle": "+1999"}))
        self.assertEqual("+1999", EW.art_recipient({"notify_handle": "+1999"}))
        self.assertEqual("", EW.art_recipient({}))


class ArtDeliveryTests(WorkerEnv):
    """The art text goes through the ordinary outbox — once — and is then the
    delivery layer's business to classify, not this worker's to assert."""

    def setUp(self):
        super().setUp()
        import outbox
        import standup
        self.outbox = outbox
        self.enqueued = []
        # sentinel.notify is NOT patched here: the point is to watch the real
        # notify path reach the real queue exactly once.
        for target, name, value in (
                (EW.sentinel, "notify", REAL_NOTIFY),
                (outbox, "enqueue", lambda text, to, attachments=None:
                 self.enqueued.append({"text": text, "to": to,
                                       "attachments": attachments})),
                (outbox, "drain", mock.Mock(return_value=(1, 0, "sent"))),
                (standup, "portable_snapshot", mock.Mock(return_value="snap.html")),
                (standup, "publish_snapshot",
                 mock.Mock(return_value=["http://192.0.2.9:9797/share/x.html"])),
        ):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.drain = outbox.drain
        self.snapshot = standup.portable_snapshot

    def test_a_verified_merge_enqueues_exactly_one_message(self):
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())

        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"], summary)
        self.assertEqual(1, len(self.enqueued), "one merge, one queued message")
        message = self.enqueued[0]
        self.assertEqual("+15550000002", message["to"])
        self.assertIn("New Piece", message["text"])
        self.assertIn("View: https://kody-w.github.io/public-art-collective/"
                      "submissions/new-piece/piece.svg", message["text"])
        self.assertIn("Static HTML report:", message["text"],
                      "the rebuilt report rides along as evidence")
        self.assertTrue(self.snapshot.call_args.kwargs["rebuild"])
        self.assertEqual(1, self.drain.call_count,
                         "the delivery layer, not this worker, decides what "
                         "'sent' means")

    def test_a_queue_only_instance_still_enqueues_and_never_sends(self):
        cfg = dict(self.cfg, notify_queue_only=True)
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            EW.run_once(cfg=cfg, health=lambda phase: healthy())
        self.assertEqual(1, len(self.enqueued))
        self.drain.assert_not_called()

    def test_nothing_is_enqueued_when_the_merge_is_not_verified(self):
        phases = {"start": healthy(), "pre-write": healthy(),
                  "pre-merge": critical("rb_workflows")}
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(cfg=self.cfg,
                                  health=lambda phase: phases[phase])
        self.assertEqual(EW.OUTCOME_ABORTED, summary["outcome"])
        self.assertEqual(1, len(self.enqueued), "the abort is reported…")
        self.assertNotIn("View:", self.enqueued[0]["text"], "…but not as art")
        self.assertNotIn(EW.SUCCESS_PREFIX, self.enqueued[0]["text"])

    def test_the_worker_never_claims_delivery_it_cannot_verify(self):
        # A queue that cannot be drained must not turn a merge into a failure,
        # and must not turn a failed send into a success either: the merge is
        # recorded, the message is queued, and alert_delivery is what tells a
        # human the channel is broken.
        self.outbox.drain.side_effect = RuntimeError("Messages is not running")
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"])
        self.assertEqual(1, len(self.enqueued))
        row = json.loads(EW.HISTORY_PATH.read_text())[0]
        self.assertEqual(EW.OUTCOME_CONTRIBUTED, row["outcome"])
        self.assertIn("merge_commit", row)

    def test_unverified_delivery_blocks_the_next_cycle(self):
        # outbox.status() reports unverified/dead-letter counts (232ce7e);
        # checks.py turns those into a failing alert_delivery, and this worker
        # refuses to make more art until a human has been reachable again.
        degraded = {"status": "degraded", "critical": [],
                    "failed": ["alert_delivery"], "checks": [], "summary": "x"}
        cfg = worker_cfg(evolve_worker={
            "repo": str(self.origin),
            "degraded_allowlist": ["alert_delivery", "w_openrappter_spin"]})
        with mock.patch.object(EW, "run_model") as maker:
            summary = EW.run_once(cfg=cfg, health=lambda phase: degraded)
        self.assertEqual("skipped", summary["outcome"])
        self.assertIn("alert_delivery", summary["reason"])
        maker.assert_not_called()
        self.assertEqual([], self.enqueued)


# ── the tick keeps ticking ──────────────────────────────────────────────────
class TickDelegationTests(unittest.TestCase):
    """Requirement 1: a delegated tick spends no model on art, and still
    diagnoses a critical platform failure."""

    def setUp(self):
        self.home = scratch_dir("tick")
        (self.home / "state").mkdir()
        (self.home / "logs").mkdir()
        for name, value in (("STATE", self.home / "state"),
                            ("LOGS", self.home / "logs"),
                            ("STOP", self.home / "STOP")):
            p = mock.patch.object(sentinel, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name in ("notify", "refresh_dashboard", "publish_head_hook"):
            if hasattr(sentinel, name):
                p = mock.patch.object(sentinel, name, mock.Mock())
                p.start()
                self.addCleanup(p.stop)
        p = mock.patch.object(sentinel, "NB", mock.MagicMock())
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(sentinel, "outsider_smoke", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(sentinel, "config", return_value={
            **sentinel.DEFAULTS, "level": 3, "notify": False,
            "repair_enabled": False,
            "evolve_worker": {"enabled": True},
        })
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_enabled_flag_reads_the_block(self):
        self.assertTrue(sentinel.evolution_worker_enabled(
            {"evolve_worker": {"enabled": True}}))
        self.assertFalse(sentinel.evolution_worker_enabled(
            {"evolve_worker": {"enabled": False}}))
        self.assertFalse(sentinel.evolution_worker_enabled({}))
        self.assertFalse(sentinel.evolution_worker_enabled(
            {"evolve_worker": "yes"}))

    def test_a_healthy_delegated_tick_invokes_no_model(self):
        with mock.patch.object(sentinel, "run_health", return_value=healthy()), \
             mock.patch.object(sentinel, "evolve") as evolve, \
             mock.patch.object(sentinel, "escalate") as escalate:
            self.assertEqual(0, sentinel.main())
        evolve.assert_not_called()
        escalate.assert_not_called()

    def test_a_delegated_tick_still_diagnoses_a_critical_failure(self):
        verdict = {"status": "critical", "failed": ["rb_frontdoor"],
                   "critical": ["rb_frontdoor"], "checks": [], "summary": "down"}
        with mock.patch.object(sentinel, "run_health", return_value=verdict), \
             mock.patch.object(sentinel, "evolve") as evolve, \
             mock.patch.object(sentinel, "escalate",
                               return_value=(True, "SENTINEL_RESULT: BLOCKED x")) as esc:
            self.assertEqual(0, sentinel.main())
        evolve.assert_not_called()
        esc.assert_called_once()
        self.assertEqual("diagnose", esc.call_args.args[3],
                         "repair_enabled=false must still be honored")


if __name__ == "__main__":
    unittest.main()
