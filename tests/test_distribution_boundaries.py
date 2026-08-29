import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class DistributionBoundaryTests(unittest.TestCase):
    def test_runtime_python_contains_no_miloco_specific_code(self):
        offenders = []
        for path in (ROOT / "jarvis-bridge").glob("*.py"):
            if "miloco" in path.read_text(encoding="utf-8").lower():
                offenders.append(path.name)
        self.assertEqual([], offenders)

    def test_example_config_keeps_external_home_plugin_disabled_and_generic(self):
        config = (ROOT / "config" / ".env.example").read_text(encoding="utf-8")
        self.assertIn("JARVIS_EXTERNAL_HOME_ENABLED=0", config)
        self.assertIn("JARVIS_EXTERNAL_HOME_BASE_URL=", config)
        self.assertNotIn("MILOCO", config.upper())

    def test_scripts_do_not_download_or_clone_miloco(self):
        offenders = []
        for path in (ROOT / "scripts").glob("*"):
            if path.is_file() and "miloco" in path.read_text(encoding="utf-8", errors="ignore").lower():
                offenders.append(path.name)
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
