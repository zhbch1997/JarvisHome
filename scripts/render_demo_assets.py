#!/usr/bin/env python3
"""Render privacy-safe Jarvis Home demo assets from fixed public copy."""
from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"
BG = "#080b14"
PANEL = "#111827"
BORDER = "#26334d"
TEXT = "#dbe7ff"
MUTED = "#7e8ca8"
BLUE = "#6ea8fe"
PURPLE = "#a78bfa"
GREEN = "#4ade80"
AMBER = "#fbbf24"
FONT_REGULAR = Path("/System/Library/Fonts/Menlo.ttc")
FONT_BOLD = Path("/System/Library/Fonts/SFNS.ttf")

LINES = [
    ("$ ./demo.sh", BLUE),
    ("", TEXT),
    ("User        Turn on the demo room light", TEXT),
    ("Decision    QUICK_TOOL", PURPLE),
    ("Capability  mock.light.set", BLUE),
    ("Safety      allowlisted mock device", GREEN),
    ("Action      demo-light-1 · on=true", AMBER),
    ("Result      Demo room light is on", GREEN),
    ("", TEXT),
    ("No network. No real devices. No credentials.", MUTED),
]


def font(size: int, bold: bool = False):
    path = FONT_BOLD if bold and FONT_BOLD.exists() else FONT_REGULAR
    return ImageFont.truetype(str(path), size)


def rounded(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def background(size):
    w, h = size
    image = Image.new("RGB", size, BG)
    draw = ImageDraw.Draw(image)
    for y in range(h):
        t = y / max(h - 1, 1)
        color = (8 + int(7 * t), 11 + int(8 * t), 20 + int(15 * t))
        draw.line((0, y, w, y), fill=color)
    for x, y, r, color in [
        (int(w * .12), int(h * .16), int(w * .22), (27, 72, 130)),
        (int(w * .88), int(h * .12), int(w * .25), (70, 38, 130)),
    ]:
        glow = Image.new("RGBA", size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        for i in range(8, 0, -1):
            rr = r * i / 8
            gd.ellipse((x-rr, y-rr, x+rr, y+rr), fill=(*color, 4))
        image = Image.alpha_composite(image.convert("RGBA"), glow).convert("RGB")
    return image


def terminal_frame(progress: float, size=(960, 540)):
    image = background(size)
    draw = ImageDraw.Draw(image)
    w, h = size
    rounded(draw, (48, 40, w-48, h-40), 20, PANEL, BORDER, 2)
    draw.ellipse((72, 62, 86, 76), fill="#ff5f57")
    draw.ellipse((94, 62, 108, 76), fill="#febc2e")
    draw.ellipse((116, 62, 130, 76), fill="#28c840")
    draw.text((w-250, 58), "JARVIS HOME / OFFLINE", font=font(15), fill=MUTED)
    shown = min(len(LINES), max(1, math.ceil(progress * len(LINES))))
    y = 112
    body = font(20)
    for idx, (line, color) in enumerate(LINES[:shown]):
        if line:
            draw.text((78, y), line, font=body, fill=color)
        y += 37
    if shown < len(LINES) and int(progress * 30) % 2 == 0:
        draw.rectangle((78, y, 91, y+23), fill=BLUE)
    return image


def social_preview():
    size = (1200, 630)
    image = background(size)
    draw = ImageDraw.Draw(image)
    draw.text((74, 70), "JARVIS", font=font(72, True), fill=TEXT)
    draw.text((370, 70), "HOME", font=font(72, True), fill=BLUE)
    draw.text((78, 165), "A local-first control plane for voice,", font=font(34), fill=TEXT)
    draw.text((78, 212), "AI agents, and the smart home.", font=font(34), fill=TEXT)
    nodes = [
        (78, 325, 278, 430, "VOICE", "request", BLUE),
        (360, 325, 560, 430, "ROUTER", "local", PURPLE),
        (642, 325, 842, 430, "TOOL", "allowlisted", GREEN),
        (924, 325, 1124, 430, "HOME", "mock first", AMBER),
    ]
    for x1, y1, x2, y2, title, subtitle, color in nodes:
        rounded(draw, (x1, y1, x2, y2), 16, PANEL, color, 2)
        draw.text((x1+22, y1+20), title, font=font(24, True), fill=color)
        draw.text((x1+22, y1+60), subtitle, font=font(17), fill=MUTED)
    for x in (298, 580, 862):
        draw.line((x, 377, x+42, 377), fill=BORDER, width=3)
        draw.polygon(((x+42,377),(x+31,370),(x+31,384)), fill=BORDER)
    draw.text((78, 515), "LOCAL-FIRST", font=font(18, True), fill=BLUE)
    draw.text((282, 515), "AUDITABLE", font=font(18, True), fill=PURPLE)
    draw.text((450, 515), "SAFE BY DEFAULT", font=font(18, True), fill=GREEN)
    draw.text((78, 563), "github.com/zhbch1997/JarvisHome", font=font(18), fill=MUTED)
    return image


def render():
    ASSETS.mkdir(parents=True, exist_ok=True)
    preview = social_preview()
    preview.save(ASSETS / "social-preview.png", optimize=True)

    frames = []
    count = 72
    for i in range(count):
        progress = min(1.0, (i + 1) / 58)
        frames.append(terminal_frame(progress))
    frames[0].save(
        ASSETS / "jarvis-home-demo.gif",
        save_all=True,
        append_images=frames[1:],
        duration=125,
        loop=0,
        optimize=True,
    )

    try:
        import imageio.v3 as iio
        import numpy as np
        video_frames = [np.asarray(frame.resize((1280, 720))) for frame in frames]
        iio.imwrite(ASSETS / "jarvis-home-demo.mp4", video_frames, fps=8, codec="libx264", quality=8)
    except ImportError:
        print("MP4 skipped: install imageio, imageio-ffmpeg and numpy", file=sys.stderr)

    print(ASSETS / "social-preview.png")
    print(ASSETS / "jarvis-home-demo.gif")
    if (ASSETS / "jarvis-home-demo.mp4").exists():
        print(ASSETS / "jarvis-home-demo.mp4")


if __name__ == "__main__":
    render()
