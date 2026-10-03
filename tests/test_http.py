from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib import error, request

from tiinyengineer.reporter import Reporter
from tiinyengineer.server import App
from tiinyengineer.store import Store


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "data.db")
        self.store.seed([{"address": "10.0.0.20", "name": "Workshop Tiiny"}])
        self.store.save_check({"id": "release-poll", "kind": "beat", "target": {"job": "release-poll"},
                               "display_name": "Release poll", "period_s": 3600, "grace_s": 1800,
                               "misses_before_real": 1, "severity": "warning", "alert": True})
        reporter = Reporter("https://example.invalid", "", False, logging.getLogger("test"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), App(self.store, reporter).handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.temp.cleanup()

    def test_status_page_lists_last_ran(self):
        with request.urlopen(self.base + "/", timeout=2) as response:
            body = response.read().decode()
        self.assertIn("Last ran", body)
        self.assertIn("Registry", body)

    def test_beat_requires_token_and_records_run(self):
        with self.assertRaises(error.HTTPError) as caught:
            request.urlopen(self.base + "/beat/release-poll?token=wrong", timeout=2)
        self.assertEqual(401, caught.exception.code)
        token = self.store.token_for("release-poll")
        with request.urlopen(self.base + f"/beat/release-poll?token={token}&ok=1&ms=42", timeout=2) as response:
            self.assertEqual(b"ok\n", response.read())
        row = next(row for row in self.store.beat_rows() if row["id"] == "release-poll")
        self.assertEqual(42, row["duration_ms"])
        self.assertIsNotNone(row["last_beat_at"])

    def test_status_api(self):
        with request.urlopen(self.base + "/api/status", timeout=2) as response:
            self.assertEqual("application/json", response.headers.get_content_type())

    def test_incident_first_step_has_one_sentence_stop(self):
        app = App(self.store, Reporter("https://example.invalid", "", False, logging.getLogger("test")))
        check = self.store.check("my-tiiny-1")
        check["first_step"] = "Check power."
        lines = app._five_lines({"opened_at": "2026-10-03T19:35:03+00:00"}, check)
        self.assertIn("Check power. Owner:", lines[3])
        self.assertNotIn("Check power..", lines[3])

    def test_dashboard_assets_and_feeds_are_served(self):
        for path, content_type in (("/history", "text/html"), ("/assets/style.css", "text/css"),
                                   ("/assets/app.js", "text/javascript"), ("/api/checks", "application/json"),
                                   ("/api/incidents", "application/json"), ("/api/history", "application/json")):
            with request.urlopen(self.base + path, timeout=2) as response:
                self.assertEqual(content_type, response.headers.get_content_type())
                self.assertTrue(response.read())

    def test_registry_adds_a_check_without_a_terminal_step(self):
        payload = json.dumps({
            "id": "example-health", "kind": "http", "target": "https://example.test/health",
            "display_name": "Example health", "where_text": "On the internet", "severity": "warning",
            "owner": "site owner", "first_step": "Check the service status page", "meaning": "The site stopped answering",
            "period_s": 60, "grace_s": 90, "misses_before_real": 3, "cooldown_s": 600,
        }).encode()
        req = request.Request(self.base + "/api/checks", data=payload, method="POST", headers={"Content-Type": "application/json"})
        with request.urlopen(req, timeout=2) as response:
            self.assertEqual(201, response.status)
            saved = json.load(response)["check"]
        self.assertEqual("https://example.test/health", saved["target"]["url"])
        self.assertEqual("cloud", saved["place"])

    def test_secret_source_setting_is_write_only_and_mode_600(self):
        import tiinyengineer.server as server_module
        original = server_module.setting_definitions
        server_module.setting_definitions = lambda: [
            {"key": "sample_token", "label": "Sample token", "secret": True, "help": ""}
        ]
        try:
            payload = json.dumps({"sample_token": "never-return-this"}).encode()
            req = request.Request(self.base + "/api/settings", data=payload, method="POST", headers={"Content-Type": "application/json"})
            with request.urlopen(req, timeout=2) as response:
                result = json.load(response)
            self.assertTrue(result["settings"][0]["configured"])
            self.assertEqual("", result["settings"][0]["value"])
            self.assertEqual("never-return-this", self.store.setting("sample_token"))
            path = Path(self.temp.name) / "source-settings.json"
            self.assertEqual(0o600, os.stat(path).st_mode & 0o777)
            self.assertEqual(0o600, os.stat(self.store.path).st_mode & 0o777)
        finally:
            server_module.setting_definitions = original


if __name__ == "__main__":
    unittest.main()
