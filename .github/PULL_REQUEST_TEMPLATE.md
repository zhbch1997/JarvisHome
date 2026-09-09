## What changed

Describe the behavior change and why it belongs in Jarvis Home.

## How it was verified

List the exact commands and results. Do not paste private logs.

```text
uv run --locked --extra test bash scripts/test_all.sh
python3 scripts/release_guard.py
```

## Boundary impact

- Does this connect to an external service?
- Can it affect a real device?
- Does it change authentication, routing, permissions, or failure behavior?
- Is it disabled by default?

## Checklist

- [ ] I added or updated focused tests.
- [ ] The full offline suite passes.
- [ ] The release guard passes.
- [ ] I removed credentials, private household data, logs, media, and runtime state.
- [ ] External integrations remain disabled by default unless the existing contract says otherwise.
- [ ] Documentation describes what is implemented rather than planned.
