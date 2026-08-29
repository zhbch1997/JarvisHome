# MiGPT Vue fork preparation

## Decision

MiGPT Vue remains a **separate source repository**. Jarvis Home will not vendor its Git history, `node_modules`, generated bundles, Electron packages, AIRI distributions, models, or character assets.

## Verified local source baseline

The existing development checkout identifies itself as `mi-gpt` version `1.0.0`, is based on MIT-licensed MiGPT work, and retains:

- `Copyright (c) 2024 Del Wang`
- the complete MIT permission and warranty text

The inspected local commit was:

```text
09db82d8d285ad51d5412cb9371dccb3ee571468
```

This hash is audit evidence only, not yet the public integration pin. The checkout has uncommitted source and test changes, so it must not be copied, archived, tagged, or pushed as-is.

## Required fork workflow

1. Create a new clean clone of the chosen upstream/fork source.
2. Add the original repository as `upstream` and preserve its history.
3. Reapply only source-level Jarvis integration changes as reviewable commits.
4. Keep the upstream `LICENSE` file and copyright notice unchanged.
5. Add a `NOTICE` or fork README section describing Jarvis-specific modifications.
6. Exclude generated `dist/`, `node_modules/`, Electron packages, AIRI web distributions, Live2D/VRM assets, Cubism SDK files, logs, local databases, and credentials.
7. Run Node tests and a clean frontend build from locked dependencies.
8. Scan the complete fork history and build artifacts for credentials and household data.
9. Publish the fork first; record its immutable commit SHA in Jarvis Home documentation.
10. Integrate by pinned external revision or Git submodule only after the fork is clean and public.

## Source changes eligible for review

- OpenAI-compatible Jarvis endpoint configuration
- Session continuity and dashboard source code
- Wake/continuous-conversation controls
- TTS interruption and duplicate-submit protection
- AIRI proxy integration code, provided no generated AIRI bundle or restricted asset is included
- Tests and public documentation

## Excluded from redistribution

- MiGPT compiled patch bundles copied from upstream build output
- AIRI generated distribution
- Live2D, VRM, Cubism, voice, or character assets without verified redistribution terms
- Household names, devices, rooms, camera data, logs, and runtime databases
- Any Miloco/MiMo code, model, or configuration

## Integration manifest

Until a clean fork exists, the Jarvis Home integration remains documentation-only. A future manifest should record:

```yaml
name: migpt-vue
repository: https://github.com/<owner>/<clean-fork>
revision: <40-character-commit-sha>
license: MIT
included: false
```

No floating branch or tag should be used for reproducible installation.
