#!/usr/bin/env python3
"""Host disk/load pressure is visible, cheap, honest, and never a repair page."""
import io
import json
import os
import tempfile
import unittest
from collections import namedtuple
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
SCRATCH = ROOT / "state" / "prove-host-pressure"
SCRATCH.mkdir(parents=True, exist_ok=True)
IMPORT_HOME = tempfile.TemporaryDirectory(dir=str(SCRATCH))
os.environ["SENTINEL_HOME"] = IMPORT_HOME.name

import checks as C
import health as H
import sentinel as S

GIB = 2**30
Usage = namedtuple("Usage", "total used free")


class HostPressureProof(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=str(SCRATCH))
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.patch(C, "HOME", new=self.home)
        self.disk = self.patch(C.shutil, "disk_usage",
                               return_value=Usage(200 * GIB, 160 * GIB, 40 * GIB))
        self.load = self.patch(C.os, "getloadavg", return_value=(20, 10, 10))
        self.cpus = self.patch(C.os, "cpu_count", return_value=10)
        self.commands = self.patch(
            C.subprocess, "run", side_effect=AssertionError("must not spawn a process"))

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def config(self, limits):
        (self.home / "config.json").write_text(
            json.dumps({"host_pressure": limits}), encoding="utf-8")

    def warn(self, needle):
        result = C.host_pressure()
        self.assertEqual("host_pressure", result["id"])
        self.assertFalse(result["ok"], result)
        self.assertEqual(C.WARN, result["severity"])
        self.assertIn(needle, result["detail"])
        return result

    def test_healthy_default_measures_instance_volume_once_without_processes(self):
        result = C.host_pressure()
        self.assertTrue(result["ok"], result)
        self.assertIn("40.00 GiB free (20.0%)", result["detail"])
        self.assertIn(str(self.home), result["detail"])
        self.disk.assert_called_once_with(self.home)
        self.load.assert_called_once_with()
        self.cpus.assert_called_once_with()
        self.commands.assert_not_called()

    def test_absolute_floor_and_disk_full_warn(self):
        for free in (9 * GIB, 0):
            with self.subTest(free=free):
                self.disk.return_value = Usage(100 * GIB, 100 * GIB - free, free)
                self.warn("below floor")

    def test_percentage_floor_is_not_hidden_by_absolute_floor(self):
        self.disk.return_value = Usage(1000 * GIB, 960 * GIB, 40 * GIB)
        self.warn("floor 50.00 GiB - below floor")

    def test_exact_disk_and_load_boundaries_pass(self):
        self.disk.return_value = Usage(200 * GIB, 190 * GIB, 10 * GIB)
        self.load.return_value = (170, 40, 40)
        self.assertTrue(C.host_pressure()["ok"])

    def test_custom_thresholds_and_partial_config(self):
        self.config({"min_free_gib": 41})
        self.warn("floor 41.00 GiB")
        self.config({"min_free_gib": 0, "min_free_percent": 21})
        self.warn("floor 42.00 GiB")
        self.config({"min_free_gib": 0, "min_free_percent": 0, "load_per_core": 2})
        self.load.return_value = (200, 21, 21)
        self.warn("both > 20, 2x cores")

    def test_only_sustained_five_and_fifteen_minute_pressure_warns(self):
        self.load.return_value = (200, 170, 150)
        self.warn("sustained load above ceiling")
        for load in ((200, 10, 10), (200, 170, 10), (200, 10, 170)):
            with self.subTest(load=load):
                self.load.return_value = load
                self.assertTrue(C.host_pressure()["ok"])

    def test_cpu_count_scales_the_load_bar(self):
        self.load.return_value = (50, 50, 50)
        self.cpus.return_value = 16
        self.assertTrue(C.host_pressure()["ok"])
        self.cpus.return_value = 8
        self.warn("both > 32, 4x cores")

    def test_unreadable_disk_does_not_hide_measured_load(self):
        self.disk.side_effect = OSError("permission denied")
        result = self.warn("disk measurement unavailable")
        self.assertIn("load 5m=10.00", result["detail"])

    def test_unavailable_load_or_cpu_count_never_defaults_to_healthy(self):
        for error in (OSError("unavailable"), AttributeError("getloadavg unsupported")):
            self.load.side_effect = error
            self.warn("load measurement unavailable")
        self.load.side_effect = None
        for cores in (None, 0, -1, True):
            self.cpus.return_value = cores
            self.warn("CPU count unavailable")

    def test_invalid_measurements_warn(self):
        for usage in (Usage(0, 0, 0), Usage(100, 101, -1),
                      Usage(100, 0, 101), Usage(float("nan"), 0, 10)):
            self.disk.return_value = usage
            self.warn("invalid disk capacity/free-space measurement")
        self.disk.return_value = Usage(200 * GIB, 160 * GIB, 40 * GIB)
        for load in ((0, -1, 1), (0, float("nan"), 1), (0, 1, float("inf"))):
            self.load.return_value = load
            self.warn("invalid 5/15-minute load measurement")

    def test_invalid_config_never_silently_uses_defaults(self):
        invalid = [None, [], {"min_free_gib": -1}, {"min_free_gib": True},
                   {"min_free_gib": "10"}, {"min_free_gib": float("nan")},
                   {"min_free_gib": float("inf")}, {"min_free_gib": 1e308},
                   {"min_free_percent": 101}, {"load_per_core": 0}]
        for value in invalid:
            with self.subTest(value=value):
                self.config(value)
                self.warn("cannot read host pressure thresholds")
        for text in ('{"host_pressure":', "[]"):
            (self.home / "config.json").write_text(text, encoding="utf-8")
            self.warn("cannot read host pressure thresholds")

    def test_registered_required_and_classified(self):
        self.assertIn(C.host_pressure, C.all_checks())
        manifest = json.loads((ROOT / "required_checks.json").read_text(encoding="utf-8"))
        self.assertIn("host_pressure", manifest["required"])
        self.assertEqual({"domain": "host", "kind": "capacity"},
                         manifest["kinds"]["host_pressure"])

    def test_runner_reports_degraded_never_critical(self):
        self.disk.return_value = Usage(200 * GIB, 199 * GIB, GIB)
        self.load.return_value = (200, 170, 150)
        self.patch(C, "_REGISTRY", new=[C.host_pressure])
        self.patch(H.HUB, "run_all", return_value=[])
        self.patch(H, "probe_watchers", return_value=([], []))
        for name in ("check_outsider_coverage", "check_freshness_pairing",
                     "check_completeness"):
            self.patch(H, name, return_value=C.ok(name))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, H.main())
        verdict = json.loads(output.getvalue())
        self.assertEqual("degraded", verdict["status"])
        self.assertEqual(["host_pressure"], verdict["failed"])
        self.assertEqual([], verdict["critical"])

    def test_pressure_is_local_machinery_and_never_invokes_repair(self):
        self.disk.return_value = Usage(200 * GIB, 199 * GIB, GIB)
        result = C.host_pressure()
        groups = S._group_failing_checks([result])
        self.assertEqual([result], groups["own"])
        self.assertEqual([], groups["platform"])
        state = self.home / "state"
        state.mkdir()
        self.patch(S, "HOME", new=self.home)
        self.patch(S, "STATE", new=state)
        self.patch(S, "STOP", new=self.home / "STOP")
        self.patch(S, "config", return_value=dict(S.DEFAULTS, level=2, notify=False))
        self.patch(S, "run_health", return_value={
            "status": "degraded", "checks": [result], "failed": ["host_pressure"],
            "critical": [], "summary": result["detail"],
        })
        for name in ("log", "ensure_evolution_worker_loaded", "publish_head_hook",
                     "notify", "silence_breaker"):
            self.patch(S, name)
        self.patch(S, "outsider_smoke", return_value=False)
        repair = self.patch(S, "escalate")
        nb = self.patch(S, "NB")
        nb.roll_call.return_value = {}
        nb.peer_roll_call.return_value = {}
        nb.check_anchors.return_value = {}
        self.assertEqual(0, S.main())
        repair.assert_not_called()
        self.commands.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
