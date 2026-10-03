from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from tiinyengineer.signals import PathState, SIGNAL_SETTINGS, SignalRouter, test_text as path_test_text
from tiinyengineer.store import Store


class FakeStore:
    def __init__(self):
        self.settings = {
            "telegram_bot_token": "fixture-token",
            "telegram_chat_id": "fixture-chat",
            "watchtower_beat_endpoint": "https://watchtower.invalid/beat",
            "watchtower_beat_token": "fixture-beat",
        }
        self.signals = []
        self.path_tests = []

    def setting(self, key, default=""):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def record_signal(self, incident_id, path, ok, detail):
        self.signals.append((incident_id, path, ok, detail))

    def record_path_test(self, path, ok, detail):
        self.path_tests.append((path, ok, detail))


class RouterFixture:
    def __init__(self, paths):
        self.store = FakeStore()
        self.alerts = []
        self.telegrams = []
        self.watchtower = []

        def send_json(url, payload, headers=None):
            self.alerts.append((url, payload, headers))
            return True, "fixture accepted"

        def send_telegram(token, chat_id, text):
            self.telegrams.append((token, chat_id, text))
            return True, "fixture accepted"

        def send_watchtower(endpoint, token):
            self.watchtower.append((endpoint, token))
            return True, "fixture accepted"

        self.router = SignalRouter(
            self.store,
            "https://alerts.invalid/report",
            "alerts-token",
            logging.getLogger("test.signals"),
            path_probe=lambda *_args: paths,
            json_sender=send_json,
            telegram_sender=send_telegram,
            watchtower_sender=send_watchtower,
        )

    def route(self, severity="warning"):
        return self.router.route("TE-20261002-001", severity, "five line alert", {"status": "five line alert"})


class SignalRouterTests(unittest.TestCase):
    def test_tailnet_down_uses_telegram(self):
        fixture = RouterFixture(PathState(False, True, True))
        self.assertEqual({"telegram": True}, fixture.route())
        self.assertEqual([], fixture.alerts)
        self.assertEqual(1, len(fixture.telegrams))

    def test_internet_down_uses_alerts(self):
        fixture = RouterFixture(PathState(True, False, True))
        self.assertEqual({"alerts": True}, fixture.route())
        self.assertEqual(1, len(fixture.alerts))
        self.assertEqual([], fixture.telegrams)

    def test_both_up_send_on_both_paths_and_ledger_each(self):
        fixture = RouterFixture(PathState(True, True, True))
        self.assertEqual({"alerts": True, "telegram": True}, fixture.route())
        self.assertEqual(["alerts", "telegram"], [row[1] for row in fixture.store.signals])

    def test_critical_alarm_uses_every_available_path(self):
        fixture = RouterFixture(PathState(True, True, False))
        self.assertEqual({"alerts": True, "telegram": True}, fixture.route("critical"))
        self.assertEqual(1, len(fixture.alerts))
        self.assertEqual(1, len(fixture.telegrams))

    def test_monday_test_is_one_message_per_path_and_ledgered(self):
        fixture = RouterFixture(PathState(True, True, True))
        now = datetime(2026, 10, 5, 10, 0, tzinfo=ZoneInfo("America/Chicago"))
        fixture.router.tick(now)
        self.assertEqual(1, len(fixture.alerts))
        self.assertEqual(1, len(fixture.telegrams))
        for text in (fixture.alerts[0][1]["status"], fixture.telegrams[0][2]):
            lines = text.splitlines()
            self.assertTrue(lines[0].startswith("TEST:"))
            self.assertEqual("nothing is down", lines[2])
        self.assertEqual(["alerts", "telegram"], [row[0] for row in fixture.store.path_tests])
        self.assertEqual("2026-10-05", fixture.store.settings["last_signal_path_test_alerts"])
        self.assertEqual("2026-10-05", fixture.store.settings["last_signal_path_test_telegram"])

    def test_monday_test_retries_a_path_that_was_down_at_ten(self):
        now = datetime(2026, 10, 5, 10, 0, tzinfo=ZoneInfo("America/Chicago"))
        first = RouterFixture(PathState(True, False, True))
        first.router.tick(now)
        self.assertIn("last_signal_path_test_alerts", first.store.settings)
        self.assertNotIn("last_signal_path_test_telegram", first.store.settings)

    def test_test_format_has_exactly_five_lines(self):
        now = datetime(2026, 10, 5, 10, 0, tzinfo=ZoneInfo("America/Chicago"))
        self.assertEqual(5, len(path_test_text("Telegram", now).splitlines()))

    def test_telegram_settings_are_page_ready(self):
        by_key = {item["key"]: item for item in SIGNAL_SETTINGS}
        self.assertTrue(by_key["telegram_bot_token"]["secret"])
        self.assertFalse(by_key["telegram_chat_id"]["secret"])

    def test_real_store_ledgers_each_path_test(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "signals.db")
            store.record_path_test("alerts", True, "fixture accepted")
            store.record_path_test("telegram", True, "fixture accepted")
            rows = [row for row in store.ledger() if row["kind"] == "path_test"]
            store.close()
        self.assertEqual(2, len(rows))

    def test_real_store_records_one_alarm_send_per_path(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "signals.db")
            store.set_setting("telegram_bot_token", "fixture-token")
            store.set_setting("telegram_chat_id", "fixture-chat")
            router = SignalRouter(
                store,
                "https://alerts.invalid/report",
                "alerts-token",
                logging.getLogger("test.signals.store"),
                path_probe=lambda *_args: PathState(True, True, True),
                json_sender=lambda *_args, **_kwargs: (True, "fixture accepted"),
                telegram_sender=lambda *_args: (True, "fixture accepted"),
            )
            router.route("TE-20261002-001", "critical", "five line alert", {"status": "five line alert"})
            rows = store._keeper.execute("SELECT path FROM signals ORDER BY id").fetchall()
            store.close()
        self.assertEqual(["alerts", "telegram"], [row[0] for row in rows])


if __name__ == "__main__":
    unittest.main()
