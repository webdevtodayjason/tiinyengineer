from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class UiTests(unittest.TestCase):
    def test_dashboard_has_every_stage_one_view(self):
        page = (ROOT / "tiinyengineer" / "ui" / "index.html").read_text(encoding="utf-8")
        for view in ("Now", "Registry", "Rules", "Reporting", "Assignment", "Timeline", "History", "Settings"):
            self.assertIn(f">{view}<", page)

    def test_shipped_tree_has_no_forbidden_copy(self):
        long_dash = chr(0x2014)
        three_letter_remote_access_term = "s" + "sh"
        for path in (ROOT / "tiinyengineer").rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(long_dash, text, path)
                self.assertNotIn(three_letter_remote_access_term, text.lower(), path)

    def test_static_files_are_in_the_package_tree(self):
        for name in ("index.html", "style.css", "app.js"):
            self.assertTrue((ROOT / "tiinyengineer" / "ui" / name).is_file())

    def test_review_regressions_stay_fixed(self):
        page = (ROOT / "tiinyengineer" / "ui" / "index.html").read_text(encoding="utf-8")
        css = (ROOT / "tiinyengineer" / "ui" / "style.css").read_text(encoding="utf-8")
        script = (ROOT / "tiinyengineer" / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn('data-theme="dark"', page)
        self.assertIn("prefers-color-scheme:light", css)
        self.assertIn("min-height:44px", css)
        self.assertIn("overflow-x:auto", css)
        self.assertIn("Waiting for its first run", script)
        self.assertIn("{down:0,up:0,away:0,unknown:0}", script)
        self.assertIn('content:attr(data-line)', css)
        self.assertIn('data-line="1"', script)
        self.assertIn('matchMedia("(prefers-color-scheme: light)")', script)

    def test_incident_card_formats_time_and_numbers_title(self):
        script = (ROOT / "tiinyengineer" / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn('new Intl.DateTimeFormat("en-US"', script)
        self.assertIn('<span class="line-no">1</span>', script)


if __name__ == "__main__":
    unittest.main()
