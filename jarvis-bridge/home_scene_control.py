"""Constrained local-4B selector for an allowlisted family of Miloco scenes."""
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
_ALLOWED_SCENE_NAMES = {
    "晚安", "准备睡觉", "卧室-起床", "打开电视", "关闭电视",
    "起居室新风", "早安", "空调睡眠24度", "空调睡眠25度", "空调睡眠26度",
}


class SceneActionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Scene:
    scene_id: str
    name: str


def _cli(args: list[str]) -> str:
    env = os.environ.copy()
    env["MILOCO_HOME"] = MILOCO_HOME
    result = subprocess.run(
        [MILOCO_CLI, *args], env=env, text=True, capture_output=True,
        timeout=20, check=False,
    )
    output = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        raise SceneActionError(output or "miloco-cli failed")
    return output


def parse_scene_list(text: str) -> list[Scene]:
    try:
        payload = json.loads(str(text or ""))
    except json.JSONDecodeError as exc:
        raise SceneActionError("米家场景列表格式不正确") from exc
    rows = payload.get("scenes") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise SceneActionError("米家场景列表格式不正确")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        scene_id = str(row.get("scene_id") or "").strip()
        name = str(row.get("scene_name") or "").strip()
        if scene_id and name in _ALLOWED_SCENE_NAMES:
            result.append(Scene(scene_id, name))
    return result


def _model_plan(text: str, scenes: list[Scene], ask: Callable[[dict[str, Any]], dict[str, Any]] | None) -> dict[str, Any]:
    catalog = [{"index": index, "name": scene.name} for index, scene in enumerate(scenes)]
    schema = {
        "type": "object",
        "properties": {"scene_index": {"type": "integer", "minimum": 0, "maximum": max(0, len(scenes) - 1)}},
        "required": ["scene_index"],
        "additionalProperties": False,
    }
    payload = {
        "model": OLLAMA_MODEL, "stream": False, "think": False, "keep_alive": -1,
        "format": schema,
        "system": "你是本地米家场景选择器，只输出JSON。只能按用户原话选择目录中的scene_index；不得输出scene_id、命令或目录外场景。",
        "prompt": f"已授权场景目录：{json.dumps(catalog, ensure_ascii=False)}\n用户原话：{text}",
        "options": {"temperature": 0, "seed": 0, "num_ctx": 1024, "num_predict": 20},
    }
    if ask is not None:
        return ask(payload)
    with httpx.Client(timeout=15) as client:
        response = client.post(OLLAMA_URL, json=payload)
        response.raise_for_status()
        outer = response.json()
    value = json.loads(str(outer.get("response") or ""))
    if not isinstance(value, dict):
        raise SceneActionError("4B返回了无效场景计划")
    return value


def trigger_ac_sleep_scene(
    text: str, *, cli: Callable[[list[str]], str] = _cli,
    ask: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> str:
    scenes = parse_scene_list(cli(["scene", "list"]))
    if not scenes:
        raise SceneActionError("没有找到已授权的空调睡眠场景")
    normalized = re.sub(r"[\s，。！？、,.!?；;：:]", "", str(text or ""))
    requested = [scene for scene in scenes if scene.name in normalized]
    if not requested:
        raise SceneActionError("请求不属于已授权的空调睡眠场景")
    plan = _model_plan(text, scenes, ask)
    if set(plan) != {"scene_index"}:
        raise SceneActionError("4B场景计划结构不合法")
    index = plan["scene_index"]
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(scenes):
        raise SceneActionError("4B选择了目录外场景")
    scene = scenes[index]
    if scene not in requested:
        raise SceneActionError("4B选择与用户请求不一致")
    cli(["scene", "trigger", scene.scene_id])
    return f"好的，已执行{scene.name}场景。"
