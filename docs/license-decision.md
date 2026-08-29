# Main-project license decision

## Decision

Jarvis Home original code is licensed under the MIT License. The canonical terms are in the repository-root `LICENSE` file and the package metadata uses the SPDX expression `MIT`.

Third-party components do not automatically inherit the main-project license. Their own terms and attribution remain controlling.

## Option A: MIT

- Short and widely understood.
- Permits commercial use, modification, redistribution, sublicensing, and private use.
- Requires preservation of copyright and license text.
- Includes warranty/liability disclaimer.
- Does not include an explicit patent grant.

Best when maximizing adoption and minimizing legal/process overhead is the priority.

## Option B: Apache License 2.0

- Permissive and allows commercial use, modification, redistribution, and private use.
- Includes an explicit contributor patent grant and patent-termination clause.
- Requires preservation of license and notices and marking modified files.
- More detailed compliance obligations than MIT.

Best when an explicit patent framework and structured contribution terms are important.

## Important boundary

Choosing MIT or Apache-2.0 for Jarvis Home would cover only original Jarvis Home code that the owner has the right to license. It would not relicense or authorize redistribution of:

- Miloco or MiMo components under Xiaomi custom/restricted terms;
- MiGPT/MiGPT Vue code, which retains its upstream MIT attribution;
- OpenClaw, Ollama, models, containers, and separately installed dependencies;
- AIRI, Cubism, Live2D/VRM, voice, or character assets;
- private household data, credentials, runtime state, logs, or media.

## Pre-publication checklist

- [x] Select MIT for original Jarvis Home code.
- [ ] Confirm all committed source is owned by the licensor or validly derived.
- [x] Add the chosen `LICENSE` file.
- [x] Update `pyproject.toml` with the SPDX expression.
- [ ] Update README status and contribution policy.
- [ ] Finalize `THIRD_PARTY_NOTICES.md`.
- [ ] Add a private security contact.
- [ ] Rebuild wheel/sdist and inspect included files.
- [ ] Run release guard and clean-clone verification.

This document is an engineering comparison, not legal advice.
