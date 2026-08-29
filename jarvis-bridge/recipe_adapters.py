"""Registered, deterministic adapters used by capability recipes."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from recipe_runtime import RecipeExecutionError
from external_home_client import ExternalHomeClient, ExternalHomeConfigError


def parse_external_home_device_list(stdout: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in str(stdout or "").splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 5:
            continue
        device_id, name, room, category, status = parts[:5]
        rows.append({
            "id": device_id,
            "name": name,
            "room": room,
            "category": category,
            "online": status == "online",
        })
    return rows


def external_home_device_list() -> list[dict[str, Any]]:
    try:
        return ExternalHomeClient.from_env().list_devices()
    except ExternalHomeConfigError as exc:
        raise RecipeExecutionError(
            "data_source_disabled", "外部家庭后端未启用",
            fallback_allowed=False,
        ) from exc
    except Exception as exc:
        raise RecipeExecutionError(
            "data_source_unavailable", "外部家庭后端设备目录读取失败",
            fallback_allowed=True,
        ) from exc


def camera_inventory_zh(rows: Any) -> str:
    if not isinstance(rows, list) or not rows:
        return "设备目录里没有找到category为camera的摄像头。"
    details = "；".join(
        f"{item.get('name', '')}（{item.get('room', '')}，{'在线' if item.get('online') else '离线'}）"
        for item in rows
        if isinstance(item, dict)
    )
    if not details:
        return "设备目录里没有找到category为camera的摄像头。"
    return f"家里一共{len(rows)}台摄像头：{details}。"


def parse_external_home_perception_logs(stdout: str, room: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in str(stdout or "").splitlines():
        timestamp, separator, payload = line.partition(": ")
        if not separator:
            continue
        try:
            value = json.loads(payload)
            observed = datetime.fromisoformat(timestamp)
        except (json.JSONDecodeError, ValueError):
            continue
        description = value.get(room) if isinstance(value, dict) else None
        if isinstance(description, str) and description.strip():
            rows.append({"time": observed.isoformat(), "description": description.strip()})
    return rows[-5:]


def external_home_turtle_recent() -> list[dict[str, str]]:
    raise RecipeExecutionError("capability_not_configured", "公开版不内置宠物感知配方")


def external_home_hamster_recent() -> list[dict[str, str]]:
    raise RecipeExecutionError("capability_not_configured", "公开版不内置宠物感知配方")


def _recent_activity_zh(rows: Any, subject: str) -> str:
    if not isinstance(rows, list) or not rows:
        return f"最近6小时没有找到{subject}对应的明确感知记录。"
    latest = rows[-1]
    try:
        observed = datetime.fromisoformat(str(latest["time"]))
        now = datetime.now(observed.tzinfo)
        age_minutes = max(0, int((now - observed).total_seconds() // 60))
        time_text = observed.strftime("%m月%d日%H:%M:%S")
        age_text = "刚刚" if age_minutes < 1 else f"约{age_minutes}分钟前"
    except (KeyError, TypeError, ValueError):
        time_text, age_text = "时间未知", "时间间隔未知"
    summary = str(latest.get("description") or "").strip()
    return f"{subject}最近一次明确记录是{time_text}（{age_text}）：{summary}"


def turtle_recent_activity_zh(rows: Any) -> str:
    return _recent_activity_zh(rows, "宠物龟")


def hamster_recent_activity_zh(rows: Any) -> str:
    return _recent_activity_zh(rows, "宠物仓鼠")
