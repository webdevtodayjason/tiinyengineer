from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from unittest import mock

from tiinyengineer.sources import tailscale


NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def status_for(*, online: bool, expiry_days: int = 30) -> dict:
    return {
        "Peer": {
            "node": {
                "HostName": "Studio",
                "DNSName": "studio.example.ts.net.",
                "Online": online,
                "LastSeen": (NOW - timedelta(minutes=3)).isoformat(),
                "KeyExpiry": (NOW + timedelta(days=expiry_days)).isoformat(),
            }
        }
    }


class TailscalePeerTests(unittest.TestCase):
    def target(self, role: str = "server") -> dict:
        return {
            "peer": "Studio",
            "role": role,
            "lan_host": "192.0.2.10",
            "lan_ports": [443],
            "arp_path": "/missing",
            "_now": NOW,
        }

    @mock.patch("tiinyengineer.sources.tailscale._lan_reachable", return_value=True)
    @mock.patch("tiinyengineer.sources.tailscale._status", return_value=status_for(online=False))
    def test_tailnet_failure_with_lan_up_is_not_called_host_down(self, _status, _lan):
        ok, status, error = tailscale.check_peer(self.target())
        self.assertFalse(ok)
        self.assertEqual("tailnet problem on Studio, LAN up; last seen 3 min ago; key expires in 30 days", status)
        self.assertNotIn("Studio is down", status)
        self.assertEqual("tailnet connection is down", error)

    @mock.patch("tiinyengineer.sources.tailscale._lan_reachable", return_value=False)
    @mock.patch("tiinyengineer.sources.tailscale._status", return_value=status_for(online=False))
    def test_full_failure_calls_host_down(self, _status, _lan):
        ok, status, error = tailscale.check_peer(self.target())
        self.assertFalse(ok)
        self.assertTrue(status.startswith("Studio is down"))
        self.assertEqual("tailnet and LAN checks both failed", error)

    @mock.patch("tiinyengineer.sources.tailscale._lan_reachable")
    @mock.patch("tiinyengineer.sources.tailscale._status", return_value=status_for(online=False))
    def test_presence_peer_never_fails_or_runs_lan_probe(self, _status, lan):
        ok, status, error = tailscale.check_peer(self.target("presence"))
        self.assertTrue(ok)
        self.assertIn("presence only", status)
        self.assertIsNone(error)
        lan.assert_not_called()

    @mock.patch("tiinyengineer.sources.tailscale._lan_reachable")
    @mock.patch("tiinyengineer.sources.tailscale._status", return_value=status_for(online=True, expiry_days=1))
    def test_presence_peer_key_expiry_is_logged_without_failure(self, _status, lan):
        ok, status, error = tailscale.check_peer(self.target("presence"))
        self.assertTrue(ok)
        self.assertIn("key expires in 1 day", status)
        self.assertIsNone(error)
        lan.assert_not_called()

    @mock.patch("tiinyengineer.sources.tailscale._lan_reachable")
    @mock.patch("tiinyengineer.sources.tailscale._status", return_value=status_for(online=True, expiry_days=7))
    def test_key_expiry_at_seven_days_is_a_warning(self, _status, lan):
        ok, status, error = tailscale.check_peer(self.target())
        self.assertFalse(ok)
        self.assertIn("Key warning for Studio", status)
        self.assertIn("key expires in 7 days", status)
        self.assertEqual("key expires in 7 days", error)
        lan.assert_not_called()

    @mock.patch("tiinyengineer.sources.tailscale._status", return_value=status_for(online=True, expiry_days=8))
    def test_online_peer_with_later_expiry_is_healthy(self, _status):
        ok, status, error = tailscale.check_peer(self.target())
        self.assertTrue(ok)
        self.assertIn("Studio is online", status)
        self.assertIsNone(error)

    def test_default_roles_are_conservative(self):
        self.assertEqual("presence", tailscale._role({}, "Office NAS"))
        self.assertEqual("presence", tailscale._role({}, "Travelling Tiiny"))
        self.assertEqual("server", tailscale._role({"role": "server"}, "Office NAS"))

    @mock.patch("tiinyengineer.sources.tailscale._status", return_value=status_for(online=True))
    def test_peer_name_matches_real_host_name(self, _status):
        ok, status, error = tailscale.check_peer(self.target())
        self.assertTrue(ok)
        self.assertIn("Studio is online", status)
        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
