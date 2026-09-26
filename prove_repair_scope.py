#!/usr/bin/env python3
"""Repair scope proof for #113. No model, network, or user repositories.

Run: python3 prove_repair_scope.py
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sentinel as S


class RepairScopeTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(
            prefix="prove-repair-", dir=Path(__file__).resolve().parent / "state")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name).resolve()
        self.home = self.root / "instance"
        self.state = self.home / "state"
        self.state.mkdir(parents=True)
        self.sources = {
            name: self.root / "live" / name
            for name in ("rappterverse", "rappterbook")
        }
        for path in self.sources.values():
            path.mkdir(parents=True)
            (path / "uncommitted.txt").write_text("operator work", encoding="utf-8")
        self.cfg = {
            "repo_paths": {name: str(path) for name, path in self.sources.items()},
            "copilot_model": "test-model",
            "copilot_timeout_s": 12,
        }
        self.events = []
        self.launches = []
        self.added = []
        self.removed = []
        self.branches = {}
        self.model_error = None
        self.model_status = 0
        self.git_failure = None
        self.non_toplevel = False
        self.commit_repair = False
        patcher = mock.patch.object(S.tempfile, "tempdir", str(self.root))
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("HOME", self.home), ("STATE", self.state)):
            patcher = mock.patch.object(S, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(S, "log")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(S.subprocess, "run", side_effect=self.run_command)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_command(self, command, **kwargs):
        self.events.append(command)
        if command[0] == "copilot":
            cwd = Path(kwargs["cwd"])
            self.launches.append((command, kwargs, sorted(p.name for p in cwd.iterdir())))
            if self.commit_repair:
                self.branches = {name: "repair-commit" for name in self.branches}
            if self.model_error:
                raise self.model_error
            return subprocess.CompletedProcess(
                command, self.model_status, "SENTINEL_RESULT: FIXED proof\n", "diagnostic\n")

        self.assertEqual(["git", "-C"], command[:2])
        self.assertTrue(kwargs["check"])
        self.assertGreater(kwargs["timeout"], 0)
        source, args = Path(command[2]), command[3:]
        self.assertIn(source, self.sources.values())
        output = ""
        if args == ["rev-parse", "--show-toplevel"]:
            output = str(source.parent if self.non_toplevel else source) + "\n"
        elif args[0] == "fetch":
            self.assertIn("origin", args)
            self.assertIn("+refs/heads/main:refs/remotes/origin/main", args)
        elif args == ["rev-parse", "--verify", "refs/remotes/origin/main^{commit}"]:
            output = "a" * 40 + "\n"
        elif args[:2] == ["worktree", "add"]:
            self.assertEqual("-b", args[2])
            branch, path, base = args[3], Path(args[4]), args[5]
            self.assertEqual("a" * 40, base)
            self.assertFalse(path.exists())
            path.mkdir()
            (path / "tracked.txt").write_text("fresh main", encoding="utf-8")
            self.branches[branch] = base
            self.added.append((source, path, branch))
        elif args[:2] == ["worktree", "remove"]:
            self.assertIn("--force", args)
            path = Path(args[-1])
            self.removed.append(path)
            if not self.git_failure or not self.git_failure(source, args):
                if path.exists():
                    shutil.rmtree(path)
        else:
            self.fail(f"unexpected git operation: {args}")
        if self.git_failure and self.git_failure(source, args):
            raise subprocess.CalledProcessError(1, command, stderr="fixture failure")
        return subprocess.CompletedProcess(command, 0, output, "")

    def escalate(self, failing=("rb_workflows",), mode="repair", **kwargs):
        verdict = {
            "status": "critical",
            "checks": [
                {"id": cid, "ok": False, "severity": "critical", "detail": "fixture failure"}
                for cid in failing
            ],
        }
        return S.escalate(self.cfg, verdict, list(failing), mode, **kwargs)

    def assert_cleaned(self):
        self.assertTrue(self.added, "the harness never prepared a worktree")
        self.assertEqual({path for _, path, _ in self.added}, set(self.removed))
        for _, path, _ in self.added:
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists(), "repair root survived cleanup")
        for source in self.sources.values():
            self.assertEqual("operator work", (source / "uncommitted.txt").read_text())

    def test_launch_retains_path_verification_and_uses_a_dedicated_root(self):
        ok, output = self.escalate()
        self.assertTrue(ok, output)
        command, options, entries = self.launches[0]
        for forbidden in ("--allow-all", "--allow-all-paths", "--allow-all-urls", "--yolo"):
            self.assertNotIn(forbidden, command)
        self.assertIn("--allow-all-tools", command)
        self.assertIn("--disallow-temp-dir", command)
        self.assertNotIn("--add-dir", command)
        self.assertEqual(self.cfg["copilot_timeout_s"], options["timeout"])
        self.assertNotEqual(self.home, Path(options["cwd"]))
        self.assertNotIn(Path(options["cwd"]), self.sources.values())
        self.assertEqual(["rappterbook"], entries)
        self.assertEqual({self.sources["rappterbook"]}, {source for source, _, _ in self.added})
        self.assert_cleaned()

    def test_all_affected_worktrees_exist_before_the_model_starts(self):
        ok, output = self.escalate(("rv_validation", "rb_workflows", "rb_shards"))
        self.assertTrue(ok, output)
        self.assertEqual(["rappterbook", "rappterverse"], self.launches[0][2])
        self.assertEqual(2, len(self.added))
        model_index = next(i for i, cmd in enumerate(self.events) if cmd[0] == "copilot")
        self.assertEqual(2, sum(cmd[3:5] == ["worktree", "add"]
                                for cmd in self.events[:model_index]))
        self.assert_cleaned()

    def test_nonprefixed_check_uses_the_registered_domain(self):
        ok, output = self.escalate(("rails_fresh",))
        self.assertTrue(ok, output)
        self.assertEqual(["rappterbook"], self.launches[0][2])
        self.assert_cleaned()

    def test_unknown_empty_and_mixed_scopes_fail_closed(self):
        for failing in ((), ("w_brainstem",), ("eco_sweep",), ("rb_unknown",),
                        ("rv_unknown",), ("rb_workflows", "w_brainstem")):
            with self.subTest(failing=failing):
                ok, output = self.escalate(failing)
                self.assertFalse(ok, output)
                self.assertIn("SENTINEL_RESULT: BLOCKED", output)
                self.assertFalse(self.launches)
                self.assertFalse(self.added)
                self.assertTrue(self.log.called)

    def test_extra_configured_repositories_do_not_expand_authority(self):
        self.cfg["repo_paths"]["openrappter"] = str(self.sources["rappterbook"])
        ok, output = self.escalate(("w_openrappter",))
        self.assertFalse(ok, output)
        self.assertFalse(self.launches)

    def test_missing_affected_target_fails_closed(self):
        del self.cfg["repo_paths"]["rappterbook"]
        ok, output = self.escalate()
        self.assertFalse(ok, output)
        self.assertIn("rappterbook", output)
        self.assertFalse(self.launches)

    def test_missing_unaffected_target_does_not_block(self):
        del self.cfg["repo_paths"]["rappterverse"]
        ok, output = self.escalate()
        self.assertTrue(ok, output)
        self.assert_cleaned()

    def test_nonexistent_source_fails_closed(self):
        self.cfg["repo_paths"]["rappterbook"] = str(self.root / "missing")
        ok, output = self.escalate()
        self.assertFalse(ok, output)
        self.assertFalse(self.launches)

    def test_root_creation_failure_never_starts_model(self):
        with mock.patch.object(S.tempfile, "mkdtemp",
                               side_effect=PermissionError("fixture denied root")):
            ok, output = self.escalate()
        self.assertFalse(ok, output)
        self.assertIn("SENTINEL_RESULT: BLOCKED", output)
        self.assertFalse(self.launches)

    def test_a_subdirectory_is_not_accepted_as_a_repository_root(self):
        self.non_toplevel = True
        ok, output = self.escalate()
        self.assertFalse(ok, output)
        self.assertFalse(self.launches)

    def test_fetch_failure_cleans_earlier_worktrees_without_starting_model(self):
        self.git_failure = lambda source, args: (
            source.name == "rappterverse" and args[0] == "fetch")
        ok, output = self.escalate(("rb_workflows", "rv_validation"))
        self.assertFalse(ok, output)
        self.assertIn("SENTINEL_RESULT: BLOCKED", output)
        self.assertFalse(self.launches)
        self.assert_cleaned()

    def test_partial_add_failure_is_also_cleaned(self):
        self.git_failure = lambda source, args: (
            source.name == "rappterverse" and args[:2] == ["worktree", "add"])
        ok, output = self.escalate(("rb_workflows", "rv_validation"))
        self.assertFalse(ok, output)
        self.assertFalse(self.launches)
        self.assertEqual(2, len(self.added))
        self.assert_cleaned()

    def test_nonzero_model_exit_cleans_up_and_keeps_output(self):
        self.model_status = 2
        ok, output = self.escalate()
        self.assertFalse(ok)
        self.assertIn("diagnostic", output)
        self.assert_cleaned()

    def test_timeout_cleans_up(self):
        self.model_error = subprocess.TimeoutExpired("copilot", 12)
        ok, output = self.escalate()
        self.assertFalse(ok)
        self.assertIn("timed out", output)
        self.assert_cleaned()

    def test_missing_cli_cleans_up(self):
        self.model_error = FileNotFoundError("fixture missing copilot")
        ok, output = self.escalate()
        self.assertFalse(ok)
        self.assertIn("not found", output)
        self.assert_cleaned()

    def test_unexpected_exception_cleans_up_before_propagating(self):
        self.model_error = RuntimeError("fixture exception")
        with self.assertRaisesRegex(RuntimeError, "fixture exception"):
            self.escalate()
        self.assert_cleaned()

    def test_cleanup_failure_is_reported_and_other_worktrees_are_still_removed(self):
        self.git_failure = lambda source, args: (
            source.name == "rappterverse" and args[:2] == ["worktree", "remove"])
        ok, output = self.escalate(("rb_workflows", "rv_validation"))
        self.assertFalse(ok, output)
        self.assertIn("cleanup", output)
        self.assertIn("SENTINEL_RESULT: BLOCKED", output)
        self.assert_cleaned()

    def test_cleanup_preserves_repair_branches_and_commits(self):
        self.commit_repair = True
        ok, output = self.escalate()
        self.assertTrue(ok, output)
        self.assert_cleaned()
        self.assertEqual({"repair-commit"}, set(self.branches.values()))

    def test_prompt_names_only_provided_worktrees_even_on_retry(self):
        ok, output = self.escalate(attempt=2, last_result="PARTIAL previous fix")
        self.assertTrue(ok, output)
        prompt = self.launches[0][0][2]
        for source in self.sources.values():
            self.assertNotIn(str(source), prompt)
        for _, path, _ in self.added:
            self.assertIn(str(path), prompt)
        self.assertNotIn("Create a fresh `git worktree`", prompt)
        self.assertNotIn("python3 diagnose.py", prompt)
        self.assertIn("ATTEMPT 2", prompt)
        self.assertIn("PARTIAL previous fix", prompt)

    def test_diagnose_keeps_existing_launch_and_prompt(self):
        ok, output = self.escalate(("w_brainstem",), mode="diagnose",
                                  attempt=2, last_result="BLOCKED previous diagnosis")
        self.assertTrue(ok, output)
        command, options, _ = self.launches[0]
        self.assertEqual(str(self.home), options["cwd"])
        self.assertIn("--allow-all", command)
        self.assertIn("READ-ONLY", command[2])
        self.assertIn("python3 diagnose.py", command[2])
        for source in self.sources.values():
            self.assertIn(str(source), command[2])
        self.assertFalse(self.added)


if __name__ == "__main__":
    unittest.main(verbosity=2)
