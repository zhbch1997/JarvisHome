"""Console entry point for the Jarvis Home HTTP bridge."""
from __future__ import annotations

import os
from typing import Any


def build_config() -> dict[str, Any]:
    raw_port = os.getenv("JARVIS_PORT", "18083")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("JARVIS_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("JARVIS_PORT must be between 1 and 65535")
    return {
        "host": os.getenv("JARVIS_HOST", "127.0.0.1"),
        "port": port,
        "reload": os.getenv("JARVIS_RELOAD", "0").lower() in {"1", "true", "yes"},
    }


def main() -> None:
    import uvicorn

    uvicorn.run("api_server:app", **build_config())


if __name__ == "__main__":
    main()
