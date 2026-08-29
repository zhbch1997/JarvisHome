# MiGPT Vue integration

The production UI currently lives in a separate Git repository derived from MiGPT/MiGPT Vue. It must be published as a proper fork with the upstream MIT license and copyright retained.

Before release:

1. Create or select the public fork.
2. Port only the source-level Jarvis, AIRI, automation and evolution UI changes.
3. Keep the upstream `LICENSE` file.
4. Document the exact upstream revision and local modifications.
5. Reference the fork here by release tag or Git submodule.

Do not copy `node_modules`, databases, speaker credentials or generated application bundles into this repository.
