#!/usr/bin/env python3
"""Prove the silence breaker against synthetic state only.

The live Dada Collective incident this encodes: since 2026-08-17 it produced
3,484 verdicts, 3,016 critical, with notification_mode=art-only and
notify_queue_only=true. The last delivered operator message was an art receipt
on 2026-08-21; after that rappterverse froze, "needs a human" was logged 2,903
times, diagnose found root causes, the art arm skipped every pass, and two
checks were blind 3,101 times. Because the tick only notifies on status change
and art-only dropped operational messages before the alert ledger, silence was
not provable. A quiet mode may hide calm, never trouble.

Run only in an isolated checkout home, for example:
  env HOME="$PWD/.scratch/home" SENTINEL_HOME="$PWD/.scratch/instance" \
      TMPDIR="$PWD/.scratch/tmp" PYTHONDONTWRITEBYTECODE=1 \
      /usr/bin/python3 prove_silence_breaker.py
"""

import json
import os
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import sentinel as S


ROOT = Path(__file__).resolve().parent
HOME = Path(os.environ["SENTINEL_HOME"]).resolve()
STATE = HOME / "state"


class SilenceBreakerProof(unittest.TestCase):
    def setUp(self):
        if HOME.exists():
            for child in HOME.iterdir():
                if child.name in {"state", "logs", "neighborhood", "config.json",
                                  "rappid.json", "STOP"}:
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
        for path in (STATE, HOME / "logs", HOME / "neighborhood" / "copilot"):
            path.mkdir(parents=True, exist_ok=True)
        for name, value in (("HOME", HOME), ("STATE", STATE),
                            ("LOGS", HOME / "logs"), ("STOP", HOME / "STOP"),
                            ("SILENCE_BREAKER_STATE", STATE / "silence-breaker.json")):
            patcher = mock.patch.object(S, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.current = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
        patcher = mock.patch.object(S, "now", side_effect=lambda: self.current)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = {**S.DEFAULTS,
                    "instance_name": "Dada Collective",
                    "notify": True,
                    "notify_handle": "+15555550100",
                    "notify_queue_only": True,
                    "notification_mode": "art-only",
                    "silence_breaker_hours": 24,
                    "level": 0}
        self.write_chain(hours=(200, 147, 1), statuses=("critical", "critical", "critical"))
        self.write_sent(self.current - timedelta(days=36), "🎨 deployed")

    def write_jsonl(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows),
                        encoding="utf-8")

    def write_sent(self, when, text):
        self.write_jsonl(STATE / "outbox-sent.jsonl", [{
            "entry_id": "1" * 32,
            "at": when.isoformat(timespec="seconds"),
            "to": self.cfg["notify_handle"],
            "text": text,
            "attachments": [],
            "sent_at": when.isoformat(timespec="seconds"),
        }])

    def write_chain(self, hours, statuses, extra_frames=()):
        rows = []
        for i, (age, status) in enumerate(zip(hours, statuses)):
            rows.append({"seq": i, "kind": "sentinel.tick",
                         "utc": (self.current - timedelta(hours=age)).isoformat(
                             timespec="seconds"),
                         "payload": {"status": status}})
        for age, kind in extra_frames:
            rows.append({"seq": len(rows), "kind": kind,
                         "utc": (self.current - timedelta(hours=age)).isoformat(
                             timespec="seconds"),
                         "payload": {"act": "diagnose"}})
        rows.sort(key=lambda r: r["utc"])
        self.write_jsonl(HOME / "neighborhood" / "copilot" / "chain.jsonl", rows)

    def verdict(self, status="critical", extra=()):
        checks = [
            {"id": "rv_world_merging", "ok": False, "severity": "critical",
             "detail": "last merge 147.6h ago"},
            {"id": "rv_meaningful_activity", "ok": False, "severity": "critical",
             "detail": "chat stale 840.8h"},
            {"id": "rv_pr_queue", "ok": False, "severity": "critical",
             "detail": "cannot read the PR queue"},
            {"id": "rb_rollup_coverage", "ok": False, "severity": "warn",
             "detail": "cannot read rollup coverage"},
            {"id": "w_evolve_worker", "ok": False, "severity": "warn",
             "detail": "skipping every pass: config error"},
        ]
        checks.extend(extra)
        failed = [c["id"] for c in checks]
        critical = failed if status == "critical" else []
        return {"generated": self.current.isoformat(timespec="seconds"),
                "status": status, "checks": checks, "failed": failed,
                "critical": critical, "summary": "; ".join(failed)}

    def queue(self):
        path = STATE / "outbox.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(
            encoding="utf-8").splitlines() if line.strip()]

    def alerts(self):
        path = STATE / "alerts.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["payload"] for line in path.read_text(
            encoding="utf-8").splitlines() if line.strip()]

    def state(self):
        return json.loads((STATE / "silence-breaker.json").read_text(
            encoding="utf-8"))

    def test_status_duration_ignores_non_tick_frames(self):
        # 147h of critical ticks, preceded by healthy ticks, with a diagnosis
        # frame (no status) as the newest record on the chain.
        self.write_chain([200, 147, 100, 10, 1],
                         ["healthy", "critical", "critical", "critical", "critical"],
                         extra_frames=[(0.5, "neighbor.acted")])
        self.assertTrue(S.silence_breaker(self.cfg, self.verdict()))
        self.assertIn("Status: critical; not healthy for 6 days (since ",
                      self.queue()[0]["text"])

    def test_flapping_unhealthy_estate_reports_time_since_last_healthy_tick(self):
        # The field incident: critical and degraded alternated for weeks with no
        # healthy tick at all. "critical for 24h" undersold that.
        self.write_chain([900, 600, 300, 24, 1],
                         ["critical", "degraded", "critical", "degraded", "critical"])
        self.assertTrue(S.silence_breaker(self.cfg, self.verdict()))
        self.assertIn("Status: critical; not healthy in any tick since ",
                      self.queue()[0]["text"])
        self.assertIn("(37 days).", self.queue()[0]["text"])

    def test_own_config_and_identity_are_not_platform_findings(self):
        extra = (
            {"id": "config_integrity", "ok": False, "severity": "warn",
             "detail": "evolve_worker.max_piece_bytes appears twice"},
            {"id": "gh_identity", "ok": False, "severity": "warn",
             "detail": "gh identity rappter1 has a GraphQL quota of 0"},
        )
        self.assertTrue(S.silence_breaker(self.cfg, self.verdict(extra=extra)))
        text = self.queue()[0]["text"]
        platforms = [l for l in text.splitlines() if l.startswith("Platforms:")][0]
        self.assertNotIn("config_integrity", platforms)
        self.assertNotIn("gh_identity", platforms)
        self.assertIn("gh_identity", [l for l in text.splitlines() if l.startswith("I can't see:")][0])
        self.assertIn("config_integrity", [l for l in text.splitlines() if l.startswith("Local machinery:")][0])
        self.assertNotIn("notify_queue_only", text)
        self.assertIn("Pause: set silence_ack_until in config.json.", text)

    def test_art_only_critical_breaks_silence_and_backs_off(self):
        self.assertTrue(S.silence_breaker(self.cfg, self.verdict()))
        q = self.queue()
        self.assertEqual(1, len(q))
        text = q[0]["text"]
        self.assertIn("36 days without a message", text)
        self.assertIn("Platforms: rv_world_merging", text)
        self.assertIn("I can't see: rv_pr_queue", text)
        self.assertIn("Local machinery: w_evolve_worker", text)
        self.assertIn("notification_mode=art-only", text)
        self.assertIn("Next reminder in 48h", text)
        self.assertLessEqual(len(text), 700)
        self.assertNotIn("dedupe_key", q[0])
        self.assertEqual(48, self.state()["window_hours"])
        self.assertEqual("paged", self.alerts()[-1]["decision"])
        self.assertIn("silence breaker", self.alerts()[-1]["reason"])

        self.assertFalse(S.silence_breaker(self.cfg, self.verdict()))
        self.assertEqual(1, len(self.queue()))
        (STATE / "outbox.jsonl").unlink()
        self.current += timedelta(hours=24)
        self.assertFalse(S.silence_breaker(self.cfg, self.verdict()))
        self.assertFalse((STATE / "outbox.jsonl").exists())
        self.current += timedelta(hours=24)
        self.assertTrue(S.silence_breaker(self.cfg, self.verdict()))
        self.assertEqual(2, self.state()["count"])
        self.assertEqual(96, self.state()["window_hours"])

    def test_failing_set_change_resets_due_window(self):
        self.assertTrue(S.silence_breaker(self.cfg, self.verdict()))
        (STATE / "outbox.jsonl").unlink()
        self.current += timedelta(hours=48)
        self.assertTrue(S.silence_breaker(self.cfg, self.verdict()))
        (STATE / "outbox.jsonl").unlink()
        self.current += timedelta(hours=24)
        changed = self.verdict(extra=[{
            "id": "rv_validation", "ok": False, "severity": "critical",
            "detail": "validated PRs stopped merging"}])
        self.assertTrue(S.silence_breaker(self.cfg, changed))
        self.assertEqual(48, self.state()["window_hours"])

    def test_healthy_degraded_recent_delivery_pending_off_ack_and_disabled(self):
        self.assertFalse(S.silence_breaker(self.cfg, self.verdict("healthy")))

        self.write_sent(self.current - timedelta(hours=80), "old")
        self.assertTrue(S.silence_breaker(self.cfg, self.verdict("degraded")))
        (STATE / "outbox.jsonl").unlink()
        (STATE / "silence-breaker.json").unlink()
        self.write_sent(self.current - timedelta(hours=70), "not old enough")
        self.assertFalse(S.silence_breaker(self.cfg, self.verdict("degraded")))

        self.write_sent(self.current - timedelta(hours=2), "recent")
        self.assertFalse(S.silence_breaker(self.cfg, self.verdict()))

        self.write_sent(self.current - timedelta(days=36), "old")
        self.write_jsonl(STATE / "outbox.jsonl", [{
            "entry_id": "2" * 32, "at": self.current.isoformat(timespec="seconds"),
            "to": self.cfg["notify_handle"], "text": "prior",
            "attachments": [], "dedupe_key": "silence-breaker:pending:1"}])
        self.assertFalse(S.silence_breaker(self.cfg, self.verdict()))

        (STATE / "outbox.jsonl").unlink()
        off = dict(self.cfg, notification_mode="off")
        self.assertFalse(S.silence_breaker(off, self.verdict()))
        self.assertFalse((STATE / "outbox.jsonl").exists())
        self.assertEqual("muted", self.alerts()[-1]["decision"])

        future = dict(self.cfg,
                      silence_ack_until=(self.current + timedelta(hours=3)).isoformat(),
                      silence_ack_reason="operator is already repairing it")
        self.assertFalse(S.silence_breaker(future, self.verdict()))
        self.assertEqual("suppressed", self.alerts()[-1]["decision"])
        past = dict(self.cfg,
                    silence_ack_until=(self.current - timedelta(seconds=1)).isoformat())
        self.assertTrue(S.silence_breaker(past, self.verdict()))

        disabled = dict(self.cfg, silence_breaker_hours=0)
        self.assertFalse(S.silence_breaker(disabled, self.verdict()))

    def test_quiet_switches_are_ledgered_once_per_window_not_every_tick(self):
        off = dict(self.cfg, notification_mode="off")
        for _ in range(4):
            self.assertFalse(S.silence_breaker(off, self.verdict()))
            self.current += timedelta(minutes=15)
        decisions = lambda: [a["decision"] for a in self.alerts()]
        self.assertEqual(1, decisions().count("muted"))
        self.current += timedelta(hours=24)
        self.assertFalse(S.silence_breaker(off, self.verdict()))
        self.assertEqual(2, decisions().count("muted"))
        self.assertEqual([], self.queue())

        ack = dict(self.cfg, silence_ack_reason="known outage",
                   silence_ack_until=(self.current + timedelta(days=3)).isoformat())
        for _ in range(4):
            self.assertFalse(S.silence_breaker(ack, self.verdict()))
            self.current += timedelta(minutes=15)
        self.assertEqual(1, decisions().count("suppressed"))
        # An acknowledgement records nothing while no breaker would be due.
        (STATE / "silence-breaker.json").unlink()
        self.write_sent(self.current - timedelta(hours=2), "recent")
        before = len(self.alerts())
        self.assertFalse(S.silence_breaker(ack, self.verdict()))
        self.assertEqual(before, len(self.alerts()))

    def test_never_delivered_gets_first_instance_grace(self):
        (STATE / "outbox-sent.jsonl").unlink()
        self.write_chain(hours=(2,), statuses=("critical",))
        self.assertFalse(S.silence_breaker(self.cfg, self.verdict()))
        self.write_chain(hours=(30,), statuses=("critical",))
        self.assertTrue(S.silence_breaker(self.cfg, self.verdict()))

    def test_lost_state_file_cannot_swallow_a_breaker(self):
        # Review repro: keys were `silence-breaker:<digest>:<count>`; losing
        # the state file restarted the count, enqueue() refused the key a
        # dead-lettered breaker already held, notify() still said True, and
        # ~13 days went silent.
        for _ in range(3):
            self.assertTrue(S.silence_breaker(self.cfg, self.verdict()))
            self.dead_letter_queue()
            (STATE / "silence-breaker.json").unlink()
        self.assertTrue(S.silence_breaker(self.cfg, self.verdict()))
        self.assertEqual(1, len(self.queue()))

    def dead_letter_queue(self):
        """Move queued entries to the dead-letter ledger exactly as
        watcher_outbox.fail() does after its final attempt."""
        import outbox
        import watcher_outbox as W
        lines = (STATE / "outbox.jsonl").read_text(encoding="utf-8").splitlines()
        for raw in [line for line in lines if line.strip()]:
            message = json.loads(raw)
            digest = W._digest(raw)
            W._append_durable(outbox.DEAD_LETTER, {
                **message,
                "entry_id": outbox._queue_entry_identity(message, digest),
                "queue_sha256": digest,
                "failed_at": outbox.now(),
                "attempts": W.MAX_ATTEMPTS,
                "reason": "fixture: Messages did not confirm delivery"})
        (STATE / "outbox.jsonl").unlink()

    def test_outbox_quarantine_incident_does_not_block_the_breaker(self):
        # Review repro: one malformed terminal-ledger line is an incident that
        # blocks every dedupe-keyed enqueue until an operator resolves it.
        import outbox
        with (STATE / "outbox-sent.jsonl").open("a", encoding="utf-8") as fh:
            fh.write('{"torn": \n')
        with self.assertRaises(outbox.DedupeAmbiguityError):
            outbox.enqueue("keyed probe", self.cfg["notify_handle"],
                           dedupe_key="probe:1")
        self.assertTrue(S.silence_breaker(self.cfg, self.verdict()))
        texts = [m["text"] for m in self.queue()]
        self.assertEqual(1, len(texts), texts)
        self.assertIn("36 days without a message", texts[0])

    def test_unreadable_ledger_line_does_not_erase_proven_deliveries(self):
        with (STATE / "outbox-sent.jsonl").open("a", encoding="utf-8") as fh:
            fh.write('{"torn": \n["not a record"]\n')
        self.assertEqual(self.current - timedelta(days=36),
                         S._last_delivered_at(self.cfg["notify_handle"]))

    def test_only_sends_to_the_operator_count_and_unverified_ones_do(self):
        other = "+15555550199"
        self.write_jsonl(STATE / "outbox-sent.jsonl", [
            {"entry_id": "1" * 32, "to": self.cfg["notify_handle"], "text": "old",
             "sent_at": (self.current - timedelta(days=36)).isoformat(timespec="seconds")},
            {"entry_id": "3" * 32, "to": other, "text": "nightwatch report",
             "sent_at": (self.current - timedelta(hours=2)).isoformat(timespec="seconds")},
        ])
        self.assertTrue(S.silence_breaker(self.cfg, self.verdict()))
        (STATE / "outbox.jsonl").unlink()
        (STATE / "silence-breaker.json").unlink()
        # Messages accepted it but chat.db could not confirm: the operator was
        # most likely told, and alert_delivery reports the unverified send.
        self.write_jsonl(STATE / "outbox-unverified.jsonl", [{
            "entry_id": "4" * 32, "to": self.cfg["notify_handle"], "text": "🔕 breaker",
            "attempted_at": (self.current - timedelta(hours=2)).isoformat(timespec="seconds"),
            "reason": "Messages accepted the send but chat.db was unreadable"}])
        self.assertFalse(S.silence_breaker(self.cfg, self.verdict()))
        self.assertEqual([], self.queue())

    def test_anything_already_queued_to_the_operator_defers_the_breaker(self):
        # e.g. the state-change alert main() enqueued earlier in this tick
        self.write_jsonl(STATE / "outbox.jsonl", [{
            "entry_id": "5" * 32, "at": self.current.isoformat(timespec="seconds"),
            "to": self.cfg["notify_handle"], "text": "⚠️ degraded → critical",
            "attachments": []}])
        self.assertFalse(S.silence_breaker(self.cfg, self.verdict()))
        self.assertEqual(1, len(self.queue()))
        self.assertFalse((STATE / "silence-breaker.json").exists())

    def test_healthy_tick_resets_backoff_so_a_relapse_is_told_promptly(self):
        # Review repro: four pages backed off to 168h; after a recovery the
        # same failing set relapsed and waited up to a week.
        for gap in (0, 48, 96, 168):
            self.current += timedelta(hours=gap)
            self.assertTrue(S.silence_breaker(self.cfg, self.verdict()))
            (STATE / "outbox.jsonl").unlink()
        self.assertEqual(168, self.state()["window_hours"])
        self.current += timedelta(hours=12)
        self.assertFalse(S.silence_breaker(self.cfg, self.verdict("healthy")))
        self.assertEqual("", self.state()["fingerprint"])
        self.assertEqual(24, self.state()["window_hours"])
        self.current += timedelta(hours=24)  # 36h after the last page
        self.assertTrue(S.silence_breaker(self.cfg, self.verdict()))
        self.assertEqual(48, self.state()["window_hours"])

    def test_invalid_mode_mutes_operational_alerts_but_not_the_breaker(self):
        typo = dict(self.cfg, notification_mode="art_only")
        self.assertFalse(S.notification_allowed(typo, "operational"))
        self.assertTrue(S.silence_breaker(typo, self.verdict()))
        text = self.queue()[0]["text"]
        self.assertIn("notification_mode='art_only' is not all/art-only/off", text)
        self.assertLessEqual(len(text), 700)

    def test_crashing_tick_breaks_silence_in_quiet_mode(self):
        with mock.patch.object(S, "config", return_value=self.cfg):
            S.report_crash(SyntaxError("invalid syntax (checks.py, line 1)"))
        self.assertEqual("crashed", S.load_json(STATE / "last_run.json", {})["status"])
        q = self.queue()
        self.assertEqual(1, len(q), q)
        self.assertIn("Local machinery: sentinel_tick (tick crashed: SyntaxError", q[0]["text"])
        decisions = [a["decision"] for a in self.alerts()]
        self.assertEqual(["muted", "paged"], decisions[-2:])

        # In "all" mode the crash alert itself is queued, and the breaker
        # waits behind it instead of doubling up.
        (STATE / "outbox.jsonl").unlink()
        (STATE / "silence-breaker.json").unlink()
        loud = dict(self.cfg, notification_mode="all")
        import standup
        with mock.patch.object(S, "config", return_value=loud), \
                mock.patch.object(standup, "portable_snapshot", return_value={}), \
                mock.patch.object(standup, "publish_snapshot", return_value=[]):
            S.report_crash(RuntimeError("boom"))
        q = self.queue()
        self.assertEqual(1, len(q), q)
        self.assertIn("CRASHED: RuntimeError: boom", q[0]["text"])

    def test_main_wraps_breaker_after_state_change_notify_before_level_zero(self):
        S.save_json(STATE / "last_run.json", {"status": "healthy"})
        events = []

        def fake_notify(*args, **kwargs):
            events.append("notify")
            return True

        def fake_breaker(cfg, verdict):
            self.assertTrue((STATE / "last_verdict.json").exists())
            events.append("breaker")
            raise RuntimeError("fixture boom")

        with mock.patch.object(S, "config", return_value=self.cfg), \
                mock.patch.object(S, "ensure_evolution_worker_loaded"), \
                mock.patch.object(S, "run_health", return_value=self.verdict()), \
                mock.patch.object(S, "notify", side_effect=fake_notify), \
                mock.patch.object(S, "silence_breaker", side_effect=fake_breaker), \
                mock.patch.object(S.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, "", "")), \
                mock.patch.object(S.NB, "emit"), \
                mock.patch.object(S.NB, "roll_call", return_value={}), \
                mock.patch.object(S.NB, "anchor_heads"), \
                mock.patch.object(S.NB, "publish_head"), \
                mock.patch.object(S.NB, "peer_roll_call", return_value={}), \
                mock.patch.object(S.NB, "check_anchors", return_value={}):
            self.assertEqual(0, S.main())
        self.assertEqual(["notify", "breaker"], events)


if __name__ == "__main__":
    unittest.main()
