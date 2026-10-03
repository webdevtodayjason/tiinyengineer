from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
import logging
import unittest
from unittest import mock
from urllib import error

from tiinyengineer.reporter import Reporter
from tiinyengineer.sources import coolify_backups


NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


class Response:
    def __init__(self, body: str):
        self.body = body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


def target(**changes):
    value = {
        "coolify_base_url": "https://coolify.example",
        "coolify_api_token": "read-only-token",
        "database_uuid": "db-1",
        "backup_uuid": "backup-1",
        "database_name": "orders",
        "application_name": "shop",
        "frequency": "daily",
        "now": NOW.isoformat(),
    }
    value.update(changes)
    return value


def executions(newest_age_hours=2, newest_size=1000, newest_status="completed"):
    rows = [{
        "uuid": "current", "filename": "orders-current.dump", "size": newest_size,
        "created_at": (NOW - timedelta(hours=newest_age_hours)).isoformat(), "status": newest_status,
    }]
    for index in range(1, 8):
        rows.append({
            "uuid": f"old-{index}", "filename": f"orders-{index}.dump", "size": 1000,
            "created_at": (NOW - timedelta(hours=newest_age_hours + 24 * index)).isoformat(),
            "status": "completed",
        })
    return {"executions": rows}


class CoolifyBackupTests(unittest.TestCase):
    def api_response(self, payload):
        return mock.patch.object(coolify_backups.request, "urlopen", return_value=Response(payload))

    def test_fresh_backup(self):
        import json
        with self.api_response(json.dumps(executions())):
            ok, status, problem = coolify_backups.check_coolify_backup(target())
        self.assertTrue(ok)
        self.assertIn("daily", status)
        self.assertIn("last good", status)
        self.assertIn("completed", status)
        self.assertIsNone(problem)

    def test_stale_by_age(self):
        import json
        with self.api_response(json.dumps(executions(newest_age_hours=37))):
            ok, status, problem = coolify_backups.check_coolify_backup(target())
        self.assertFalse(ok)
        self.assertIn("last good", status)
        self.assertIn("37.0 hours old", problem)

    def test_stale_by_size(self):
        import json
        with self.api_response(json.dumps(executions(newest_size=250))):
            ok, status, problem = coolify_backups.check_coolify_backup(target())
        self.assertFalse(ok)
        self.assertIn("250 B", status)
        self.assertIn("recent backups are usually", problem)

    def test_api_without_executions_reads_s3_with_sigv4(self):
        missing = error.HTTPError("https://coolify.example", 404, "missing", {}, BytesIO())
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <IsTruncated>false</IsTruncated>
  <Contents><Key>orders/current.dump</Key><LastModified>2026-10-02T10:00:00Z</LastModified><Size>1000</Size></Contents>
  <Contents><Key>orders/old-1.dump</Key><LastModified>2026-10-01T10:00:00Z</LastModified><Size>1000</Size></Contents>
</ListBucketResult>"""
        calls = []

        def open_fixture(req, timeout=0):
            calls.append(req)
            if len(calls) == 1:
                raise missing
            return Response(xml)

        configured = target(
            coolify_s3_endpoint="https://objects.example",
            coolify_s3_region="us-east-1",
            coolify_s3_bucket="backups",
            coolify_s3_access_key="read-key",
            coolify_s3_secret_key="secret",
            s3_prefix="orders/",
        )
        with mock.patch.object(coolify_backups.request, "urlopen", side_effect=open_fixture):
            ok, status, problem = coolify_backups.check_coolify_backup(configured)
        self.assertTrue(ok)
        self.assertIn("via backup destination", status)
        self.assertIsNone(problem)
        self.assertIn("AWS4-HMAC-SHA256", calls[1].get_header("Authorization"))
        self.assertIn("list-type=2", calls[1].full_url)
        self.assertNotIn("secret", calls[1].full_url)

    def test_discovery_lists_every_scheduled_database_and_builds_plain_alert(self):
        import json
        replies = iter([
            Response(json.dumps([
                {"uuid": "db-1", "name": "orders", "project_name": "shop"},
                {"uuid": "db-2", "name": "accounts", "project_name": "billing"},
            ])),
            Response(json.dumps([{"uuid": "schedule-1", "frequency": "daily", "enabled": True}])),
            Response(json.dumps([{"uuid": "schedule-2", "frequency": "0 3 * * *", "enabled": False}])),
        ])
        with mock.patch.object(coolify_backups.request, "urlopen", side_effect=lambda req, timeout=0: next(replies)):
            checks = coolify_backups.discover_checks({
                "coolify_base_url": "https://coolify.example",
                "coolify_api_token": "read-only-token",
            })
        self.assertEqual(["accounts", "orders"], sorted(row["target"]["database_name"] for row in checks))
        self.assertTrue(all(row["owner"] == "operations" for row in checks))
        self.assertTrue(all(row["meaning"].startswith("Restores would lose data newer than") for row in checks))

        reporter = Reporter("https://alerts.example", "", False, logging.getLogger("test"))
        text = reporter.alert_text(checks[0], {
            "transition": "DOWN", "status": "DOWN", "at": NOW.isoformat(), "failures": 1,
        }, None)
        lines = text.splitlines()
        self.assertEqual(5, len(lines))
        self.assertIn("orders in shop", lines[0])
        self.assertIn("Owner: operations", lines[3])

    def test_discovery_warns_for_every_live_database_without_a_schedule(self):
        import json
        replies = iter([
            Response(json.dumps([
                {"uuid": "db-1", "name": "storefront-db", "project_name": "Storefront"},
                {"uuid": "db-2", "name": "training-db", "project_name": "University"},
                {"uuid": "db-3", "name": "postgresql-database-uhl82x6qvq8g9gelknwileeh"},
                {"uuid": "db-4", "name": "redis-database-fz9tl41z5b6g2m40x7xp4o7x"},
            ])),
            Response("[]"), Response("[]"), Response("[]"), Response("[]"),
        ])
        with mock.patch.object(coolify_backups.request, "urlopen", side_effect=lambda req, timeout=0: next(replies)):
            checks = coolify_backups.discover_checks({
                "coolify_base_url": "https://coolify.example",
                "coolify_api_token": "read-only-token",
            })
        self.assertEqual(4, len(checks))
        self.assertTrue(all(row["display_name"].startswith("No backups for ") for row in checks))
        self.assertTrue(all(row["meaning"] == "A disk loss would lose all of it." for row in checks))
        self.assertTrue(all(row["first_step"] == "Add a scheduled backup in Coolify." for row in checks))
        self.assertTrue(all(row["owner"] == "operations" for row in checks))

    def test_missing_schedule_check_recovers_when_one_is_enabled(self):
        import json
        missing_target = target(require_backup_schedule=True)
        with self.api_response("[]"):
            ok, status, problem = coolify_backups.check_coolify_backup(missing_target)
        self.assertFalse(ok)
        self.assertEqual("no enabled backup schedule", status)
        self.assertIn("No backups are scheduled", problem)
        with self.api_response(json.dumps([{"uuid": "schedule", "frequency": "daily", "enabled": True}])):
            ok, status, problem = coolify_backups.check_coolify_backup(missing_target)
        self.assertTrue(ok)
        self.assertIn("daily", status)
        self.assertIsNone(problem)


if __name__ == "__main__":
    unittest.main()
