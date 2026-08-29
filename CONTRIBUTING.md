# Contributing

Contributions are accepted under the project's MIT License. A private security contact must still be finalized before public release.

For local development:

```bash
uv sync --locked --extra test
uv run --locked --extra test bash scripts/test_all.sh
```

Requirements:

- Add a failing test before changing behavior.
- Keep the offline demo deterministic and free of network or device side effects.
- Never add real credentials, household names, device IDs, camera media, logs, databases, models, or runtime state.
- Keep Miloco, MiMo, OpenClaw, Ollama, MiGPT and AIRI as external dependencies unless their redistribution terms are explicitly approved.
- Run the release guard and inspect the staged diff before committing.
