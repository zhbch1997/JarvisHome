# Demo assets

These files are generated from fixed public demo copy. They do not use screenshots, logs, device inventories, environment variables, or production data.

- `jarvis-home-demo.gif`: README animation, 960×540
- `jarvis-home-demo.mp4`: silent 720p clip for release pages and social posts
- `social-preview.png`: 1200×630 repository preview image

Rebuild them on macOS:

```bash
uv run \
  --with pillow \
  --with numpy \
  --with imageio \
  --with imageio-ffmpeg \
  python scripts/render_demo_assets.py
```

The renderer uses only fixed strings that match the offline `./demo.sh` flow. Visual build dependencies are temporary and are not part of the Jarvis Home runtime.
