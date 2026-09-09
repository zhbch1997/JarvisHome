# Contributing to Jarvis Home

Jarvis Home accepts bug fixes, tests, documentation, and small integrations under the MIT License. Open an issue before starting a large feature or changing a trust boundary.

## Set up the repository

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/zhbch1997/JarvisHome.git
cd JarvisHome
uv sync --locked --extra test
uv run --locked --extra test jarvis-home-demo
```

Run the full offline suite:

```bash
uv run --locked --extra test bash scripts/test_all.sh
```

## Make a change

1. Create a focused branch from `main`.
2. Add a failing test before changing behavior.
3. Keep the offline demo deterministic and free of network or device side effects.
4. Update the relevant documentation.
5. Run the full test suite and release guard.
6. Inspect the complete staged diff before opening a pull request.

```bash
uv run --locked --extra test bash scripts/test_all.sh
python3 scripts/release_guard.py
git diff --cached
```

A pull request should explain what changed, how it was tested, and whether it affects a security or device-control boundary.

## Privacy and repository boundaries

Never commit real credentials, household names, device identifiers, camera media, conversations, logs, databases, model files, or runtime state. Use obvious fictional values in tests and examples.

Miloco, MiMo, OpenClaw, Ollama, MiGPT, and AIRI remain external projects unless redistribution terms and repository scope are explicitly approved. Do not copy code, assets, model files, or private deployment configuration from those projects into this repository.

The generic external-home integration must remain disabled by default. Tests must use loopback fixtures or the in-memory Mock Home, not a real household service.

If you find a vulnerability, follow [`SECURITY.md`](SECURITY.md) instead of opening a public issue.

## Pull request checklist

- [ ] The change has focused tests.
- [ ] The full offline suite passes.
- [ ] `scripts/release_guard.py` passes.
- [ ] The change does not include private or runtime data.
- [ ] New integrations are disabled by default and documented.
- [ ] User-facing behavior and roadmap status are accurate.
