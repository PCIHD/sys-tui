"""Rasterise a Canvas to PNG or GIF (needs Pillow). Used by ``--png`` / ``--gif``."""

from __future__ import annotations

import glob

from .canvas import Canvas, mix, rgb

CELL_W, CELL_H = 8, 16
_FONT_GLOBS = [
    "/usr/share/fonts/**/DejaVuSansMono.ttf",
    "/usr/share/fonts/**/NotoSansMono-Regular.ttf",
    "/usr/share/fonts/**/NotoSansMono*.ttf",
    "/usr/share/fonts/**/*Mono*.ttf",
]


def _font(size: int):
    from PIL import ImageFont

    for pat in _FONT_GLOBS:
        hits = sorted(glob.glob(pat, recursive=True))
        if hits:
            return ImageFont.truetype(hits[0], size)
    return ImageFont.load_default()


def canvas_to_image(cv: Canvas):
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (cv.w * CELL_W, cv.h * CELL_H))
    d = ImageDraw.Draw(img)
    font = _font(13)
    for y in range(cv.h):
        for x in range(cv.w):
            top = cv.px[2 * y * cv.pw + x]
            bot = cv.px[(2 * y + 1) * cv.pw + x]
            X, Y = x * CELL_W, y * CELL_H
            t = cv.text.get((x, y))
            if t:
                ch, fg, bg, bold = t
                d.rectangle([X, Y, X + CELL_W - 1, Y + CELL_H - 1], fill=rgb(bg if bg is not None else mix(top, bot, 0.5)))
                d.text((X, Y + 1), ch, fill=rgb(fg), font=font)
            else:
                d.rectangle([X, Y, X + CELL_W - 1, Y + CELL_H // 2 - 1], fill=rgb(top))
                d.rectangle([X, Y + CELL_H // 2, X + CELL_W - 1, Y + CELL_H - 1], fill=rgb(bot))
    return img


def canvas_to_png(cv: Canvas, path: str) -> None:
    canvas_to_image(cv).save(path)


def canvases_to_gif(frames: list[Canvas], path: str, fps: float = 10) -> None:
    if not frames:
        raise ValueError("no frames")
    imgs = [canvas_to_image(cv) for cv in frames]
    duration = max(1, int(round(1000 / fps)))
    imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=duration, loop=0)
