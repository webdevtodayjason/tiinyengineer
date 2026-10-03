from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib import error, request

from tiinyengineer.reporter import Reporter
from tiinyengineer.server import App, setting_definitions
from tiinyengineer.sources import containers
from tiinyengineer.store import Store
from tools import container_reporter


NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


class Response:
    def __init__(self, payload, status=200):
        self.body = json.dumps(payload).encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


def docker_row(state="running", status="Up 4 minutes", logs=None):
    row = {
        "Id": "a" * 64,
        "Names": ["/orders-api"],
        "Image": "orders:current",
        "State": state,
        "Status": status,
    }
    if logs is not None:
        row["logs"] = logs
    return row


class ContainerSourceTests(unittest.TestCase):
    def test_container_settings_are_page_ready_without_duplicate_keys(self):
        definitions = setting_definitions()
        keys = [item["key"] for item in definitions]
        self.assertIn("container_reporter_hosts", keys)
        self.assertIn("container_reporter_tokens", keys)
        self.assertIn("container_local_enabled", keys)
        self.assertEqual(len(keys), len(set(keys)))

    def test_local_socket_container_state_and_last_fifty_logs(self):
        lines = [f"line {index}" for index in range(60)]
        with mock.patch.object(containers, "docker_containers", return_value=[docker_row("exited", "Exited (1)")]), \
             mock.patch.object(containers, "container_logs", return_value="\n".join(lines)):
            ok, status, problem = containers.check_container({"container": "orders-api"})
        self.assertFalse(ok)
        self.assertIn("Exited (1)", status)
        self.assertIn("line 10", problem)
        self.assertNotIn("line 9\n", problem)
        self.assertEqual(50, len(problem.splitlines()) - 1)

    def test_reported_stop_opens_after_two_cycles_and_restart_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = {
                "host": "edge-a",
                "reported_at": NOW.isoformat(),
                "containers": [docker_row("exited", "Exited (1)", ["database connection refused"])],
            }
            containers.save_report(root, report)
            target = {
                "mode": "report", "host": "edge-a", "container": "orders-api",
                "reports_dir": str(root),
            }
            store = Store(root / "state.db")
            try:
                store.save_check({
                    "id": "orders-container", "kind": "docker", "target": target,
                    "display_name": "Orders container", "period_s": 60, "grace_s": 0,
                    "misses_before_real": 2, "severity": "critical", "alert": True,
                    "failure_class": "container_stopped", "owner": "operations",
                })
                target["_now"] = NOW
                ok, status, problem = containers.check_container(target)
                first = store.record_result("orders-container", "poll", ok, {"status": status, "error": problem}, NOW.isoformat())
                self.assertEqual("DEGRADED", first["status"])
                target["_now"] = NOW + timedelta(seconds=60)
                containers.save_report(root, {**report, "reported_at": target["_now"].isoformat()})
                ok, status, problem = containers.check_container(target)
                second = store.record_result("orders-container", "poll", ok, {"status": status, "error": problem}, target["_now"].isoformat())
                self.assertEqual("DOWN", second["transition"])
                incident = store.incident(second["incident_id"])
                self.assertIn("container_stopped", incident["class"])
                event = store._keeper.execute("SELECT value FROM events ORDER BY id DESC LIMIT 1").fetchone()[0]
                self.assertIn("database connection refused", event)
                ledger = store._keeper.execute("SELECT body FROM ledger WHERE incident_id=? AND kind='observed'",
                                               (second["incident_id"],)).fetchone()[0]
                self.assertIn("database connection refused", ledger)

                restarted_at = NOW + timedelta(seconds=120)
                containers.save_report(root, {**report, "reported_at": restarted_at.isoformat(),
                                              "containers": [docker_row()]})
                target["_now"] = restarted_at
                ok, status, problem = containers.check_container(target)
                recovered = store.record_result("orders-container", "poll", ok, {"status": status, "error": problem}, restarted_at.isoformat())
                self.assertEqual("UP", recovered["transition"])
                self.assertEqual("closed", store.incident(second["incident_id"])["state"])
            finally:
                store.close()

    def test_coolify_mid_deploy_skips_container_failure(self):
        target = {
            "container": "orders-api", "coolify_resource_uuid": "app-1",
            "coolify_resource_type": "application", "coolify_base_url": "https://coolify.example",
            "coolify_api_token": "read-only",
        }
        with mock.patch.object(containers.request, "urlopen", return_value=Response({"deployment_status": "in_progress"})), \
             mock.patch.object(containers, "docker_containers") as docker:
            ok, status, problem = containers.check_container(target)
        self.assertTrue(ok)
        self.assertIn("deployment in progress", status)
        self.assertIsNone(problem)
        docker.assert_not_called()

    def test_reporter_collects_stopped_container_logs(self):
        with mock.patch.object(container_reporter, "docker_containers", return_value=[docker_row("exited", "Exited (2)")]), \
             mock.patch.object(container_reporter, "container_logs", return_value="first\nsecond"):
            report = container_reporter.build_report("edge-b", "/fixture/docker.sock", 2)
        self.assertEqual("edge-b", report["host"])
        self.assertEqual(["first", "second"], report["containers"][0]["logs"])

    def test_local_discovery_builds_two_cycle_checks_for_running_containers(self):
        with mock.patch.object(containers, "docker_containers", return_value=[docker_row(), docker_row("exited", "Exited")]):
            checks = containers.discover_local_checks({"container_local_name": "watcher-box"})
        self.assertEqual(1, len(checks))
        self.assertEqual("local", checks[0]["target"]["mode"])
        self.assertEqual(2, checks[0]["misses_before_real"])

    def test_coolify_discovery_lists_applications_and_services(self):
        replies = iter([
            Response([{"uuid": "app-1", "name": "Orders", "container_name": "orders-api"}]),
            Response({"data": [{"uuid": "service-1", "name": "Search", "container_name": "search"}]}),
        ])
        config = {
            "host": "host-a", "coolify_base_url": "https://coolify.example",
            "coolify_api_token": "read-only-token", "reports_dir": "/fixture/reports",
        }
        with mock.patch.object(containers.request, "urlopen", side_effect=lambda req, timeout=0: next(replies)):
            checks = containers.discover_checks(config)
        self.assertEqual({"Orders container", "Search container"}, {row["display_name"] for row in checks})
        self.assertTrue(all(row["misses_before_real"] == 2 for row in checks))
        self.assertEqual({"application", "service"}, {row["target"]["coolify_resource_type"] for row in checks})


class ContainerReportRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "data.db")
        self.store.set_setting("container_reporter_hosts", "host-a,edge-a")
        self.store.set_setting("container_reporter_tokens", json.dumps({"host-a": "host-secret", "edge-a": "edge-secret"}))
        reporter = Reporter("https://example.invalid", "", False, logging.getLogger("test"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), App(self.store, reporter).handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/containers/report"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.temp.cleanup()

    def _post(self, token, host="edge-a"):
        body = json.dumps({"host": host, "reported_at": NOW.isoformat(), "containers": [docker_row()]}).encode()
        req = request.Request(self.url, data=body, method="POST", headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        })
        return request.urlopen(req, timeout=2)

    def test_per_host_token_accepts_report_and_writes_mode_600(self):
        with self._post("edge-secret") as response:
            self.assertEqual(202, response.status)
        path = Path(self.temp.name) / "container-reports" / "edge-a.json"
        self.assertTrue(path.exists())
        self.assertEqual(0o600, os.stat(path).st_mode & 0o777)
        check = self.store.check("container-edge-a-orders-api")
        self.assertIsNotNone(check)
        self.assertEqual(2, check["misses_before_real"])

    def test_wrong_host_token_is_rejected(self):
        with self.assertRaises(error.HTTPError) as caught:
            self._post("host-secret")
        self.assertEqual(401, caught.exception.code)


if __name__ == "__main__":
    unittest.main()
