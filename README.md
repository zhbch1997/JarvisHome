# Jarvis Home

> Work-in-progress open-source extraction of a local-first home AI orchestration system.

Jarvis Home connects a voice entry point with layered local arbitration, bounded Quick Tools, OpenClaw handoff, Miloco home perception/control, and an auditable capability-evolution pipeline.

## Status

This repository is an **offline staging workspace**. It is not yet published and does not contain production credentials, household databases, camera media, model weights, or runtime state.

## Planned components

- `jarvis-bridge/` — two-stage arbitration, execution plans, capability registry, feedback and evolution pipeline
- `integrations/migpt-vue/` — MiGPT Vue fork/adapter metadata; upstream source will remain a separate repository
- `integrations/miloco/` — Miloco adapter and setup documentation
- `integrations/openclaw/` — OpenClaw adapter and setup documentation
- `config/` — redacted configuration templates
- `scripts/` — install, doctor, security and release checks
- `docs/` — architecture, privacy and deployment documentation

## Safety boundary

Do not commit real API keys, Xiaomi credentials, household profiles, camera clips, logs, databases, model caches, or OpenClaw/Miloco runtime state. Run `python3 scripts/release_guard.py` before every commit and release.

## Quick start (offline mock)

Requirements: macOS or Linux, Python 3.11+, and [uv](https://docs.astral.sh/uv/).

```bash
git clone <repository-url> jarvis-home
cd jarvis-home
uv sync --locked --extra test
uv run --locked --extra test python scripts/doctor.py
uv run --locked --extra test jarvis-home-demo
uv run --locked --extra test bash scripts/test_all.sh
```

The final command runs the complete test suite, the release guard, and a deterministic Mock Home demo. It does not connect to Miloco, OpenClaw, Ollama, cameras, or real smart-home devices.

Expected demo evidence:

```json
{"actions": [{"device_id": "demo-light-1", "property": "on", "value": true}], "adapter": "mock", "status": "ok"}
```

## Running the bridge

After installing dependencies:

```bash
cp config/.env.example .env.local
# Export only the variables you need; do not commit .env.local.
uv run jarvis-home
```

The default listener is `127.0.0.1:18083`. Starting the full bridge expects separately installed local arbitration services; use the offline Mock Home demo first.

## Verification

```bash
uv run --locked --extra test bash scripts/test_all.sh
```

The release guard scans the Git staged snapshot when files are staged, otherwise the worktree. CI runs on macOS without production credentials or services.

## Current development target

The first release targets macOS on Apple Silicon and treats Ollama, OpenClaw, Miloco and MiGPT as separately installed dependencies. Miloco/MiMo components are not redistributed because their upstream terms are not an OSI-approved open-source license.

## License

License selection is pending a final third-party attribution review. Do not redistribute this staging tree yet.
