from __future__ import annotations

import copy
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest import mock
from urllib import parse

from tiinyengineer.sources import mikrotik
from tiinyengineer.store import Store
from tiinyengineer.syslog_listener import SyslogListener


FIXTURES = Path(__file__).parent / "fixtures"


class Response:
    def __init__(self, value):
        self.body = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


def fixture(name="routeros-normal.json"):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def target(**changes):
    value = {
        "device_id": "test-router",
        "mikrotik_rest_url": "https://router.example",
        "mikrotik_rest_username": "reader",
        "mikrotik_rest_password": "test-only-password",
    }
    value.update(changes)
    return value


class MikroTikSourceTests(unittest.TestCase):
    def response_set(self, values):
        def open_fixture(req, timeout=0):
            self.assertEqual("GET", req.method)
            self.assertTrue(req.get_header("Authorization").startswith("Basic "))
            return Response(values[parse.urlsplit(req.full_url).path])
        return mock.patch.object(mikrotik.request, "urlopen", side_effect=open_fixture)

    def test_normal_poll_reads_every_required_endpoint(self):
        values = fixture()
        with self.response_set(values) as opened:
            ok, status, problem = mikrotik.check_mikrotik(target(device_id="normal-router"))
        self.assertTrue(ok)
        self.assertIsNone(problem)
        self.assertIn("CPU 24%", status)
        self.assertIn("DHCP 20%", status)
        self.assertIn("interface errors 12", status)
        self.assertIn("latest log: router started", status)
        self.assertEqual(6, opened.call_count)

    def test_port_flap_is_seen_in_the_next_poll(self):
        prior = fixture()
        current = fixture("routeros-flap.json")
        with self.response_set(prior):
            self.assertTrue(mikrotik.check_mikrotik(target(device_id="flap-router"))[0])
        with self.response_set(current):
            ok, _status, problem = mikrotik.check_mikrotik(target(device_id="flap-router"))
        self.assertFalse(ok)
        self.assertIn("port flap: ether1", problem)

    def test_cpu_threshold(self):
        values = fixture()
        values["/rest/system/resource"][0]["cpu-load"] = "91"
        with self.response_set(values):
            ok, _status, problem = mikrotik.check_mikrotik(target(device_id="cpu-router"))
        self.assertFalse(ok)
        self.assertIn("CPU is 91%", problem)

    def test_temperature_threshold(self):
        values = fixture()
        values["/rest/system/health"][0]["value"] = "81"
        with self.response_set(values):
            ok, _status, problem = mikrotik.check_mikrotik(target(device_id="hot-router"))
        self.assertFalse(ok)
        self.assertIn("temperature is 81 C", problem)

    def test_dhcp_pool_threshold(self):
        values = fixture()
        values["/rest/ip/dhcp-server/lease"] = [
            {"address": f"10.0.0.{number}", "status": "bound"} for number in range(10, 19)
        ]
        with self.response_set(values):
            ok, status, problem = mikrotik.check_mikrotik(target(device_id="dhcp-router"))
        self.assertFalse(ok)
        self.assertIn("DHCP 90%", status)
        self.assertIn("DHCP pool is 90% used", problem)

    def test_new_interface_error_counter_is_reported(self):
        values = fixture()
        previous = copy.deepcopy(values["/rest/interface"])
        prior = mikrotik._interface_counters(previous)
        values["/rest/interface"][0]["rx-error"] = "12"
        with self.response_set(values):
            ok, _status, problem = mikrotik.check_mikrotik(
                target(device_id="errors-router", previous_interfaces=prior)
            )
        self.assertFalse(ok)
        self.assertIn("new interface errors: ether1 +2", problem)


class SyslogListenerTests(unittest.TestCase):
    def test_syslog_line_reaches_ledger_within_five_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "data.db")
            listener = SyslogListener(store, "127.0.0.1", 0)
            listener.start()
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            started = time.monotonic()
            try:
                sender.sendto(b"ether1 link down\n", listener.address)
                found = None
                while time.monotonic() - started < 5:
                    rows = store.ledger()
                    if rows:
                        found = rows[0]
                        break
                    time.sleep(0.01)
                self.assertIsNotNone(found)
                self.assertLess(time.monotonic() - started, 5)
                body = json.loads(found["body"])
                self.assertEqual("syslog", found["actor"])
                self.assertEqual("ether1 link down", body["message"])
                self.assertEqual("127.0.0.1", body["sender"])
            finally:
                sender.close()
                listener.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
