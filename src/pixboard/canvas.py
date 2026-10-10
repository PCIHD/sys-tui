"""Truecolor half-block pixel canvas with a text overlay.

One terminal cell holds two vertically stacked pixels (``▀`` with fg = top pixel, bg =
bottom pixel), so pixels are roughly square and board proportions survive. Colours are
packed ``0xRRGGBB`` ints throughout for speed.
"""

from __future__ import annotations

import math
from functools import lru_cache

from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

HALF = "▀"


def rgb(c: int) -> tuple[int, int, int]:
    return (c >> 16) & 255, (c >> 8) & 255, c & 255


def pack(r: float, g: float, b: float) -> int:
    return (max(0, min(255, int(r))) << 16) | (max(0, min(255, int(g))) << 8) | max(0, min(255, int(b)))


def mix(a: int, b: int, t: float) -> int:
    """Linear blend a→b by t in 0..1."""
    if t <= 0:
        return a
    if t >= 1:
        return b
    ar, ag, ab = rgb(a)
    br, bg, bb = rgb(b)
    return pack(ar + (br - ar) * t, ag + (bg - ag) * t, ab + (bb - ab) * t)


def shade(c: int, f: float) -> int:
    """Multiply brightness by f (f > 1 brightens)."""
    r, g, b = rgb(c)
    return pack(r * f, g * f, b * f)


def ramp(t: float, stops: list[tuple[float, int]]) -> int:
    """Piecewise-linear colour ramp."""
    t = max(stops[0][0], min(stops[-1][0], t))
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            return mix(c0, c1, (t - t0) / (t1 - t0) if t1 > t0 else 1)
    return stops[-1][1]


HEAT = [(0.0, 0x2f7de1), (0.45, 0x2fd18a), (0.65, 0xf2c14e), (0.82, 0xf07f3c), (1.0, 0xe5383b)]
LOAD = [(0.0, 0x3b4252), (0.3, 0x3fae7a), (0.7, 0xf2c14e), (1.0, 0xe5383b)]


@lru_cache(maxsize=65536)
def _style(fg: int, bg: int, bold: bool = False) -> Style:
    return Style(color=Color.from_rgb(*rgb(fg)), bgcolor=Color.from_rgb(*rgb(bg)), bold=bold)


class Canvas:
    __slots__ = ("w", "h", "pw", "ph", "px", "text")

    def __init__(self, w: int, h: int, bg: int = 0x0B0E14):
        self.w, self.h = max(1, w), max(1, h)
        self.pw, self.ph = self.w, self.h * 2
        self.px: list[int] = [bg] * (self.pw * self.ph)
        # (cell_x, cell_y) -> (char, fg, bg | None, bold)
        self.text: dict[tuple[int, int], tuple[str, int, int | None, bool]] = {}

    def copy(self) -> "Canvas":
        c = Canvas.__new__(Canvas)
        c.w, c.h, c.pw, c.ph = self.w, self.h, self.pw, self.ph
        c.px = self.px.copy()
        c.text = self.text.copy()
        return c

    # ------------------------------------------------------------------------- pixels
    def get(self, x: int, y: int) -> int:
        if 0 <= x < self.pw and 0 <= y < self.ph:
            return self.px[y * self.pw + x]
        return 0

    def set(self, x: int, y: int, c: int) -> None:
        if 0 <= x < self.pw and 0 <= y < self.ph:
            self.px[y * self.pw + x] = c

    def blend(self, x: int, y: int, c: int, a: float) -> None:
        if 0 <= x < self.pw and 0 <= y < self.ph:
            i = y * self.pw + x
            self.px[i] = mix(self.px[i], c, a)

    def fill(self, x0: int, y0: int, x1: int, y1: int, c: int) -> None:
        """Fill [x0, x1) × [y0, y1)."""
        x0, x1 = max(0, x0), min(self.pw, x1)
        y0, y1 = max(0, y0), min(self.ph, y1)
        if x1 <= x0:
            return
        row = [c] * (x1 - x0)
        for y in range(y0, y1):
            i = y * self.pw
            self.px[i + x0:i + x1] = row

    def frame(self, x0: int, y0: int, x1: int, y1: int, c: int) -> None:
        for x in range(x0, x1):
            self.set(x, y0, c)
            self.set(x, y1 - 1, c)
        for y in range(y0, y1):
            self.set(x0, y, c)
            self.set(x1 - 1, y, c)

    def gradient_v(self, x0: int, y0: int, x1: int, y1: int, top: int, bottom: int) -> None:
        n = max(1, y1 - y0 - 1)
        for y in range(y0, y1):
            self.fill(x0, y, x1, y + 1, mix(top, bottom, (y - y0) / n))

    def disc(self, cx: float, cy: float, r: float, c: int) -> None:
        for y in range(int(cy - r - 1), int(cy + r + 2)):
            for x in range(int(cx - r - 1), int(cx + r + 2)):
                if (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 <= r * r:
                    self.set(x, y, c)

    def line(self, x0: float, y0: float, x1: float, y1: float, c: int, a: float = 1.0) -> None:
        for x, y in line_points(x0, y0, x1, y1):
            if a >= 1:
                self.set(x, y, c)
            else:
                self.blend(x, y, c, a)

    def polygon(self, pts: list[tuple[float, float]], c: int) -> None:
        """Scanline fill of a simple polygon given in pixel coordinates."""
        if len(pts) < 3:
            return
        ys = [p[1] for p in pts]
        for y in range(max(0, int(min(ys))), min(self.ph, int(math.ceil(max(ys))))):
            yc = y + 0.5
            xs = []
            for (ax, ay), (bx, by) in zip(pts, pts[1:] + pts[:1]):
                if (ay <= yc < by) or (by <= yc < ay):
                    xs.append(ax + (yc - ay) * (bx - ax) / (by - ay))
            xs.sort()
            for a, b in zip(xs[::2], xs[1::2]):
                self.fill(int(round(a)), y, int(round(b)), y + 1, c)

    # --------------------------------------------------------------------------- text
    def put_text(self, cx: int, cy: int, s: str, fg: int, bg: int | None = None, bold: bool = False) -> None:
        for i, ch in enumerate(s):
            x = cx + i
            if 0 <= x < self.w and 0 <= cy < self.h:
                self.text[(x, cy)] = (ch, fg, bg, bold)

    def put_text_center(self, cx0: int, cx1: int, cy: int, s: str, fg: int, bg: int | None = None,
                        bold: bool = False) -> bool:
        """Centre ``s`` in cells [cx0, cx1); returns False (and draws nothing) if it doesn't fit."""
        width = cx1 - cx0
        if len(s) > width or width <= 0:
            return False
        self.put_text(cx0 + (width - len(s)) // 2, cy, s, fg, bg, bold)
        return True

    # ------------------------------------------------------------------------- output
    def _cell(self, x: int, y: int) -> tuple[str, Style]:
        top = self.px[2 * y * self.pw + x]
        bot = self.px[(2 * y + 1) * self.pw + x]
        t = self.text.get((x, y))
        if t is not None:
            ch, fg, bg, bold = t
            return ch, _style(fg, bg if bg is not None else mix(top, bot, 0.5), bold)
        return HALF, _style(top, bot)

    def segments(self, y: int) -> list[Segment]:
        segs: list[Segment] = []
        run_chars: list[str] = []
        run_style: Style | None = None
        for x in range(self.w):
            ch, st = self._cell(x, y)
            if st is run_style:
                run_chars.append(ch)
            else:
                if run_chars:
                    segs.append(Segment("".join(run_chars), run_style or Style()))
                run_chars, run_style = [ch], st
        if run_chars:
            segs.append(Segment("".join(run_chars), run_style or Style()))
        return segs

    def to_rich(self) -> Text:
        out = Text()
        for y in range(self.h):
            for seg in self.segments(y):
                out.append(seg.text, seg.style)
            if y < self.h - 1:
                out.append("\n")
        return out


def line_points(x0: float, y0: float, x1: float, y1: float) -> list[tuple[int, int]]:
    x0, y0, x1, y1 = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    out = []
    while True:
        out.append((x0, y0))
        if x0 == x1 and y0 == y1:
            return out
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def polyline_points(pts: list[tuple[float, float]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in zip(pts, pts[1:]):
        seg = line_points(*a, *b)
        if out and seg and out[-1] == seg[0]:
            seg = seg[1:]
        out.extend(seg)
    return out


def ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def lerp_box(a: tuple[float, float, float, float], b: tuple[float, float, float, float], t: float):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t,
            a[2] + (b[2] - a[2]) * t, a[3] + (b[3] - a[3]) * t)


def sample_crop(src: Canvas, box, w: int, h: int) -> Canvas:
    """Nearest-neighbour scale of pixel box ``box`` on ``src`` into a ``w``×``h`` cell canvas."""
    out = Canvas(w, h, bg=0)
    x0, y0, x1, y1 = box
    bw, bh = max(1.0, x1 - x0), max(1.0, y1 - y0)
    spw, sph, opw, oph = src.pw, src.ph, out.pw, out.ph
    spx, opx = src.px, out.px
    for y in range(oph):
        sy = int(y0 + (y + 0.5) * bh / oph)
        sy = 0 if sy < 0 else sph - 1 if sy >= sph else sy
        srow, drow = sy * spw, y * opw
        for x in range(opw):
            sx = int(x0 + (x + 0.5) * bw / opw)
            sx = 0 if sx < 0 else spw - 1 if sx >= spw else sx
            opx[drow + x] = spx[srow + sx]
    return out


def mix_canvas(a: Canvas, b: Canvas, t: float) -> None:
    """Blend ``b`` onto ``a`` in place by ``t`` in 0..1. Same size required."""
    if t <= 0:
        return
    apx, bpx = a.px, b.px
    n = min(len(apx), len(bpx))
    if t >= 1:
        a.px[:n] = bpx[:n]
        a.text = b.text.copy()
        return
    for i in range(n):
        apx[i] = mix(apx[i], bpx[i], t)


def transition_frame(outgoing: Canvas, incoming: Canvas, focus, p: float, inbound: bool = True) -> Canvas:
    """One frame of a zoom-through-``focus`` crossfade. ``p`` is 0..1 linear time.

    Inbound: crop of ``outgoing`` tightens onto ``focus`` while ``incoming`` fades up
    (fly into the GPU card, PCB appears). Outbound is the reverse.
    """
    if p <= 0:
        return outgoing.copy()
    if p >= 1:
        return incoming.copy()
    t = ease(p)
    full = (0.0, 0.0, float(outgoing.pw), float(outgoing.ph))
    box = focus if focus is not None else full
    if inbound:
        crop = lerp_box(full, box, t)
        out = sample_crop(outgoing, crop, incoming.w, incoming.h)
        fade = max(0.0, (t - 0.32) / 0.68)
        mix_canvas(out, incoming, fade)
        if 0.40 <= t <= 0.62:
            flash = 0.20 * (1.0 - abs(t - 0.51) / 0.11)
            white = 0xE8F4FF
            px = out.px
            for i in range(len(px)):
                px[i] = mix(px[i], white, flash)
        if fade > 0.85:
            out.text = incoming.text.copy()
        return out
    crop = lerp_box(box, full, t)
    out = sample_crop(incoming, crop, incoming.w, incoming.h)
    fade = max(0.0, 1.0 - t / 0.55)
    mix_canvas(out, outgoing, fade)
    if fade < 0.2:
        out.text = incoming.text.copy()
    return out
