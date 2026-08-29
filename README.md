# Jarvis Home

> Work-in-progress open-source extraction of a local-first home AI orchestration system.

Jarvis Home connects a voice entry point with layered local arbitration, bounded Quick Tools, OpenClaw handoff, Miloco home perception/control, and an auditable capability-evolution pipeline.

## Status

This repository is an **offline staging workspace**. It is not yet published and does not contain production credentials, household databases, camera media, model weights, or runtime state.

## Documentation

- [Architecture and trust boundaries](docs/architecture.md)
- [MiGPT Vue fork preparation](docs/migpt-vue-fork.md)
- [Main-project license decision](docs/license-decision.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)
- [Security policy](SECURITY.md)

## Included components

- `jarvis-bridge/` — two-stage arbitration, execution plans, capability registry, feedback and evolution pipeline
- `integrations/` — external-integration boundaries and preparation documentation; external runtimes are not bundled
- `config/` — redacted configuration templates
- `scripts/` — doctor, test, security, build-verification and clean-clone checks
- `docs/` — architecture, licensing and integration documentation

## Roadmap

- Clean MiGPT Vue source fork pinned by immutable commit SHA
- Operator-focused macOS installation and service templates
- Optional Miloco and OpenClaw adapter setup guides
- Public release only after the private security contact and final provenance review are completed

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
bash scripts/verify_clean_clone.sh
```

The clean-clone command validates the current committed `HEAD`: it clones that revision into a temporary directory, installs only locked dependencies, runs all offline checks, builds distribution artifacts, installs the wheel, and deletes the temporary workspace. It does not include uncommitted or merely staged files. Release maintainers must first export a staged candidate into an isolated temporary commit before using the script as candidate-release evidence.

The release guard scans the Git staged snapshot when files are staged, otherwise the worktree. CI runs on macOS without production credentials or services.

## Current development target

The first release targets macOS on Apple Silicon and treats Ollama, OpenClaw, Miloco and MiGPT as separately installed dependencies. Miloco/MiMo components are not redistributed because their upstream terms are not an OSI-approved open-source license.

## License

Jarvis Home original code is licensed under the [MIT License](LICENSE). Third-party projects and assets retain their own terms; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Miloco is not included or redistributed and is supported only through a disabled-by-default, brand-neutral external HTTP boundary.
