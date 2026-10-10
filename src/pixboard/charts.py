"""Pixel chart primitives on a half-block Canvas (2 vertical pixels per cell).

All boxes are pixel boxes [x0, x1) × [y0, y1). Series are oldest→newest; the newest sample
sits at the right edge.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from .canvas import Canvas, mix, ramp, shade

GRID = 0x1C222C
TRACK = 0x1A1F27
AXIS = 0x2A3240
TEXT = 0xC9D1D9
DIM = 0x6E7A8A

# series colours (dark-background friendly, distinct in hue and lightness)
CYAN = 0x39C5FF
ORANGE = 0xFF9A3C
GREEN = 0x3DDC84
YELLOW = 0xF2C14E
RED = 0xE5383B
VIOLET = 0xA78BFA
PINK = 0xF472B6
BLUE = 0x5B9BD5

# traffic direction, always relative to the CPU/host: IN (towards it) is drawn above the
# axis and marked ▲, OUT (away from it) below and marked ▼
IN = 0x4FD1FF
OUT = 0xFF9A3C

LOAD_STOPS = [(0.0, 0x2F7DE1), (0.35, 0x2FD18A), (0.65, 0xF2C14E), (0.85, 0xF07F3C), (1.0, 0xE5383B)]


def fit(values: Sequence[float], n: int) -> list[float]:
    """The last ``n`` values, left-padded with zeros."""
    vals = list(values)[-n:]
    return [0.0] * (n - len(vals)) + vals


def nice_max(v: float, floor: float = 1.0) -> float:
    """Round a maximum up to 1/2/5 × 10^k for stable autoscaled axes."""
    v = max(v, floor)
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * e:
            return m * e
    return 10 * e


def grid(cv: Canvas, box, rows: int = 4, color: int = GRID) -> None:
    x0, y0, x1, y1 = box
    h = y1 - y0
    for k in range(1, rows):
        y = y0 + round(h * k / rows)
        for x in range(x0, x1, 2):
            cv.set(x, y, color)


def area(cv: Canvas, box, values: Sequence[float], vmax: float, color: int, top: int | None = None,
         fill_alpha: float = 1.0, down: bool = False) -> None:
    """Filled area chart with a vertical gradient (bright edge, dark base)."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    vals = fit(values, w)
    hi = top if top is not None else shade(color, 1.25)
    base = mix(color, 0x0B0E14, 0.78)
    vmax = vmax or 1.0
    for i, v in enumerate(vals):
        frac = max(0.0, min(1.0, v / vmax))
        n = frac * h
        full = int(n)
        if n > 0 and full == 0:
            full = 1
        x = x0 + i
        for k in range(full):
            t = k / max(1, h - 1)
            c = mix(base, color, 0.25 + 0.75 * t)
            y = (y0 + k) if down else (y1 - 1 - k)
            if fill_alpha >= 1:
                cv.set(x, y, c)
            else:
                cv.blend(x, y, c, fill_alpha)
        if full > 0:
            y = (y0 + full - 1) if down else (y1 - full)
            cv.set(x, y, hi)


def line(cv: Canvas, box, values: Sequence[float], vmax: float, color: int, vmin: float = 0.0) -> None:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w <= 1 or h <= 0:
        return
    vals = list(values)[-w:]
    off = w - len(vals)  # right-aligned; no padding, so a short history doesn't draw a fake ramp
    rng = (vmax - vmin) or 1.0
    prev = None
    for i, v in enumerate(vals, start=off):
        frac = max(0.0, min(1.0, (v - vmin) / rng))
        y = y1 - 1 - round(frac * (h - 1))
        if prev is not None:
            cv.line(x0 + i - 1, prev, x0 + i, y, color)
        else:
            cv.set(x0 + i, y, color)
        prev = y


def mirrored(cv: Canvas, box, up: Sequence[float], down: Sequence[float], vmax: float,
             up_color: int = IN, down_color: int = OUT) -> None:
    """Two series around a centre axis: ``up`` above, ``down`` below (read/write, rx/tx)."""
    x0, y0, x1, y1 = box
    mid = (y0 + y1) // 2
    area(cv, (x0, y0, x1, mid), up, vmax, up_color)
    area(cv, (x0, mid + 1, x1, y1), down, vmax, down_color, down=True)
    for x in range(x0, x1):
        cv.set(x, mid, AXIS)


def hbar(cv: Canvas, box, frac: float | None, stops=LOAD_STOPS, color: int | None = None, track: int = TRACK) -> None:
    """Horizontal bar whose fill colour runs along the gradient up to ``frac``."""
    x0, y0, x1, y1 = box
    cv.fill(x0, y0, x1, y1, track)
    if frac is None:
        return
    frac = max(0.0, min(1.0, frac))
    n = round((x1 - x0) * frac)
    for i in range(n):
        c = color if color is not None else ramp(i / max(1, x1 - x0 - 1), stops)
        cv.fill(x0 + i, y0, x0 + i + 1, y1, c)
        if y1 - y0 >= 3:
            cv.set(x0 + i, y0, shade(c, 1.25))


def stacked(cv: Canvas, box, parts: Sequence[tuple[float, int]], track: int = TRACK) -> None:
    x0, y0, x1, y1 = box
    cv.fill(x0, y0, x1, y1, track)
    w = x1 - x0
    x = float(x0)
    for frac, c in parts:
        nx = x + max(0.0, frac) * w
        a, b = round(x), round(nx)
        if b > a:
            cv.fill(a, y0, min(b, x1), y1, c)
            if y1 - y0 >= 3:
                cv.fill(a, y0, min(b, x1), y0 + 1, shade(c, 1.25))
        x = nx


def ring(cv: Canvas, cx: float, cy: float, r: float, thick: float, frac: float | None, color: int | None = None,
         stops=LOAD_STOPS, track: int = TRACK, sweep: float = 270.0) -> None:
    """Arc gauge, open at the bottom; the fill follows ``stops`` (or a solid ``color``)."""
    start = 90 + (360 - sweep) / 2  # degrees, y down: 90 = straight down
    frac = None if frac is None else max(0.0, min(1.0, frac))
    r_in = max(0.0, r - thick)
    for y in range(int(cy - r - 1), int(cy + r + 2)):
        for x in range(int(cx - r - 1), int(cx + r + 2)):
            dx, dy = x + 0.5 - cx, y + 0.5 - cy
            d = math.hypot(dx, dy)
            if not (r_in <= d <= r):
                continue
            ang = (math.degrees(math.atan2(dy, dx)) - start) % 360
            if ang > sweep:
                continue
            pos = ang / sweep
            if frac is not None and pos <= frac:
                c = color if color is not None else ramp(pos, stops)
                cv.set(x, y, c)
            else:
                cv.set(x, y, track)


def needle(cv: Canvas, cx: float, cy: float, r: float, angle: float, color: int, alpha: float = 1.0) -> None:
    cv.line(cx, cy, cx + r * math.cos(angle), cy + r * math.sin(angle), color, alpha)


def text(cv: Canvas, px_x: int, px_y: int, s: str, fg: int = TEXT, bold: bool = False, bg: int | None = None) -> None:
    """Text at a pixel position (snapped to the cell grid)."""
    cv.put_text(px_x, px_y // 2, s, fg, bg, bold)


def text_right(cv: Canvas, px_x1: int, px_y: int, s: str, fg: int = TEXT, bold: bool = False) -> None:
    cv.put_text(px_x1 - len(s), px_y // 2, s, fg, None, bold)


def human_bytes(n: float | None, rate: bool = False, digits: int = 1) -> str:
    if n is None:
        return "—"
    suffix = "/s" if rate else ""
    for unit, div in (("T", 2**40), ("G", 2**30), ("M", 2**20), ("K", 2**10)):
        if abs(n) >= div:
            return f"{n / div:.{digits}f}{unit}{suffix}"
    return f"{n:.0f}B{suffix}"


def human_bits(bps: float | None) -> str:
    if bps is None:
        return "—"
    b = bps * 8
    for unit, div in (("Gb", 1e9), ("Mb", 1e6), ("kb", 1e3)):
        if b >= div:
            return f"{b / div:.1f}{unit}/s"
    return f"{b:.0f}b/s"


def human_rate_pair(inn: float | None, out: float | None, bits: bool = False) -> str:
    """Compact ``▲in ▼out`` rate label."""
    f = human_bits if bits else (lambda v: human_bytes(v, True))
    return f"▲{f(inn)} ▼{f(out)}"
