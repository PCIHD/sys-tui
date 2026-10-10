"""Pixel-art sprites for motherboard parts. Each draws into a pixel box [x0,x1)×[y0,y1) and
returns the pixel indices live overlays may recolour ("hot" pixels)."""

from __future__ import annotations

import math

from ..boards import Component
from ..canvas import Canvas, mix, shade

PCB = 0x16181D
PCB_DARK = 0x111317
PCB_ALT = 0x1A1D23
PCB_EDGE = 0x08090B
PCB_TRACE = 0x22262E
PCB_PLANE = 0x1B1E24
SILK = 0xC9CED6
GOLD = 0xD4A83C
GOLD_DIM = 0x7A6128
SILVER = 0xB8BDC4
STEEL = 0x8E949B
BLACK = 0x121316
SLOT = 0x0D0E10
CHOKE_TOP = 0x6E7278
CHOKE_SIDE = 0x3C3F44
HEATSINK = 0x3A3E45
HEATSINK_FIN = 0x50565F
IHS = 0xC3C8CE
SUBSTRATE = 0x1D5A37
DRIVE = 0x101114
USB5 = 0x2D6CDF
USB10 = 0xD33A3A
LED_GREEN = 0x3DFF7A
LED_AMBER = 0xFFB238

BARE = {PCB, PCB_DARK, PCB_ALT, PCB_TRACE, PCB_PLANE}


def idx(cv: Canvas, x: int, y: int) -> int | None:
    if 0 <= x < cv.pw and 0 <= y < cv.ph:
        return y * cv.pw + x
    return None


def box_indices(cv: Canvas, x0: int, y0: int, x1: int, y1: int) -> list[int]:
    out = []
    for y in range(max(0, y0), min(cv.ph, y1)):
        base = y * cv.pw
        for x in range(max(0, x0), min(cv.pw, x1)):
            out.append(base + x)
    return out


# ----------------------------------------------------------------------------- CPU
def draw_socket(cv: Canvas, b, c: Component) -> dict:
    """AM5 ILM frame, substrate and IHS. Returns the IHS box and its pixels plus die boxes."""
    x0, y0, x1, y1 = b
    w, h = x1 - x0, y1 - y0
    cv.fill(x0, y0, x1, y1, 0x2A2C30)
    t = max(1, round(min(w, h) * 0.06))
    for i in range(t):
        cv.frame(x0 + i, y0 + i, x1 - i, y1 - i, shade(STEEL, 1.1 - 0.15 * i))
    # retention screws at the ILM corners
    for sx, sy in ((x0 - 1, y0 + t), (x1, y0 + t), (x0 - 1, y1 - t - 1), (x1, y1 - t - 1)):
        cv.set(sx, sy, SILVER)
    # substrate (green) and IHS (silver, with the AM5 "octopus" notches)
    ix0, iy0 = x0 + round(w * 0.17), y0 + round(h * 0.16)
    ix1, iy1 = x1 - round(w * 0.17), y1 - round(h * 0.16)
    cv.fill(ix0 - 1, iy0 - 1, ix1 + 1, iy1 + 1, SUBSTRATE)
    hot = []
    for y in range(iy0, iy1):
        for x in range(ix0, ix1):
            sheen = 1.0 + 0.12 * (1 - ((x - ix0) / max(1, ix1 - ix0) + (y - iy0) / max(1, iy1 - iy0)))
            cv.set(x, y, shade(IHS, sheen))
            i = idx(cv, x, y)
            if i is not None:
                hot.append(i)
    iw = ix1 - ix0
    if iw >= 8:
        for f in (0.22, 0.72):
            nx = ix0 + round(iw * f)
            for nx_ in range(nx, nx + max(1, iw // 10)):
                for ny in (iy0, iy1 - 1):
                    cv.set(nx_, ny, SUBSTRATE)
                    i = idx(cv, nx_, ny)
                    if i in hot:
                        hot.remove(i)
    # lever on the right edge
    lx = x1 + 1
    cv.line(lx, y0 + t, lx, y1 - t, 0xA0A5AA)
    cv.set(lx - 1, y0 + t, 0xA0A5AA)
    # dies under the IHS (shown in the thermal "delid" view): CCD left, IOD right — approximate
    ih = iy1 - iy0
    ccd = (ix0 + round(iw * 0.06), iy0 + round(ih * 0.10), ix0 + round(iw * 0.46), iy0 + round(ih * 0.90))
    iod = (ix0 + round(iw * 0.54), iy0 + round(ih * 0.24), ix0 + round(iw * 0.94), iy0 + round(ih * 0.80))
    return {"ihs": (ix0, iy0, ix1, iy1), "hot": hot, "ccd": ccd, "iod": iod}


def draw_heatsink(cv: Canvas, b, c: Component) -> list[int]:
    x0, y0, x1, y1 = b
    cv.fill(x0, y0, x1, y1, HEATSINK)
    horiz = (x1 - x0) >= (y1 - y0)
    if horiz:
        for y in range(y0 + 1, y1 - 1):
            if (y - y0) % 2 == 1:
                cv.fill(x0 + 1, y, x1 - 1, y + 1, HEATSINK_FIN)
    else:
        for x in range(x0 + 1, x1 - 1):
            if (x - x0) % 2 == 1:
                cv.fill(x, y0 + 1, x + 1, y1 - 1, HEATSINK_FIN)
    cv.frame(x0, y0, x1, y1, 0x5E646C)
    # brushed accent stripe
    if horiz and y1 - y0 >= 5:
        cv.fill(x0 + 2, y0 + 1, x1 - 2, y0 + 2, 0x8A9098)
    elif not horiz and x1 - x0 >= 5:
        cv.fill(x0 + 1, y0 + 2, x0 + 2, y1 - 2, 0x8A9098)
    return box_indices(cv, x0, y0, x1, y1)


def draw_choke(cv: Canvas, b, c: Component) -> list[int]:
    x0, y0, x1, y1 = b
    cv.fill(x0, y0, x1, y1, CHOKE_SIDE)
    if x1 - x0 >= 3 and y1 - y0 >= 3:
        cv.fill(x0 + 1, y0 + 1, x1 - 1, y1 - 1, CHOKE_TOP)
        cv.fill(x0 + 1, y0 + 1, x1 - 1, y0 + 2, shade(CHOKE_TOP, 1.2))
        return box_indices(cv, x0 + 1, y0 + 1, x1 - 1, y1 - 1)
    cv.fill(x0, y0, x1, y1, CHOKE_TOP)
    return box_indices(cv, x0, y0, x1, y1)


def draw_power(cv: Canvas, b, c: Component) -> list[int]:
    x0, y0, x1, y1 = b
    cv.fill(x0, y0, x1, y1, 0x18191B)
    cv.frame(x0, y0, x1, y1, 0x2E2F33)
    vertical = (y1 - y0) > (x1 - x0)
    n = c.pins or 8
    rows, cols = (n // 2, 2) if vertical else (2, n // 2)
    iw, ih = x1 - x0 - 2, y1 - y0 - 2
    pins = []
    if iw >= 1 and ih >= 1:
        for r in range(rows):
            for k in range(cols):
                px = x0 + 1 + int((k + 0.5) * iw / cols)
                py = y0 + 1 + int((r + 0.5) * ih / rows)
                cv.set(px, py, GOLD)
                i = idx(cv, px, py)
                if i is not None and i not in pins:
                    pins.append(i)
    return pins


# ----------------------------------------------------------------------------- memory / slots
def draw_dimm(cv: Canvas, b, c: Component, populated: bool) -> list[int]:
    """Slot with latches; a heat-spread DDR5 stick when populated. Hot = centre stripe, bottom→top."""
    x0, y0, x1, y1 = b
    cv.fill(x0, y0, x1, y1, SLOT)
    lat = max(1, (y1 - y0) // 30)
    cv.fill(x0, y0, x1, y0 + lat, 0x7B7F86)
    cv.fill(x0, y1 - lat, x1, y1, 0x7B7F86)
    if not populated:
        cx = (x0 + x1) // 2
        cv.fill(cx, y0 + lat + 1, cx + 1, y1 - lat - 1, 0x2A2C30)
        return []
    sx0, sx1 = x0, x1
    cv.fill(sx0, y0 + lat, sx1, y1 - lat, 0x2B2E35)
    if x1 - x0 >= 3:
        cv.fill(sx0, y0 + lat, sx0 + 1, y1 - lat, 0x40444C)
    hot = []
    cx = (x0 + x1) // 2
    cols = [cx] if x1 - x0 < 4 else [cx - 1, cx]
    for y in range(y1 - lat - 2, y0 + lat + 1, -1):
        for x in cols:
            cv.set(x, y, 0x1E2026)
            i = idx(cv, x, y)
            if i is not None:
                hot.append(i)
    return hot


def draw_pcie(cv: Canvas, b, c: Component) -> list[int]:
    x0, y0, x1, y1 = b
    armored = (c.lanes or 1) >= 16
    cv.fill(x0, y0, x1, y1, SLOT)
    if armored:
        cv.frame(x0, y0, x1, y1, 0xA5AAB0)
    else:
        cv.frame(x0, y0, x1, y1, 0x2A2C31)
    cy = (y0 + y1) // 2
    key = x0 + max(1, round((x1 - x0) * (11.65 / 89 if armored else 11.65 / 25)))
    hot = []
    for x in range(x0 + 1, x1 - 1):
        if x == key:
            continue
        cv.set(x, cy, 0x232428)
        i = idx(cv, x, cy)
        if i is not None:
            hot.append(i)
    if armored and x1 - x0 > 12:  # latch at the far end
        cv.fill(x1 - 2, y0 - 1, x1, y1 + 1, 0x2B2C30)
    return hot


def draw_m2(cv: Canvas, b, c: Component, populated: bool, board_px_per_pct: float) -> list[int]:
    x0, y0, x1, y1 = b
    conn_w = max(1, round(3.1 * board_px_per_pct))
    cv.fill(x0, y0, x0 + conn_w, y1, 0x1A1B1E)
    cv.frame(x0, y0, x0 + conn_w, y1, 0x2E3035)
    # standoff at 2280 position
    sx, sy = x1 - 1, (y0 + y1) / 2
    hot = []
    if c.shield:
        cv.fill(x0 + conn_w, y0, x1, y1, 0x41464E)
        for x in range(x0 + conn_w + 1, x1 - 1, 3):
            cv.line(x, y0 + 1, x + (y1 - y0) // 2, y1 - 2, 0x4D535C)
        cv.frame(x0 + conn_w, y0, x1, y1, 0x6A7078)
        if y1 - y0 >= 6:
            cv.fill(x0 + conn_w + 2, y0 + 1, x1 - 2, y0 + 2, 0x8A9098)
        hot = box_indices(cv, x0 + conn_w + 1, y0 + 1, x1 - 1, y1 - 1)
    elif populated:
        dx0, dx1 = x0 + conn_w, x1
        cv.fill(dx0, y0 + 1, dx1, y1 - 1, DRIVE)
        # controller + NAND packages
        L = dx1 - dx0
        ch = max(1, (y1 - y0) - 4)
        cv.fill(dx0 + round(L * 0.08), y0 + 2, dx0 + round(L * 0.24), y0 + 2 + ch, 0x2A2C31)
        cv.fill(dx0 + round(L * 0.34), y0 + 2, dx0 + round(L * 0.62), y0 + 2 + ch, 0x24262B)
        cv.fill(dx0 + round(L * 0.68), y0 + 2, dx0 + round(L * 0.94), y0 + 2 + ch, 0x24262B)
        hot = box_indices(cv, dx0, y0 + 1, dx1, y1 - 1)
    else:
        cv.frame(x0 + conn_w, y0 + 1, x1, y1 - 1, 0x2C3038)
    cv.disc(sx, sy, max(0.8, (y1 - y0) * 0.16), GOLD_DIM)
    return hot


def draw_chipset(cv: Canvas, b, c: Component) -> list[int]:
    x0, y0, x1, y1 = b
    cv.fill(x0, y0, x1, y1, 0x41464E)
    for k in range(x0 - (y1 - y0), x1, 3):
        cv.line(k, y1 - 1, k + (y1 - y0), y0, 0x4B515A)
    cv.frame(x0, y0, x1, y1, 0x737A84)
    if x1 - x0 >= 6:
        cv.frame(x0 + 2, y0 + 2, x1 - 2, y1 - 2, 0x5A6069)
    return box_indices(cv, x0 + 1, y0 + 1, x1 - 1, y1 - 1)


def draw_sata(cv: Canvas, b, c: Component) -> list[int]:
    x0, y0, x1, y1 = b
    stacked = (y1 - y0) > (x1 - x0)
    cv.fill(x0, y0, x1, y1, 0x111214)
    cells = [(x0, y0, x1, (y0 + y1) // 2), (x0, (y0 + y1) // 2, x1, y1)] if stacked else [(x0, y0, x1, y1)]
    hot = []
    for (a0, b0, a1, b1) in cells:
        cv.frame(a0, b0, a1, b1, 0x2F3136)
        if a1 - a0 >= 3 and b1 - b0 >= 3:
            cv.fill(a0 + 1, b0 + 1, a1 - 1, b0 + 2, 0x3A3C42)
            hot += box_indices(cv, a0 + 1, b0 + 1, a1 - 1, b1 - 1)
    return hot


def draw_fan_header(cv: Canvas, b, c: Component) -> list[int]:
    x0, y0, x1, y1 = b
    cv.fill(x0, y0, x1, y1, 0x1C1D20)
    cv.frame(x0, y0, x1, y1, 0x34363B)
    pins = []
    n = 4
    horiz = (x1 - x0) >= (y1 - y0)
    for k in range(n):
        if horiz:
            px, py = x0 + int((k + 0.5) * (x1 - x0) / n), (y0 + y1) // 2
        else:
            px, py = (x0 + x1) // 2, y0 + int((k + 0.5) * (y1 - y0) / n)
        cv.set(px, py, GOLD)
        i = idx(cv, px, py)
        if i is not None:
            pins.append(i)
    return pins


def draw_header(cv: Canvas, b, c: Component) -> list[int]:
    x0, y0, x1, y1 = b
    cv.fill(x0, y0, x1, y1, 0x18191C)
    horiz = (x1 - x0) >= (y1 - y0)
    if horiz:
        for x in range(x0 + 1, x1 - 1, 2):
            for y in range(y0, y1):
                if (y - y0) % 2 == 0 or y1 - y0 <= 2:
                    cv.set(x, y, GOLD_DIM)
    else:
        for y in range(y0 + 1, y1 - 1, 2):
            for x in range(x0, x1):
                if (x - x0) % 2 == 0 or x1 - x0 <= 2:
                    cv.set(x, y, GOLD_DIM)
    return []


def draw_jumper(cv: Canvas, b, c: Component) -> list[int]:
    x0, y0, x1, y1 = b
    cv.fill(x0, y0, x1, y1, 0x18191C)
    cv.set(x0, y0, GOLD_DIM)
    return []


def draw_chip(cv: Canvas, b, c: Component) -> list[int]:
    x0, y0, x1, y1 = b
    cv.fill(x0, y0, x1, y1, 0x0E0E10)
    for x in range(x0 + 1, x1 - 1, 2):
        cv.set(x, y0, 0x8A8A8A)
        cv.set(x, y1 - 1, 0x8A8A8A)
    for y in range(y0 + 1, y1 - 1, 2):
        cv.set(x0, y, 0x8A8A8A)
        cv.set(x1 - 1, y, 0x8A8A8A)
    cv.set(x0 + 1, y0 + 1, 0x5A5A5A)
    return box_indices(cv, x0 + 1, y0 + 1, x1 - 1, y1 - 1)


def draw_io(cv: Canvas, b, c: Component) -> list[int]:
    """Rear-panel port stack seen from above: steel shells with a coloured tongue per port."""
    x0, y0, x1, y1 = b
    cv.fill(x0, y0, x1, y1, 0x9BA1A8)
    cv.frame(x0, y0, x1, y1, 0x6C727A)
    ports = c.ports or []
    hot = []
    if not ports:
        return hot
    n = len(ports)
    for k, p in enumerate(ports):
        a0 = y0 + 1 + (y1 - y0 - 2) * k // n
        a1 = y0 + 1 + (y1 - y0 - 2) * (k + 1) // n
        if a1 <= a0:
            continue
        if p in ("usb5", "usb10"):
            cv.fill(x0 + 1, a0, x1 - 2, a1, 0x7E848C)
            cv.fill(x0 + 1, (a0 + a1) // 2, x1 - 3, (a0 + a1) // 2 + 1, USB5 if p == "usb5" else USB10)
        elif p == "usbc":
            cv.fill(x0 + 1, a0, x0 + max(2, (x1 - x0) // 2), a1, 0x2A2C31)
        elif p in ("hdmi", "dp"):
            cv.fill(x0 + 1, a0, x1 - 1, a1, 0x5C6168)
            cv.fill(x0 + 1, (a0 + a1) // 2, x1 - 2, (a0 + a1) // 2 + 1, 0x1B1C1F)
        elif p == "lan":
            cv.fill(x0 + 1, a0, x1 - 1, a1, 0x8F959C)
            cv.fill(x0 + 2, a0, x1 - 3, a1, 0x1C1D20)
            for lx in (x1 - 2,):
                for ly in (a0, a1 - 1):
                    cv.set(lx, ly, 0x333333)
                    i = idx(cv, lx, ly)
                    if i is not None:
                        hot.append(i)
        elif p == "audio":
            cols = (0x7AC943, 0x3E7CE0, 0xE06AA8)
            for j, col in enumerate(cols):
                cx = x0 + 1 + (x1 - x0 - 2) * (j + 0.5) / 3
                cv.disc(cx, (y0 + y1) / 2, max(0.6, min((x1 - x0) / 7, (y1 - y0) / 2.6)), col)
        else:  # button
            cv.fill(x0 + 1, a0, x1 - 1, a1, 0x2A2C31)
    return hot


def draw_wifi(cv: Canvas, b, c: Component) -> list[int]:
    x0, y0, x1, y1 = b
    cv.fill(x0, y0, x1, y1, 0x1A3E2A)
    if x1 - x0 >= 6:
        cv.fill(x0 + 2, y0, x1 - 2, y1, 0x9CA2A9)
        cv.frame(x0 + 2, y0, x1 - 2, y1, 0x7A8088)
    cv.set(x0, y0, GOLD)
    cv.set(x0, y1 - 1, GOLD)
    led = idx(cv, x1 - 1, y0)
    return [led] if led is not None else []


def draw_battery(cv: Canvas, b, c: Component) -> list[int]:
    x0, y0, x1, y1 = b
    r = min(x1 - x0, y1 - y0) / 2
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    cv.disc(cx, cy, r, 0x5E636A)
    cv.disc(cx, cy, r * 0.82, 0xB9BEC5)
    cv.disc(cx - r * 0.2, cy - r * 0.2, r * 0.35, 0xD3D7DC)
    return []


def draw_hole(cv: Canvas, x: float, y: float, r: float) -> None:
    cv.disc(x, y, r, GOLD_DIM)
    cv.disc(x, y, max(0.5, r * 0.55), 0x0B0E14)


Z = {"gpu": -1, "pcie": 0, "m2": 0, "dimm": 0, "chipset": 1, "heatsink": 1, "socket": 2, "choke": 3, "power": 3,
     "sata": 3, "io": 4, "wifi": 4}


def z_key(c: Component):
    return (Z.get(c.kind, 5), -(c.w * c.h))


def label_colors(kind: str) -> tuple[int, int | None]:
    return {
        "socket": (0x101215, None), "heatsink": (0xE6E9ED, None), "chipset": (0xE6E9ED, None),
        "power": (0xF2C14E, None), "pcie": (0x8FB8DE, None), "m2": (0xE6E9ED, None),
        "chip": (0xB8BDC4, None), "io": (0x101215, None), "wifi": (0x101215, None),
    }.get(kind, (SILK, None))


def spinner(phase: float) -> str:
    return "◴◷◶◵"[int(phase) % 4]


def ring(cx: float, cy: float, r: float, n: int) -> list[tuple[float, float]]:
    return [(cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n)) for k in range(n)]


def blend_box(cv: Canvas, x0: int, y0: int, x1: int, y1: int, c: int, a: float) -> None:
    for i in box_indices(cv, x0, y0, x1, y1):
        cv.px[i] = mix(cv.px[i], c, a)
