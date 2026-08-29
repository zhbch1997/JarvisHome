"""Direct Ollama adapter for short, streaming voice conversation."""
from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

import httpx


OLLAMA_CHAT_URL = os.getenv(
    "JARVIS_OLLAMA_URL", "http://127.0.0.1:11434/v1/chat/completions"
)
OLLAMA_MODEL = os.getenv("JARVIS_LOCAL_MODEL", "qwen35-4b-16k:latest")
LOCAL_CHAT_MAX_TOKENS = int(os.getenv("JARVIS_LOCAL_MAX_TOKENS", "160"))

GENERAL_CHAT_PROMPT = """你是贾维斯，正在通过小爱音箱和主人语音对话。
直接回答用户当前的问题，使用自然、简短、口语化的中文，通常一到两句话。
第一句不超过20个汉字，先说核心结论并以完整中文句号结束，再用第二句补充必要解释。
只使用规范中文和常见中文标点，不夹杂英文单词、标签、井号或奇怪符号。
不得声称正在查看，也不得编造家庭、宠物或设备的当前状态；缺少真实工具证据时，只能说明需要查询实时记录。
不要解释你的职责范围，不要输出分析过程、Markdown、角色名前缀或括号动作。"""


def _chat_prompt(surface: str) -> str:
    if str(surface or "").strip().lower() == "airi":
        return GENERAL_CHAT_PROMPT.replace(
            "正在通过小爱音箱和主人语音对话",
            "正在通过AIRI数字形象界面和主人交流；如果主人询问你在哪里或通过什么界面交流，必须原样回答“AIRI数字形象界面”，不要猜测具体终端设备",
        )
    return GENERAL_CHAT_PROMPT


def build_payload(body: dict[str, Any]) -> dict[str, Any]:
    messages = [
        message for message in (body.get("messages") or [])
        if isinstance(message, dict) and message.get("role") in {"user", "assistant"}
    ][-10:]
    return {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "system", "content": _chat_prompt(body.get("surface", "speaker"))}, *messages],
        "stream": bool(body.get("stream")),
        "reasoning_effort": "none",
        "max_tokens": LOCAL_CHAT_MAX_TOKENS,
        "temperature": float(body.get("temperature", 0.7)),
        "options": {
            "temperature": float(body.get("temperature", 0.7)),
            "num_ctx": 16384,
            "num_predict": LOCAL_CHAT_MAX_TOKENS,
        },
    }


def normalize_sse_line(line: str) -> str | None:
    stripped = str(line or "").strip()
    if not stripped.startswith("data: "):
        return None
    payload = stripped[6:].strip()
    if payload == "[DONE]":
        return "data: [DONE]\n\n"
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return f"data: {json.dumps(parsed, ensure_ascii=False, separators=(',', ':'))}\n\n"


class LocalChatBackend:
    def __init__(self, url: str = OLLAMA_CHAT_URL, timeout: float = 60.0) -> None:
        self.url = url
        self.timeout = timeout

    async def stream(self, body: dict[str, Any]) -> AsyncIterator[str]:
        payload = build_payload({**body, "stream": True})
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST", self.url, json=payload,
                headers={"Content-Type": "application/json"},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    event = normalize_sse_line(line)
                    if event is not None:
                        yield event

    async def complete(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = build_payload({**body, "stream": False})
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.url, json=payload)
            response.raise_for_status()
            return response.json()
