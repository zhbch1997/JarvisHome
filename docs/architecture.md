# Jarvis Home architecture

## Scope

Jarvis Home is a local-first orchestration bridge. It accepts an OpenAI-compatible chat request, assigns exactly one execution owner, and streams the result back to the caller. The public repository contains the bridge kernel and an offline mock; it does not redistribute external runtimes, models, household state, or UI assets.

## Runtime flow

```text
Voice/UI client
    |
    | OpenAI-compatible HTTP
    v
Jarvis Bridge (FastAPI, loopback by default)
    |
    +--> Level 1 arbitration (small local model)
    |       +--> local chat
    |       +--> approved Quick Tool
    |       `--> handoff
    |
    `--> Level 2 arbitration (larger local model, only after handoff)
            +--> approved Quick Tool
            `--> OpenClaw

Quick Tool adapters ----> separately installed Miloco/device interfaces
OpenClaw adapter -------> separately installed OpenClaw runtime
Local chat/arbitration --> separately installed Ollama-compatible endpoints
```

## Components

### HTTP surface

`jarvis-bridge/api_server.py` exposes health, OpenAI-compatible chat completions, session/history views, capability inspection, validation, promotion, rollback, and evolution review endpoints.

The console entry point in `jarvis-bridge/jarvis_launcher.py` binds to `127.0.0.1:18083` by default. Binding to another interface is an explicit operator decision.

### Arbitration and execution ownership

`jarvis-bridge/arbitration.py` implements two-stage selection. `jarvis-bridge/execution_plan.py` converts an arbitration envelope into one authoritative `ExecutionPlan`.

Valid execution classes are:

- `local_chat`: answer with the configured local model;
- `quick_tool`: execute one registered, versioned capability;
- `openclaw`: hand the open-ended task to the external OpenClaw runtime.

A failed Quick Tool does not automatically escalate to unrestricted OpenClaw. The current kernel fails closed until a capability-bound fallback profile can enforce the same permission boundary.

### Capability registry

`jarvis-bridge/capability_registry.py` stores versioned capability bundles under an operator-selected state directory. Bundles define routing, selector eligibility, execution owner, permissions, delivery text, and review metadata. Writes use private permissions, file locks, temporary files, `fsync`, and atomic replacement.

Runtime state is never source material and must not be committed.

### Home adapters

`home_device_control.py` and `home_scene_control.py` constrain model output to catalog indexes and allowlisted semantic operations. Device identifiers and writable properties are re-read from the external adapter before execution. Cameras, locks, speakers, smoke alarms, and gas sensors are excluded from ordinary device Quick Tools.

`mock_home.py` is the only bundled home runtime. It is deterministic, performs no network calls, and records simulated actions in memory.

### External integrations

- **Ollama-compatible endpoints:** local arbitration and chat inference; no model weights are included.
- **OpenClaw:** external agent runtime for open-ended planning; no OpenClaw state or credentials are included.
- **External home HTTP backend:** optional, disabled-by-default client boundary for a separately installed compatible service. Miloco may be deployed independently by an operator, but Jarvis Home does not contain, download, install, start, embed, or redistribute it.
- **MiGPT Vue:** separate MIT-derived source fork used as a voice/UI entry point; not vendored into this repository.

## Trust boundaries

1. **Untrusted client input:** HTTP request bodies, session IDs, and capability IDs require validation.
2. **Model output:** treated as untrusted selection data; it cannot supply device IDs, commands, or arbitrary tool names.
3. **Capability bundles:** versioned and validated before becoming selectable.
4. **External runtimes:** separate processes with separate credentials and state; compromise is not contained by this repository alone.
5. **Local state:** private runtime data, excluded by `.gitignore` and `scripts/release_guard.py`.

## Security invariants

- Default network binding is loopback.
- One request has one execution owner.
- Quick Tools select only registered capabilities.
- Model output is never passed to a shell.
- High-risk device categories are excluded from ordinary local control.
- Quick Tool failure does not grant broader permissions.
- The offline demo has no external side effects.
- Git staged content is scanned fail-closed before commit/release.

## Persistence

The public kernel may create capability and feedback state beneath paths configured through environment variables. Tests redirect all state to temporary directories. Clean-clone verification must leave the repository free of state, bytecode, caches, databases, media, and models.

## Deployment boundary

The repository is not a turnkey copy of any private household deployment. Operators must separately install and configure chosen model servers, OpenClaw, MiGPT, and any optional home adapter. Production credentials belong outside the repository in operator-managed secret storage.
