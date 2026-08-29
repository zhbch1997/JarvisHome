"""Optional HTTP client boundary for a separately installed home backend."""
from __future__ import annotations

import ipaddress
import os
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx


class ExternalHomeConfigError(ValueError):
    pass


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class ExternalHomeClient:
    def __init__(self, base_url: str, *, transport: Any = httpx) -> None:
        parsed = urlsplit(str(base_url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ExternalHomeConfigError("external home base URL must be HTTP(S)")
        if parsed.username is not None or parsed.password is not None:
            raise ExternalHomeConfigError("credentials must not be embedded in the base URL")
        if parsed.query or parsed.fragment:
            raise ExternalHomeConfigError("external home base URL must not contain query or fragment")
        if parsed.scheme == "http" and not self._is_loopback(parsed.hostname):
            raise ExternalHomeConfigError("non-loopback external home services require HTTPS")
        normalized_path = parsed.path.rstrip("/")
        self.base_url = urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))
        self.transport = transport

    @staticmethod
    def _is_loopback(host: str) -> bool:
        if host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    @classmethod
    def from_env(cls, *, transport: Any = httpx) -> "ExternalHomeClient":
        if not _enabled(os.getenv("JARVIS_EXTERNAL_HOME_ENABLED")):
            raise ExternalHomeConfigError("external home plugin is disabled")
        base_url = os.getenv("JARVIS_EXTERNAL_HOME_BASE_URL", "").strip()
        if not base_url:
            raise ExternalHomeConfigError("external home base URL is required")
        return cls(base_url, transport=transport)

    def list_devices(self) -> list[dict[str, Any]]:
        response = self.transport.get(f"{self.base_url}/v1/devices", timeout=10.0)
        response.raise_for_status()
        payload = response.json()
        devices = payload.get("devices") if isinstance(payload, dict) else None
        if not isinstance(devices, list) or any(not isinstance(item, dict) for item in devices):
            raise RuntimeError("invalid device response from external home backend")
        return devices

    def _json(self, method: str, path: str, *, payload: dict[str, Any] | None = None) -> Any:
        request = getattr(self.transport, method)
        kwargs: dict[str, Any] = {"timeout": 10.0}
        if payload is not None:
            kwargs["json"] = payload
        response = request(f"{self.base_url}{path}", **kwargs)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _segment(value: str) -> str:
        raw = str(value)
        if not raw or len(raw) > 512:
            raise RuntimeError("invalid external home identifier")
        return quote(raw, safe="")

    def device_specs(self, device_id: str) -> dict[str, Any]:
        value = self._json("get", f"/v1/devices/{self._segment(device_id)}/specs")
        if not isinstance(value, dict):
            raise RuntimeError("invalid device specs response")
        return value

    def control_device(self, device_id: str, operation: str, value: Any = None) -> Any:
        return self._json("post", f"/v1/devices/{self._segment(device_id)}/actions", payload={"operation": operation, "value": value})

    def list_scenes(self) -> list[dict[str, Any]]:
        value = self._json("get", "/v1/scenes")
        scenes = value.get("scenes") if isinstance(value, dict) else None
        if not isinstance(scenes, list) or any(not isinstance(item, dict) for item in scenes):
            raise RuntimeError("invalid scene response")
        return scenes

    def trigger_scene(self, scene_id: str) -> Any:
        return self._json("post", f"/v1/scenes/{self._segment(scene_id)}/trigger", payload={})

    def query(self, text: str) -> str:
        value = self._json("post", "/v1/query", payload={"query": str(text)[:300]})
        answer = value.get("answer") if isinstance(value, dict) else None
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("invalid query response")
        return answer.strip()
