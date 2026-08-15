import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import checks
import nightwatch
import outbox
import standup


class PortableReportTests(unittest.TestCase):
    def test_snapshot_inlines_local_transcript(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            old = (standup.OUT, standup.LOGS, standup.REPORTS)
            standup.OUT = root / "dashboard"
            standup.LOGS = root / "logs"
            standup.REPORTS = root / "reports"
            standup.OUT.mkdir()
            standup.LOGS.mkdir()
            (standup.LOGS / "decision.log").write_text(
                "verified transcript", encoding="utf-8")
            (standup.OUT / "index.html").write_text(
                '<html><head><meta http-equiv="refresh" content="300"></head>'
                '<body><a href="/logs/decision.log">full transcript</a>'
                '<footer>auto-refresh 5 min · nothing here is stored twice'
                '</footer></body></html>',
                encoding="utf-8",
            )
            try:
                snapshot = standup.portable_snapshot(rebuild=False)
                page = snapshot.read_text(encoding="utf-8")
            finally:
                standup.OUT, standup.LOGS, standup.REPORTS = old
            self.assertNotIn('http-equiv="refresh"', page)
            self.assertNotIn('href="/logs/', page)
            self.assertIn("verified transcript", page)
            self.assertIn("portable static snapshot", page)

    def test_publish_snapshot_uses_tokenized_private_urls(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            snapshot = root / "report.html"
            snapshot.write_text("<html>private</html>", encoding="utf-8")
            old = standup.SHARED_REPORTS
            standup.SHARED_REPORTS = root / "shared"
            try:
                with mock.patch.object(
                        standup, "private_report_addresses",
                        return_value=["100.64.1.2", "192.168.1.2"]):
                    urls = standup.publish_snapshot(snapshot)
            finally:
                standup.SHARED_REPORTS = old
            self.assertEqual(2, len(urls))
            self.assertTrue(all("/share/" in url for url in urls))
            token_names = {url.rsplit("/", 1)[-1] for url in urls}
            self.assertEqual(1, len(token_names))
            self.assertTrue((root / "shared" / token_names.pop()).is_file())


class OutboxAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.old = (
            outbox.QUEUE, outbox.SENT, outbox.LAST_DRAIN, outbox.REPORTS)
        outbox.QUEUE = root / "outbox.jsonl"
        outbox.SENT = root / "sent.jsonl"
        outbox.LAST_DRAIN = root / "last.json"
        outbox.REPORTS = root / "reports"
        outbox.REPORTS.mkdir()

    def tearDown(self):
        outbox.QUEUE, outbox.SENT, outbox.LAST_DRAIN, outbox.REPORTS = self.old
        self.temp.cleanup()

    def test_drain_passes_attachment_and_cleans_generated_snapshot(self):
        report = outbox.REPORTS / "report.html"
        report.write_text("<html>report</html>", encoding="utf-8")
        outbox.enqueue("summary", "recipient", [report])
        queued = Path(outbox._pending()[0]["attachments"][0])
        with mock.patch.object(outbox, "_send", return_value=(True, "")) as send:
            sent, kept, why = outbox.drain()
        self.assertEqual((1, 0, ""), (sent, kept, why))
        self.assertEqual([str(queued)], send.call_args.args[2])
        self.assertFalse(report.exists())
        self.assertFalse(queued.exists())

    def test_html_is_queued_as_a_zip_containing_the_static_file(self):
        report = outbox.REPORTS / "phone-report.html"
        report.write_text("<html>phone</html>", encoding="utf-8")

        outbox.enqueue("summary", "recipient", [report])

        queued = Path(outbox._pending()[0]["attachments"][0])
        self.assertEqual(".zip", queued.suffix)
        self.assertFalse(report.exists())
        with zipfile.ZipFile(queued) as bundle:
            self.assertEqual(["phone-report.html"], bundle.namelist())
            self.assertEqual(
                b"<html>phone</html>", bundle.read("phone-report.html"))

    def test_missing_attachment_is_visible(self):
        outbox.enqueue("summary", "recipient", [outbox.REPORTS / "missing.html"])
        self.assertEqual(1, outbox.status()["missing_attachments"])

    def test_applescript_coerces_files_outside_messages_context(self):
        script = outbox.APPLESCRIPT
        self.assertLess(
            script.index("set attachmentFile to (POSIX file attachmentPath) as alias"),
            script.index('tell application "Messages"', script.index("repeat with")),
        )


class MeaningfulActivityTests(unittest.TestCase):
    def test_single_repetitive_actor_and_stale_chat_fail(self):
        stale = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        actions = [{
            "agentId": "clawdbot-001",
            "type": "emote",
            "data": {"emote": "think"},
        } for _ in range(50)]
        documents = [
            ({"actions": actions, "_meta": {"lastUpdate": stale}}, ""),
            ({"messages": [{"id": "old"}], "_meta": {"lastUpdate": stale}}, ""),
            ({"agents": [], "_meta": {"lastUpdate": stale}}, ""),
        ]
        with mock.patch.object(checks, "public_json", side_effect=documents):
            result = checks.world_is_meaningfully_active()
        self.assertFalse(result["ok"])
        self.assertIn("only 1 actor", result["detail"])
        self.assertIn("chat stale", result["detail"])

    def test_diverse_fresh_world_passes(self):
        fresh = datetime.now(timezone.utc).isoformat()
        actions = [{
            "agentId": f"agent-{index % 5}",
            "type": ("move", "chat", "interact")[index % 3],
            "data": {"index": index},
        } for index in range(50)]
        documents = [
            ({"actions": actions, "_meta": {"lastUpdate": fresh}}, ""),
            ({"messages": [{"id": "new"}], "_meta": {"lastUpdate": fresh}}, ""),
            ({"agents": [], "_meta": {"lastUpdate": fresh}}, ""),
        ]
        with mock.patch.object(checks, "public_json", side_effect=documents):
            result = checks.world_is_meaningfully_active()
        self.assertTrue(result["ok"])

    def test_old_duplicate_queue_fails_below_depth_threshold(self):
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        prs = [
            {"number": index, "title": "[action] same emote", "createdAt": old}
            for index in range(1, 11)
        ]
        with mock.patch.object(checks, "gh", return_value=prs):
            result = checks.queue_draining()
        self.assertFalse(result["ok"])
        self.assertIn("oldest PR", result["detail"])
        self.assertIn("repeat", result["detail"])


class RappterbookDerivedTruthTests(unittest.TestCase):
    def test_impossible_live_counters_fail(self):
        documents = [
            ({"total_posts": 97}, ""),
            ({"_meta": {"total_posts_analyzed": 11634}}, ""),
            ({"summary": {
                "total_comments": 107,
                "reply_rate_pct": 0,
                "avg_thread_depth": 107,
            }}, ""),
            ({"posts": [{"commentCount": 2}, {"commentCount": 1}]}, ""),
        ]
        with mock.patch.object(checks, "public_json", side_effect=documents):
            result = checks.rb_derived_state_tells_the_truth()
        self.assertFalse(result["ok"])
        self.assertIn("stats reports 97", result["detail"])
        self.assertIn("0% reply rate", result["detail"])

    def test_consistent_derived_state_passes(self):
        documents = [
            ({"total_posts": 16000}, ""),
            ({"_meta": {
                "real_posts_analyzed": 16000,
                "synthetic_posts_analyzed": 4000,
                "total_posts_analyzed": 20000,
            }}, ""),
            ({"summary": {
                "total_comments": 4,
                "reply_rate_pct": 50,
                "avg_thread_depth": 2,
            }}, ""),
            ({"posts": [{"commentCount": 2}, {"commentCount": 2}]}, ""),
        ]
        with mock.patch.object(checks, "public_json", side_effect=documents):
            result = checks.rb_derived_state_tells_the_truth()
        self.assertTrue(result["ok"])


class MessageContentTests(unittest.TestCase):
    def test_nightwatch_never_sends_localhost_link(self):
        source = Path(nightwatch.__file__).read_text(encoding="utf-8")
        self.assertNotIn("http://localhost:9797", source)
        self.assertIn("Static HTML report:", source)

    def test_repairs_render_as_repairs_instead_of_question_marks(self):
        outcome, transcript_kind = standup.action_outcome({
            "act": "repair",
            "result": "FIXED — restored the write path",
        })
        self.assertEqual(
            "REPAIR — FIXED — restored the write path", outcome)
        self.assertEqual("escalation", transcript_kind)


if __name__ == "__main__":
    unittest.main()
