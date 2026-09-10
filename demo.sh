#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' "Jarvis Home demo requires uv: https://docs.astral.sh/uv/"
  exit 1
fi

uv run --locked jarvis-home-demo --story
