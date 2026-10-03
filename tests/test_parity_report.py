from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from tools.summarize_parity import summarize


class ParityReportTests(unittest.TestCase):
    def samples(self, directory: str, hours: int, disagree: bool = False) -> Path:
        start = datetime(2026, 10, 2, tzinfo=timezone.utc)
        path = Path(directory) / "samples.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for offset in range(hours + 1):
                checks = [{"check_id": f"check-{number}", "canary": "UP", "tiinyengineer": "UP",
                           "agree": not (disagree and offset == hours and number == 9)} for number in range(10)]
                handle.write(json.dumps({"at": (start + timedelta(hours=offset)).isoformat(),
                                         "all_agree": all(item["agree"] for item in checks), "checks": checks}) + "\n")
        return path

    def test_complete_agreement(self):
        with tempfile.TemporaryDirectory() as directory:
            report, status = summarize(self.samples(directory, 24), "2026-10-02T00:00:00+00:00", 24)
        self.assertEqual(0, status)
        self.assertIn("all 10 shared checks agreed", report)

    def test_incomplete_window_refuses_report(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "not complete"):
                summarize(self.samples(directory, 23), "2026-10-02T00:00:00+00:00", 24)

    def test_difference_returns_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            report, status = summarize(self.samples(directory, 24, True), "2026-10-02T00:00:00+00:00", 24)
        self.assertEqual(1, status)
        self.assertIn("Differences: 1", report)


if __name__ == "__main__":
    unittest.main()
