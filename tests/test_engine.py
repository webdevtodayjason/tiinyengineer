from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from tiinyengineer import checks
from tiinyengineer.reporter import Reporter
from tiinyengineer.store import Store, judge


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "data.db")
        self.store.seed([{"address": "10.0.0.20", "name": "Workshop Tiiny"}])

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_seed_has_only_generic_first_run_checks(self):
        self.assertEqual({"tiinyapp-farm-health", "my-tiiny-1"}, {row["id"] for row in self.store.checks()})
        self.assertEqual([], self.store.checks("beat"))

    def test_existing_store_is_never_reseeded_or_changed(self):
        before = self.store.checks()
        self.assertFalse(self.store.seed([{"address": "10.0.0.99", "name": "Different Tiiny"}]))
        self.assertEqual(before, self.store.checks())

    def test_down_after_threshold_then_recovery_closes_same_incident(self):
        for expected in ("DEGRADED", "DEGRADED", "DOWN"):
            state = self.store.record_result("my-tiiny-1", "poll", False, {"status": "no answer"})
            self.assertEqual(expected, state["status"])
        incident_id = state["incident_id"]
        self.assertRegex(incident_id, r"^TE-\d{8}-001$")
        state = self.store.record_result("my-tiiny-1", "poll", True, {"status": "connected"})
        self.assertEqual("UP", state["transition"])
        incident = self.store.incident(incident_id)
        self.assertEqual("closed", incident["state"])
        self.assertIn("UP again after", incident["close_reason"])

    def test_ledger_refuses_changes(self):
        for _ in range(3):
            self.store.record_result("my-tiiny-1", "poll", False, {"status": "no answer"})
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._keeper.execute("UPDATE ledger SET actor='changed'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._keeper.execute("DELETE FROM ledger")

    def test_beat_token_and_deadline(self):
        self.store.save_check({"id": "release-poll", "kind": "beat", "target": {"job": "release-poll"},
                               "display_name": "Release poll", "period_s": 2, "grace_s": 1,
                               "misses_before_real": 1, "severity": "warning", "alert": True})
        token = self.store.token_for("release-poll")
        self.assertFalse(self.store.record_beat("release-poll", "wrong", True, 4, ""))
        self.assertTrue(self.store.record_beat("release-poll", token, True, 4, ""))
        row = next(r for r in self.store.beat_rows() if r["id"] == "release-poll")
        late = datetime.fromisoformat(row["last_beat_at"]) + timedelta(seconds=row["period_s"] + row["grace_s"] + 1)
        self.assertIn("release-poll", [r["id"] for r in self.store.find_missed_beats(late)])

    def test_new_beat_check_gets_a_token_and_enters_deadline_scan(self):
        self.store.save_check({"id": "new-job", "kind": "beat", "target": {"job": "new-job"},
                               "display_name": "New job", "period_s": 2, "grace_s": 1,
                               "misses_before_real": 1, "severity": "warning", "alert": True})
        self.assertIsNotNone(self.store.token_for("new-job"))
        self.assertIn("new-job", [row["id"] for row in self.store.beat_rows()])

    def test_judge_is_pass_through(self):
        value = [{"one": 1}]
        self.assertIs(value, judge(value))

    def test_old_events_roll_up_and_raw_rows_are_removed(self):
        self.store.record_result("my-tiiny-1", "poll", True, {"status": "old"}, "2025-01-01T01:05:00+00:00")
        self.store.record_result("my-tiiny-1", "poll", False, {"status": "old"}, "2025-01-01T01:35:00+00:00")
        self.store.rollup_old_events("2026-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00")
        raw = self.store._keeper.execute("SELECT COUNT(*) FROM events WHERE check_id='my-tiiny-1'").fetchone()[0]
        rollup = self.store._keeper.execute("SELECT observations,successes,failures FROM event_rollups WHERE check_id='my-tiiny-1'").fetchone()
        self.assertEqual(0, raw)
        self.assertEqual((2, 1, 1), tuple(rollup))

    @unittest.skipUnless(Path("/proc/self/fd").exists(), "Linux descriptor accounting")
    def test_two_thousand_store_calls_keep_descriptors_flat(self):
        baseline = len(os.listdir("/proc/self/fd"))
        for _ in range(2000):
            self.store.checks()
        final = len(os.listdir("/proc/self/fd"))
        self.assertLessEqual(final, baseline + 2, f"file descriptors grew from {baseline} to {final}")


class CheckTests(unittest.TestCase):
    def test_arp_presence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "arp"
            path.write_text("IP address HW type Flags HW address Mask Device\n192.0.2.2 0x1 0x2 aa:bb:cc:dd:ee:ff * eth0\n", encoding="utf-8")
            self.assertTrue(checks.arp_has("192.0.2.2", str(path)))
            self.assertEqual((True, "present in the local address table", None), checks.check_ping({"host": "192.0.2.2", "arp_path": str(path)}))

    @mock.patch("tiinyengineer.checks.socket.socket")
    def test_ping_treats_refused_connection_as_host_reachable(self, socket_type):
        instance = socket_type.return_value
        instance.connect_ex.return_value = 61
        with mock.patch("tiinyengineer.checks.errno.ECONNREFUSED", 61):
            ok, status, error = checks.check_ping({"host": "192.0.2.3", "ports": [9], "arp_path": "/missing"})
        self.assertTrue(ok)
        self.assertIn("TCP 9", status)
        self.assertIsNone(error)


class ReporterTests(unittest.TestCase):
    def setUp(self):
        self.reporter = Reporter("https://example.invalid", "token", False, logging.getLogger("test"))
        self.check = {"id": "thing", "kind": "beat", "target": {"job": "thing"}, "display_name": "Nightly export",
                      "where_text": "office scheduler", "meaning": "The export is no longer running.",
                      "first_step": "Open its last run.", "owner": "operations", "console_url": "https://console.invalid"}

    def test_down_alert_has_five_plain_lines(self):
        state = {"transition": "DOWN", "status": "DOWN", "at": "2026-10-02T12:00:00+00:00", "failures": 1}
        text = self.reporter.alert_text(self.check, state, None)
        lines = text.splitlines()
        self.assertEqual(5, len(lines))
        self.assertTrue(lines[0].startswith("DOWN: Nightly export"))
        self.assertIn("Since", lines[1])
        self.assertEqual("The export is no longer running.", lines[2])
        self.assertIn("Owner: operations", lines[3])
        self.assertIn("Alerts view", lines[4])

    def test_recovery_says_up_again(self):
        state = {"transition": "UP", "status": "UP", "at": "2026-10-02T12:10:00+00:00", "failures": 0}
        incident = {"opened_at": "2026-10-02T12:00:00+00:00"}
        self.assertTrue(self.reporter.alert_text(self.check, state, incident).startswith("UP again after 10 min"))


if __name__ == "__main__":
    unittest.main()
