# Open-source scope and allowlist

## Included in the staging extraction

- `jarvis-bridge/*.py`
- `jarvis-bridge/evolution_dashboard.html`
- Tests colocated in `jarvis-bridge/`
- New public documentation, examples, CI and release-check scripts

## Explicitly excluded

- `config.json` and all real secrets
- `jarvis-background/`
- `snapshots/`, recordings, images and videos
- `home-profile/`, `memory/`, `state/` and conversation/evolution ledgers
- `miloco.db*`, `observability.db*` and other databases
- `miot_cache/`, `coreml_cache/` and model files
- logs, certificates and production LaunchAgent files
- local OpenClaw and Miloco state directories
- `node_modules/` and generated frontend output

## External dependency policy

- MiGPT Vue remains a separate MIT-licensed source-level fork or pinned integration. Preserve its upstream license and attribution; do not vendor generated bundles or dependencies here.
- OpenClaw and Ollama remain separately installed external dependencies.
- Xiaomi Miloco and MiMo-VL-Miloco are governed by a custom non-commercial, use-restricted license. They are not distributed by this repository and are disabled by default; users must obtain them separately under Xiaomi's terms.
- No Miloco configuration copied from upstream, model weight, converted weight, container export, database, cache or household media may enter this repository.
- AIRI code, compiled output, Cubism components, Live2D/VRM models, fonts and other assets are excluded from the initial release. Every asset must pass an individual redistribution review before inclusion.
- See `THIRD_PARTY_NOTICES.md` for the current compliance inventory.

## Source provenance

The extraction source is the local production workspace, but this staging repository starts with a new clean Git history. Production state and old local history are not imported.
