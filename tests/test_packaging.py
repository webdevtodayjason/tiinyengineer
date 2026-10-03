from pathlib import Path
import unittest


class PackagingTests(unittest.TestCase):
    def test_service_runs_as_the_owner_without_bytecode_writes(self):
        unit = (Path(__file__).parents[1] / "systemd" / "tiinyengineer.service").read_text(encoding="utf-8")
        self.assertIn("User=tiinyengineer\n", unit)
        self.assertIn("Group=tiinyengineer\n", unit)
        self.assertIn("UMask=0077\n", unit)
        self.assertIn("Environment=PYTHONDONTWRITEBYTECODE=1\n", unit)


if __name__ == "__main__":
    unittest.main()
