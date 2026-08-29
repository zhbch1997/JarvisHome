import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from review_job import DEFAULT_STATE_DIR, NoRedirect, direct_provider_reviewer


class FakeResponse:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.value).encode()


class ReviewJobTests(unittest.TestCase):
    def make_state(self, root: Path):
        secret = root / "secrets" / "xiaomi.txt"
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text("test-secret\n", encoding="utf-8")
        config = {
            "models": {"providers": {"xiaomi-direct": {
                "baseUrl": "https://example.invalid/v1",
                "api": "openai-completions",
                "apiKey": {"source": "file", "provider": "xiaomi_direct_key", "id": "api-key"},
                "models": [{"id": "mimo-v2.5-pro"}],
            }}},
            "secrets": {"providers": {"xiaomi_direct_key": {
                "source": "file", "mode": "singleValue", "path": str(secret),
            }}},
        }
        (root / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")

    def test_reviewer_calls_provider_directly_without_agent_gateway_or_tools(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_state(root)
            captured = {}

            def open_url(request, timeout):
                captured["url"] = request.full_url
                captured["headers"] = dict(request.header_items())
                captured["body"] = json.loads(request.data)
                captured["timeout"] = timeout
                return FakeResponse({
                    "choices": [{"message": {"content": '{"ok":true}'}}],
                })

            with patch("review_job._open_no_redirect", side_effect=open_url):
                text = direct_provider_reviewer("review this", state_dir=str(root))
            self.assertEqual(text, '{"ok":true}')
            self.assertEqual(captured["url"], "https://example.invalid/v1/chat/completions")
            self.assertEqual(captured["body"]["model"], "mimo-v2.5-pro")
            self.assertEqual(captured["body"]["messages"], [{"role": "user", "content": "review this"}])
            self.assertEqual(captured["body"]["temperature"], 0)
            self.assertNotIn("tools", captured["body"])
            self.assertEqual(captured["headers"]["Authorization"], "Bearer test-secret")
            self.assertNotIn("gateway", json.dumps(captured).lower())

    def test_reviewer_rejects_non_file_secret_and_malformed_provider_response(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_state(root)
            config_path = root / "openclaw.json"
            config = json.loads(config_path.read_text())
            config["secrets"]["providers"]["xiaomi_direct_key"]["source"] = "exec"
            config_path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                direct_provider_reviewer("review", state_dir=str(root))

            self.make_state(root)
            with patch("review_job._open_no_redirect", return_value=FakeResponse({"choices": []})):
                with self.assertRaises(ValueError):
                    direct_provider_reviewer("review", state_dir=str(root))

    def test_reviewer_refuses_every_http_redirect(self):
        handler = NoRedirect()
        request = object()
        self.assertIsNone(handler.redirect_request(
            request, None, 302, "Found", {}, "http://evil.invalid/steal",
        ))
        self.assertIsNone(handler.redirect_request(
            request, None, 307, "Temporary Redirect", {},
            "https://other-host.invalid/steal",
        ))

    def test_default_state_is_independent_jarvis_root(self):
        self.assertEqual(DEFAULT_STATE_DIR, str(Path.home() / ".local/share/jarvis-home/openclaw"))


if __name__ == "__main__":
    unittest.main()
