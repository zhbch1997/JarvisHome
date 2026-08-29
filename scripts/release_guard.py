#!/usr/bin/env python3
"""Fail closed when an open-source staging tree contains private/runtime data."""
from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    kind: str
    path: str
    detail: str


FORBIDDEN_NAMES = {
    "config.json",
    "supervisord.conf",
    "jarvis-background",
    "snapshots",
    "recordings",
    "home-profile",
    "miot_cache",
    "coreml_cache",
    "models",
    "node_modules",
    "secrets",
    "state",
    "home_profile",
    ".pytest_cache",
    "__pycache__",
}
FORBIDDEN_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".pem", ".key", ".mp4", ".mov", ".mkv",
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".wav", ".mp3", ".m4a",
    ".gguf", ".onnx", ".mlmodel", ".crt", ".cer", ".p12", ".pfx", ".plist",
}
SKIP_NAMES = {".git", ".venv"}
TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".vue", ".html", ".css", ".md", ".txt",
    ".json", ".yaml", ".yml", ".toml", ".sh", ".plist", ".example", "",
}
PERSONAL_PATH = re.compile(r"/Users/(?!example(?:/|$)|username(?:/|$)|yourname(?:/|$))[^/\s]+/")
QUOTED_CREDENTIAL_LITERAL = re.compile(
    r"(?i)[\"']?(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|client[_-]?secret)"
    r"[\"']?\s*[=:]\s*[\"']([^\"']{8,})[\"']"
)
ENV_CREDENTIAL_LITERAL = re.compile(
    r"(?im)^\s*(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|client[_-]?secret)"
    r"\s*=\s*([A-Za-z0-9._~+/=-]{8,})\s*$"
)
PRIVATE_KEY = re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY")
BEARER = re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]{12,}")
PLACEHOLDER_VALUES = {
    "changeme", "replace-me", "example", "placeholder", "your-token", "your-api-key",
    "test-token", "test-api-key",
}
HOUSEHOLD_SPECIFIC_TERMS = (
    "芝" + "麻糊", "龟" + "龟", "我滴" + "龟" + "龟", "电竞" + "房",
    "乌龟" + "灯", "觉觉" + "猪", "张" + "北辰",
    "宠物仓鼠" + "之眼", "我滴" + "宠物龟", "穹顶" + "之眼", "宠物" + "房",
)


def _is_forbidden(path: Path, relative: Path) -> bool:
    if any(part in FORBIDDEN_NAMES for part in relative.parts):
        return True
    name = path.name.lower()
    return (
        path.suffix.lower() in FORBIDDEN_SUFFIXES
        or name.endswith(".db-wal")
        or name.endswith(".db-shm")
        or name.endswith(".sqlite-wal")
        or name.endswith(".sqlite-shm")
        or name.endswith(".log")
    )


def _text_findings(path: Path, relative: Path) -> list[Finding]:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return _text_content_findings(text, relative)


def _text_content_findings(text: str, relative: Path) -> list[Finding]:
    findings: list[Finding] = []
    if PERSONAL_PATH.search(text):
        findings.append(Finding("personal_absolute_path", str(relative), "contains a personal macOS home path"))
    if any(term in text for term in HOUSEHOLD_SPECIFIC_TERMS):
        findings.append(Finding("household_specific_term", str(relative), "contains a private household term"))
    if PRIVATE_KEY.search(text):
        findings.append(Finding("private_key", str(relative), "contains a private-key block"))
    if BEARER.search(text):
        findings.append(Finding("bearer_literal", str(relative), "contains a bearer credential literal"))
    for pattern in (QUOTED_CREDENTIAL_LITERAL, ENV_CREDENTIAL_LITERAL):
        for match in pattern.finditer(text):
            value = match.group(2).strip().lower()
            if value not in PLACEHOLDER_VALUES and not value.startswith("${") and not value.startswith("test-"):
                findings.append(Finding("credential_literal", str(relative), f"contains literal {match.group(1)}"))
                return findings
    return findings


def scan_tree(root: Path) -> list[Finding]:
    root = root.resolve()
    findings: list[Finding] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.as_posix() == "scripts/release_guard.py":
            continue
        if any(part in SKIP_NAMES for part in relative.parts):
            continue
        if _is_forbidden(path, relative):
            findings.append(Finding("forbidden_path", str(relative), "runtime/private file or directory"))
            if path.is_dir():
                continue
        if path.is_file():
            findings.extend(_text_findings(path, relative))
    return findings


def tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=root, text=True, capture_output=True, check=False,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def _staged_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=root, text=True, capture_output=True, check=False,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def scan_staged(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for name in _staged_files(root):
        relative = Path(name)
        if relative.as_posix() == "scripts/release_guard.py":
            continue
        if any(part in FORBIDDEN_NAMES or part == ".venv" for part in relative.parts) or relative.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append(Finding("forbidden_path", name, "runtime/private staged path"))
            continue
        blob = subprocess.run(
            ["git", "show", f":{name}"], cwd=root, capture_output=True, check=False,
        )
        if blob.returncode != 0:
            findings.append(Finding("unreadable_staged_file", name, "cannot read staged blob"))
            continue
        try:
            text = blob.stdout.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(Finding("binary_staged_file", name, "staged file is not UTF-8 text"))
            continue
        findings.extend(_text_content_findings(text, relative))
    return findings


def scan_release(root: Path) -> tuple[list[Finding], str]:
    root = root.resolve()
    if _staged_files(root):
        return scan_staged(root), "staged"
    return scan_tree(root), "worktree"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    args = parser.parse_args()
    findings, source = scan_release(args.root)
    if findings:
        print("RELEASE GUARD: BLOCKED")
        for item in findings:
            print(f"- {item.kind}: {item.path} ({item.detail})")
        return 1
    print(f"RELEASE GUARD: PASS ({source})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
