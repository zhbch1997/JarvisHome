#!/usr/bin/env python3
"""Direct provider-only Jarvis evolution reviewer (no Agent or Gateway)."""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_STATE_DIR = str(Path.home() / ".local/share/jarvis-home/openclaw")
DEFAULT_PROVIDER = "xiaomi-direct"
DEFAULT_MODEL = "mimo-v2.5-pro"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward the provider Authorization header across redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_no_redirect(request: urllib.request.Request, timeout: int):
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)


def _load_provider(state_dir: str) -> tuple[str, str, str]:
    root = Path(state_dir).expanduser().resolve()
    try:
        config = json.loads((root / "openclaw.json").read_text(encoding="utf-8"))
        provider = config["models"]["providers"][DEFAULT_PROVIDER]
        if provider.get("api") != "openai-completions":
            raise ValueError("review provider must use openai-completions")
        base_url = str(provider["baseUrl"]).rstrip("/")
        key_ref = provider["apiKey"]
        if not isinstance(key_ref, dict) or key_ref.get("source") != "file":
            raise ValueError("review provider key must use file secret")
        secret_provider = config["secrets"]["providers"][key_ref["provider"]]
        if (
            not isinstance(secret_provider, dict)
            or secret_provider.get("source") != "file"
            or secret_provider.get("mode") != "singleValue"
        ):
            raise ValueError("review secret provider must be a single-value file")
        secret_path = Path(str(secret_provider["path"])).expanduser().resolve()
        token = secret_path.read_text(encoding="utf-8").strip()
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid independent review provider configuration") from exc
    if not base_url.startswith("https://") or not token:
        raise ValueError("invalid independent review provider endpoint or token")
    model_ids = {
        str(item.get("id") or "") for item in provider.get("models", [])
        if isinstance(item, dict)
    }
    if DEFAULT_MODEL not in model_ids:
        raise ValueError("review model is not configured")
    return base_url, token, DEFAULT_MODEL


def _extract_provider_text(value: Any) -> str:
    try:
        text = value["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("invalid review provider response") from exc
    if not isinstance(text, str) or not text.strip():
        raise ValueError("review provider returned empty text")
    return text


def direct_provider_reviewer(prompt: str, *, state_dir: str | None = None) -> str:
    base_url, token, model = _load_provider(state_dir or os.getenv(
        "OPENCLAW_STATE_DIR", DEFAULT_STATE_DIR,
    ))
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": str(prompt)}],
        "temperature": 0,
        "stream": False,
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with _open_no_redirect(request, timeout=240) as response:
            value = json.loads(response.read())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Jarvis evolution reviewer provider failed") from exc
    return _extract_provider_text(value)


def openclaw_reviewer(prompt: str) -> str:
    """Compatibility name for API wiring; implementation bypasses OpenClaw runtime."""
    return direct_provider_reviewer(prompt)
