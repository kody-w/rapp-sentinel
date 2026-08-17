import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import sentinel


class EvolutionCadenceTests(unittest.TestCase):
    def setUp(self):
        self.current = datetime(2026, 8, 17, 22, 0, tzinfo=timezone.utc)
        self.cfg = {"evolve_interval_hours": 3}

    def test_first_evolution_is_allowed(self):
        with mock.patch.object(sentinel, "now", return_value=self.current):
            allowed, reason = sentinel.evolution_allowed([], self.cfg)
        self.assertTrue(allowed)
        self.assertEqual("first evolution", reason)

    def test_recent_evolution_waits_for_global_cadence(self):
        history = [{
            "at": (self.current - timedelta(hours=2)).isoformat(),
            "mode": "evolve",
        }]
        with mock.patch.object(sentinel, "now", return_value=self.current):
            allowed, reason = sentinel.evolution_allowed(history, self.cfg)
        self.assertFalse(allowed)
        self.assertIn("2.0h of 3.0h", reason)

    def test_evolution_recurs_without_repair_attempt_cap(self):
        history = [{
            "at": (self.current - timedelta(hours=4)).isoformat(),
            "mode": "evolve",
            "attempts": 10_000,
        }]
        with mock.patch.object(sentinel, "now", return_value=self.current):
            allowed, _ = sentinel.evolution_allowed(history, self.cfg)
        self.assertTrue(allowed)

    def test_degraded_evolution_requires_opt_in_and_no_critical(self):
        degraded = {"status": "degraded", "critical": []}
        self.assertFalse(sentinel.evolution_status_allowed({}, degraded))
        self.assertTrue(sentinel.evolution_status_allowed(
            {"evolve_on_degraded": True}, degraded))
        self.assertFalse(sentinel.evolution_status_allowed(
            {"evolve_on_degraded": True},
            {"status": "critical", "critical": ["rb_workflows"]},
        ))

    def test_repair_authority_can_be_disabled_at_level_three(self):
        self.assertEqual("diagnose", sentinel.escalation_mode(
            {"repair_enabled": False}, 3))
        self.assertEqual("repair", sentinel.escalation_mode(
            {"repair_enabled": True}, 3))


class EvolutionPromptTests(unittest.TestCase):
    def test_prompt_carries_named_collective_brief_and_state(self):
        slug = next(iter(sentinel.NB.NEIGHBORS))
        cfg = {
            "instance_name": "Dada Collective",
            "copilot_model": "test-model",
            "evolve_timeout_s": 60,
            "contribution_targets": [
                "https://github.com/kody-w/public-art-collective",
            ],
            "creative_state_file": "state/dada-ideation.json",
            "evolve_brief": {
                "candidates_per_round": 10,
                "selection": "build only the most extreme winner",
            },
        }
        completed = subprocess.CompletedProcess([], 0, stdout="done", stderr="")
        with mock.patch.object(
                sentinel.NB, "identities",
                return_value={slug: "rappid:@kody-w/dada:test"}), \
             mock.patch.object(
                 sentinel.NB, "roll_call",
                 return_value={peer: {"alive": True}
                               for peer in sentinel.NB.NEIGHBORS}), \
             mock.patch.object(
                 sentinel.NB, "chain_path",
                 return_value=Path("/tmp/dada-chain.jsonl")), \
             mock.patch.object(
                 sentinel.subprocess, "run", return_value=completed) as run:
            ok, output = sentinel.evolve(cfg, slug)

        self.assertTrue(ok)
        self.assertEqual("done", output)
        prompt = run.call_args.args[0][2]
        self.assertIn("collective: Dada Collective", prompt)
        self.assertIn('"candidates_per_round": 10', prompt)
        self.assertIn("public-art-collective", prompt)
        self.assertIn("state/dada-ideation.json", prompt)


if __name__ == "__main__":
    unittest.main()
