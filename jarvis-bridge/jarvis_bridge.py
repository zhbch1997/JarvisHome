"""Core routing engine for MiGPT -> Hermes Jarvis bridge."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")
DEFAULT_FALLBACK = "http://127.0.0.1:18082/v1/chat/completions"
LOCAL_MODEL_URL = "http://127.0.0.1:18085/v1/chat/completions"
OPENCLAW_URL = "http://127.0.0.1:18085/v1/chat/completions"
MILOCO_BIN = os.path.expanduser("~/.local/bin/miloco-cli")
logger = logging.getLogger("jarvis_bridge")

_NATIVE_PATTERNS = [
    re.compile(r"^(?:播放|放一?首|放点|来一?首|听|搜索并播放)"),
    re.compile(r"^(?:暂停|继续播放|继续|恢复播放|停止播放|停止音乐)(?:一下|吧|好吗)?$"),
    re.compile(r"^(?:上|下|换)一首$"),
    re.compile(r"^(?:把)?(?:音量|声音).*(?:调|设|开|关|大|小|静音)"),
    re.compile(r"^(?:调|设)(?:高|低|大|小)音量"),
    re.compile(r"^(?:设置?|取消|删除|查看).*(?:闹钟|倒计时|计时器)"),
    re.compile(r"^(?:倒计时|计时)(?:一下|\d|[一二三四五六七八九十百])"),
]

_HOME_REALTIME_TERMS = re.compile(
    r"摄像头|客厅|卧室|书房|厨房|卫生间|阳台|门锁|灯|空调|插座|"
    r"传感器|温度|湿度|空气|烟雾|燃气|米家|设备|家庭任务|宠物龟|宠物仓鼠|小管家"
)
_HOME_ACTION_TERMS = re.compile(
    r"现在|当前|在线|状态|查看|看看|查询|确认|有没有|几台|多少|打开|关闭|"
    r"开灯|关灯|调到|设置|执行|创建|取消|删除"
)


def extract_user_text(body: dict[str, Any]) -> str:
    for message in reversed(body.get("messages") or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text"]
            text = " ".join(parts).strip()
            if text:
                return text
    raise ValueError("user message is required")


def _strip_trigger(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^(?:小爱同学[，,、 ]*)", "", text)
    text = re.sub(r"^(?:请|你(?:帮我|给我|替我)?|贾维斯)[，,、:： ]*", "", text)
    return text.strip()


def is_native_intent(text: str) -> bool:
    normalized = _strip_trigger(text)
    return any(pattern.search(normalized) for pattern in _NATIVE_PATTERNS)


def is_visual_query(text: str) -> bool:
    """Whether a request explicitly asks to inspect a camera/live scene."""
    normalized = re.sub(r"\s+", "", text or "")
    subjects = ("摄像头", "画面", "看看", "看一下", "在干嘛", "怎么样", "有没有人")
    home_targets = ("乌龟", "宠物龟", "宠物仓鼠", "仓鼠", "示例房间", "客厅", "门口", "家里", "房间")
    return any(x in normalized for x in subjects) and any(x in normalized for x in home_targets)


def is_realtime_home_request(text: str) -> bool:
    normalized = re.sub(r"[\s，。！？,.!?]", "", text)
    return bool(
        _HOME_REALTIME_TERMS.search(normalized)
        and _HOME_ACTION_TERMS.search(normalized)
    )


_OPENCLAW_TERMS = re.compile(
    r"龟|乌龟|鱼缸|仓鼠|宠物仓鼠|跑轮|饮水器|晒背|宠物龟|"
    r"小爱|音箱|空调|窗帘|扫地|灯|开关|米家|家里|家庭|"
    r"摄像头|画面|实时|看看"
)
_OPENCLAW_ACTION_TERMS = re.compile(
    r"现在|当前|状态|查看|看看|查询|确认|有没有|多少|几台|"
    r"打开|关闭|开灯|关灯|调到|设置|执行|怎么样|在干嘛|有啥"
)

def is_openclaw_intent(text: str) -> bool:
    """Hardware/home intent that should go to OpenClaw instead of Hermes."""
    normalized = re.sub(r"[\s，。！？,.!?]", "", text)
    return bool(
        _OPENCLAW_TERMS.search(normalized)
        and _OPENCLAW_ACTION_TERMS.search(normalized)
    )


def default_run_openclaw(body: dict[str, Any]) -> str:
    """Call OpenClaw Core (local hardware brain)."""
    payload = dict(body)
    payload["model"] = "openclaw"
    payload["stream"] = False
    messages = list(payload.get("messages") or [])
    messages.insert(0, {
        "role": "system",
        "content": "你是贾维斯，用简短自然的中文回答家用设备状态，一两句话，不使用Markdown。",
    })
    payload["messages"] = messages[-10:]
    request = urllib.request.Request(
        OPENCLAW_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        data = json.loads(response.read())
    content = str(data["choices"][0]["message"].get("content") or "")
    # Strip thinking tags from OpenClaw too
    return re.sub(r"<think>[\s\S]*?</think>", "", content, flags=re.I).strip()


def parse_hermes_output(output: str, existing_session: Optional[str] = None) -> Tuple[Optional[str], str]:
    output = output.strip()
    match = re.match(r"^session_id:\s*([^\s]+)\s*(?:\n|$)", output)
    if match:
        return match.group(1), output[match.end():].strip()
    return existing_session, output


def parse_hermes_streams(
    stdout: str, stderr: str, existing_session: Optional[str] = None
) -> Tuple[Optional[str], str]:
    """Hermes quiet mode writes the answer to stdout and session_id to stderr."""
    sid = existing_session
    for stream in (stderr, stdout):
        match = re.search(r"(?m)^session_id:\s*([^\s]+)\s*$", stream or "")
        if match:
            sid = match.group(1)
            break
    answer = re.sub(r"(?m)^session_id:\s*[^\s]+\s*$", "", stdout or "").strip()
    return sid, answer


def sanitize_for_tts(text: str, max_chars: int = 240) -> str:
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.I)
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
    text = re.sub(r"[*_`~]", "", text)
    text = re.sub(r"(?m)^.*(?:工具调用|tool call|preparing ).*$", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip("，,；;：:。.!！？? ") + "。"
    return text


def openai_response(content: str, model: str, route: str) -> dict[str, Any]:
    now = int(time.time())
    return {
        "id": f"chatcmpl-jarvis-{now}",
        "object": "chat.completion",
        "created": now,
        "model": model or "hermes-jarvis",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "jarvis": {"route": route},
    }


def default_run_hermes(text: str, session_id: Optional[str]) -> Tuple[Optional[str], str]:
    command = [HERMES_BIN, "-p", "jarvis", "chat", "-Q", "--source", "tool", "-q", text]
    if session_id:
        command += ["--resume", session_id, "--no-restore-cwd"]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=75, shell=False)
    if proc.returncode != 0:
        details = "\n".join(
            line for line in (proc.stderr or "").splitlines()
            if not line.startswith("session_id:")
        ).strip()
        raise RuntimeError((details or proc.stdout or "Hermes failed").strip()[-1000:])
    sid, answer = parse_hermes_streams(proc.stdout, proc.stderr, session_id)
    if not answer:
        raise RuntimeError("Hermes returned an empty answer")
    return sid, answer


def default_run_fallback(body: dict[str, Any]) -> str:
    payload = dict(body)
    payload["model"] = "qwen3-4b-local-digest"
    payload["stream"] = False
    messages = list(payload.get("messages") or [])
    messages.insert(0, {
        "role": "system",
        "content": "你是贾维斯，示例用户的本地语音管家。用简短自然的中文回答，一到两句话，不使用Markdown。",
    })
    payload["messages"] = messages[-10:]
    request = urllib.request.Request(
        os.getenv("LOCAL_JARVIS_URL", LOCAL_MODEL_URL),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        data = json.loads(response.read())
    return str(data["choices"][0]["message"].get("content") or "")


def extract_miloco_answer(payload: Any) -> str:
    if isinstance(payload, dict):
        answer = payload.get("answer")
        if isinstance(answer, str) and answer.strip():
            return answer.strip()
        data = payload.get("data")
        if isinstance(data, dict):
            nested = data.get("answer")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
        if isinstance(data, str) and data.strip():
            return data.strip()
    return ""


def _explicit_live_camera_request(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text or "")
    return any(term in normalized for term in (
        "实时看", "实时查看", "现在看一下", "现在看看", "重新看", "打开摄像头", "当前画面",
    ))


def _latest_perception_log(stdout: str, room_name: str) -> tuple[str, str]:
    for line in reversed((stdout or "").splitlines()):
        match = re.match(r"^(.+?):\s+(\{.*\})$", line.strip())
        if not match:
            continue
        try:
            descriptions = json.loads(match.group(2))
        except json.JSONDecodeError:
            continue
        description = descriptions.get(room_name) if isinstance(descriptions, dict) else None
        if isinstance(description, str) and description.strip():
            timestamp = match.group(1)
            time_match = re.search(r"T(\d{2}:\d{2})", timestamp)
            return (time_match.group(1) if time_match else timestamp, description.strip())
    return "", ""


def default_run_home(text: str) -> str:
    """Read recent camera logs by default; capture a new view only when explicitly requested."""
    env = os.environ.copy()
    env.setdefault("MILOCO_HOME", os.path.expanduser("~/.hermes/miloco"))
    devices = subprocess.run(
        [MILOCO_BIN, "perceive", "devices"], capture_output=True, text=True,
        timeout=8, env=env, shell=False,
    )
    if devices.returncode != 0:
        raise RuntimeError("Miloco devices unavailable")
    rows = (json.loads(devices.stdout).get("data") or [])
    by_name = {row.get("name"): row for row in rows if row.get("online")}
    normalized = re.sub(r"\s+", "", text)
    if "摄像头" in normalized and any(x in normalized for x in ("几台", "多少", "列表", "有哪些")):
        cameras = [row for row in rows if row.get("online") and row.get("device_type") == "camera"]
        names_text = "、".join(str(row.get("name")) for row in cameras if row.get("name"))
        return f"家里有{len(cameras)}台在线摄像头：{names_text}。"
    names = []
    if any(x in normalized for x in ("宠物仓鼠", "示例房间")):
        names.append("示例摄像头A")
    if any(x in normalized for x in ("乌龟", "宠物龟", "示例摄像头B", "客厅")):
        names.append("示例摄像头B")
    if any(x in normalized for x in ("家里", "几个摄像头", "所有摄像头", "全部摄像头")):
        names = ["示例摄像头B", "示例摄像头A"]
    names = list(dict.fromkeys(names))
    if not names:
        raise RuntimeError("camera target not recognized")
    missing = [name for name in names if name not in by_name]
    if missing:
        raise RuntimeError("requested camera unavailable")
    if not _explicit_live_camera_request(text):
        logs = subprocess.run(
            [MILOCO_BIN, "perceive", "logs", "--since", "30m", "--jsonl"],
            capture_output=True, text=True, timeout=12, env=env, shell=False,
        )
        if logs.returncode != 0:
            raise RuntimeError("Miloco perception logs unavailable")
        room_name = "示例房间" if "示例摄像头A" in names else "客厅"
        log_time, description = _latest_perception_log(logs.stdout, room_name)
        if description:
            return f"最近一次感知是{log_time}：{description}"
        subject = "宠物仓鼠" if "示例摄像头A" in names else "乌龟"
        return f"最近的感知日志没有记录到{subject}的明确活动。"
    argv = [MILOCO_BIN, "perceive", "query"]
    for name in names:
        argv += ["--source", str(by_name[name]["did"])]
    argv += ["--query", text[:300]]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=50, env=env, shell=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("Miloco query failed")
    payload = json.loads(proc.stdout)
    answer = extract_miloco_answer(payload)
    if not answer:
        raise RuntimeError("Miloco returned an empty answer")
    return answer


class BridgeEngine:
    def __init__(
        self,
        state_path: Path,
        run_hermes: Callable[[str, Optional[str]], Tuple[Optional[str], str]] = default_run_hermes,
        run_fallback: Callable[[dict[str, Any]], str] = default_run_fallback,
        run_home: Callable[[str], str] = default_run_home,
        run_openclaw: Callable[[dict[str, Any]], str] = default_run_openclaw,
        local_only: bool = False,
    ) -> None:
        self.state_path = Path(state_path)
        self.run_hermes = run_hermes
        self.run_fallback = run_fallback
        self.run_home = run_home
        self.run_openclaw = run_openclaw
        self.local_only = local_only

    def _session_id(self) -> Optional[str]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8")).get("session_id")
        except Exception:
            return None

    def _save_session(self, session_id: Optional[str]) -> None:
        if not session_id:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(json.dumps({"session_id": session_id}, ensure_ascii=False), encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(self.state_path)

    def complete(self, body: dict[str, Any]) -> dict[str, Any]:
        text = extract_user_text(body)
        model = str(body.get("model") or "hermes-jarvis")
        if is_native_intent(text):
            return openai_response("", model, "native-silent")
        if self.local_only:
            # Camera/live-scene requests use deterministic Miloco first;
            # other home controls keep OpenClaw tool routing.
            if is_visual_query(text):
                try:
                    answer = sanitize_for_tts(self.run_home(text))
                    return openai_response(answer, model, "local-miloco")
                except Exception as exc:
                    logger.warning("Local Miloco route failed: %s: %s", type(exc).__name__, str(exc)[:300])
                    return openai_response("家庭服务暂时不可用，我暂时无法确认实时状态。", model, "home-unavailable")
            if is_openclaw_intent(text):
                try:
                    answer = sanitize_for_tts(self.run_openclaw(body))
                    return openai_response(answer or "我暂时没连上，请稍后再试。", model, "openclaw")
                except Exception as exc:
                    logger.warning("OpenClaw route failed: %s: %s", type(exc).__name__, str(exc)[:300])
            if is_realtime_home_request(text):
                try:
                    answer = sanitize_for_tts(self.run_home(text))
                    return openai_response(answer, model, "local-miloco")
                except Exception as exc:
                    logger.warning("Local Miloco route failed: %s: %s", type(exc).__name__, str(exc)[:300])
                    return openai_response("家庭服务暂时不可用，我暂时无法确认实时状态。", model, "home-unavailable")
            answer = sanitize_for_tts(self.run_fallback(body))
            return openai_response(answer or "我暂时没连上，请稍后再试。", model, "local-model")
        try:
            session_id, answer = self.run_hermes(text, self._session_id())
            self._save_session(session_id)
            answer = sanitize_for_tts(answer)
            if not answer:
                raise RuntimeError("empty sanitized answer")
            return openai_response(answer, model, "hermes")
        except Exception as exc:
            logger.warning("Hermes route failed: %s: %s", type(exc).__name__, str(exc)[:300])
            # Hermes failed: try OpenClaw for home/hardware intent, then fallback
            if is_openclaw_intent(text):
                try:
                    answer = sanitize_for_tts(self.run_openclaw(body))
                    return openai_response(answer or "我暂时没连上，请稍后再试。", model, "openclaw-fallback")
                except Exception as exc2:
                    logger.warning("OpenClaw fallback also failed: %s", str(exc2)[:200])
            if is_realtime_home_request(text):
                return openai_response(
                    "家庭服务暂时不可用，我暂时无法确认实时状态，请稍后再试。",
                    model,
                    "home-unavailable",
                )
            answer = sanitize_for_tts(self.run_fallback(body))
            return openai_response(answer or "我暂时没连上，请稍后再试。", model, "fallback")
