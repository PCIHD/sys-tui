"""Pixel-art sprites for PCB components. Each draws into a pixel box [x0,x1)×[y0,y1) and
returns the pixel indices that live overlays may recolour ("hot" pixels)."""

from __future__ import annotations

from ..boards import Component
from ..canvas import Canvas, mix, shade

PCB = 0x0E4526
PCB_DARK = 0x093219
PCB_EDGE = 0x06240F
PCB_TRACE = 0x15603A
PCB_PLANE = 0x115233
COPPER = 0xB07A3C
GOLD = 0xD4A83C
GOLD_DIM = 0x7A6128
SILVER = 0xB8BDC4
SILVER_DARK = 0x80858C
BLACK = 0x121316
MEM_BODY = 0x18191C
MEM_EDGE = 0x2E3035
CHOKE_TOP = 0x6E7278
CHOKE_SIDE = 0x3C3F44
DIE = 0x2A2F3A
SUBSTRATE = 0x1C2A21

BODY_COLORS = {
    "mem": MEM_BODY, "gpu": SUBSTRATE, "choke": CHOKE_TOP, "stage": BLACK, "ctrl": BLACK,
    "mcu": BLACK, "power": 0x151515, "shunt": SILVER, "fuse": 0xD9C35B, "header": 0x1B1B1B,
    "switch": 0xA82424, "output": SILVER_DARK, "b2b": 0xD8C9A3, "cap": 0x3A2E4A, "mlcc": 0xB59A6A,
    "pad": 0x6F7479, "fingers": GOLD,
}


def _idx(cv: Canvas, x: int, y: int) -> int | None:
    if 0 <= x < cv.pw and 0 <= y < cv.ph:
        return y * cv.pw + x
    return None


def _box_indices(cv: Canvas, x0: int, y0: int, x1: int, y1: int) -> list[int]:
    out = []
    for y in range(max(0, y0), min(cv.ph, y1)):
        base = y * cv.pw
        for x in range(max(0, x0), min(cv.pw, x1)):
            out.append(base + x)
    return out


def draw_mem(cv: Canvas, x0, y0, x1, y1, c: Component) -> list[int]:
    cv.fill(x0, y0, x1, y1, MEM_BODY)
    cv.frame(x0, y0, x1, y1, MEM_EDGE)
    if x1 - x0 >= 4 and y1 - y0 >= 4:
        cv.fill(x0 + 1, y0 + 1, x1 - 1, y0 + 2, 0x24262B)  # top sheen
        cv.set(x0 + 1, y0 + 1, 0xD0D0D0)  # pin-1 mark
    return _box_indices(cv, x0 + 1, y0 + 1, x1 - 1, y1 - 1)


def draw_pad(cv: Canvas, x0, y0, x1, y1, c: Component) -> list[int]:
    for y in range(y0, y1):
        for x in range(x0, x1):
            if (x + y) % 2 == 0 and x0 < x < x1 - 1 and y0 < y < y1 - 1:
                cv.set(x, y, 0x6F7479)
    cv.frame(x0, y0, x1, y1, 0x3C6B4E)
    return []


def draw_gpu(cv: Canvas, x0, y0, x1, y1, c: Component, die_box: tuple[int, int, int, int],
             stiffener: int = SILVER) -> list[int]:
    cv.fill(x0, y0, x1, y1, SUBSTRATE)
    w = max(1, min(x1 - x0, y1 - y0) // 10)
    for i in range(w):
        cv.frame(x0 + i, y0 + i, x1 - i, y1 - i, shade(stiffener, 1.0 - 0.12 * i))
    dx0, dy0, dx1, dy1 = die_box
    # ring of tiny SMD caps on the substrate around the die
    for x in range(dx0 - 1, dx1 + 1, 2):
        cv.set(x, dy0 - 2, 0xB08D57)
        cv.set(x, dy1 + 1, 0xB08D57)
    for y in range(dy0 - 1, dy1 + 1, 2):
        cv.set(dx0 - 2, y, 0xB08D57)
        cv.set(dx1 + 1, y, 0xB08D57)
    hot = []
    h = max(1, dy1 - dy0)
    wd = max(1, dx1 - dx0)
    for y in range(dy0, dy1):
        for x in range(dx0, dx1):
            sheen = 1.0 + 0.35 * max(0.0, 1 - ((x - dx0) / wd + (y - dy0) / h))
            cv.set(x, y, shade(DIE, sheen))
            i = _idx(cv, x, y)
            if i is not None:
                hot.append(i)
    return hot


def draw_choke(cv: Canvas, x0, y0, x1, y1, c: Component) -> list[int]:
    top = CHOKE_TOP if (c.marking or "").startswith("LR") or not c.marking else 0x7B7466
    cv.fill(x0, y0, x1, y1, CHOKE_SIDE)
    if x1 - x0 >= 3 and y1 - y0 >= 3:
        cv.fill(x0 + 1, y0 + 1, x1 - 1, y1 - 1, top)
        cv.fill(x0 + 1, y0 + 1, x1 - 1, y0 + 2, shade(top, 1.2))
        return _box_indices(cv, x0 + 1, y0 + 1, x1 - 1, y1 - 1)
    cv.fill(x0, y0, x1, y1, top)
    return _box_indices(cv, x0, y0, x1, y1)


def draw_stage(cv: Canvas, x0, y0, x1, y1, c: Component) -> list[int]:
    cv.fill(x0, y0, x1, y1, BLACK)
    if x1 - x0 >= 3 and y1 - y0 >= 3:
        cv.frame(x0, y0, x1, y1, 0x2A2B2E)
    return _box_indices(cv, x0, y0, x1, y1)


def draw_qfn(cv: Canvas, x0, y0, x1, y1, c: Component) -> list[int]:
    cv.fill(x0, y0, x1, y1, 0x0E0E10)
    for x in range(x0 + 1, x1 - 1, 2):
        cv.set(x, y0, 0x8A8A8A)
        cv.set(x, y1 - 1, 0x8A8A8A)
    for y in range(y0 + 1, y1 - 1, 2):
        cv.set(x0, y, 0x8A8A8A)
        cv.set(x1 - 1, y, 0x8A8A8A)
    cv.set(x0 + 1, y0 + 1, 0x5A5A5A)
    return []


def draw_power(cv: Canvas, x0, y0, x1, y1, c: Component) -> list[int]:
    cv.fill(x0, y0, x1, y1, 0x151515)
    cv.frame(x0, y0, x1, y1, 0x2B2B2B)
    pins = []
    eight = "8-pin" in (c.part or "")
    cols = 4 if eight else 6
    iw, ih = x1 - x0 - 2, y1 - y0 - 2
    if iw >= cols and ih >= 2:
        for r in range(2):
            for k in range(cols):
                px = x0 + 1 + int((k + 0.5) * iw / cols)
                py = y0 + 1 + int((r + 0.5) * ih / 2)
                cv.set(px, py, GOLD)
                i = _idx(cv, px, py)
                if i is not None:
                    pins.append(i)
    return pins


def draw_simple(cv: Canvas, x0, y0, x1, y1, c: Component) -> list[int]:
    body = BODY_COLORS.get(c.kind, 0x444444)
    if c.kind == "header" and "white" in (c.part or "").lower():
        body = 0xE8E6DF
    cv.fill(x0, y0, x1, y1, body)
    if c.kind == "header" and x1 - x0 >= 3:
        for x in range(x0 + 1, x1 - 1, 2):
            for y in range(y0 + 1, y1 - 1, 2):
                cv.set(x, y, GOLD)
    elif c.kind == "switch":
        cv.fill(x0 + (x1 - x0) // 2, y0, x1, y1, 0xEEEEEE)
    elif c.kind == "fuse":
        cv.fill(x0, y0, x0 + 1, y1, 0x1A1A1A)
        cv.fill(x1 - 1, y0, x1, y1, 0x1A1A1A)
    elif c.kind == "b2b":
        for x in range(x0 + 1, x1 - 1, 2):
            cv.set(x, y0 + (y1 - y0) // 2, 0x8A7A55)
    return _box_indices(cv, x0, y0, x1, y1)


def draw_caps(cv: Canvas, x0, y0, x1, y1, c: Component) -> list[int]:
    w, h = x1 - x0, y1 - y0
    if c.kind == "mlcc":
        for y in range(y0, y1):
            for x in range(x0, x1):
                if (x - x0) % 2 == 0 and (y - y0) % 2 == 0:
                    cv.set(x, y, 0xB59A6A)
        return []
    r = max(0.6, min(w, h) / 2 - 0.2)
    cv.disc(x0 + w / 2, y0 + h / 2, r, 0x3A2E4A)
    cv.disc(x0 + w / 2, y0 + h / 2, max(0.5, r - 1), 0x9EA3A8)
    return []


def draw_output(cv: Canvas, x0, y0, x1, y1, c: Component) -> list[int]:
    cv.fill(x0, y0, x1, y1, 0xA8ADB3)
    cv.frame(x0, y0, x1, y1, 0x6F747A)
    if x1 - x0 >= 3 and y1 - y0 >= 3:
        cv.fill(x0 + 1, y0 + 1, x1 - 1, y1 - 1, 0x101012)
        if (c.label or "").startswith("DP") and y1 - y0 >= 4:
            cv.set(x1 - 2, y1 - 2, 0xA8ADB3)  # DP's chamfered corner
    return []


def draw_component(cv: Canvas, box: tuple[int, int, int, int], c: Component, **kw) -> list[int]:
    x0, y0, x1, y1 = box
    k = c.kind
    if k == "mem":
        return draw_mem(cv, x0, y0, x1, y1, c)
    if k == "pad":
        return draw_pad(cv, x0, y0, x1, y1, c)
    if k == "gpu":
        return draw_gpu(cv, x0, y0, x1, y1, c, kw["die_box"], kw.get("stiffener", SILVER))
    if k == "choke":
        return draw_choke(cv, x0, y0, x1, y1, c)
    if k == "stage":
        return draw_stage(cv, x0, y0, x1, y1, c)
    if k in ("ctrl", "mcu"):
        return draw_qfn(cv, x0, y0, x1, y1, c)
    if k == "power":
        return draw_power(cv, x0, y0, x1, y1, c)
    if k in ("cap", "mlcc"):
        return draw_caps(cv, x0, y0, x1, y1, c)
    if k == "output":
        return draw_output(cv, x0, y0, x1, y1, c)
    return draw_simple(cv, x0, y0, x1, y1, c)


def label_colors(kind: str) -> tuple[int, int | None]:
    """(fg, bg) for a component's overlay label."""
    body = BODY_COLORS.get(kind)
    if kind in ("mem", "gpu", "stage", "ctrl", "mcu", "power", "header"):
        return 0xE6E6E6, body
    if kind == "choke":
        return 0x101010, CHOKE_TOP
    if kind in ("fuse", "b2b", "shunt", "pad"):
        return 0x101010, body
    if kind == "output":
        return 0xE6E6E6, 0x101012
    if kind == "switch":
        return 0xFFFFFF, 0xA82424
    return 0xE6E6E6, None


Z_ORDER = ["mlcc", "cap", "pad", "stage", "choke", "shunt", "fuse", "ctrl", "mcu", "header", "switch",
           "b2b", "output", "power", "mem", "gpu"]


def z_key(c: Component) -> int:
    return Z_ORDER.index(c.kind) if c.kind in Z_ORDER else 0


def hot_tint(base: int, color: int, amount: float) -> int:
    return mix(base, color, max(0.0, min(1.0, amount)))
