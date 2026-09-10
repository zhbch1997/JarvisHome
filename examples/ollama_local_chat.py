#!/usr/bin/env python3
"""Send one local chat request to an Ollama-compatible OpenAI endpoint."""
from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import math
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit


DEFAULT_URL = "http://127.0.0.1:11434/v1/chat/completions"
DEFAULT_MODEL = "qwen3:0.6b"
MAX_RESPONSE_BYTES = 1024 * 1024



class LocalModelUnavailable(RuntimeError):
    """Raised when the local model endpoint cannot complete the request."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep the validated request on its original loopback endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_endpoint(url: str) -> None:
    try:
        parsed = urlsplit(url)
        authority = parsed.netloc.rsplit("@", 1)[-1]
        if authority.endswith(":"):
            raise ValueError
        host = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError as exc:
        raise ValueError("the example accepts only a valid HTTP loopback IP endpoint") from exc
    is_allowed_loopback = (
        host.version == 4 and host in ipaddress.ip_network("127.0.0.0/8")
    ) or host == ipaddress.ip_address("::1")
    if parsed.scheme != "http" or not is_allowed_loopback:
        raise ValueError("the example accepts only a valid HTTP loopback IP endpoint")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("the example accepts only a valid HTTP loopback IP endpoint")
    if (
        parsed.username is not None
        or parsed.password is not None
        or "?" in url
        or "#" in url
    ):
        raise ValueError("the loopback endpoint must not contain credentials, query, or fragment")


def _build_local_opener():
    """Build an opener that neither follows redirects nor inherits proxy settings."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def request_chat(
    url: str,
    model: str,
    prompt: str,
    *,
    timeout: float = 60.0,
) -> str:
    """Return the assistant text from one non-streaming local request."""
    _validate_endpoint(url)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number")
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    opener = _build_local_opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise LocalModelUnavailable(
                    "the local model server returned an unexpected response shape"
                )
            result = json.loads(body)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        raise LocalModelUnavailable(
            "the local model server is unavailable; confirm that Ollama is running and the model is installed"
        ) from exc
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, http.client.HTTPException) as exc:
        raise LocalModelUnavailable(
            "the local model server returned an unexpected response shape"
        ) from exc

    try:
        answer = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LocalModelUnavailable(
            "the local model server returned an unexpected response shape"
        ) from exc
    if not isinstance(answer, str) or not answer.strip():
        raise LocalModelUnavailable("the local model server returned an empty answer")
    return answer.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", default="Reply with: Jarvis Home is ready.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        print(request_chat(args.url, args.model, args.prompt, timeout=args.timeout))
    except (ValueError, LocalModelUnavailable) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
