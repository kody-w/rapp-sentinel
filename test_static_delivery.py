import http.client
import io
import json
import os
import plistlib
import re
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import checks
import health
import neighborhood
import nightwatch
import outbox
import participate
import paths
import retro
import sentinel
import serve
import standup
import watcher_outbox


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

    def test_outbox_launchd_drainer_contract(self):
        root = Path(__file__).resolve().parent
        payload = plistlib.loads(
            (root / "com.rapp.outbox-drain.plist.template").read_bytes())
        self.assertEqual("com.rapp.outbox-drain", payload["Label"])
        self.assertEqual(
            ["/usr/bin/python3", "__DIR__/outbox.py", "drain"],
            payload["ProgramArguments"],
        )
        self.assertEqual("Aqua", payload["LimitLoadToSessionType"])
        self.assertTrue(payload["RunAtLoad"])
        self.assertLessEqual(payload["StartInterval"], 300)
        self.assertIn("outbox-drain.out.log", payload["StandardOutPath"])
        self.assertIn("outbox-drain.err.log", payload["StandardErrorPath"])

    def test_install_script_loads_outbox_drainer(self):
        root = Path(__file__).resolve().parent
        script = (root / "install-launchd.sh").read_text(encoding="utf-8")
        self.assertIn('OLABEL="com.rapp.outbox-drain"', script)
        self.assertIn('$DIR/$OLABEL.plist.template', script)
        self.assertIn('launchctl load "$OPLIST"', script)


class ServeRoutePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root
        (root / "dashboard").mkdir(parents=True, exist_ok=True)
        (root / "logs").mkdir(parents=True, exist_ok=True)
        (root / "public").mkdir(parents=True, exist_ok=True)
        (root / "state" / "shared-reports").mkdir(parents=True, exist_ok=True)
        (root / "state").mkdir(parents=True, exist_ok=True)
        (root / "dashboard" / "index.html").write_text(
            "<html><body>dashboard ok</body></html>", encoding="utf-8")
        (root / "logs" / "decision.log").write_text(
            "decision transcript", encoding="utf-8")
        (root / "public" / "sentinel-head.json").write_text(
            '{"schema":"rapp-sentinel-head/1.0"}', encoding="utf-8")
        (root / "state" / "private.json").write_text("{}", encoding="utf-8")
        self.shared_name = "tokenized-report.html"
        (root / "state" / "shared-reports" / self.shared_name).write_text(
            "<html>shared</html>", encoding="utf-8")

        self.old = (serve.HOME, serve.DASH, serve.LOGS, serve.SHARED_REPORTS)
        serve.HOME = root
        serve.DASH = root / "dashboard"
        serve.LOGS = root / "logs"
        serve.SHARED_REPORTS = root / "state" / "shared-reports"
        self.rebuild_patcher = mock.patch.object(serve, "rebuild")
        self.rebuild = self.rebuild_patcher.start()

        socketserver.TCPServer.allow_reuse_address = True
        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), serve.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        self.rebuild_patcher.stop()
        serve.HOME, serve.DASH, serve.LOGS, serve.SHARED_REPORTS = self.old
        self.temp.cleanup()

    def _request(self, method, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path)
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, dict(resp.getheaders()), body
        finally:
            conn.close()

    def test_private_routes_require_loopback_for_get_and_head(self):
        not_loopback = mock.Mock()
        not_loopback.is_loopback = False
        with mock.patch.object(
                serve.ipaddress, "ip_address", return_value=not_loopback):
            for method in ("GET", "HEAD"):
                for path in ("/", "/logs/decision.log", "/state/private.json",
                             "/sentinel-head.json"):
                    status, _, _ = self._request(method, path)
                    self.assertEqual(404, status, f"{method} {path}")

    def test_shared_tokenized_report_allows_get_and_head(self):
        not_loopback = mock.Mock()
        not_loopback.is_loopback = False
        with mock.patch.object(
                serve.ipaddress, "ip_address", return_value=not_loopback):
            for method in ("GET", "HEAD"):
                status, headers, body = self._request(
                    method, f"/share/{self.shared_name}")
                self.assertEqual(200, status, method)
                self.assertEqual("text/html; charset=utf-8",
                                 headers["Content-Type"])
                self.assertEqual("private, no-store", headers["Cache-Control"])
                self.assertEqual(b"" if method == "HEAD" else b"<html>shared</html>",
                                 body)

    def test_loopback_dashboard_still_serves(self):
        for method in ("GET", "HEAD"):
            status, _, body = self._request(method, "/")
            self.assertEqual(200, status, method)
            if method == "GET":
                self.assertIn(b"dashboard ok", body)
            else:
                self.assertEqual(b"", body)


class SparseWorkflowSweepTests(unittest.TestCase):
    def test_failed_sparse_workflow_cannot_fall_out_of_shared_window(self):
        shared = [
            {"name": "CI", "conclusion": "success"}
            for _ in range(100)
        ]
        release = [
            {"conclusion": "failure"},
            {"conclusion": "failure"},
            {"conclusion": "success"},
            {"conclusion": "success"},
        ]

        def fake_gh(args, default=None):
            if args[:2] == ["api", "repos/kody-w/openrappter/actions/workflows"]:
                return ["CI", "Release"]
            if "--workflow" in args:
                self.assertEqual("Release", args[args.index("--workflow") + 1])
                return release
            return shared

        with mock.patch.object(checks, "gh", side_effect=fake_gh):
            result = checks.workflows_currently_broken(
                "kody-w/openrappter", streak=4)

        self.assertEqual(2, result["Release"]["streak"])
        self.assertEqual(4, result["Release"]["of"])
        self.assertTrue(result["Release"]["insufficient_window"])
        self.assertTrue(result["Release"]["sparse"])

    def test_sparse_workflow_with_latest_success_is_not_flagged(self):
        shared = [{"name": "CI", "conclusion": "success"}] * 100

        def fake_gh(args, default=None):
            if args[:2] == ["api", "repos/kody-w/openrappter/actions/workflows"]:
                return ["CI", "Release"]
            if "--workflow" in args:
                return [
                    {"conclusion": "success"},
                    {"conclusion": "failure"},
                ]
            return shared

        with mock.patch.object(checks, "gh", side_effect=fake_gh):
            result = checks.workflows_currently_broken(
                "kody-w/openrappter", streak=4)

        self.assertNotIn("Release", result)

    def test_sparse_detail_reports_newest_failure_streak(self):
        with mock.patch.object(
                checks, "declared_repos",
                return_value=["kody-w/openrappter"]), \
                mock.patch.object(checks, "DEEPLY_CHECKED", set()), \
                mock.patch.object(
                    checks, "workflows_currently_broken",
                    return_value={
                        "Release": {
                            "streak": 2,
                            "of": 4,
                            "failed": 2,
                            "insufficient_window": True,
                            "sparse": True,
                        },
                    }), \
                mock.patch.object(checks, "_write_coverage_receipt"):
            result = checks.ecosystem_not_silently_broken()

        self.assertFalse(result["ok"])
        self.assertIn("Release (newest 2 failed; 4 runs inspected)",
                      result["detail"])


class OutboxAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.old = (
            outbox.QUEUE, outbox.SENT, outbox.LAST_DRAIN, outbox.REPORTS,
            outbox.LOCK, outbox.DRAIN_LOCK, outbox.UNVERIFIED,
            outbox.DEAD_LETTER)
        outbox.QUEUE = root / "outbox.jsonl"
        outbox.SENT = root / "sent.jsonl"
        outbox.LAST_DRAIN = root / "last.json"
        outbox.REPORTS = root / "reports"
        outbox.LOCK = root / "outbox.lock"
        outbox.DRAIN_LOCK = root / "outbox-drain.lock"
        outbox.UNVERIFIED = root / "outbox-unverified.jsonl"
        outbox.DEAD_LETTER = root / "outbox-dead-letter.jsonl"
        outbox.REPORTS.mkdir()

    def tearDown(self):
        (outbox.QUEUE, outbox.SENT, outbox.LAST_DRAIN, outbox.REPORTS,
         outbox.LOCK, outbox.DRAIN_LOCK, outbox.UNVERIFIED,
         outbox.DEAD_LETTER) = self.old
        self.temp.cleanup()

    def test_drain_passes_attachment_and_cleans_generated_snapshot(self):
        report = outbox.REPORTS / "report.html"
        report.write_text("<html>report</html>", encoding="utf-8")
        outbox.enqueue("summary", "recipient", [report])
        queued = Path(outbox._pending()[0]["attachments"][0])
        rewrite_seen = {}
        real_rewrite = outbox._rewrite_queue_unlocked

        def wrapped_rewrite(lines):
            rewrite_seen["attachment_present_during_commit"] = queued.exists()
            return real_rewrite(lines)

        with mock.patch.object(outbox, "_send", return_value=(True, "")) as send, \
                mock.patch.object(
                    outbox,
                    "_rewrite_queue_unlocked",
                    side_effect=wrapped_rewrite,
                ):
            sent, kept, why = outbox.drain()
        self.assertEqual((1, 0, ""), (sent, kept, why))
        self.assertEqual([str(queued)], send.call_args.args[2])
        self.assertTrue(rewrite_seen["attachment_present_during_commit"])
        self.assertFalse(report.exists())
        self.assertFalse(queued.exists())

    def test_sent_ledger_write_failure_keeps_head_and_attachment(self):
        report = outbox.REPORTS / "ledger-failure.html"
        report.write_text("<html>ledger-failure</html>", encoding="utf-8")
        outbox.enqueue("summary", "recipient", [report])
        queued = Path(outbox._pending()[0]["attachments"][0])

        with mock.patch.object(outbox, "_send", return_value=(True, "")), \
                mock.patch.object(
                    outbox, "_append_sent", side_effect=OSError("ledger unavailable")
                ):
            sent, kept, why = outbox.drain()

        self.assertEqual((0, 1), (sent, kept))
        self.assertIn("sent ledger write failed", why)
        self.assertTrue(queued.exists())
        pending = outbox._pending()
        self.assertEqual(1, len(pending))
        self.assertEqual(str(queued), pending[0]["attachments"][0])

    def test_queue_rewrite_failure_keeps_attachment_and_recovers(self):
        report = outbox.REPORTS / "rewrite-failure.html"
        report.write_text("<html>rewrite-failure</html>", encoding="utf-8")
        outbox.enqueue("summary", "recipient", [report])
        queued = Path(outbox._pending()[0]["attachments"][0])

        with mock.patch.object(outbox, "_send", return_value=(True, "")), \
                mock.patch.object(
                    outbox.os, "replace", side_effect=OSError("rewrite failed")
                ):
            with self.assertRaises(OSError):
                outbox.drain()

        self.assertTrue(queued.exists())
        self.assertEqual(1, len(outbox._pending()))

        with mock.patch.object(outbox, "_send", return_value=(True, "")) as send:
            sent, kept, why = outbox.drain()

        self.assertEqual((1, 0, ""), (sent, kept, why))
        self.assertEqual([str(queued)], send.call_args.args[2])
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

    def test_unreadable_delivery_ledger_never_becomes_success(self):
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(outbox, "_delivered_count", return_value=None), \
                mock.patch.object(outbox.subprocess, "run", return_value=completed):
            ok, reason = outbox._send("alert", "recipient")
        self.assertFalse(ok)
        self.assertIn("unverifiable", reason)

    def test_drain_keeps_enqueue_from_another_process(self):
        outbox.enqueue("first", "recipient")
        cmd = "\n".join([
            "import outbox",
            "from pathlib import Path",
            f"outbox.QUEUE = Path({str(outbox.QUEUE)!r})",
            f"outbox.SENT = Path({str(outbox.SENT)!r})",
            f"outbox.LAST_DRAIN = Path({str(outbox.LAST_DRAIN)!r})",
            f"outbox.REPORTS = Path({str(outbox.REPORTS)!r})",
            f"outbox.LOCK = Path({str(outbox.LOCK)!r})",
            f"outbox.DRAIN_LOCK = Path({str(outbox.DRAIN_LOCK)!r})",
            "outbox.enqueue('late', 'recipient')",
        ])

        injected = {"done": False}

        def fake_send(_text, _to, _attachments=None):
            if not injected["done"]:
                injected["done"] = True
                subprocess.run(
                    [sys.executable, "-c", cmd],
                    check=True,
                    cwd=str(Path(outbox.__file__).resolve().parent),
                )
            return True, ""

        with mock.patch.object(outbox, "_send", side_effect=fake_send):
            sent, kept, why = outbox.drain(limit=1)

        self.assertEqual((1, 1, ""), (sent, kept, why))
        pending = outbox._pending()
        self.assertEqual(["late"], [m["text"] for m in pending])


class WatcherOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.old = (
            outbox.QUEUE, outbox.SENT, outbox.LAST_DRAIN, outbox.REPORTS,
            outbox.LOCK, outbox.DRAIN_LOCK, outbox.UNVERIFIED,
            outbox.DEAD_LETTER, watcher_outbox.CLAIMS,
            watcher_outbox.ATTEMPTS)
        outbox.QUEUE = root / "outbox.jsonl"
        outbox.SENT = root / "sent.jsonl"
        outbox.LAST_DRAIN = root / "last.json"
        outbox.REPORTS = root / "reports"
        outbox.LOCK = root / "outbox.lock"
        outbox.DRAIN_LOCK = root / "outbox-drain.lock"
        outbox.UNVERIFIED = root / "outbox-unverified.jsonl"
        outbox.DEAD_LETTER = root / "outbox-dead-letter.jsonl"
        watcher_outbox.CLAIMS = root / "watcher-claims"
        watcher_outbox.ATTEMPTS = root / "outbox-attempts.json"
        outbox.REPORTS.mkdir()

    def tearDown(self):
        (outbox.QUEUE, outbox.SENT, outbox.LAST_DRAIN, outbox.REPORTS,
         outbox.LOCK, outbox.DRAIN_LOCK, outbox.UNVERIFIED,
         outbox.DEAD_LETTER, watcher_outbox.CLAIMS,
         watcher_outbox.ATTEMPTS) = self.old
        self.temp.cleanup()

    def _claim(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, watcher_outbox.claim())
        return Path(output.getvalue().strip())

    def test_acknowledge_removes_only_the_claimed_head(self):
        outbox.enqueue("first", "recipient")
        claim = self._claim()
        outbox.enqueue("second", "recipient")

        watcher_outbox.acknowledge(claim, "sent by authorized watcher")

        self.assertEqual(["second"], [row["text"] for row in outbox._pending()])
        sent = [json.loads(line) for line in outbox.SENT.read_text().splitlines()]
        self.assertEqual(["first"], [row["text"] for row in sent])
        self.assertEqual("sent by authorized watcher",
                         outbox.last_drain()["why"])

    def test_changed_head_is_never_acknowledged(self):
        outbox.enqueue("first", "recipient")
        claim = self._claim()
        with outbox._locked(outbox.LOCK):
            outbox._rewrite_queue_unlocked([
                json.dumps({
                    "at": outbox.now(), "to": "recipient",
                    "text": "replacement", "attachments": [],
                }),
            ])

        with self.assertRaises(RuntimeError):
            watcher_outbox.acknowledge(claim)

        self.assertEqual(["replacement"],
                         [row["text"] for row in outbox._pending()])
        self.assertFalse(outbox.SENT.exists())

    def test_uncertain_send_leaves_a_durable_unverified_record(self):
        outbox.enqueue("uncertain", "recipient")
        claim = self._claim()

        watcher_outbox.uncertain(claim, "chat.db unreadable")

        self.assertEqual([], outbox._pending())
        self.assertFalse(outbox.SENT.exists())
        record = json.loads(outbox.UNVERIFIED.read_text().splitlines()[0])
        self.assertEqual("uncertain", record["text"])
        self.assertIn("chat.db unreadable", record["reason"])
        self.assertEqual(1, outbox.status()["unverified"])

    def test_failed_send_backs_off_instead_of_immediate_resend(self):
        outbox.enqueue("retry", "recipient")
        claim = self._claim()

        watcher_outbox.fail(claim, "Messages unavailable")

        self.assertEqual(5, watcher_outbox.claim())
        attempts = json.loads(watcher_outbox.ATTEMPTS.read_text())
        self.assertEqual(1, next(iter(attempts.values()))["count"])
        self.assertEqual(["retry"], [row["text"] for row in outbox._pending()])

    def test_third_failed_send_moves_to_dead_letter(self):
        outbox.enqueue("dead", "recipient")
        raw_line = outbox._queue_lines_unlocked()[0]
        digest = watcher_outbox._digest(raw_line)
        watcher_outbox.ATTEMPTS.write_text(json.dumps({
            digest: {"count": 2, "reason": "prior failures"},
        }))
        claim = self._claim()

        watcher_outbox.fail(claim, "still unavailable")

        self.assertEqual([], outbox._pending())
        record = json.loads(outbox.DEAD_LETTER.read_text().splitlines()[0])
        self.assertEqual(3, record["attempts"])
        self.assertEqual("dead", record["text"])
        self.assertEqual(1, outbox.status()["dead_letter"])

    def test_corrupt_attempt_ledger_fails_closed(self):
        outbox.enqueue("do not send", "recipient")
        watcher_outbox.ATTEMPTS.write_text("{broken", encoding="utf-8")

        with self.assertRaises(RuntimeError):
            watcher_outbox.claim()

        self.assertEqual(["do not send"],
                         [row["text"] for row in outbox._pending()])


class SmokePolicyTests(unittest.TestCase):
    def test_read_only_instance_declares_smoke_disabled(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "config.json").write_text(
                '{"smoke_enabled": false}\n', encoding="utf-8")
            old_home = checks.HOME
            checks.HOME = root
            try:
                result = checks.outsider_smoke_exercised()
            finally:
                checks.HOME = old_home

        self.assertTrue(result["ok"])
        self.assertIn("read-only", result["detail"])


class AlertDeliveryTests(unittest.TestCase):
    def test_unverified_send_is_not_reported_healthy(self):
        with mock.patch.object(outbox, "status", return_value={
            "pending": 0,
            "oldest_minutes": None,
            "missing_attachments": 0,
            "unverified": 1,
            "dead_letter": 0,
            "last_drain": {"why": "delivery unverified: chat.db unreadable"},
        }):
            result = checks.alerts_can_actually_reach_you()
        self.assertFalse(result["ok"])
        self.assertIn("explicitly unverified", result["detail"])

    def test_dead_letter_is_not_reported_healthy(self):
        with mock.patch.object(outbox, "status", return_value={
            "pending": 0,
            "oldest_minutes": None,
            "missing_attachments": 0,
            "unverified": 0,
            "dead_letter": 1,
            "last_drain": {"why": "dead-lettered"},
        }):
            result = checks.alerts_can_actually_reach_you()
        self.assertFalse(result["ok"])
        self.assertIn("dead-letter", result["detail"])


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

    def test_single_type_bulk_phase_with_many_actors_passes(self):
        fresh = datetime.now(timezone.utc).isoformat()
        actions = [{
            "agentId": f"teacher-{index:03d}",
            "type": "teach",
            "data": {"studentId": f"student-{index:03d}", "skill": "art"},
        } for index in range(100)]
        documents = [
            ({"actions": actions, "_meta": {"lastUpdate": fresh}}, ""),
            ({"messages": [{"id": "new"}], "_meta": {"lastUpdate": fresh}}, ""),
            ({"agents": [], "_meta": {"lastUpdate": fresh}}, ""),
        ]
        with mock.patch.object(checks, "public_json", side_effect=documents):
            result = checks.world_is_meaningfully_active()
        self.assertTrue(result["ok"])
        self.assertIn("50 actors, 1 action types", result["detail"])

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


class CompletenessRegressionTests(unittest.TestCase):
    def test_missing_required_check_still_fails_critical(self):
        required = json.loads(
            (health.CODE / "required_checks.json").read_text(encoding="utf-8")
        )["required"]
        self.assertIn("w_checks_complete", required)
        self.assertGreater(len(required), 1)

        missing = next(cid for cid in required if cid != "w_checks_complete")
        results = [{
            "id": cid,
            "ok": True,
            "severity": checks.WARN,
            "detail": "",
            "produced_by": "test",
        } for cid in required if cid not in {missing, "w_checks_complete"}]

        verdict = health.check_completeness(results)
        self.assertFalse(verdict["ok"])
        self.assertEqual(checks.CRITICAL, verdict["severity"])
        self.assertIn(missing, verdict["detail"])


class SentinelHomeContractTests(unittest.TestCase):
    """paths.py is the ONLY place HOME is derived (#1, ask 1).

    Ten modules deriving HOME independently is ten chances for one of them to
    miss SENTINEL_HOME — and a module that misses it writes a second, silent
    instance into the code tree. So the contract is enforced two ways: by
    source (no runtime module re-derives Path(__file__).resolve().parent) and
    by identity (every module's HOME is literally paths.HOME, not a copy that
    could drift).
    """

    RUNTIME_MODULES = (
        sentinel, health, checks, neighborhood, outbox,
        standup, participate, nightwatch, serve, retro,
    )
    _DERIVATION = re.compile(r"Path\(__file__\)\.resolve\(\)\.parent")

    def test_no_runtime_module_rederives_home_from_file(self):
        # The one legitimate use of the module's own location is putting the
        # module directory on sys.path so a sibling import works from any
        # cwd — that is a fact about the CODE and must stay on __file__.
        # Anything else is a path derivation that would bypass SENTINEL_HOME.
        for module in self.RUNTIME_MODULES:
            source = Path(module.__file__).read_text(encoding="utf-8")
            for number, line in enumerate(source.splitlines(), start=1):
                if not self._DERIVATION.search(line):
                    continue
                self.assertIn(
                    "sys.path.insert", line,
                    f"{Path(module.__file__).name}:{number} derives a path "
                    f"from __file__ outside paths.py: {line.strip()!r}")

    def test_every_runtime_module_shares_the_paths_home(self):
        for module in self.RUNTIME_MODULES:
            self.assertIs(
                module.HOME, paths.HOME,
                f"{Path(module.__file__).name} carries its own HOME")

    def test_unset_env_home_is_the_code_directory(self):
        # The molt constraint: the live install runs with SENTINEL_HOME unset
        # and its paths — including the external ledger key derived from
        # str(HOME) — must be byte-identical to what the old per-module
        # derivation produced.
        env = {k: v for k, v in os.environ.items() if k != "SENTINEL_HOME"}
        out = subprocess.run(
            [sys.executable, "-c", "import paths; print(paths.HOME)"],
            capture_output=True, text=True, env=env,
            cwd=str(Path(__file__).resolve().parent), check=True)
        self.assertEqual(
            str(Path(__file__).resolve().parent), out.stdout.strip())

    def test_install_script_offers_a_home_flag(self):
        script = (Path(__file__).resolve().parent /
                  "install-launchd.sh").read_text(encoding="utf-8")
        self.assertIn("--home", script)
        self.assertIn("EnvironmentVariables:SENTINEL_HOME", script)


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
