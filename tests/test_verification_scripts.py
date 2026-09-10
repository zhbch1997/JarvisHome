import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class VerificationScriptTests(unittest.TestCase):
    def test_root_demo_script_runs_the_offline_story_mode(self):
        script = (ROOT / "demo.sh").read_text(encoding="utf-8")
        self.assertIn("command -v uv", script)
        self.assertIn("uv run --locked jarvis-home-demo --story", script)
        self.assertNotIn("curl", script)
        self.assertNotIn("wget", script)

    def test_clean_clone_wheel_install_reuses_locked_environment_without_resolving_dependencies(self):
        script = (ROOT / "scripts" / "verify_clean_clone.sh").read_text(encoding="utf-8")
        self.assertIn(
            'uv pip install --python .venv/bin/python --reinstall --no-deps "$wheel"',
            script,
        )
        self.assertNotIn('python3 -m venv', script)
        self.assertNotIn('bin/pip" install', script)


if __name__ == "__main__":
    unittest.main()
