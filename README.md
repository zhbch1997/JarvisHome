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

## Current development target

The first release will target macOS on Apple Silicon and treat Ollama, OpenClaw, Miloco and MiGPT as separately installed dependencies.

## License

License selection is pending a final third-party attribution review. Do not redistribute this staging tree yet.
