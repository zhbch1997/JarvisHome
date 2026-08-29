#!/usr/bin/env python3
"""Offline installation diagnostics for Jarvis Home."""
from __future__ import annotations

import importlib.util
import json
import platform
import shutil
import sys
from typing import Any


def report() -> dict[str, Any]:
    modules = {name: importlib.util.find_spec(name) is not None for name in ("fastapi", "httpx", "uvicorn", "websockets")}
    return {
        "python": platform.python_version(),
        "python_supported": sys.version_info >= (3, 11),
        "platform": platform.platform(),
        "runtime_dependencies": modules,
        "optional_commands": {
            "ollama": shutil.which("ollama") is not None,
            "miloco-cli": shutil.which("miloco-cli") is not None,
        },
        "offline_demo_available": importlib.util.find_spec("mock_home") is not None,
    }


def main() -> int:
    value = report()
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if value["python_supported"] and all(value["runtime_dependencies"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
