#!/usr/bin/env python3
"""Host disk/load/swap/memory pressure is cheap, honest, and never a repair page."""
import errno
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
        self.load = self.patch(C.os, "getloadavg", return_value=(20, 10, 10), create=True)
        self.cpus = self.patch(C.os, "cpu_count", return_value=10)
        self.patch(C.sys, "platform", new="darwin")
        self.swap = C._DarwinSwapUsage(10 * GIB, 8 * GIB, 2 * GIB, 16384, 1)
        self.memory_level = 50
        self.native_errors = {}
        self.native = self.patch(C, "_darwin_sysctl", side_effect=self.read_native)
        self.commands = self.patch(
            C.subprocess, "run", side_effect=AssertionError("must not spawn a process"))

    def read_native(self, name, value):
        if name in self.native_errors:
            raise self.native_errors[name]
        if name == "vm.swapusage":
            self.assertIsInstance(value, C._DarwinSwapUsage)
            return self.swap
        self.assertEqual("kern.memorystatus_level", name)
        self.assertIsInstance(value, C.ctypes.c_uint32)
        return C.ctypes.c_uint32(self.memory_level)

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
        self.assertEqual(["vm.swapusage", "kern.memorystatus_level"],
                         [call.args[0] for call in self.native.call_args_list])
        self.assertIn("swap 2.00/10.00 GiB used (20.0%", result["detail"])
        self.assertIn("memory available level 50%", result["detail"])
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
                   {"min_free_percent": 101}, {"load_per_core": 0},
                   {"max_swap_used_percent": 0}, {"max_swap_used_percent": 101},
                   {"max_swap_used_percent": float("nan")},
                   {"min_memory_percent": -1}, {"min_memory_percent": 101},
                   {"min_memory_percent": True}, {"min_memory_percent": "15"}]
        for value in invalid:
            with self.subTest(value=value):
                self.config(value)
                self.warn("cannot read host pressure thresholds")
        for text in ('{"host_pressure":', "[]"):
            (self.home / "config.json").write_text(text, encoding="utf-8")
            self.warn("cannot read host pressure thresholds")

    def test_swap_warns_at_exact_threshold_and_above_but_not_below(self):
        for used, expected in ((84, True), (85, False), (94, False), (100, False)):
            with self.subTest(used=used):
                self.swap = C._DarwinSwapUsage(
                    100 * GIB, (100 - used) * GIB, used * GIB, 16384, 1)
                result = C.host_pressure()
                self.assertEqual(expected, result["ok"], result)
                self.assertEqual(C.WARN, result["severity"])
                self.assertIn("warn >= 85%", result["detail"])
                if not expected:
                    self.assertIn("high swap usage", result["detail"])

    def test_swap_override_and_zero_allocation_are_explicit(self):
        self.config({"max_swap_used_percent": 20})
        self.warn("high swap usage")
        self.config({"max_swap_used_percent": 21})
        self.assertTrue(C.host_pressure()["ok"])
        self.swap = C._DarwinSwapUsage()
        result = C.host_pressure()
        self.assertTrue(result["ok"], result)
        self.assertIn("no swap allocated", result["detail"])

    def test_swap_capacity_is_validated_and_memory_is_still_measured(self):
        for total, used in ((0, 1), (10, 11)):
            self.swap = C._DarwinSwapUsage(total, 0, used, 16384, 1)
            result = self.warn("swap measurement unavailable")
            self.assertIn("memory available level 50%", result["detail"])

    def test_memory_floor_and_override_have_exact_boundary_semantics(self):
        self.memory_level = 15
        self.assertTrue(C.host_pressure()["ok"])
        self.memory_level = 14
        self.warn("memory available level 14% (kern.memorystatus_level; floor 15%) - below floor")
        self.config({"min_memory_percent": 14})
        self.assertTrue(C.host_pressure()["ok"])
        self.config({"min_memory_percent": 40})
        self.memory_level = 39
        self.warn("floor 40%) - below floor")
        self.memory_level = 0
        self.config({"min_memory_percent": 0})
        self.assertTrue(C.host_pressure()["ok"])

    def test_invalid_memory_percent_never_reads_as_healthy(self):
        self.memory_level = 101
        self.warn("memory measurement unavailable")

    def test_unavailable_native_metrics_are_independent_warning_failures(self):
        for name, word in (("vm.swapusage", "swap"), ("kern.memorystatus_level", "memory")):
            for error in (OSError(errno.ENOENT, "not supported"),
                          PermissionError(errno.EPERM, "not permitted"),
                          ValueError("wrong reply size"), AttributeError("missing native API")):
                with self.subTest(name=name, error=error):
                    self.native_errors = {name: error}
                    result = self.warn(word + " measurement unavailable")
                    self.assertIn("disk 40.00 GiB", result["detail"])
                    self.assertIn("load 5m=10.00", result["detail"])
                    self.assertIn("memory available level 50%" if word == "swap"
                                  else "swap 2.00/10.00 GiB", result["detail"])

    def test_non_macos_skips_only_native_metrics_without_a_sysctl_call(self):
        for platform in ("linux", "win32", "freebsd13"):
            with self.subTest(platform=platform), mock.patch.object(C.sys, "platform", platform):
                result = C.host_pressure()
                self.assertTrue(result["ok"], result)
                self.assertIn("swap/memory measurements skipped", result["detail"])
        self.native.assert_not_called()
        self.commands.assert_not_called()
        self.assertEqual(3, self.disk.call_count)
        self.assertEqual(3, self.load.call_count)

    def test_registered_required_and_classified(self):
        self.assertIn(C.host_pressure, C.all_checks())
        manifest = json.loads((ROOT / "required_checks.json").read_text(encoding="utf-8"))
        self.assertIn("host_pressure", manifest["required"])
        self.assertEqual({"domain": "host", "kind": "capacity"},
                         manifest["kinds"]["host_pressure"])

    def test_runner_reports_degraded_never_critical(self):
        self.disk.return_value = Usage(200 * GIB, 199 * GIB, GIB)
        self.load.return_value = (200, 170, 150)
        self.swap = C._DarwinSwapUsage(10 * GIB, 0, 10 * GIB, 16384, 1)
        self.memory_level = 5
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
        self.swap = C._DarwinSwapUsage(10 * GIB, 0, 10 * GIB, 16384, 1)
        self.memory_level = 5
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


class NativeSysctlProof(unittest.TestCase):
    def test_native_swap_abi_is_read_only_and_correctly_sized(self):
        expected = C._DarwinSwapUsage(10 * GIB, GIB, 9 * GIB, 16384, 1)
        self.assertEqual(32, C.ctypes.sizeof(expected))
        library = mock.Mock()

        def read(name, output, size, new_value, new_size):
            self.assertEqual(b"vm.swapusage", name)
            self.assertIsNone(new_value)
            self.assertEqual(0, new_size)
            length = C.ctypes.cast(size, C.ctypes.POINTER(C.ctypes.c_size_t))
            self.assertEqual(32, length.contents.value)
            C.ctypes.memmove(output, C.ctypes.byref(expected), 32)
            return 0

        library.sysctlbyname.side_effect = read
        with mock.patch.object(C.ctypes, "CDLL", return_value=library) as loader:
            result = C._darwin_sysctl("vm.swapusage", C._DarwinSwapUsage())
        loader.assert_called_once_with(None, use_errno=True)
        self.assertEqual(9 * GIB, result.used)
        self.assertEqual(10 * GIB, result.total)
        self.assertEqual(16384, result.pagesize)
        self.assertEqual(C.ctypes.c_int, library.sysctlbyname.restype)
        self.assertEqual([C.ctypes.c_char_p, C.ctypes.c_void_p,
                          C.ctypes.POINTER(C.ctypes.c_size_t),
                          C.ctypes.c_void_p, C.ctypes.c_size_t],
                         library.sysctlbyname.argtypes)

    def test_scalar_reads_are_fresh_not_cached(self):
        library = mock.Mock()
        values = iter((50, 5))

        def read(name, output, size, new_value, new_size):
            self.assertEqual(b"kern.memorystatus_level", name)
            self.assertEqual(4, C.ctypes.cast(
                size, C.ctypes.POINTER(C.ctypes.c_size_t)).contents.value)
            self.assertIsNone(new_value)
            self.assertEqual(0, new_size)
            C.ctypes.cast(output, C.ctypes.POINTER(C.ctypes.c_uint32)).contents.value = next(values)
            return 0

        library.sysctlbyname.side_effect = read
        with mock.patch.object(C.ctypes, "CDLL", return_value=library):
            first = C._darwin_sysctl("kern.memorystatus_level", C.ctypes.c_uint32()).value
            second = C._darwin_sysctl("kern.memorystatus_level", C.ctypes.c_uint32()).value
        self.assertEqual((50, 5), (first, second))
        self.assertEqual(2, library.sysctlbyname.call_count)

    def test_native_failure_retains_errno_instead_of_returning_zero(self):
        library = mock.Mock()
        library.sysctlbyname.return_value = -1
        with mock.patch.object(C.ctypes, "CDLL", return_value=library), \
                mock.patch.object(C.ctypes, "get_errno", return_value=errno.ENOENT):
            with self.assertRaises(OSError) as raised:
                C._darwin_sysctl("kern.memorystatus_level", C.ctypes.c_uint32())
        self.assertEqual(errno.ENOENT, raised.exception.errno)
        self.assertEqual("kern.memorystatus_level", raised.exception.filename)

    def test_short_or_oversized_kernel_reply_is_refused(self):
        library = mock.Mock()
        for reported in (0, 3, 8):
            def read(name, output, size, new_value, new_size):
                C.ctypes.cast(size, C.ctypes.POINTER(C.ctypes.c_size_t)).contents.value = reported
                return 0
            library.sysctlbyname.side_effect = read
            with self.subTest(reported=reported), \
                    mock.patch.object(C.ctypes, "CDLL", return_value=library), \
                    self.assertRaisesRegex(ValueError, "returned .* bytes, expected 4"):
                C._darwin_sysctl("kern.memorystatus_level", C.ctypes.c_uint32())


if __name__ == "__main__":
    unittest.main(verbosity=2)
