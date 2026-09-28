#!/usr/bin/env python3
"""No default dashboard cost on ticks; bounded opt-in and single-flight renders."""
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
SCRATCH = ROOT / "state" / "prove-tick-dashboard"
SCRATCH.mkdir(parents=True, exist_ok=True)
IMPORT_HOME = tempfile.TemporaryDirectory(dir=str(SCRATCH))
os.environ["SENTINEL_HOME"] = IMPORT_HOME.name

import filelock
import sentinel as S
import serve
import standup


class TickDashboardProof(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=str(SCRATCH))
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / "state").mkdir()
        for module in (S, serve, standup):
            self.patch(module, "HOME", self.home)
        self.patch(S, "STATE", self.home / "state")
        self.patch(S, "STOP", self.home / "STOP")
        self.log = self.patch(S, "log")

    def patch(self, target, name, *args, **kwargs):
        patcher = mock.patch.object(target, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def test_default_and_explicit_false_spawn_nothing(self):
        run = self.patch(S.subprocess, "run")
        for cfg in ({}, S.DEFAULTS, {"dashboard_refresh_on_tick": False}):
            self.assertFalse(S.refresh_dashboard(cfg))
        run.assert_not_called()

    def test_invalid_boolean_is_logged_not_treated_as_true(self):
        run = self.patch(S.subprocess, "run")
        for value in ("false", 1, None, []):
            self.assertFalse(S.refresh_dashboard({"dashboard_refresh_on_tick": value}))
        self.assertEqual(4, self.log.call_count)
        run.assert_not_called()

    def test_opt_in_is_bounded_uses_code_path_and_checks_exit_status(self):
        run = self.patch(S.subprocess, "run", return_value=subprocess.CompletedProcess([], 0))
        self.assertTrue(S.refresh_dashboard({"dashboard_refresh_on_tick": True}))
        command = run.call_args.args[0]
        self.assertEqual(str(S.CODE / "standup.py"), command[1])
        self.assertEqual("--hours=14", command[2])
        self.assertEqual(180, run.call_args.kwargs["timeout"])
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertEqual(str(self.home), run.call_args.kwargs["cwd"])

    def test_timeouts_nonzero_exits_and_spawn_failures_are_visible_nonfatal(self):
        run = self.patch(S.subprocess, "run")
        for error in (subprocess.TimeoutExpired(["python", "standup.py"], 180),
                      subprocess.CalledProcessError(1, ["python"], stderr="render failed"),
                      OSError("cannot start renderer")):
            run.side_effect = error
            self.assertFalse(S.refresh_dashboard({"dashboard_refresh_on_tick": True}))
            self.assertIn(type(error).__name__, self.log.call_args.args[0])

    def tick(self, enabled):
        cfg = dict(S.DEFAULTS, level=0, notify=False,
                   dashboard_refresh_on_tick=enabled)
        verdict = {"status": "healthy", "failed": [], "critical": [],
                   "checks": [], "summary": "all passing"}
        self.patch(S, "config", return_value=cfg)
        self.patch(S, "run_health", return_value=verdict)
        for name in ("ensure_evolution_worker_loaded", "publish_head_hook",
                     "silence_breaker", "outsider_smoke", "notify", "escalate"):
            self.patch(S, name)
        nb = self.patch(S, "NB")
        nb.roll_call.return_value = {}
        nb.peer_roll_call.return_value = {}
        nb.check_anchors.return_value = {}
        return S.main()

    def test_real_tick_has_no_default_render_and_saves_heartbeat_first_on_opt_in(self):
        def rendering(command, **kwargs):
            self.assertTrue((S.STATE / "last_run.json").exists())
            saved = json.loads((S.STATE / "last_verdict.json").read_text())
            self.assertEqual("healthy", saved["status"])
            raise subprocess.TimeoutExpired(command, 180)

        run = self.patch(S.subprocess, "run", side_effect=rendering)
        self.assertEqual(0, self.tick(False))
        run.assert_not_called()
        self.assertEqual(0, self.tick(True))
        run.assert_called_once()

    def test_file_lock_refuses_other_renderers_and_releases_after_failure(self):
        render = self.patch(standup, "_render", return_value=({"hours": 14}, True))
        path = self.home / "state" / "dashboard-refresh.lock"
        with path.open("a", encoding="utf-8") as held:
            self.assertTrue(filelock.lock_nb(held))
            try:
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    standup.render()
                render.assert_not_called()
            finally:
                filelock.unlock(held)
        self.assertEqual(({"hours": 14}, True), standup.render())
        render.side_effect = ValueError("render failed")
        with self.assertRaisesRegex(ValueError, "render failed"):
            standup.render()
        render.side_effect = None
        self.assertEqual(({"hours": 14}, True), standup.render())

    def test_lock_is_held_through_render_and_is_instance_scoped(self):
        other = self.home / "other"
        other.mkdir()
        lock_path = self.home / "state" / "dashboard-refresh.lock"

        def render(hours):
            with lock_path.open("a", encoding="utf-8") as contender:
                self.assertFalse(filelock.lock_nb(contender))
            with mock.patch.object(standup, "HOME", other), \
                    mock.patch.object(standup, "_render", return_value=("other", True)):
                self.assertEqual(("other", True), standup.render())
            return ("first", True)

        self.patch(standup, "_render", side_effect=render)
        self.assertEqual(("first", True), standup.render())

    def prepare_server(self):
        self.patch(serve, "_building", False)
        self.patch(serve, "_last_build", 0.0)
        clock = self.patch(serve.time, "monotonic", return_value=100.0)
        thread = self.patch(serve.threading, "Thread")
        run = self.patch(serve.subprocess, "run")
        return clock, thread, run

    def test_request_refresh_never_queues_threads_behind_a_slow_render(self):
        clock, thread, run = self.prepare_server()
        serve.rebuild(hours=24)
        run.assert_not_called()
        clock.return_value = 1000
        for _ in range(20):
            serve.rebuild()
        thread.assert_called_once()
        self.assertTrue(thread.call_args.kwargs["daemon"])
        thread.call_args.kwargs["target"]()
        self.assertFalse(serve._building)
        self.assertEqual("--hours=24", run.call_args.args[0][-1])
        self.assertEqual(180, run.call_args.kwargs["timeout"])
        self.assertTrue(run.call_args.kwargs["check"])
        serve.rebuild()
        self.assertEqual(2, thread.call_count)

    def test_server_failure_is_reported_and_does_not_latch_refresh_closed(self):
        clock, thread, run = self.prepare_server()
        run.side_effect = subprocess.TimeoutExpired(["standup.py"], 180)
        serve.rebuild()
        output = io.StringIO()
        with redirect_stderr(output):
            thread.call_args.kwargs["target"]()
        self.assertIn("TimeoutExpired", output.getvalue())
        self.assertFalse(serve._building)
        clock.return_value = 1000
        serve.rebuild()
        self.assertEqual(2, thread.call_count)

    def test_thread_start_failure_releases_single_flight_guard(self):
        _, thread, _ = self.prepare_server()
        thread.return_value.start.side_effect = RuntimeError("thread unavailable")
        output = io.StringIO()
        with redirect_stderr(output):
            serve.rebuild()
        self.assertIn("could not start", output.getvalue())
        self.assertFalse(serve._building)


if __name__ == "__main__":
    unittest.main(verbosity=2)
