# Third-party notices and distribution boundaries

This file is a pre-release compliance inventory. It is not a substitute for the license texts shipped by each upstream project.

## MiGPT / MiGPT Vue

- Upstream: https://github.com/idootop/mi-gpt
- License: MIT
- Copyright: Copyright (c) 2024 Del Wang
- Policy: publish UI changes through a separate source-level fork, retain the upstream MIT license and copyright, identify the pinned upstream revision, and describe modifications. Do not vendor generated bundles or `node_modules` in this repository.

## OpenClaw

- Upstream: https://github.com/openclaw/openclaw
- License observed during the audit: MIT
- Copyright observed during the audit: Copyright (c) 2026 OpenClaw Foundation
- Policy: optional external dependency only. If any source or binary is vendored later, include its license and applicable third-party notices.

## Xiaomi Miloco and MiMo-VL-Miloco

- Upstreams: https://github.com/XiaoMi/xiaomi-miloco and https://github.com/XiaoMi/xiaomi-mimo-vl-miloco
- License: Xiaomi custom Miloco license; non-commercial and use-restricted, not an OSI-approved open-source license.
- Policy: **not distributed by this repository**. Users must obtain and install these components separately under Xiaomi's terms. Do not publish Miloco source, configuration copied from Miloco, images, model weights, converted/quantized weights, container exports, or derived images without separate legal review or written permission.
- Project documentation must not imply Xiaomi endorsement or grant broader commercial rights.

## Ollama and local models

- Policy: runtime and model weights remain external downloads. Configuration may name a compatible model, but no blobs, GGUF files, Ollama stores, converted weights or caches are distributed here.
- Every downloadable model requires its own pinned provenance, checksum and license review before automation is added.

## AIRI, Live2D and VRM assets

- AIRI upstream: https://github.com/moeru-ai/airi
- AIRI code license observed during the audit: MIT
- Policy: no compiled AIRI distribution, Cubism SDK/Core, Live2D model, VRM avatar, font, icon or other bundled asset is distributed in the initial release. Each asset requires individual redistribution verification. A later integration should build AIRI from a pinned upstream source revision or require users to install it separately.

## Release rule

The source archive, MiGPT fork, application bundle, container image and model download are separate distribution surfaces. Each must have its own bill of materials and license review before publication.
