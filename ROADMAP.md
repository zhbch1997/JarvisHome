# Jarvis Home roadmap

This roadmap separates code that is already reproducible from ideas that still need design or integration work. A checked item must have tests, documentation, and a clean-clone verification path. A production setup outside this repository does not count as a released feature.

## Available now

- [x] OpenAI-compatible bridge API with streaming and non-streaming responses
- [x] Two-tier local arbitration
- [x] One-executor ownership across local chat, Quick Tools, and OpenClaw
- [x] Versioned capabilities with validation, promotion, rollback, and audit records
- [x] Constrained device and scene planning
- [x] Deterministic offline Mock Home
- [x] Generic external-home HTTP integration, disabled by default
- [x] Locked builds, clean-clone verification, and release privacy scanning

## 0.1 alpha: make the core easy to evaluate

- [ ] Publish a short demo captured from a clean, non-private environment
- [ ] Add a minimal Ollama-compatible local-chat example
- [ ] Provide a single documented command for starting the evaluated stack
- [ ] Verify setup on a clean macOS account and a Linux environment
- [ ] Publish the first prerelease with checksums and concise release notes

The demo must label simulated and real components. It must not include household names, device identifiers, credentials, camera media, or private conversations.

## 0.2: installation and operations

- [ ] Add a reversible macOS installer and uninstaller
- [ ] Add service status and diagnostic commands
- [ ] Document backup, upgrade, and rollback procedures
- [ ] Add bounded log rotation and redacted diagnostics export
- [ ] Define a stable configuration migration policy

## 0.3: integrations

- [ ] Publish a vendor-neutral home-backend contract with a conformance test kit
- [ ] Maintain the MiGPT Vue voice frontend as a separate, pinned fork
- [ ] Document shared-session access for trusted mobile clients
- [ ] Define the AIRI presentation-layer interface without moving agent ownership into the UI
- [ ] Add more tested examples for OpenClaw handoff

## Capability safety work

These requirements apply throughout the roadmap:

- A model may select only registered capabilities and catalog entries.
- Quick Tool failure must not silently escalate privileges.
- Risky operations require an explicit confirmation boundary.
- Promotion requires replay, shadow evaluation, negative cases, and rollback evidence.
- Tests and examples must not contact real devices by default.

## How to help

Look for issues labeled [`good first issue`](https://github.com/zhbch1997/JarvisHome/labels/good%20first%20issue) or [`help wanted`](https://github.com/zhbch1997/JarvisHome/labels/help%20wanted). Before proposing a large integration, open an issue describing the trust boundary, external dependencies, and a way to test it without private infrastructure.
