# Security policy

## Reporting

Do not open a public issue for a suspected credential, household-data, device-control, or remote-execution vulnerability. Contact the maintainers privately before disclosure. A dedicated security contact will be added before the first public release.

## Release boundary

- The repository must not contain production credentials, household profiles, camera media, model weights, runtime state, or device identifiers.
- Miloco, MiMo, OpenClaw, Ollama, MiGPT, AIRI, and model assets are separately installed dependencies and are not redistributed here.
- The offline demo is deterministic and performs no network or smart-home side effects.
- Run `uv run --locked --extra test bash scripts/test_all.sh` before every commit and release.

## Supported versions

No public release is supported yet. This repository remains a pre-release staging project.
