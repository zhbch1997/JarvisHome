#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="${1:-$ROOT}"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

export UV_CACHE_DIR="$TMP_ROOT/uv-cache"
export PYTHONDONTWRITEBYTECODE=1

git clone --quiet --no-hardlinks "$SOURCE" "$TMP_ROOT/repo"
cd "$TMP_ROOT/repo"

uv sync --locked --extra test
uv run --locked --extra test python scripts/doctor.py
uv run --locked --extra test bash scripts/test_all.sh
uv build

wheel="$(find dist -maxdepth 1 -name '*.whl' -print -quit)"
if [[ -z "$wheel" ]]; then
  echo "clean-clone verification failed: wheel not found" >&2
  exit 1
fi

uv pip install --python .venv/bin/python --reinstall --no-deps "$wheel"
.venv/bin/jarvis-home-demo
.venv/bin/python -c 'import api_server, jarvis_launcher, mock_home; print("clean_clone_wheel_import=PASS")'

echo "CLEAN CLONE: PASS"
