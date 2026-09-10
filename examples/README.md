# Local model example

This example sends one non-streaming chat request to an Ollama-compatible OpenAI endpoint on the local machine. It does not start Jarvis Bridge, OpenClaw, or a home backend, and it cannot control a device.

## Run it

Install [Ollama](https://ollama.com/), then download a small public model:

```bash
ollama pull qwen3:0.6b
```

With Ollama running on its default loopback port:

```bash
uv run python examples/ollama_local_chat.py "Reply with: Jarvis Home is ready."
```

Ollama returns an OpenAI-compatible response shaped like this:

```json
{"choices":[{"message":{"role":"assistant","content":"Jarvis Home is ready."}}]}
```

The example prints only the assistant `content` field:

```text
Jarvis Home is ready.
```

Model wording can vary. The command succeeds when it prints a non-empty assistant response.

Choose another installed model explicitly:

```bash
uv run python examples/ollama_local_chat.py \
  --model your-local-model \
  "In one sentence, explain what a local-first assistant is."
```

The script accepts only literal HTTP loopback IP endpoints (`127.0.0.0/8` or `::1`). It rejects hostnames, remote addresses, HTTPS, embedded credentials, query strings, fragments, redirects, and environment proxy settings. Network and response failures return a generic diagnostic rather than echoing the endpoint or response body.

## Clean up

Stop the foreground Ollama process with `Ctrl-C` if you started one for this check. Remove the optional example model when you no longer need it:

```bash
ollama rm qwen3:0.6b
```

Removing the model is optional and does not affect Jarvis Home source files.

## What this proves

This is a small integration check for a local model server. It proves that a clean Jarvis Home checkout can send an OpenAI-compatible request and parse the answer. It does not prove that two-tier routing, Quick Tools, OpenClaw handoff, voice input, or a smart-home backend is configured.

For the deterministic no-model demo, run:

```bash
uv run --locked --extra test jarvis-home-demo
```
