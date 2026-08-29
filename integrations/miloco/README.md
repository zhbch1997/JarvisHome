# Optional third-party home backend: Miloco

Jarvis Home does **not** contain, vendor, download, install, start, embed, or redistribute Miloco, MiMo-VL-Miloco, their web pages, source, binaries, containers, models, configuration, credentials, or runtime data.

Miloco is a separately installed third-party service governed by its own terms. Users who choose it must obtain and deploy it independently and determine whether their use complies with the upstream license.

Jarvis Home exposes only a brand-neutral optional HTTP client boundary:

```dotenv
JARVIS_EXTERNAL_HOME_ENABLED=0
JARVIS_EXTERNAL_HOME_BASE_URL=
```

The plugin is disabled by default. To use a separately installed compatible backend, an operator explicitly enables it and provides the backend URL. Plain HTTP is accepted only for loopback addresses; other hosts require HTTPS. Credentials must not be embedded in the URL.

Jarvis Home does not provide a Miloco installer or one-click download script. This documentation does not imply Xiaomi endorsement and does not grant any rights to third-party software beyond its own terms.
