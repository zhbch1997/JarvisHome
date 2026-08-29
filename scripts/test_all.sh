#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

export PYTHONDONTWRITEBYTECODE=1
export HOME="$TMP_ROOT/home"
export JARVIS_CAPABILITY_STATE="$TMP_ROOT/capabilities"
export JARVIS_FEEDBACK_STATE="$TMP_ROOT/feedback"
export OPENCLAW_STATE_DIR="$TMP_ROOT/openclaw"
mkdir -p "$HOME"

cd "$ROOT"
python -m unittest discover -s jarvis-bridge -p 'test_*.py'
python -m unittest discover -s tests -p 'test_*.py'
python scripts/release_guard.py
jarvis-home-demo
