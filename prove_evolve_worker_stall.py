#!/usr/bin/env python3
"""Prove the art arm cannot skip the same broken preflight for weeks as green.

Field incident (Dada Collective, running since 2026-08-17): evolve_worker.py
skipped every launchd pass since ~2026-08-22 with `publication profile
preflight failed: azure-reviewed-png requires max_piece_bytes between 4194304
and 33554432`, but w_evolve_worker kept saying ok "skipped 12m ago". The
heartbeat now carries `since` for consecutive identical (outcome, reason)
pairs, and the check treats long broken skips as a WARN failure while keeping
health-gate/budget/cadence skips ok.
"""

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
SCRATCH = ROOT / "state" / "prove-evolve-stall"
SCRATCH.mkdir(parents=True, exist_ok=True)
IMPORT_HOME = tempfile.TemporaryDirectory(dir=str(SCRATCH))
os.environ["SENTINEL_HOME"] = IMPORT_HOME.name
sys.modules.setdefault("requests", types.SimpleNamespace())

import checks as C
import evolve_worker as EW


class EvolveWorkerStallProof(unittest.TestCase):
    def setUp(self):
        self.home_tmp = tempfile.TemporaryDirectory(dir=str(SCRATCH))
        self.addCleanup(self.home_tmp.cleanup)
        self.home = Path(self.home_tmp.name)
        (self.home / "state").mkdir()
        self.patch(C, "HOME", self.home)
        self.patch(EW, "HOME", self.home)
        self.patch(EW, "STATE", self.home / "state")
        self.patch(EW, "STATUS_PATH", self.home / "state" / "evolve-worker-status.json")
        self.patch(EW, "HISTORY_PATH", self.home / "state" / "evolve-worker-history.json")
        self.now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
        self.patch(EW.sentinel, "now", side_effect=lambda: self.now)
        (self.home / "config.json").write_text(json.dumps({
            "evolve_worker": {"enabled": True, "interval_minutes": 30, "stall_hours": 6}
        }), encoding="utf-8")

    def patch(self, target, name, *args, **kwargs):
        p = mock.patch.object(target, name, *args, **kwargs)
        v = p.start()
        self.addCleanup(p.stop)
        return v

    def status(self, **extra):
        current = datetime.now(timezone.utc).isoformat(timespec="seconds")
        data = {"at": current, **extra}
        (self.home / "state" / "evolve-worker-status.json").write_text(
            json.dumps(data), encoding="utf-8")

    def test_status_since_survives_identical_pair_and_resets_on_reason_change(self):
        EW.write_status("skipped", "publication profile preflight failed: bad max")
        first = json.loads(EW.STATUS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(first["at"], first["since"])
        self.now += timedelta(minutes=30)
        EW.write_status("skipped", "publication profile preflight failed: bad max")
        second = json.loads(EW.STATUS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(first["since"], second["since"])
        self.now += timedelta(minutes=30)
        EW.write_status("skipped", "creative cadence (0.5h of 4.0h)")
        third = json.loads(EW.STATUS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(third["at"], third["since"])
        self.assertNotEqual(second["since"], third["since"])

    def test_broken_skip_stalled_for_days_fails_with_last_contribution(self):
        reason = ("publication profile preflight failed: azure-reviewed-png requires "
                  "max_piece_bytes between 4194304 and 33554432")
        self.status(outcome="skipped", reason=reason,
                    since=(datetime.now(timezone.utc) - timedelta(days=35)).isoformat(timespec="seconds"))
        (self.home / "state" / "evolve-worker-history.json").write_text(json.dumps([
            {"outcome": "contributed", "at": "2026-08-22T03:00:00+00:00", "slug": "last-real-piece"}
        ]), encoding="utf-8")
        r = C.evolve_worker_is_alive()
        self.assertFalse(r["ok"], r)
        self.assertEqual(C.WARN, r["severity"])
        self.assertIn("art arm stalled for 35.0d", r["detail"])
        self.assertIn("max_piece_bytes", r["detail"])
        self.assertIn("last contribution 2026-08-22T03:00:00+00:00", r["detail"])

    def test_by_design_skip_and_old_status_without_since_stay_ok(self):
        self.status(outcome="skipped", reason="critical checks failing at start: rb_shards",
                    since=(datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds"))
        self.assertTrue(C.evolve_worker_is_alive()["ok"])
        self.status(outcome="skipped", reason="publication profile preflight failed: bad")
        r = C.evolve_worker_is_alive()
        self.assertTrue(r["ok"], r)
        self.assertIn("duration unknown", r["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
