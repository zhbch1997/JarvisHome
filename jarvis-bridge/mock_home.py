"""Deterministic, side-effect-free home adapter used by the public demo."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MockHomeAdapter:
    actions: list[dict[str, Any]] = field(default_factory=list)

    def cli(self, args: list[str]) -> str:
        command = tuple(args)
        if command == ("device", "list"):
            return "demo-light-1|示例灯|示例房间|light|online\n"
        if command == ("device", "spec", "demo-light-1"):
            return "prop.2.1 on|rw|bool\nprop.2.2 brightness|rw|int|1-100\n"
        if len(command) == 5 and command[:2] == ("device", "control") and command[2] == "demo-light-1":
            prop, raw_value = command[3], command[4]
            if prop not in {"on", "brightness"}:
                raise ValueError("unsupported mock command")
            value: Any = raw_value
            if raw_value in {"true", "false"}:
                value = raw_value == "true"
            elif raw_value.isdigit():
                value = int(raw_value)
            self.actions.append({"device_id": command[2], "property": prop, "value": value})
            return "ok"
        if command == ("scene", "list"):
            return '{"scenes":[{"scene_id":"demo-scene-1","scene_name":"晚安"}]}'
        if command == ("scene", "trigger", "demo-scene-1"):
            self.actions.append({"scene_id": "demo-scene-1", "action": "trigger"})
            return "ok"
        raise ValueError("unsupported mock command")


def run_demo() -> dict[str, Any]:
    adapter = MockHomeAdapter()
    adapter.cli(["device", "control", "demo-light-1", "on", "true"])
    return {"status": "ok", "adapter": "mock", "actions": adapter.actions}


def main() -> None:
    print(json.dumps(run_demo(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
