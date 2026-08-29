import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "jarvis-bridge"))

import jarvis_launcher


class LauncherTests(unittest.TestCase):
    def test_build_config_defaults_to_loopback(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                {"host": "127.0.0.1", "port": 18083, "reload": False},
                jarvis_launcher.build_config(),
            )

    def test_build_config_reads_environment(self):
        env = {"JARVIS_HOST": "0.0.0.0", "JARVIS_PORT": "19000", "JARVIS_RELOAD": "1"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                {"host": "0.0.0.0", "port": 19000, "reload": True},
                jarvis_launcher.build_config(),
            )

    def test_invalid_port_fails_closed(self):
        with patch.dict(os.environ, {"JARVIS_PORT": "invalid"}, clear=True):
            with self.assertRaisesRegex(ValueError, "JARVIS_PORT"):
                jarvis_launcher.build_config()


if __name__ == "__main__":
    unittest.main()
