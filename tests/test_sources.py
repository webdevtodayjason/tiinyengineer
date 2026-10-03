import sys
import types
import unittest

from tiinyengineer import checks, sources


class SourcesTest(unittest.TestCase):
    def test_source_module_kinds_and_settings_are_found(self):
        name = "tiinyengineer.sources.zz_fake"
        module = types.ModuleType(name)
        module.KINDS = {"fake": lambda target: (True, "fine", None)}
        module.SETTINGS = [{"key": "fake_token", "label": "Fake token", "secret": True, "help": ""}]
        sys.modules[name] = module
        original = sources._MODULES
        sources._MODULES = (module,)
        try:
            self.assertIn("fake", sources.kinds())
            self.assertEqual(sources.settings()[0]["source"], "zz_fake")
        finally:
            sources._MODULES = original
            del sys.modules[name]

    def test_builtin_kinds_survive(self):
        self.assertTrue({"http", "tcp", "ping"} <= set(checks.RUNNERS))


if __name__ == "__main__":
    unittest.main()
