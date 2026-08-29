"""Constrained local-4B planner for ordinary Miloco device actions.

The model may choose only array indices and semantic operations. DID and spec_name
always come from fresh miloco-cli output and are validated again before execution.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx


MILOCO_CLI = os.getenv("MILOCO_CLI", "miloco-cli")
MILOCO_HOME = os.getenv("MILOCO_HOME", str(Path.home() / ".local/share/miloco"))
OLLAMA_URL = os.getenv("JARVIS_LEVEL2_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = os.getenv("JARVIS_LEVEL2_MODEL", "qwen35-4b-16k:latest")
_BLOCKED_CATEGORIES = {"lock", "door-lock", "camera", "speaker", "smoke-alarm", "gas-sensor"}


class DeviceActionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Device:
    did: str
    name: str
    room: str
    category: str
    online: bool


def _cli(args: list[str]) -> str:
    env = os.environ.copy()
    env["MILOCO_HOME"] = MILOCO_HOME
    result = subprocess.run(
        [MILOCO_CLI, *args], env=env, text=True, capture_output=True,
        timeout=20, check=False,
    )
    output = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        raise DeviceActionError(output or "miloco-cli failed")
    return output


def parse_device_list(text: str) -> list[Device]:
    rows: list[Device] = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) != 5:
            continue
        did, name, room, category, status = (part.strip() for part in parts)
        if not did or not name:
            continue
        rows.append(Device(did, name, room, category, status == "online"))
    return rows


def parse_specs(text: str) -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    pattern = re.compile(r"^(prop|action)\.\d+\.\d+\s+([^|\s]+)\|([^|\s]+)(?:\|([^|\s]+))?(?:\|([^|\s]+))?")
    for raw in text.splitlines():
        match = pattern.match(raw.strip())
        if not match:
            continue
        kind, name, access, value_format, constraint = match.groups()
        specs[name] = {
            "kind": kind, "access": access, "format": value_format or "",
            "constraint": constraint or "",
        }
    return specs


def _shortlist(text: str, devices: list[Device]) -> list[Device]:
    safe = [d for d in devices if d.online and d.category not in _BLOCKED_CATEGORIES]
    exact = [d for d in safe if d.name in text]
    if exact:
        return exact[:12]
    room_hits = [d for d in safe if d.room and d.room in text]
    category_aliases = {
        "灯": {"light", "switch", "outlet"}, "空调": {"air-conditioner"},
        "窗帘": {"curtain"}, "风扇": {"fan"}, "插座": {"outlet"},
        "加湿器": {"humidifier"}, "除湿机": {"dehumidifier"},
        "净化器": {"air-purifier"},
    }
    wanted = set().union(*(values for word, values in category_aliases.items() if word in text))
    narrowed = [d for d in room_hits if not wanted or d.category in wanted]
    if narrowed:
        return narrowed[:12]
    by_category = [d for d in safe if d.category in wanted]
    return (by_category or safe)[:12]


def _model_plan(text: str, candidates: list[Device], ask: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
    catalog = [
        {"index": index, "name": d.name, "room": d.room, "category": d.category}
        for index, d in enumerate(candidates)
    ]
    schema = {
        "type": "object",
        "properties": {
            "device_index": {"type": "integer", "minimum": 0, "maximum": max(0, len(candidates) - 1)},
            "operation": {"type": "string", "enum": ["turn_on", "turn_off", "set_brightness", "set_temperature"]},
            "value": {"type": ["number", "boolean", "null"]},
        },
        "required": ["device_index", "operation", "value"],
        "additionalProperties": False,
    }
    payload = {
        "model": OLLAMA_MODEL, "stream": False, "think": False, "keep_alive": -1,
        "format": schema,
        "system": (
            "你是本地家居控制参数选择器，只输出JSON。只能选择目录中的device_index，"
            "不得输出DID、spec_name、命令或目录外设备。根据用户原话选择一个设备和普通控制操作。"
        ),
        "prompt": f"设备目录：{json.dumps(catalog, ensure_ascii=False)}\n用户原话：{text}",
        "options": {"temperature": 0, "seed": 0, "num_ctx": 2048, "num_predict": 50},
    }
    if ask is not None:
        return ask(payload)
    with httpx.Client(timeout=15) as client:
        response = client.post(OLLAMA_URL, json=payload)
        response.raise_for_status()
        outer = response.json()
    value = json.loads(str(outer.get("response") or ""))
    if not isinstance(value, dict):
        raise DeviceActionError("4B returned a non-object plan")
    return value


def _resolve_command(operation: str, value: Any, specs: dict[str, dict[str, Any]]) -> tuple[str, str, Any]:
    if operation in {"turn_on", "turn_off"}:
        desired = operation == "turn_on"
        if "on" in specs and specs["on"]["access"] in {"w", "wr"}:
            return "control", "on", desired
        action_name = "turn-on" if desired else "turn-off"
        if action_name in specs and specs[action_name]["kind"] == "action":
            return "action", action_name, None
    elif operation == "set_brightness" and "brightness" in specs:
        number = int(value)
        if 1 <= number <= 100 and specs["brightness"]["access"] in {"w", "wr"}:
            return "control", "brightness", number
    elif operation == "set_temperature":
        for name in ("target-temperature", "temperature"):
            if name in specs and specs[name]["access"] in {"w", "wr"}:
                number = float(value)
                if 16 <= number <= 30:
                    return "control", name, number
    raise DeviceActionError("设备不支持请求的普通控制属性")


def control_device(text: str, *, cli: Callable[[list[str]], str] = _cli, ask: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> str:
    devices = parse_device_list(cli(["device", "list"]))
    candidates = _shortlist(text, devices)
    if not candidates:
        raise DeviceActionError("没有找到安全且在线的候选设备")
    plan = _model_plan(text, candidates, ask)
    if set(plan) != {"device_index", "operation", "value"}:
        raise DeviceActionError("4B计划结构不合法")
    index = plan["device_index"]
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(candidates):
        raise DeviceActionError("4B选择了目录外设备")
    device = candidates[index]
    if device.category in _BLOCKED_CATEGORIES:
        raise DeviceActionError("该设备类别禁止本地Quick Tool控制")
    specs = parse_specs(cli(["device", "spec", device.did]))
    kind, spec_name, value = _resolve_command(str(plan["operation"]), plan["value"], specs)
    if kind == "action":
        cli(["device", "action", device.did, spec_name])
    else:
        rendered = str(value).lower() if isinstance(value, bool) else str(value)
        cli(["device", "control", device.did, spec_name, rendered])
    return f"好的，已{('打开' if plan['operation'] == 'turn_on' else '关闭') if plan['operation'] in {'turn_on', 'turn_off'} else '调整'}{device.room}{device.name}。"
