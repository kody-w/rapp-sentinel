import base64
import hashlib
import http.client
import io
import json
import os
import plistlib
import re
import socketserver
import sqlite3
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
import verify_outbox
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
        self.assertIn('launchctl enable "gui/$(id -u)/$OLABEL"', script)
        self.assertIn('launchctl load "$OPLIST"', script)

    def test_install_script_reconciles_art_and_nightwatch_jobs(self):
        root = Path(__file__).resolve().parent
        script = (root / "install-launchd.sh").read_text(encoding="utf-8")
        self.assertIn("config_allows_nightwatch", script)
        self.assertIn('launchctl disable "gui/$(id -u)/$NLABEL"', script)
        self.assertIn('launchctl enable "gui/$(id -u)/$ELABEL"', script)


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
            outbox.DEAD_LETTER, outbox.EXPIRED, outbox.UNKNOWN,
            outbox.UNKNOWN_RESOLVED,
            outbox.INFLIGHT, outbox.QUARANTINE,
            outbox.QUARANTINE_RESOLVED, outbox.STRICT_RECOVERY)
        outbox.QUEUE = root / "outbox.jsonl"
        outbox.SENT = root / "sent.jsonl"
        outbox.LAST_DRAIN = root / "last.json"
        outbox.REPORTS = root / "reports"
        outbox.LOCK = root / "outbox.lock"
        outbox.DRAIN_LOCK = root / "outbox-drain.lock"
        outbox.UNVERIFIED = root / "outbox-unverified.jsonl"
        outbox.DEAD_LETTER = root / "outbox-dead-letter.jsonl"
        outbox.EXPIRED = root / "outbox-expired.jsonl"
        outbox.UNKNOWN = root / "outbox-unknown.jsonl"
        outbox.UNKNOWN_RESOLVED = root / "outbox-unknown-resolved.jsonl"
        outbox.INFLIGHT = root / "outbox-inflight.json"
        outbox.QUARANTINE = root / "outbox-terminal-quarantine.jsonl"
        outbox.QUARANTINE_RESOLVED = (
            root / "outbox-terminal-quarantine-resolved.jsonl")
        outbox.STRICT_RECOVERY = root / "outbox-strict-ledger-recovery.json"
        outbox.REPORTS.mkdir()

    def tearDown(self):
        (outbox.QUEUE, outbox.SENT, outbox.LAST_DRAIN, outbox.REPORTS,
         outbox.LOCK, outbox.DRAIN_LOCK, outbox.UNVERIFIED,
         outbox.DEAD_LETTER, outbox.EXPIRED, outbox.UNKNOWN,
         outbox.UNKNOWN_RESOLVED,
         outbox.INFLIGHT, outbox.QUARANTINE,
         outbox.QUARANTINE_RESOLVED, outbox.STRICT_RECOVERY) = self.old
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

    def test_sent_ledger_write_failure_quarantines_ambiguous_head(self):
        report = outbox.REPORTS / "ledger-failure.html"
        report.write_text("<html>ledger-failure</html>", encoding="utf-8")
        outbox.enqueue("summary", "recipient", [report])
        queued = Path(outbox._pending()[0]["attachments"][0])

        with mock.patch.object(outbox, "_send", return_value=(True, "")), \
                mock.patch.object(
                    outbox, "_append_sent", side_effect=OSError("ledger unavailable")
                ):
            sent, kept, why = outbox.drain()

        self.assertEqual((0, 0), (sent, kept))
        self.assertIn("sent ledger write failed", why)
        self.assertTrue(queued.exists())
        self.assertEqual([], outbox._pending())
        unknown = [
            json.loads(line) for line in outbox.UNKNOWN.read_text().splitlines()
        ]
        self.assertEqual(1, len(unknown))
        self.assertEqual("summary", unknown[0]["text"])
        self.assertIn("send started", unknown[0]["reason"])

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

        with mock.patch.object(outbox, "_send") as send:
            sent, kept, why = outbox.drain()

        self.assertEqual((1, 0, ""), (sent, kept, why))
        send.assert_not_called()
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

    def test_unreadable_delivery_ledger_sends_once_and_never_resends(self):
        """osascript has already sent by the time chat.db turns out unreadable. The
        old contract reported failure, requeued, and the next drain sent the same
        alert again - the operator received every alert twice (Principal, 2026-08-18).
        New contract: an unverifiable send is recorded as sent-unverified exactly once."""
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(outbox, "_delivered_count", return_value=None), \
                mock.patch.object(outbox.subprocess, "run", return_value=completed):
            ok, reason = outbox._send("alert", "recipient")
        self.assertTrue(ok)
        self.assertIn("unverified", reason)
        outbox.enqueue("alert once", "recipient")
        with mock.patch.object(outbox, "_delivered_count", return_value=None), \
                mock.patch.object(outbox.subprocess, "run", return_value=completed):
            sent, kept, why = outbox.drain()
            self.assertEqual((sent, kept), (1, 0))
            self.assertIn("unverified", why)
            sent2, kept2, _ = outbox.drain()
        self.assertEqual((sent2, kept2), (0, 0), "an unverified send must never be sent again")
        ledger = outbox.SENT.read_text().strip().splitlines()
        self.assertTrue(ledger and "unverified" in ledger[-1])

    def test_stable_dedupe_identity_survives_queue_and_sent_ledgers(self):
        identity = "evolve-art:" + "a" * 64
        self.assertTrue(
            outbox.enqueue("final art", "recipient", dedupe_key=identity))
        self.assertFalse(
            outbox.enqueue("final art", "recipient", dedupe_key=identity))
        self.assertEqual(1, len(outbox._pending()))

        with mock.patch.object(outbox, "_send", return_value=(True, "")):
            self.assertEqual((1, 0, ""), outbox.drain())
        self.assertFalse(
            outbox.enqueue("final art", "recipient", dedupe_key=identity))
        self.assertEqual([], outbox._pending())

    def test_identical_same_second_plain_entries_each_send_once(self):
        stamp = "2026-08-24T01:01:15+00:00"
        with mock.patch.object(outbox, "now", return_value=stamp):
            self.assertTrue(outbox.enqueue("same", "recipient"))
            self.assertTrue(outbox.enqueue("same", "recipient"))
            queued = outbox._pending()
            raw_lines = outbox._queue_lines_unlocked()
            self.assertEqual(2, len(queued))
            self.assertEqual(
                2, len({message["entry_id"] for message in queued}))
            self.assertEqual(
                2, len({outbox._line_digest(line) for line in raw_lines}))

            with mock.patch.object(
                    outbox, "_send", return_value=(True, "")) as send:
                self.assertEqual((2, 0, ""), outbox.drain())

        self.assertEqual(2, send.call_count)
        sent = [
            json.loads(line) for line in outbox.SENT.read_text().splitlines()
        ]
        self.assertEqual(2, len(sent))
        self.assertEqual(2, len({record["entry_id"] for record in sent}))
        self.assertEqual(2, len({record["queue_sha256"] for record in sent}))

    def test_plain_sent_evidence_survives_queue_rewrite_crash(self):
        stamp = "2026-08-24T01:01:15+00:00"
        with mock.patch.object(outbox, "now", return_value=stamp):
            outbox.enqueue("same", "recipient")
            outbox.enqueue("same", "recipient")
        with mock.patch.object(
                outbox, "_send", return_value=(True, "")) as send, \
             mock.patch.object(
                 outbox.os, "replace", side_effect=OSError("rewrite failed")):
            with self.assertRaises(OSError):
                outbox.drain()
        self.assertEqual(2, send.call_count)
        self.assertEqual(2, len(outbox._pending()))

        with mock.patch.object(outbox, "_send") as resend:
            self.assertEqual((2, 0, ""), outbox.drain())
        resend.assert_not_called()
        self.assertEqual([], outbox._pending())

    def test_dedupe_identity_prevents_resend_after_queue_rewrite_crash(self):
        identity = "evolve-art:" + "b" * 64
        outbox.enqueue("final art", "recipient", dedupe_key=identity)
        with mock.patch.object(outbox, "_send", return_value=(True, "")) as send, \
                mock.patch.object(
                    outbox.os, "replace", side_effect=OSError("rewrite failed")
                ):
            with self.assertRaises(OSError):
                outbox.drain()
        self.assertEqual(1, send.call_count)
        self.assertEqual(1, len(outbox._pending()))

        with mock.patch.object(outbox, "_send") as resend:
            self.assertEqual((1, 0, ""), outbox.drain())
        resend.assert_not_called()
        self.assertEqual([], outbox._pending())

    def test_torn_terminal_tail_quarantines_ambiguous_head_without_wedging(self):
        outbox.enqueue("ambiguous", "recipient", dedupe_key="ambiguous-key")
        outbox.SENT.write_text('{"sent_at":"half', encoding="utf-8")

        with mock.patch.object(outbox, "_send") as send:
            self.assertEqual((0, 0, "empty"), outbox.drain())
        send.assert_not_called()
        self.assertEqual([], outbox._pending())
        unknown = [
            json.loads(line) for line in outbox.UNKNOWN.read_text().splitlines()
        ]
        message = next(
            record for record in unknown if record.get("text") == "ambiguous")
        self.assertEqual("ambiguous-key", message["dedupe_key"])
        status = outbox.status()
        self.assertGreaterEqual(status["unknown"], 2)
        self.assertEqual(1, status["quarantine"])
        self.assertEqual(1, status["dedupe_ambiguity"])

        outbox.enqueue("later", "recipient")
        with mock.patch.object(outbox, "_send", return_value=(True, "")) as send:
            self.assertEqual((1, 0, ""), outbox.drain())
        send.assert_called_once()

    def test_killed_after_send_intent_never_resends_the_ambiguous_message(self):
        outbox.enqueue("kill-window", "recipient")
        raw_line = outbox._queue_lines_unlocked()[0]
        with outbox._locked(outbox.LOCK):
            outbox._write_inflight_unlocked(raw_line, "simulated SIGKILL")

        with mock.patch.object(outbox, "_send") as send:
            self.assertEqual((0, 0, "empty"), outbox.drain())
        send.assert_not_called()
        self.assertEqual([], outbox._pending())
        record = json.loads(outbox.UNKNOWN.read_text().splitlines()[0])
        self.assertEqual("kill-window", record["text"])
        self.assertIn("SIGKILL", record["reason"])

        outbox.enqueue("next", "recipient")
        with mock.patch.object(outbox, "_send", return_value=(True, "")) as send:
            self.assertEqual((1, 0, ""), outbox.drain())
        send.assert_called_once()

    def test_unknown_health_recovers_only_after_explicit_resolution(self):
        outbox.enqueue("killed sender", "recipient")
        raw_line = outbox._queue_lines_unlocked()[0]
        with outbox._locked(outbox.LOCK):
            outbox._write_inflight_unlocked(
                raw_line, "sender killed after side effect began")

        with mock.patch.object(outbox, "_send") as resend:
            self.assertEqual((0, 0, "empty"), outbox.drain())
        resend.assert_not_called()
        evidence_before = outbox.UNKNOWN.read_bytes()

        unhealthy = checks.alerts_can_actually_reach_you()
        self.assertFalse(unhealthy["ok"])
        self.assertIn("UNKNOWN delivery evidence", unhealthy["detail"])
        before = outbox.status()
        self.assertEqual(1, before["unknown"])
        self.assertEqual(1, before["unknown_total"])
        self.assertEqual(0, before["unknown_resolved"])
        unknown_id = before["unknown_unresolved_ids"][0]

        with self.assertRaises(ValueError):
            outbox.resolve_unknown_delivery(
                "not-an-id", "not-delivered", "operator checked Messages")
        with self.assertRaises(ValueError):
            outbox.resolve_unknown_delivery(
                "0" * 64, "not-delivered", "operator checked Messages")

        self.assertTrue(outbox.resolve_unknown_delivery(
            unknown_id,
            "not-delivered",
            "operator confirmed no matching delivered Messages row",
        ))
        self.assertFalse(outbox.resolve_unknown_delivery(
            unknown_id,
            "not-delivered",
            "operator confirmed no matching delivered Messages row",
        ))

        after = outbox.status()
        self.assertEqual(0, after["unknown"])
        self.assertEqual(1, after["unknown_total"])
        self.assertEqual(1, after["unknown_resolved"])
        self.assertEqual([], after["unknown_unresolved_ids"])
        healthy = checks.alerts_can_actually_reach_you()
        self.assertTrue(healthy["ok"], healthy)
        self.assertIn("explicitly resolved", healthy["detail"])
        self.assertEqual(evidence_before, outbox.UNKNOWN.read_bytes())
        resolution = json.loads(
            outbox.UNKNOWN_RESOLVED.read_text().splitlines()[0])
        self.assertEqual(unknown_id, resolution["unknown_id"])
        self.assertEqual("not-delivered", resolution["resolution"])

    def test_status_surfaces_stale_inflight_without_recovering_it_as_success(self):
        outbox.enqueue("stale intent", "recipient")
        raw_line = outbox._queue_lines_unlocked()[0]
        started = (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).isoformat(timespec="seconds")
        outbox.INFLIGHT.write_text(json.dumps({
            "raw_line": raw_line,
            "queue_sha256": outbox._line_digest(raw_line),
            "started_at": started,
            "reason": "simulated killed sender",
        }) + "\n", encoding="utf-8")

        status = outbox.status()

        self.assertTrue(status["inflight"])
        self.assertGreaterEqual(status["inflight_minutes"], 9.9)
        self.assertTrue(outbox.INFLIGHT.exists())
        self.assertEqual(["stale intent"],
                         [json.loads(line)["text"]
                          for line in outbox._queue_lines_unlocked()])

    def test_interior_terminal_corruption_is_quarantined_and_rewritten(self):
        first = {"dedupe_key": "first", "sent_at": outbox.now()}
        last = {"dedupe_key": "last", "sent_at": outbox.now()}
        outbox.SENT.write_text(
            json.dumps(first) + "\n"
            + "{broken\n"
            + json.dumps(last) + "\n",
            encoding="utf-8",
        )
        self.assertTrue(outbox.enqueue("plain alert", "recipient"))
        with mock.patch.object(
                outbox, "_send", return_value=(True, "")) as send:
            self.assertEqual((1, 0, ""), outbox.drain())
        send.assert_called_once()

        sent = [
            json.loads(line) for line in outbox.SENT.read_text().splitlines()
        ]
        self.assertEqual(
            ["first", "last"],
            [record["dedupe_key"] for record in sent if "dedupe_key" in record])
        quarantine = [
            json.loads(line)
            for line in outbox.QUARANTINE.read_text().splitlines()
        ]
        self.assertEqual(1, len(quarantine))
        self.assertEqual(
            b"{broken\n",
            base64.b64decode(quarantine[0]["raw_base64"]))
        status = outbox.status()
        self.assertEqual(1, status["quarantine"])
        self.assertEqual(1, status["dedupe_ambiguity"])
        self.assertGreaterEqual(status["unknown"], 1)

    def test_blank_and_non_object_terminal_records_are_quarantined(self):
        cases = {
            "blank": "\n",
            "non-object": "[1, 2, 3]\n",
        }
        for label, corrupt in cases.items():
            for path in (
                    outbox.SENT, outbox.UNKNOWN, outbox.QUARANTINE,
                    outbox.QUARANTINE_RESOLVED, outbox.UNKNOWN_RESOLVED):
                path.unlink(missing_ok=True)
            outbox.SENT.write_text(
                json.dumps({"record": "before"}) + "\n"
                + corrupt
                + json.dumps({"record": "after"}) + "\n",
                encoding="utf-8")
            with self.subTest(case=label):
                status = outbox.status()
                records = [
                    json.loads(line)
                    for line in outbox.SENT.read_text().splitlines()
                ]
                self.assertEqual(
                    ["before", "after"],
                    [record["record"] for record in records])
                self.assertEqual(1, status["quarantine"])
                self.assertEqual(1, status["dedupe_ambiguity"])

    def test_corruption_never_resends_dedupe_message_and_resolution_is_explicit(self):
        identity = "evolve-art:" + "c" * 64
        self.assertTrue(
            outbox.enqueue("possibly sent art", "recipient",
                           dedupe_key=identity))
        outbox.SENT.write_text(
            json.dumps({"sent_at": outbox.now()}) + "\n"
            + "{old interior tear\n"
            + json.dumps({"sent_at": outbox.now(), "other": True}) + "\n",
            encoding="utf-8")

        with mock.patch.object(outbox, "_send") as send:
            self.assertEqual((0, 0, "empty"), outbox.drain())
        send.assert_not_called()
        self.assertEqual([], outbox._pending())
        unknown = [
            json.loads(line) for line in outbox.UNKNOWN.read_text().splitlines()
        ]
        self.assertTrue(any(
            record.get("dedupe_key") == identity for record in unknown))
        with self.assertRaises(outbox.DedupeAmbiguityError):
            outbox.enqueue("new keyed alert", "recipient",
                           dedupe_key="different-key")

        status = outbox.status()
        incident = status["dedupe_ambiguity_ids"][0]
        self.assertTrue(outbox.resolve_terminal_quarantine(
            incident, "operator reviewed preserved raw evidence"))
        self.assertFalse(outbox.resolve_terminal_quarantine(
            incident, "operator reviewed preserved raw evidence"))
        self.assertFalse(
            outbox.enqueue("possibly sent art", "recipient",
                           dedupe_key=identity),
            "UNKNOWN evidence must suppress the original key after resolution")
        self.assertTrue(
            outbox.enqueue("new keyed alert", "recipient",
                           dedupe_key="different-key"))
        with mock.patch.object(
                outbox, "_send", return_value=(True, "")) as send:
            self.assertEqual((1, 0, ""), outbox.drain())
        send.assert_called_once_with("new keyed alert", "recipient", [])

    def _reset_strict_recovery_case(self):
        for path in (
                outbox.QUEUE, outbox.SENT, outbox.LAST_DRAIN,
                outbox.UNVERIFIED, outbox.DEAD_LETTER, outbox.EXPIRED,
                outbox.UNKNOWN, outbox.UNKNOWN_RESOLVED, outbox.INFLIGHT,
                outbox.QUARANTINE, outbox.QUARANTINE_RESOLVED,
                outbox.STRICT_RECOVERY):
            path.unlink(missing_ok=True)

    @staticmethod
    def _quarantine_record(identifier):
        return {
            "schema": outbox.QUARANTINE_SCHEMA,
            "id": identifier,
            "evidence_sha256": hashlib.sha256(
                identifier.encode("ascii")).hexdigest(),
            "raw_base64": base64.b64encode(
                identifier.encode("ascii")).decode("ascii"),
        }

    @staticmethod
    def _quarantine_resolution(identifier):
        return {
            "schema": outbox.QUARANTINE_RESOLUTION_SCHEMA,
            "incident_id": identifier,
            "resolved_at": outbox.now(),
            "reason": "valid control resolution",
        }

    @staticmethod
    def _unknown_resolution(identifier):
        return {
            "schema": outbox.UNKNOWN_RESOLUTION_SCHEMA,
            "unknown_id": identifier,
            "resolution": "not-delivered",
            "resolved_at": outbox.now(),
            "reason": "valid control resolution",
        }

    def _write_strict_corruption(
            self, path, before, corrupt, after, position):
        lines = [
            json.dumps(record).encode("utf-8") + b"\n"
            for record in before
        ]
        raw_corrupt = corrupt + (b"" if position == "tail" else b"\n")
        lines.append(raw_corrupt)
        if position == "interior":
            lines.extend(
                json.dumps(record).encode("utf-8") + b"\n"
                for record in after
            )
        path.write_bytes(b"".join(lines))
        return raw_corrupt

    def _assert_strict_recovery_blocks_dedupe_but_plain_sends(
            self, ledger, raw_corrupt, expected_valid):
        self.assertTrue(outbox.enqueue("plain alert", "recipient"))
        with mock.patch.object(
                outbox, "_send", return_value=(True, "")) as send:
            self.assertEqual((1, 0, ""), outbox.drain())
        send.assert_called_once_with("plain alert", "recipient", [])

        status = outbox.status()
        self.assertEqual(1, status["strict_recovery"])
        self.assertEqual(
            str(outbox.STRICT_RECOVERY),
            status["strict_recovery_evidence_file"])
        evidence = json.loads(outbox.STRICT_RECOVERY.read_text())
        self.assertEqual(outbox.STRICT_RECOVERY_SCHEMA, evidence["schema"])
        self.assertEqual(1, len(evidence["incidents"]))
        incident = evidence["incidents"][0]
        self.assertEqual(ledger.name, incident["ledger"])
        self.assertEqual(raw_corrupt, base64.b64decode(
            incident["raw_base64"]))
        self.assertEqual(
            hashlib.sha256(raw_corrupt).hexdigest(),
            incident["raw_sha256"])
        self.assertEqual(
            expected_valid,
            [
                json.loads(line)
                for line in ledger.read_text(encoding="utf-8").splitlines()
            ],
        )
        with self.assertRaises(outbox.DedupeAmbiguityError):
            outbox.enqueue(
                "dedupe must wait", "recipient", dedupe_key="strict-key")
        health = checks.alerts_can_actually_reach_you()
        self.assertFalse(health["ok"], health)
        self.assertIn("strict-ledger recovery", health["detail"])
        self.assertIn("ack-recovery", health["detail"])
        return incident["id"]

    def test_quarantine_recovers_torn_tail_and_interior_raw_records(self):
        corrupt = b'{"schema":"rapp-outbox-terminal-quarantine/1.0","id":"torn'
        for position in ("tail", "interior"):
            with self.subTest(position=position):
                self._reset_strict_recovery_case()
                before = [self._quarantine_record("a" * 64)]
                after = [self._quarantine_record("b" * 64)]
                raw_corrupt = self._write_strict_corruption(
                    outbox.QUARANTINE, before, corrupt, after, position)
                expected = before + (after if position == "interior" else [])
                recovery_id = (
                    self._assert_strict_recovery_blocks_dedupe_but_plain_sends(
                        outbox.QUARANTINE, raw_corrupt, expected))

                with self.assertRaises(outbox.DedupeAmbiguityError):
                    outbox.resolve_terminal_quarantine(
                        expected[0]["id"], "must acknowledge recovery first")
                self.assertTrue(outbox.acknowledge_strict_recovery(
                    recovery_id, "operator inspected preserved quarantine bytes"))
                self.assertFalse(outbox.acknowledge_strict_recovery(
                    recovery_id, "operator inspected preserved quarantine bytes"))
                self.assertEqual(0, outbox.status()["strict_recovery"])
                for record in expected:
                    self.assertTrue(outbox.resolve_terminal_quarantine(
                        record["id"], "operator reviewed original incident"))
                self.assertTrue(outbox.enqueue(
                    "safe new key", "recipient", dedupe_key="safe-key"))

    def test_quarantine_resolutions_recover_without_false_resolution(self):
        target = "c" * 64
        corrupt = (
            b'{"schema":"rapp-outbox-terminal-quarantine-resolution/1.0",'
            b'"incident_id":"' + target[:12].encode("ascii"))
        for position in ("tail", "interior"):
            with self.subTest(position=position):
                self._reset_strict_recovery_case()
                outbox.QUARANTINE.write_text(
                    json.dumps(self._quarantine_record(target)) + "\n",
                    encoding="utf-8")
                before = [self._quarantine_resolution("d" * 64)]
                after = [self._quarantine_resolution("e" * 64)]
                raw_corrupt = self._write_strict_corruption(
                    outbox.QUARANTINE_RESOLVED,
                    before,
                    corrupt,
                    after,
                    position,
                )
                expected = before + (after if position == "interior" else [])
                recovery_id = (
                    self._assert_strict_recovery_blocks_dedupe_but_plain_sends(
                        outbox.QUARANTINE_RESOLVED, raw_corrupt, expected))

                status = outbox.status()
                self.assertIn(target, status["dedupe_ambiguity_ids"])
                with self.assertRaises(outbox.DedupeAmbiguityError):
                    outbox.resolve_terminal_quarantine(
                        target, "corrupt resolution cannot be trusted")
                self.assertTrue(outbox.acknowledge_strict_recovery(
                    recovery_id, "operator inspected torn resolution bytes"))
                self.assertIn(
                    target, outbox.status()["dedupe_ambiguity_ids"],
                    "a corrupt resolution must not resolve quarantine")
                self.assertTrue(outbox.resolve_terminal_quarantine(
                    target, "operator explicitly resolves original incident"))
                self.assertTrue(outbox.enqueue(
                    "safe new key", "recipient", dedupe_key="safe-key"))

    def test_unknown_resolutions_recover_without_false_resolution(self):
        target = "f" * 64
        corrupt = (
            b'{"schema":"rapp-outbox-unknown-resolution/1.0",'
            b'"unknown_id":"' + target[:12].encode("ascii"))
        for position in ("tail", "interior"):
            with self.subTest(position=position):
                self._reset_strict_recovery_case()
                outbox.UNKNOWN.write_text(
                    json.dumps({
                        "unknown_id": target,
                        "dedupe_key": "possibly-delivered",
                        "unknown_at": outbox.now(),
                        "reason": "control UNKNOWN evidence",
                    }) + "\n",
                    encoding="utf-8",
                )
                before = [self._unknown_resolution("1" * 64)]
                after = [self._unknown_resolution("2" * 64)]
                raw_corrupt = self._write_strict_corruption(
                    outbox.UNKNOWN_RESOLVED,
                    before,
                    corrupt,
                    after,
                    position,
                )
                expected = before + (after if position == "interior" else [])
                recovery_id = (
                    self._assert_strict_recovery_blocks_dedupe_but_plain_sends(
                        outbox.UNKNOWN_RESOLVED, raw_corrupt, expected))

                status = outbox.status()
                self.assertEqual([target], status["unknown_unresolved_ids"])
                with self.assertRaises(outbox.DedupeAmbiguityError):
                    outbox.resolve_unknown_delivery(
                        target, "not-delivered",
                        "corrupt resolution cannot be trusted")
                self.assertTrue(outbox.acknowledge_strict_recovery(
                    recovery_id, "operator inspected torn resolution bytes"))
                self.assertEqual(
                    [target],
                    outbox.status()["unknown_unresolved_ids"],
                    "a corrupt resolution must not resolve UNKNOWN evidence",
                )
                self.assertTrue(outbox.resolve_unknown_delivery(
                    target,
                    "not-delivered",
                    "operator confirmed no matching delivered message",
                ))
                self.assertEqual(0, outbox.status()["unknown"])
                self.assertTrue(outbox.enqueue(
                    "safe new key", "recipient", dedupe_key="safe-key"))

    def test_unreadable_terminal_file_is_not_caught_or_rewritten(self):
        outbox.SENT.write_text(
            json.dumps({"sent_at": outbox.now()}) + "\n",
            encoding="utf-8")
        real_read_bytes = Path.read_bytes

        def denied(path):
            if Path(path) == outbox.SENT:
                raise PermissionError("ledger denied")
            return real_read_bytes(path)

        with mock.patch.object(Path, "read_bytes", denied), \
                self.assertRaises(outbox.TerminalLedgerError) as raised:
            outbox.enqueue("plain but blind", "recipient")
        self.assertIn("unreadable", str(raised.exception))
        self.assertFalse(outbox.QUEUE.exists())
        self.assertFalse(outbox.QUARANTINE.exists())

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
            f"outbox.UNKNOWN = Path({str(outbox.UNKNOWN)!r})",
            "outbox.UNKNOWN_RESOLVED = "
            f"Path({str(outbox.UNKNOWN_RESOLVED)!r})",
            f"outbox.INFLIGHT = Path({str(outbox.INFLIGHT)!r})",
            f"outbox.QUARANTINE = Path({str(outbox.QUARANTINE)!r})",
            "outbox.QUARANTINE_RESOLVED = "
            f"Path({str(outbox.QUARANTINE_RESOLVED)!r})",
            "outbox.STRICT_RECOVERY = "
            f"Path({str(outbox.STRICT_RECOVERY)!r})",
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
            outbox.DEAD_LETTER, outbox.EXPIRED, outbox.UNKNOWN,
            outbox.UNKNOWN_RESOLVED,
            outbox.INFLIGHT, outbox.QUARANTINE,
            outbox.QUARANTINE_RESOLVED, outbox.STRICT_RECOVERY,
            watcher_outbox.CLAIMS,
            watcher_outbox.ATTEMPTS)
        outbox.QUEUE = root / "outbox.jsonl"
        outbox.SENT = root / "sent.jsonl"
        outbox.LAST_DRAIN = root / "last.json"
        outbox.REPORTS = root / "reports"
        outbox.LOCK = root / "outbox.lock"
        outbox.DRAIN_LOCK = root / "outbox-drain.lock"
        outbox.UNVERIFIED = root / "outbox-unverified.jsonl"
        outbox.DEAD_LETTER = root / "outbox-dead-letter.jsonl"
        outbox.EXPIRED = root / "outbox-expired.jsonl"
        outbox.UNKNOWN = root / "outbox-unknown.jsonl"
        outbox.UNKNOWN_RESOLVED = root / "outbox-unknown-resolved.jsonl"
        outbox.INFLIGHT = root / "outbox-inflight.json"
        outbox.QUARANTINE = root / "outbox-terminal-quarantine.jsonl"
        outbox.QUARANTINE_RESOLVED = (
            root / "outbox-terminal-quarantine-resolved.jsonl")
        outbox.STRICT_RECOVERY = root / "outbox-strict-ledger-recovery.json"
        watcher_outbox.CLAIMS = root / "watcher-claims"
        watcher_outbox.ATTEMPTS = root / "outbox-attempts.json"
        outbox.REPORTS.mkdir()

    def tearDown(self):
        (outbox.QUEUE, outbox.SENT, outbox.LAST_DRAIN, outbox.REPORTS,
         outbox.LOCK, outbox.DRAIN_LOCK, outbox.UNVERIFIED,
         outbox.DEAD_LETTER, outbox.EXPIRED, outbox.UNKNOWN,
         outbox.UNKNOWN_RESOLVED,
         outbox.INFLIGHT, outbox.QUARANTINE,
         outbox.QUARANTINE_RESOLVED, outbox.STRICT_RECOVERY,
         watcher_outbox.CLAIMS,
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

    def test_identical_entries_have_distinct_claims_and_attempts(self):
        stamp = "2026-08-24T01:01:15+00:00"
        with mock.patch.object(outbox, "now", return_value=stamp):
            outbox.enqueue("same", "recipient")
            outbox.enqueue("same", "recipient")
        first, second = outbox._queue_lines_unlocked()
        first_message, second_message = map(json.loads, (first, second))
        first_digest = watcher_outbox._digest(first)
        second_digest = watcher_outbox._digest(second)
        self.assertNotEqual(first_digest, second_digest)
        watcher_outbox.ATTEMPTS.write_text(json.dumps({
            first_digest: {"count": 2, "reason": "prior failures"},
        }), encoding="utf-8")

        first_claim = self._claim()
        self.assertEqual(first_digest, first_claim.name)
        watcher_outbox.fail(first_claim, "third failure")

        second_claim = self._claim()
        self.assertEqual(second_digest, second_claim.name)
        self.assertNotEqual(first_claim, second_claim)
        watcher_outbox.fail(second_claim, "first failure")

        attempts = json.loads(watcher_outbox.ATTEMPTS.read_text())
        self.assertEqual([second_digest], list(attempts))
        self.assertEqual(1, attempts[second_digest]["count"])
        self.assertEqual(
            second_message["entry_id"],
            attempts[second_digest]["entry_id"])
        dead = json.loads(outbox.DEAD_LETTER.read_text().splitlines()[0])
        self.assertEqual(first_message["entry_id"], dead["entry_id"])

    def test_corrupt_attempt_ledger_fails_closed(self):
        outbox.enqueue("do not send", "recipient")
        watcher_outbox.ATTEMPTS.write_text("{broken", encoding="utf-8")

        with self.assertRaises(RuntimeError):
            watcher_outbox.claim()

        self.assertEqual(["do not send"],
                         [row["text"] for row in outbox._pending()])

    def test_plain_watcher_delivery_survives_terminal_quarantine(self):
        outbox.SENT.write_text(
            json.dumps({"record": "before"}) + "\n"
            + "{old interior tear\n"
            + json.dumps({"record": "after"}) + "\n",
            encoding="utf-8")
        self.assertTrue(outbox.enqueue("plain watcher alert", "recipient"))

        claim = self._claim()
        watcher_outbox.acknowledge(claim, "watcher delivered plain alert")

        self.assertEqual([], outbox._pending())
        sent = [
            json.loads(line) for line in outbox.SENT.read_text().splitlines()
        ]
        self.assertTrue(any(
            record.get("text") == "plain watcher alert" for record in sent))
        self.assertEqual(1, outbox.status()["dedupe_ambiguity"])


class VerifyOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.old = (
            outbox.QUEUE, outbox.SENT, outbox.LAST_DRAIN, outbox.REPORTS,
            outbox.LOCK, outbox.DRAIN_LOCK, outbox.UNVERIFIED,
            outbox.DEAD_LETTER, outbox.EXPIRED, outbox.UNKNOWN,
            outbox.UNKNOWN_RESOLVED,
            outbox.INFLIGHT, outbox.QUARANTINE,
            outbox.QUARANTINE_RESOLVED, outbox.STRICT_RECOVERY,
            verify_outbox.CHAT_DB)
        outbox.QUEUE = root / "outbox.jsonl"
        outbox.SENT = root / "sent.jsonl"
        outbox.LAST_DRAIN = root / "last.json"
        outbox.REPORTS = root / "reports"
        outbox.LOCK = root / "outbox.lock"
        outbox.DRAIN_LOCK = root / "outbox-drain.lock"
        outbox.UNVERIFIED = root / "outbox-unverified.jsonl"
        outbox.DEAD_LETTER = root / "outbox-dead-letter.jsonl"
        outbox.EXPIRED = root / "outbox-expired.jsonl"
        outbox.UNKNOWN = root / "outbox-unknown.jsonl"
        outbox.UNKNOWN_RESOLVED = root / "outbox-unknown-resolved.jsonl"
        outbox.INFLIGHT = root / "outbox-inflight.json"
        outbox.QUARANTINE = root / "outbox-terminal-quarantine.jsonl"
        outbox.QUARANTINE_RESOLVED = (
            root / "outbox-terminal-quarantine-resolved.jsonl")
        outbox.STRICT_RECOVERY = root / "outbox-strict-ledger-recovery.json"
        outbox.REPORTS.mkdir()
        verify_outbox.CHAT_DB = root / "chat.db"
        self.connection = sqlite3.connect(verify_outbox.CHAT_DB)
        self.connection.executescript("""
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE message (
              ROWID INTEGER PRIMARY KEY,
              handle_id INTEGER,
              is_from_me INTEGER,
              is_sent INTEGER,
              is_delivered INTEGER,
              error INTEGER,
              date INTEGER,
              text TEXT,
              attributedBody BLOB
            );
            INSERT INTO handle (ROWID, id) VALUES (1, 'recipient');
        """)
        self.attempted = datetime(
            2026, 8, 17, 22, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.connection.close()
        (outbox.QUEUE, outbox.SENT, outbox.LAST_DRAIN, outbox.REPORTS,
         outbox.LOCK, outbox.DRAIN_LOCK, outbox.UNVERIFIED,
         outbox.DEAD_LETTER, outbox.EXPIRED, outbox.UNKNOWN,
         outbox.UNKNOWN_RESOLVED,
         outbox.INFLIGHT, outbox.QUARANTINE,
         outbox.QUARANTINE_RESOLVED, outbox.STRICT_RECOVERY,
         verify_outbox.CHAT_DB) = self.old
        self.temp.cleanup()

    @staticmethod
    def _archive(text):
        encoded = text.encode("utf-8")
        if len(encoded) < 0x81:
            prefix = bytes([len(encoded)])
        else:
            prefix = b"\x82" + len(encoded).to_bytes(2, "little")
        return b"archive NSString fields +" + prefix + encoded + b" tail"

    def _queue_uncertain(self, text="art link"):
        outbox.UNVERIFIED.write_text(json.dumps({
            "at": self.attempted.isoformat(),
            "attempted_at": self.attempted.isoformat(),
            "to": "recipient",
            "text": text,
            "attachments": [],
            "reason": "chat.db unreadable",
        }) + "\n", encoding="utf-8")

    def _insert_delivery(self, text="art link", offset_seconds=1):
        apple = int((
            self.attempted.timestamp()
            - verify_outbox.APPLE_EPOCH_OFFSET
            + offset_seconds
        ) * 1_000_000_000)
        self.connection.execute(
            "INSERT INTO message VALUES (1,1,1,1,1,0,?,NULL,?)",
            (apple, self._archive(text)),
        )
        self.connection.commit()

    def test_delivered_attributed_body_verifies_uncertain_send(self):
        self._queue_uncertain()
        self._insert_delivery()

        verified, remaining = verify_outbox.verify()

        self.assertEqual((1, 0), (verified, remaining))
        self.assertEqual("", outbox.UNVERIFIED.read_text())
        sent = json.loads(outbox.SENT.read_text().splitlines()[0])
        self.assertEqual("Messages/chat.db",
                         sent["delivery_evidence"]["source"])
        self.assertEqual(1.0, sent["delivery_evidence"]["delta_seconds"])

    def test_content_mismatch_remains_unverified(self):
        self._queue_uncertain()
        self._insert_delivery("different")

        verified, remaining = verify_outbox.verify()

        self.assertEqual((0, 1), (verified, remaining))
        self.assertFalse(outbox.SENT.exists())

    def test_duplicate_matching_rows_fail_closed(self):
        self._queue_uncertain()
        self._insert_delivery(offset_seconds=1)
        apple = int((
            self.attempted.timestamp()
            - verify_outbox.APPLE_EPOCH_OFFSET
            + 2
        ) * 1_000_000_000)
        self.connection.execute(
            "INSERT INTO message VALUES (2,1,1,1,1,0,?,NULL,?)",
            (apple, self._archive("art link")),
        )
        self.connection.commit()

        verified, remaining = verify_outbox.verify()

        self.assertEqual((0, 1), (verified, remaining))

    def test_unreadable_database_never_mutates_ledger(self):
        self._queue_uncertain()
        self.connection.close()
        verify_outbox.CHAT_DB = Path(self.temp.name) / "missing.db"
        with self.assertRaises(RuntimeError):
            verify_outbox.verify()
        self.assertTrue(outbox.UNVERIFIED.read_text().strip())
        self.connection = sqlite3.connect(":memory:")


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

    def test_unknown_delivery_evidence_is_visible_with_empty_transport(self):
        with mock.patch.object(outbox, "status", return_value={
            "pending": 0,
            "oldest_minutes": None,
            "missing_attachments": 0,
            "unverified": 0,
            "dead_letter": 0,
            "unknown": 2,
            "dedupe_ambiguity": 1,
            "inflight": False,
            "last_drain": {},
        }):
            result = checks.alerts_can_actually_reach_you()
        self.assertFalse(result["ok"])
        self.assertEqual("warn", result["severity"])
        self.assertIn("UNKNOWN delivery evidence", result["detail"])
        self.assertIn("blocking dedupe-keyed sends", result["detail"])

    def test_stale_inflight_intent_is_visible(self):
        with mock.patch.object(outbox, "status", return_value={
            "pending": 1,
            "oldest_minutes": 9,
            "missing_attachments": 0,
            "unverified": 0,
            "dead_letter": 0,
            "unknown": 0,
            "dedupe_ambiguity": 0,
            "inflight": True,
            "inflight_minutes": 10.0,
            "last_drain": {},
        }):
            result = checks.alerts_can_actually_reach_you()
        self.assertFalse(result["ok"])
        self.assertEqual("warn", result["severity"])
        self.assertIn("send intent is stale", result["detail"])
        self.assertIn("UNKNOWN", result["detail"])

    def test_expired_history_is_visible_without_becoming_active_failure(self):
        with mock.patch.object(outbox, "status", return_value={
            "pending": 0,
            "oldest_minutes": None,
            "missing_attachments": 0,
            "unverified": 0,
            "dead_letter": 0,
            "expired": 16,
            "last_drain": {"why": "expired by operator"},
        }):
            result = checks.alerts_can_actually_reach_you()
        self.assertTrue(result["ok"])
        self.assertIn("16 historical undelivered", result["detail"])


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

    def test_art_only_mode_suppresses_nightwatch(self):
        with mock.patch.object(nightwatch, "cfg",
                               return_value={"notification_mode": "art-only"}), \
             mock.patch.object(nightwatch, "send") as send:
            self.assertEqual(0, nightwatch.main())
        send.assert_not_called()

    def test_art_only_mode_filters_operational_messages(self):
        cfg = {"notify": True, "notification_mode": "art-only",
               "notify_handle": "test"}
        self.assertFalse(sentinel.notification_allowed(cfg, "operational"))
        self.assertTrue(sentinel.notification_allowed(cfg, "art"))

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
