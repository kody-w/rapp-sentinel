#!/usr/bin/env python3
"""Prove #114 against the real tick with synthetic, isolated state.

No models, network, notifications, launchd, or live state. Every outside
effect is stubbed; issues.json and escalations.json are actually persisted
and re-read between ticks.

Run: python3 prove_per_check_throttle.py
"""

import copy
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


SCRATCH = Path(__file__).resolve().parent / ".scratch-home" / "per-check-throttle"
SCRATCH.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(dir=SCRATCH) as import_home:
    with mock.patch.dict(os.environ, {"SENTINEL_HOME": import_home}):
        import sentinel as S


class PerCheckThrottleProof(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(home.cleanup)
        self.home = Path(home.name)
        for name in ("state", "logs"):
            (self.home / name).mkdir()
        for name, value in (("HOME", self.home),
                            ("STATE", self.home / "state"),
                            ("LOGS", self.home / "logs"),
                            ("STOP", self.home / "STOP")):
            self.patch(S, name, value)
        self.current = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        self.patch(S, "now", side_effect=lambda: self.current)
        self.cfg = {**S.DEFAULTS, "level": 1, "notify": False,
                    "daily_escalation_budget": 100}
        self.patch(S, "config", return_value=self.cfg)
        self.patch(S, "ensure_evolution_worker_loaded")
        self.patch(S, "publish_head_hook")
        self.patch(S, "outsider_smoke", return_value=False)
        self.github = self.patch(S, "github_degraded", return_value=[])
        self.patch(S.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, stdout="", stderr=""))
        self.nb = self.patch(S, "NB")
        self.nb.roll_call.return_value = {}
        self.nb.peer_roll_call.return_value = {}
        self.nb.check_anchors.return_value = {}
        self.log = self.patch(S, "log")
        self.notify = self.patch(S, "notify", return_value=True)
        self.escalate = self.patch(
            S, "escalate",
            return_value=(False, "SENTINEL_RESULT: BLOCKED synthetic"))
        self.seed()

    def patch(self, target, name, *args, **kwargs):
        patcher = mock.patch.object(target, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def seed(self, issues=None, prev=None):
        S.save_json(S.STATE / "issues.json", issues or {})
        S.save_json(S.STATE / "escalations.json", [])
        S.save_json(S.STATE / "last_run.json", prev or {"status": "critical"})
        for stub in (self.escalate, self.notify, self.log, self.nb):
            stub.reset_mock()

    def record(self, attempts, hours_ago=0, result="BLOCKED previous", **extra):
        return {"attempts": attempts,
                "last_attempt": (self.current - timedelta(hours=hours_ago)).isoformat(
                    timespec="seconds"),
                "last_result": result, **extra}

    def read(self, name="issues.json"):
        return json.loads((S.STATE / name).read_text(encoding="utf-8"))

    def tick(self, critical, after=None):
        def verdict(rows):
            failed = [cid for cid, ok in rows if ok is not True]
            return {
                "status": "critical" if failed else "healthy",
                "critical": failed, "failed": failed,
                "summary": ", ".join(failed),
                "checks": [{"id": cid, "ok": ok, "severity": "critical",
                            "detail": "synthetic"} for cid, ok in rows],
            }

        before = verdict([(cid, False) for cid in critical])
        after = before if after is None else verdict(list(after.items()))
        with mock.patch.object(S, "run_health", side_effect=[before, after]):
            self.assertEqual(0, S.main())

    def human_pages(self):
        return [call.args[1] for call in self.notify.call_args_list
                if "needs you" in call.args[1]]

    def test_flapping_sets_cannot_bypass_cooldown_or_cap(self):
        batches = [
            (["A"], ["A", "B"]),
            (["A"], ["B", "A"]),
            (["A", "B"], ["A"]),
            (["A", "B"], ["A", "C"]),
            (["A", "B"], ["B", "A"]),
        ]
        for level in (1, 2):
            for guard, cap, cooldown in (("cooling down", 3, 4),
                                         ("attempt cap", 1, 0)):
                for first, second in batches:
                    with self.subTest(level=level, guard=guard,
                                      first=first, second=second):
                        self.cfg.update(level=level, max_attempts_per_issue=cap,
                                        issue_cooldown_hours=cooldown)
                        self.seed()
                        self.tick(first)
                        self.tick(second)
                        self.assertEqual(1, self.escalate.call_count,
                                         "changing companions must not buy A a retry")
                        self.assertEqual(1, len(self.read("escalations.json")))
                        self.assertEqual(1, self.read()["check:A"]["attempts"])
                        messages = "\n".join(c.args[0] for c in self.log.call_args_list)
                        self.assertIn(f"A: {guard}", messages)

    def test_each_included_check_is_charged_once_per_spend(self):
        for batch in (["A", "B"], ["B", "A"], ["B", "A", "A"]):
            with self.subTest(batch=batch):
                self.cfg["issue_cooldown_hours"] = 0
                self.seed()
                for attempt in (1, 2):
                    self.tick(batch)
                    self.assertEqual(
                        {"check:A": self.record(attempt, result="BLOCKED synthetic"),
                         "check:B": self.record(attempt, result="BLOCKED synthetic")},
                        self.read())
                    history = self.read("escalations.json")
                    self.assertEqual(attempt, len(history))
                    self.assertEqual("A,B", history[-1]["key"])
                    acts = [c.args[2] for c in self.nb.emit.call_args_list
                            if c.args[1] == "neighbor.acted"]
                    self.assertEqual("A,B", acts[-1]["issue"])
                    self.assertEqual(attempt, acts[-1]["attempt"])
                    self.assertEqual(attempt, self.escalate.call_args.kwargs["attempt"])
                    self.assertEqual(
                        None if attempt == 1 else "BLOCKED synthetic",
                        self.escalate.call_args.kwargs["last_result"])

    def test_retry_context_uses_most_advanced_then_most_recent_history(self):
        cases = [
            ("advanced", self.record(2, 10, "advanced"),
             self.record(1, 5, "recent"), 3, "advanced"),
            ("recent tie", self.record(2, 10, "older"),
             self.record(2, 5, "newer"), 3, "newer"),
            ("offset tie", {**self.record(2, result="older"),
                            "last_attempt": "2026-09-25T10:00:00+03:00"},
             {**self.record(2, result="newer"),
              "last_attempt": "2026-09-25T08:00:00+00:00"}, 3, "newer"),
        ]
        self.cfg["max_attempts_per_issue"] = 10
        for name, a, b, attempt, result in cases:
            for batch in (["A", "B"], ["B", "A"]):
                with self.subTest(case=name, batch=batch):
                    self.seed({"A": a, "B": b})
                    self.tick(batch)
                    self.assertEqual(
                        {"attempt": attempt, "last_result": result},
                        self.escalate.call_args.kwargs)
                    self.assertEqual(a["attempts"] + 1, self.read()["check:A"]["attempts"])
                    self.assertEqual(b["attempts"] + 1, self.read()["check:B"]["attempts"])

    def test_every_blocker_is_named_and_fresh_companions_are_not_charged(self):
        a, b = self.record(3, 10), self.record(1)
        self.seed({"A": a, "B": b})
        self.tick(["C", "B", "A"])
        self.escalate.assert_not_called()
        self.assertEqual([], self.read("escalations.json"))
        messages = "\n".join(c.args[0] for c in self.log.call_args_list)
        self.assertIn("A: attempt cap", messages)
        self.assertIn("B: cooling down", messages)
        self.assertEqual({"check:A": {**a, "human_notified": True},
                          "check:B": b}, self.read())
        self.assertEqual(1, len(self.human_pages()))
        self.assertIn("'A'", self.human_pages()[0])
        self.tick(["C"])
        self.escalate.assert_called_once()
        self.assertEqual(1, self.read()["check:C"]["attempts"])
        self.assertEqual(3, self.read()["check:A"]["attempts"])

    def test_cooldown_boundary(self):
        for age, allowed in ((4 - 1 / 3600, False), (4, True), (4 + 1 / 3600, True)):
            with self.subTest(hours_ago=age):
                self.seed({"A": self.record(1, age)})
                self.tick(["B", "A"])
                self.assertEqual(int(allowed), self.escalate.call_count)
                self.assertEqual(1 + int(allowed), self.read()["check:A"]["attempts"])

    def test_partial_recovery_only_clears_explicit_true(self):
        cases = [
            {"A": True, "B": False},
            {"A": True},
            {"A": True, "B": None},
            {"A": True, "B": 1},
            {"A": False, "B": True},
            {"A": 1, "B": False},
            {},
            {"A": True, "B": True},
        ]
        self.cfg["level"] = 2
        unrelated = {"check:C": self.record(2, 12),
                     "smoke:one,two": {"opaque": ["do not reinterpret"]},
                     "evolve:artist": self.record(8)}
        for after in cases:
            with self.subTest(after=after):
                self.seed({**unrelated, "A": self.record(1, 5),
                           "B": self.record(1, 5)})
                self.tick(["A", "B"], after=after)
                cleared = {cid for cid in ("A", "B") if after.get(cid) is True}
                remaining = {f"check:{cid}": self.record(2, result="BLOCKED synthetic")
                             for cid in ("A", "B") if cid not in cleared}
                self.assertEqual({**unrelated, **remaining}, self.read())
                verified = [c.args[2] for c in self.nb.emit.call_args_list
                            if c.args[1] == "repair.verified"]
                self.assertEqual(sorted(cleared), verified[-1]["cleared"])
                self.assertEqual("A,B", verified[-1]["issue"])
                if cleared == {"A"}:
                    self.tick(["B", "C"])
                    self.assertEqual(1, self.escalate.call_count,
                                     "B retains its cooldown after A recovers")
                    self.tick(["A"])
                    self.assertEqual(2, self.escalate.call_count)
                    self.assertEqual(1, self.escalate.call_args.kwargs["attempt"])
                    self.assertEqual(2, self.read()["check:B"]["attempts"])

    def test_legacy_migration_adds_each_attempt_once_and_preserves_context(self):
        old = self.record(2, 10, "older", human_notified=True)
        new = self.record(1, 5, "newer")
        cases = [
            ("single", {"A": old}, {"A": old}),
            ("composite", {"A,B": old}, {"A": old, "B": old}),
            ("reordered", {"B,A": old}, {"A": old, "B": old}),
            ("duplicate member", {"A,A,B": old}, {"A": old, "B": old}),
            ("overlap", {"A": old, "A,B": new, "B,A": old},
             {"A": {**new, "attempts": 5, "human_notified": True},
              "B": {**new, "attempts": 3, "human_notified": True}}),
            ("newer single", {"A": new, "A,B": old},
             {"A": {**new, "attempts": 3, "human_notified": True}, "B": old}),
            ("mixed formats", {"check:A": new, "A": old, "A,B": new},
             {"A": {**new, "attempts": 4, "human_notified": True}, "B": new}),
            ("three checks", {"A,B,hub:owner/check": new},
             {"A": new, "B": new, "hub:owner/check": new}),
            ("offsets", {"A": {**old, "last_attempt": "2026-09-25T10:00:00+03:00"},
                         "A,B": {**new, "last_attempt": "2026-09-25T08:00:00+00:00"}},
             {"A": {**new, "last_attempt": "2026-09-25T08:00:00+00:00",
                    "attempts": 3, "human_notified": True},
              "B": {**new, "last_attempt": "2026-09-25T08:00:00+00:00"}}),
        ]
        unrelated = {
            "smoke:one,two": {"opaque": {"keep": [1, 2]}, "human_notified": True},
            "evolve:one,two": {"attempts": 99, "last_result": "creative"},
        }
        for name, legacy, expected in cases:
            for reverse in (False, True):
                with self.subTest(case=name, reverse=reverse):
                    items = list(legacy.items())
                    self.seed({**dict(reversed(items) if reverse else items), **unrelated})
                    self.tick(["Z"])
                    migrated = {**{f"check:{cid}": copy.deepcopy(rec)
                                   for cid, rec in expected.items()}, **unrelated,
                                "check:Z": self.record(1, result="BLOCKED synthetic")}
                    self.assertEqual(migrated, self.read())
                    self.tick(["Z"])
                    self.assertEqual(migrated, self.read(), "migration must be idempotent")
                    self.assertEqual(1, self.escalate.call_count)

    def test_legacy_limits_block_changed_batches_before_spending(self):
        cases = [
            ("single cooldown", {"A": self.record(1)}, "cooling down"),
            ("composite cooldown", {"A,B": self.record(1)}, "cooling down"),
            ("single cap", {"A": self.record(3, 100)}, "attempt cap"),
            ("composite cap", {"A,B": self.record(3, 100)}, "attempt cap"),
            ("accumulated cap", {"A": self.record(1, 10),
                                 "B,A": self.record(2, 5)}, "attempt cap"),
        ]
        for name, legacy, reason in cases:
            with self.subTest(case=name):
                self.seed(legacy)
                self.tick(["C", "A"])
                self.escalate.assert_not_called()
                self.assertEqual([], self.read("escalations.json"))
                self.assertNotIn("check:C", self.read())
                self.assertFalse(any("," in key for key in self.read()))
                messages = "\n".join(c.args[0] for c in self.log.call_args_list)
                self.assertIn(f"A: {reason}", messages)
                saved = self.read()
                self.tick(["A"])
                self.assertEqual(saved, self.read())

    def test_human_escalation_is_deduped_per_capped_check(self):
        self.seed({"A": self.record(3, 10), "B": self.record(3, 10)})
        for batch, pages in ((["A"], 1), (["A"], 1), (["A"], 1),
                             (["A", "C"], 1), (["B", "A"], 2),
                             (["B"], 2), (["A", "B"], 2),
                             ([], 2), (["B", "A"], 2)):
            with self.subTest(batch=batch, pages=pages):
                self.tick(batch)
                self.assertEqual(pages, len(self.human_pages()))
                self.assertTrue(self.read()["check:A"]["human_notified"])
                if pages == 2:
                    self.assertTrue(self.read()["check:B"]["human_notified"])
                    self.assertIn("'B'", self.human_pages()[-1])
                    self.assertNotIn("'A,B'", self.human_pages()[-1])
        self.escalate.assert_not_called()

    def test_legacy_human_notification_marker_is_preserved_per_check(self):
        for marker in ("A,B", "B,A"):
            with self.subTest(marker=marker):
                rec = self.record(3, 10)
                self.seed({"A,B": rec}, prev={
                    "status": "critical", "at": "old heartbeat",
                    "escalated_human": marker})
                self.tick(["A", "C"])
                self.escalate.assert_not_called()
                self.assertEqual([], self.human_pages())
                self.assertEqual(
                    {"check:A": {**rec, "human_notified": True},
                     "check:B": {**rec, "human_notified": True}}, self.read())
                self.assertEqual(self.current.isoformat(timespec="seconds"),
                                 self.read("last_run.json")["at"])

    def test_other_pre_spend_guards_remain_unchanged(self):
        for guard in ("observe", "budget", "outage", "stop"):
            with self.subTest(guard=guard):
                self.seed()
                self.cfg.update(level=0 if guard == "observe" else 1,
                                daily_escalation_budget=0 if guard == "budget" else 100)
                self.github.return_value = ["Actions"] if guard == "outage" else []
                if guard == "stop":
                    S.STOP.touch()
                self.tick(["rb_workflows"])
                self.escalate.assert_not_called()
                self.assertEqual({}, self.read())
                self.assertEqual([], self.human_pages())


if __name__ == "__main__":
    unittest.main(verbosity=2)
