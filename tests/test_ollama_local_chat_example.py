import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "examples" / "ollama_local_chat.py"


def load_example():
    spec = importlib.util.spec_from_file_location("ollama_local_chat_example", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Ollama example")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ChatHandler(BaseHTTPRequestHandler):
    request_body = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        type(self).request_body = json.loads(self.rfile.read(length))
        body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "Local model ready."}}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class OllamaLocalChatExampleTests(unittest.TestCase):
    def setUp(self):
        ChatHandler.request_body = None

    def _serve_response_and_assert_safe_failure(self, module, body: bytes):
        class ResponseHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), ResponseHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/private-value"
            with self.assertRaises(module.LocalModelUnavailable) as caught:
                module.request_chat(url, "test-model", "hello", timeout=2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertNotIn("private-value", str(caught.exception))

    def test_deeply_nested_json_becomes_a_safe_diagnostic(self):
        module = load_example()
        body = ("[" * 2000 + "]" * 2000).encode()
        self._serve_response_and_assert_safe_failure(module, body)

    def test_oversized_response_becomes_a_safe_diagnostic(self):
        module = load_example()
        body = b"x" * (module.MAX_RESPONSE_BYTES + 1)
        self._serve_response_and_assert_safe_failure(module, body)

    def test_example_calls_loopback_openai_compatible_endpoint(self):
        module = load_example()
        server = ThreadingHTTPServer(("127.0.0.1", 0), ChatHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
            answer = module.request_chat(url, "test-model", "Say hello", timeout=2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual("Local model ready.", answer)
        self.assertEqual("test-model", ChatHandler.request_body["model"])
        self.assertEqual("Say hello", ChatHandler.request_body["messages"][0]["content"])
        self.assertFalse(ChatHandler.request_body["stream"])

    def test_endpoint_contract_accepts_http_loopback(self):
        module = load_example()
        for endpoint in (
            "http://127.0.0.1:11434/v1/chat/completions",
            "http://127.42.0.7:11434/v1/chat/completions",
            "http://[::1]:11434/v1/chat/completions",
        ):
            with self.subTest(endpoint=endpoint):
                module._validate_endpoint(endpoint)

    def test_endpoint_contract_rejects_unsafe_urls(self):
        module = load_example()
        unsafe = (
            "https://127.0.0.1:11434/v1/chat/completions",
            "http://localhost:11434/v1/chat/completions",
            "http://192.0.2.10:11434/v1/chat/completions",
            "http://[::ffff:127.0.0.1]/v1/chat/completions",
            "http://user:password@127.0.0.1:11434/v1/chat/completions",
            "http://@127.0.0.1:11434/v1/chat/completions",
            "http://:@127.0.0.1:11434/v1/chat/completions",
            "http://127.0.0.1:/v1/chat/completions",
            "http://[::1]:/v1/chat/completions",
            "http://127.0.0.1:11434/v1/chat/completions?token=value",
            "http://127.0.0.1:11434/v1/chat/completions?",
            "http://127.0.0.1:11434/v1/chat/completions#private",
            "http://127.0.0.1:11434/v1/chat/completions#",
        )
        for endpoint in unsafe:
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    module._validate_endpoint(endpoint)

    def test_malformed_port_is_rejected_without_cli_traceback(self):
        module = load_example()
        endpoint = "http://127.0.0.1:not-a-port/private-value"
        stderr = io.StringIO()
        with patch.object(sys, "argv", ["ollama_local_chat.py", "--url", endpoint]):
            with contextlib.redirect_stderr(stderr):
                code = module.main()
        diagnostic = stderr.getvalue()
        self.assertEqual(1, code)
        self.assertNotIn("Traceback", diagnostic)
        self.assertNotIn("not-a-port", diagnostic)
        self.assertNotIn("private-value", diagnostic)

    def test_url_parser_error_is_rejected_without_cli_disclosure(self):
        module = load_example()
        endpoint = "http://127.0.0.1／PRIVATE-PATH"
        stderr = io.StringIO()
        with patch.object(sys, "argv", ["ollama_local_chat.py", "--url", endpoint]):
            with contextlib.redirect_stderr(stderr):
                code = module.main()
        diagnostic = stderr.getvalue()
        self.assertEqual(1, code)
        self.assertNotIn("Traceback", diagnostic)
        self.assertNotIn("PRIVATE-PATH", diagnostic)
        self.assertIn("valid HTTP loopback IP endpoint", diagnostic)

    def test_invalid_timeouts_are_rejected_without_cli_traceback(self):
        module = load_example()
        for timeout in ("-1", "0", "nan", "inf", "1e309"):
            with self.subTest(timeout=timeout):
                stderr = io.StringIO()
                argv = ["ollama_local_chat.py", "--timeout", timeout]
                with patch.object(sys, "argv", argv):
                    with contextlib.redirect_stderr(stderr):
                        code = module.main()
                diagnostic = stderr.getvalue()
                self.assertEqual(1, code)
                self.assertNotIn("Traceback", diagnostic)
                self.assertIn("finite positive number", diagnostic)

    def test_opener_ignores_environment_proxy_settings(self):
        module = load_example()
        with patch.object(
            module.urllib.request,
            "getproxies",
            side_effect=AssertionError("environment proxies must not be inspected"),
        ):
            opener = module._build_local_opener()
        self.assertFalse(
            any(
                isinstance(handler, module.urllib.request.ProxyHandler)
                for handler in opener.handlers
            )
        )

    def test_redirects_are_rejected_before_following_target(self):
        module = load_example()

        class TargetHandler(BaseHTTPRequestHandler):
            requests = 0

            def do_GET(self):
                type(self).requests += 1
                body = json.dumps(
                    {"choices": [{"message": {"content": "redirect followed"}}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                return

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(303)
                self.send_header(
                    "Location", f"http://127.0.0.1:{target.server_port}/private-target"
                )
                self.end_headers()

            def log_message(self, format, *args):
                return

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()
        try:
            url = f"http://127.0.0.1:{redirect.server_port}/v1/chat/completions"
            with self.assertRaises(module.LocalModelUnavailable):
                module.request_chat(url, "test-model", "hello", timeout=2)
        finally:
            redirect.shutdown()
            redirect.server_close()
            redirect_thread.join(timeout=2)
            target.shutdown()
            target.server_close()
            target_thread.join(timeout=2)
        self.assertEqual(0, TargetHandler.requests)

    def test_connection_error_does_not_echo_endpoint(self):
        module = load_example()
        endpoint = "http://127.0.0.1:9/private-value/v1/chat/completions"
        with self.assertRaises(module.LocalModelUnavailable) as caught:
            module.request_chat(endpoint, "test-model", "hello", timeout=0.1)
        self.assertNotIn("private-value", str(caught.exception))

    def test_truncated_http_response_becomes_a_safe_diagnostic(self):
        module = load_example()

        class TruncatedHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = b"{"
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), TruncatedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/private-value"
            with self.assertRaises(module.LocalModelUnavailable) as caught:
                module.request_chat(url, "test-model", "hello", timeout=2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertNotIn("private-value", str(caught.exception))

    def test_malformed_json_becomes_a_safe_diagnostic(self):
        module = load_example()

        class MalformedHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = b"not-json"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), MalformedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/private-value"
            with self.assertRaises(module.LocalModelUnavailable) as caught:
                module.request_chat(url, "test-model", "hello", timeout=2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertNotIn("private-value", str(caught.exception))

    def test_example_is_documented_without_machine_specific_model_names(self):
        readme = (ROOT / "examples" / "README.md").read_text(encoding="utf-8")
        self.assertIn("qwen3:0.6b", readme)
        self.assertNotIn("qwen35-4b-16k", readme)


if __name__ == "__main__":
    unittest.main()
