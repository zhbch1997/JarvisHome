"""Deterministic, side-effect-free home adapter used by the public demo."""
from __future__ import annotations

import json
import sys
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


def render_story(result: dict[str, Any]) -> str:
    """Render the deterministic demo as a short, human-readable execution trace."""
    action = result["actions"][0]
    state = "on" if action["value"] is True else str(action["value"])
    return "\n".join([
        "Jarvis Home · Safe offline demo",
        "",
        "User: Turn on the demo room light",
        "Decision: QUICK_TOOL",
        "Capability: mock.light.set",
        "Safety: allowlisted mock device",
        f"Action: {action['device_id']} · {action['property']}={str(action['value']).lower()}",
        f"Result: Demo room light is {state}",
        "",
        "No network. No real devices. No credentials.",
    ])


def main() -> None:
    result = run_demo()
    if "--story" in sys.argv[1:]:
        print(render_story(result))
        return
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
