"""Built-in safe baseline bundles installed before state-backed evolution."""
from __future__ import annotations

import os
from typing import Any


def camera_inventory_bundle() -> dict[str, Any]:
    return {
        "id": "camera_inventory", "version": 1, "status": "active",
        "routing": {
            "route": "home", "operation": "query",
            "examples": ["家里有几台摄像头", "有哪些摄像头", "摄像头清单"],
        },
        "execution": {
            "primary": "bridge_recipe", "producer": "external_home",
            "fallback": "openclaw", "recipe": "camera_inventory_v1",
        },
        "recipe": {
            "tool": "external_home.device_list",
            "transforms": [
                {"op": "filter_eq", "field": "category", "value": "camera"},
            ],
            "response_template": "camera_inventory_zh",
        },
        "permissions": {
            "risk": "read_only", "allowed_tools": ["external_home.device_list"],
            "forbidden_tools": ["external_home.device_action", "shell", "web"],
        },
        "delivery": {
            "transition": "好的主人，我查一下。",
            "progress": "我正在读取设备目录。",
        },
        "review": {
            "minimum_samples": 20, "fact_consistency": 1.0,
            "tool_argument_accuracy": 1.0,
        },
    }


def pet_recent_bundle(
    capability_id: str, subject: str, tool: str, template: str,
) -> dict[str, Any]:
    examples = [
        f"{subject}在干嘛", f"看看{subject}在干嘛", f"{subject}最近在做什么",
        f"{subject}在哪里", f"{subject}出来了吗",
    ]
    return {
        "id": capability_id, "version": 1, "status": "active",
        "routing": {
            "route": "camera", "operation": "camera_recent", "examples": examples,
        },
        "selector": {
            "tier": "08b_eligible",
            "description": f"只读查询{subject}对应房间的最近感知记录，不打开实时摄像头",
            "positive_examples": examples,
            "dangerous_negatives": [
                f"现在打开摄像头看看{subject}", f"实时看看{subject}",
                f"{subject}为什么喜欢这样", "家里有几台摄像头",
            ],
            "min_confidence": {"0.8b": 0.95, "4b": 0.95},
        },
        "taxonomy": {
            "domain_tags": ["camera", "pet_care"],
            "execution_kind": "deterministic_query",
        },
        "execution": {
            "primary": "bridge_recipe", "producer": "external_home",
            "fallback": None, "recipe": f"{capability_id}_v1",
        },
        "recipe": {"tool": tool, "transforms": [], "response_template": template},
        "permissions": {
            "risk": "read_only", "allowed_tools": [tool],
            "forbidden_tools": ["external_home.perceive_query", "external_home.device_action", "shell", "web"],
        },
        "delivery": {
            "transition": f"好的主人，我查一下{subject}最近的记录。",
            "progress": f"我正在读取{subject}最近的感知记录。",
        },
        "review": {
            "minimum_samples": 20, "fact_consistency": 1.0,
            "tool_argument_accuracy": 1.0,
        },
    }


def turtle_recent_activity_bundle() -> dict[str, Any]:
    return pet_recent_bundle(
        "turtle_recent_activity", "宠物龟", "external_home.turtle_recent",
        "turtle_recent_activity_zh",
    )


def hamster_recent_activity_bundle() -> dict[str, Any]:
    return pet_recent_bundle(
        "hamster_recent_activity", "宠物仓鼠", "external_home.hamster_recent",
        "hamster_recent_activity_zh",
    )


def camera_inventory_quick_bundle() -> dict[str, Any]:
    bundle = camera_inventory_bundle()
    bundle["id"] = "camera_inventory_quick"
    bundle["execution"] = {
        "primary": "bridge_recipe", "producer": "external_home",
        "fallback": None, "recipe": "camera_inventory_quick_v1",
    }
    bundle["selector"] = {
        "tier": "4b_eligible",
        "description": "只读查询摄像头数量、名称或清单；不打开摄像头",
        "positive_examples": [
            "家里有几台摄像头", "有几个摄像头", "有哪些摄像头", "摄像头清单",
        ],
        "dangerous_negatives": [
            "现在打开摄像头看看", "看看宠物龟在干嘛", "查看摄像头画面",
        ],
        "min_confidence": {"0.8b": 0.99, "4b": 0.90},
    }
    bundle["taxonomy"] = {
        "domain_tags": ["home", "camera"],
        "execution_kind": "deterministic_query",
    }
    return bundle


def home_device_action_bundle() -> dict[str, Any]:
    examples = [
        "打开客厅灯", "关闭客厅灯", "打开客厅空调", "把主卧空调调到二十六度",
        "把饲养灯亮度调到百分之五十",
    ]
    return {
        "id": "home_device_action", "version": 1, "status": "active",
        "routing": {"route": "home", "operation": "action", "examples": examples},
        "selector": {
            "tier": "4b_eligible",
            "description": "控制已授权的米家设备开关、温度、亮度等普通属性",
            "positive_examples": examples,
            "dangerous_negatives": [
                "购买一台空调", "删除全部自动化", "打开摄像头看画面",
                "把门锁打开", "关闭烟雾报警器", "支付电费",
            ],
            "min_confidence": {"0.8b": 0.99, "4b": 0.95},
        },
        "taxonomy": {
            "domain_tags": ["home", "device_control"],
            "execution_kind": "device_action",
        },
        "execution": {
            "primary": "local_4b_device", "producer": "local_4b",
            "fallback": None, "recipe": "home_device_action_v1",
        },
        "recipe": {
            "tool": "external_home.device_action", "transforms": [],
            "response_template": "device_action_zh",
        },
        "permissions": {
            "risk": "device_action", "allowed_tools": ["external_home.device_action"],
            "forbidden_tools": ["shell", "web", "payment", "door_lock", "camera_live"],
        },
        "delivery": {
            "transition": "好的主人，我来控制设备。",
            "progress": "本地4B正在匹配设备并执行。",
        },
        "review": {
            "minimum_samples": 5, "fact_consistency": 1.0,
            "tool_argument_accuracy": 1.0,
        },
    }


def home_scene_action_bundle() -> dict[str, Any]:
    examples = [
        "空调睡眠24度", "执行空调睡眠24度场景", "主卧空调睡眠24度",
        "空调睡眠24度，仅限主卧", "空调睡眠25度", "空调睡眠26度",
        "该睡觉了", "执行该睡觉了场景", "准备睡觉", "主卧起床",
        "打开电视", "关闭电视", "客厅新风", "执行早安场景",
    ]
    return {
        "id": "home_scene_action", "version": 1, "status": "active",
        "routing": {"route": "home", "operation": "action", "examples": examples},
        "selector": {
            "tier": "4b_eligible",
            "description": "触发实时米家场景目录中明确授权的家庭场景",
            "positive_examples": examples,
            "dangerous_negatives": [
                "执行离家场景", "执行全屋晚安", "打开门锁", "删除自动化",
                "创建一个空调睡眠场景", "修改空调睡眠场景",
            ],
            "min_confidence": {"0.8b": 0.99, "4b": 0.95},
        },
        "taxonomy": {"domain_tags": ["home", "scene_control"], "execution_kind": "scene_action"},
        "execution": {
            "primary": "local_4b_scene", "producer": "local_4b",
            "fallback": None, "recipe": "home_scene_action_v1",
        },
        "recipe": {
            "tool": "external_home.scene_trigger", "transforms": [],
            "response_template": "scene_action_zh",
        },
        "permissions": {
            "risk": "scene_action", "allowed_tools": ["external_home.scene_trigger"],
            "forbidden_tools": [
                "shell", "web", "payment", "door_lock", "camera_live",
                "scene_create", "scene_update", "scene_delete",
            ],
        },
        "delivery": {
            "transition": "好的主人，我来执行空调睡眠场景。",
            "progress": "本地4B正在匹配已授权的米家场景。",
        },
        "review": {
            "minimum_samples": 5, "fact_consistency": 1.0,
            "tool_argument_accuracy": 1.0,
        },
    }


def install_defaults(registry: Any, *, external_home_enabled: bool | None = None) -> None:
    if external_home_enabled is None:
        external_home_enabled = os.getenv("JARVIS_EXTERNAL_HOME_ENABLED", "0").strip().lower() in {
            "1", "true", "yes", "on",
        }
    if not external_home_enabled:
        return
    # External-home capabilities are registered only after explicit opt-in.
    for bundle in (
        camera_inventory_bundle(), camera_inventory_quick_bundle(),
        turtle_recent_activity_bundle(),
        hamster_recent_activity_bundle(), home_device_action_bundle(),
        home_scene_action_bundle(),
    ):
        if registry.active(bundle["id"]) is None:
            registry.install_initial(bundle)
