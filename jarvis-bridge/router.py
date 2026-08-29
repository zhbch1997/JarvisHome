"""Native speaker boundary and route decision types for Jarvis."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Route(str, Enum):
    NATIVE = "native"
    TASK = "task"
    CAMERA = "camera"
    HOME = "home"
    WEB_QUERY = "web_query"
    LOCAL_CHAT = "local_chat"


@dataclass(frozen=True)
class RouteDecision:
    route: Route
    intent: str
    rule_id: str
    risk: str = "none"
    confidence: float = 0.0
    capability: str = "none"


_NATIVE_PATTERNS = (
    re.compile(r"^(?:播放|放一?首|放点|来一?首|听|搜索并播放)"),
    re.compile(r"^(?:暂停|继续播放|继续|恢复播放|停止播放|停止音乐)(?:一下|吧|好吗)?$"),
    re.compile(r"^(?:上|下|换)一首$"),
    re.compile(r"^(?:把)?(?:音量|声音).*(?:调|设|开|关|大|小|静音)"),
    re.compile(r"^(?:设置?|取消|删除|查看).*(?:闹钟|倒计时|计时器)"),
    re.compile(r"^(?:倒计时|计时)(?:一下|\d|[一二三四五六七八九十百])"),
)


def _normalize_native(text: str) -> str:
    value = re.sub(r"^(?:小爱同学|贾维斯)[，,、:： ]*", "", (text or "").strip())
    return re.sub(r"[\s，。！？,.!?]", "", value)


class Router:
    """Only deterministic native-speaker commands bypass the learning router."""

    def decide(self, text: str) -> RouteDecision:
        normalized = _normalize_native(text)
        if any(pattern.search(normalized) for pattern in _NATIVE_PATTERNS):
            return RouteDecision(Route.NATIVE, "native", "native_explicit")
        # Availability fallback only. It must never infer task/camera/home semantics.
        return RouteDecision(Route.LOCAL_CHAT, "chat", "model_unavailable", "none")
