"""Cores scene: one tile per GPC (its SMs enabled or fused), plus SM pipelines and history.

The topology comes from the chip spec. The live numbers do not: NVML's GPM counters (SM
activity, occupancy, tensor/FP pipes) are device-wide, so every GPC tile shows the same
activity and the per-SM sparkle is a visual spread of that aggregate.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from ..boards import BoardProfile, Component
from ..canvas import HEAT, Canvas, mix, ramp, shade
from ..chips import SkuSpec, enabled_sm_map
from ..telemetry import GpuSnapshot
from . import Scene
from . import charts as ch
from .die import SM_BASE, SM_LIT, SM_OFF, TENSOR, _hash

BG = 0x0D1117
TILE = 0x131A23
TILE_EDGE = 0x243041
TILE_OFF = 0x16181C
TEXT = 0xE6EDF3
CLOCK = 0x8FB8DE

# (label, GPM metric) for the pipelines box
PIPES = [("SM active", "SM_UTIL"), ("Occupancy", "SM_OCCUPANCY"), ("Tensor", "ANY_TENSOR_UTIL"),
         ("FP32", "FP32_UTIL"), ("FP16", "FP16_UTIL"), ("FP64", "FP64_UTIL"), ("INT", "INTEGER_UTIL"),
         ("Graphics", "GRAPHICS_UTIL"), ("DRAM BW", "DRAM_BW_UTIL")]


def _best_grid(n: int, W: int, H: int, aspect: float = 1.6) -> tuple[int, int]:
    best, best_s = (n, 1), -1.0
    for cols in range(1, n + 1):
        rows = math.ceil(n / cols)
        s = min(W / cols / aspect, H / rows)
        if s > best_s:
            best, best_s = (cols, rows), s
    return best


def _put(cv: Canvas, cx: int, cy: int, s: str, width: int, fg: int, bold: bool = False) -> None:
    """Text clipped to ``width`` cells, so it never spills into the next tile."""
    if width > 0:
        cv.put_text(cx, cy, s[:width], fg, None, bold)


def _put_first(cv: Canvas, cx: int, cy: int, options: Sequence[str], width: int, fg: int) -> None:
    """The longest of ``options`` that fits, else the last one clipped."""
    _put(cv, cx, cy, next((o for o in options if len(o) <= width), options[-1]), width, fg)


def _right(cv: Canvas, cx1: int, cy: int, s: str, fg: int, bold: bool = False) -> None:
    cv.put_text(cx1 - len(s), cy, s, fg, None, bold)


class CoresScene(Scene):
    def __init__(self, profile: BoardProfile, sku: SkuSpec, w: int, h: int, labels: bool = True,
                 history: Mapping[str, Sequence[float]] | None = None):
        super().__init__(w, h)
        self.profile, self.sku, self.labels, self.history = profile, sku, labels, history
        chip = self.chip = sku.chip_spec
        self.title = (f"{sku.name} cores — {sku.sms} SMs · {sku.cuda_cores} CUDA · {sku.gpcs} GPCs"
                      " · SM metrics are device-wide (NVML has no per-GPC counters)")
        self.grid = enabled_sm_map(chip, sku)
        # gpc -> (tile box, [(sm box, enabled)])
        self.tiles: dict[int, tuple[tuple[int, int, int, int], list[tuple[tuple[int, int, int, int], bool]]]] = {}
        self.pipes_box: tuple[int, int, int, int] | None = None
        self.hist_box: tuple[int, int, int, int] | None = None
        self._draw()

    # ------------------------------------------------------------------------ layout
    def _draw(self) -> None:
        cv, chip = self.static, self.chip
        cv.fill(0, 0, cv.pw, cv.ph, BG)
        rows_total = cv.h
        top = rows_total if rows_total < 14 else max(6, round(rows_total * 0.6))
        cols, rows = _best_grid(chip.gpcs, cv.pw, top * 2, aspect=1.7)
        for gi in range(chip.gpcs):
            col, row = gi % cols, gi // cols
            x0, x1 = cv.pw * col // cols + 1, cv.pw * (col + 1) // cols - 1
            cy0, cy1 = top * row // rows, top * (row + 1) // rows
            self._tile(gi, (x0, cy0 * 2, x1, cy1 * 2 - 1))
        if top < rows_total:
            split = cv.pw if cv.pw < 70 else max(38, cv.pw * 45 // 100)
            self.pipes_box = (1, top * 2, split - 1, rows_total * 2 - 1)
            self._frame_box(self.pipes_box, "SM pipelines (device-wide)")
            self.comps["PIPES"] = Component("PIPES", "pipes", (0, 0, 0, 0), label="SM pipelines",
                                            part="GPM pipe utilization, all SMs")
            self.boxes["PIPES"] = self.pipes_box
            if cv.pw - split >= 20:
                self.hist_box = (split + 1, top * 2, cv.pw - 1, rows_total * 2 - 1)
                self._frame_box(self.hist_box, "History")

    def _frame_box(self, box, title: str) -> None:
        cv = self.static
        x0, y0, x1, y1 = box
        cv.fill(x0, y0, x1, y1, TILE)
        cv.frame(x0, y0, x1, y1, TILE_EDGE)
        _put(cv, x0 + 1, y0 // 2, title, x1 - x0 - 2, CLOCK, bold=True)

    def _tile(self, gi: int, box) -> None:
        cv, chip = self.static, self.chip
        x0, y0, x1, y1 = box
        en = self.grid[gi]
        n_on = sum(en)
        on = n_on > 0
        cv.fill(x0, y0, x1, y1, TILE if on else TILE_OFF)
        cv.frame(x0, y0, x1, y1, TILE_EDGE if on else 0x23262B)
        cx0, cy0, cx1, cy1 = x0, y0 // 2, x1, (y1 + 1) // 2  # cell rows cy0 .. cy1-1
        tw = cx1 - cx0 - 2
        self.comps[f"GPC{gi}"] = Component(
            f"GPC{gi}", "gpc", (0, 0, 0, 0), label=f"GPC{gi}", channel=gi,
            part=f"Graphics processing cluster: raster engine, {chip.tpcs_per_gpc} TPCs × {chip.sms_per_tpc} SMs",
            note=f"{n_on}/{len(en)} SMs enabled")
        self.boxes[f"GPC{gi}"] = box
        # SM map between the two text rows at the top and the footer row (cy1-2; cy1-1 holds the frame)
        sms: list[tuple[tuple[int, int, int, int], bool]] = []
        my0, my1 = (cy0 + 2) * 2, (cy1 - 2) * 2 - 1
        ax0, ax1 = x0 + 2, x1 - 2
        if my1 - my0 < 2:  # tiny tile: map fills what's left under the header
            my0, my1 = (cy0 + 1) * 2, y1 - 1
        if my1 - my0 >= 1 and ax1 - ax0 >= chip.tpcs_per_gpc:
            tpcs = chip.tpcs_per_gpc
            tpc_rows = 2 if (my1 - my0) >= 6 and (ax1 - ax0) / (my1 - my0) < 4 else 1
            tpc_cols = math.ceil(tpcs / tpc_rows)
            sm_cols = tpc_cols * chip.sms_per_tpc
            for t in range(tpcs):
                tr, tc = divmod(t, tpc_cols)
                for s in range(chip.sms_per_tpc):
                    c = tc * chip.sms_per_tpc + s
                    sx0 = ax0 + c * (ax1 - ax0) // sm_cols + (1 if s == 0 and tc > 0 else 0)
                    sx1 = max(sx0 + 1, ax0 + (c + 1) * (ax1 - ax0) // sm_cols - 1)
                    sy0 = my0 + tr * (my1 - my0) // tpc_rows
                    sy1 = max(sy0 + 1, my0 + (tr + 1) * (my1 - my0) // tpc_rows - (1 if tpc_rows > 1 else 0))
                    sms.append(((sx0, sy0, sx1, sy1), en[t * chip.sms_per_tpc + s]))
        self.tiles[gi] = (box, sms)
        self._draw_sms(cv, sms, None, 0)
        if not self.labels:
            return
        _put(cv, cx0 + 1, cy0, f"GPC{gi}", tw, TEXT if on else ch.DIM, bold=True)
        if cy1 - cy0 >= 4:
            subs = [f"{n_on}/{len(en)} SM · {n_on * chip.fp32_per_sm} CUDA", f"{n_on}/{len(en)} SM"] if on else ["fused off"]
            _put_first(cv, cx0 + 1, cy0 + 1, subs, tw, ch.DIM)
        elif not on:
            _put(cv, cx0 + 6, cy0, "fused off", tw - 5, ch.DIM)

    def _draw_sms(self, cv: Canvas, sms, act: float | None, frame: int, tensor: float = 0.0) -> None:
        for n, ((x0, y0, x1, y1), en) in enumerate(sms):
            if not en:
                cv.fill(x0, y0, x1, y1, SM_OFF)
                if x1 - x0 >= 3 and y1 - y0 >= 3:
                    cv.line(x0, y0, x1 - 1, y1 - 1, 0x44464B)
                    cv.line(x1 - 1, y0, x0, y1 - 1, 0x44464B)
                continue
            if act is None:
                cv.fill(x0, y0, x1, y1, SM_BASE)
                continue
            v = act * (0.65 + 0.35 * _hash(n, frame) / 65535)
            cv.fill(x0, y0, x1, y1, mix(SM_BASE, SM_LIT, v))
            if tensor > 0.01 and y1 - y0 >= 3:
                cv.fill(x0, y1 - 1, x1, y1, mix(SM_BASE, TENSOR, min(1.0, tensor * 1.4)))

    # -------------------------------------------------------------------------- live
    def draw_live(self, cv: Canvas, t: float, snap: GpuSnapshot | None, mode: str) -> None:
        self.dt(t)
        if snap is None or mode == "off":
            return
        g = snap.gpm
        act = snap.sm_activity or 0.0
        tensor = g.get("ANY_TENSOR_UTIL", 0.0) / 100
        frame = int(t * 8)
        if mode == "thermal":
            tf = 0.0 if snap.temp_gpu is None else snap.temp_gpu / (snap.temp_slowdown or 90)
            acc = ramp(max(0.0, min(1.0, tf)), HEAT)
        else:
            acc = ramp(act, ch.LOAD_STOPS)
        for gi, (box, sms) in self.tiles.items():
            n_on = sum(self.grid[gi])
            if not n_on:
                continue
            x0, y0, x1, y1 = box
            cv.fill(x0 + 1, y0 + 1, x1 - 1, y1 - 1, mix(TILE, acc, 0.08 + 0.22 * act))
            cv.frame(x0, y0, x1, y1, mix(TILE_EDGE, acc, 0.55))
            self._draw_sms(cv, sms, act, frame + gi * 3, tensor)
            cx0, cy0, cx1, cy1 = x0, y0 // 2, x1, (y1 + 1) // 2
            tw = cx1 - cx0 - 2
            if snap.clk_sm and tw >= 14:
                _right(cv, cx1 - 1, cy0, f"{snap.clk_sm / 1000:.2f} GHz", CLOCK)
            pct = f"{act * 100:.0f}%"
            if cy1 - cy0 >= 4 and tw >= 26:
                _right(cv, cx1 - 1, cy0 + 1, pct, 0xFFFFFF, bold=True)
            elif tw >= 10:
                _right(cv, cx1 - 1, cy0, pct, 0xFFFFFF, bold=True)
            if cy1 - cy0 >= 6:
                busy = round(act * n_on * self.chip.fp32_per_sm)
                total = n_on * self.chip.fp32_per_sm
                _put_first(cv, cx0 + 1, cy1 - 2, [f"≈{busy}/{total} CUDA busy", f"≈{busy}/{total} busy", f"≈{busy}"], tw, ch.DIM)
        if self.pipes_box:
            self._draw_pipes(cv, snap)
        if self.hist_box:
            self._draw_history(cv, snap)

    def _draw_pipes(self, cv: Canvas, snap: GpuSnapshot) -> None:
        x0, y0, x1, y1 = self.pipes_box
        cx0, cy0, cx1, cy1 = x0, y0 // 2, x1, (y1 + 1) // 2
        g = snap.gpm
        rows = [(lab, g.get(key)) for lab, key in PIPES]
        if not g:
            rows = [("GPU util", None if snap.util_gpu is None else float(snap.util_gpu))]
            _put(cv, cx0 + 1, cy0 + 2, "GPM unsupported: device util only", cx1 - cx0 - 2, ch.DIM)
        bx0, bx1 = cx0 + 12, cx1 - 8
        for i, (lab, v) in enumerate(rows):
            cy = cy0 + 1 + i
            if cy >= cy1 - 1:
                break
            _put(cv, cx0 + 1, cy, lab, 11, ch.TEXT)
            if bx1 - bx0 >= 4:
                ch.hbar(cv, (bx0, cy * 2, bx1, cy * 2 + 2), None if v is None else v / 100)
            _right(cv, cx1 - 1, cy, "N/A" if v is None else f"{v:5.1f}%", ch.DIM if v is None else TEXT)

    def _draw_history(self, cv: Canvas, snap: GpuSnapshot) -> None:
        x0, y0, x1, y1 = self.hist_box
        cx0, cy0, cx1, cy1 = x0, y0 // 2, x1, (y1 + 1) // 2
        h = self.history
        if not h:
            _put(cv, cx0 + 1, (cy0 + cy1) // 2, "collecting…", cx1 - cx0 - 2, ch.DIM)
            return
        legend = [("SM", ch.GREEN), ("occ", ch.CYAN), ("tensor", ch.VIOLET), ("SM clk", ch.YELLOW)]
        x = cx0 + 10
        for txt, col in legend:
            if x + len(txt) + 2 > cx1 - 1:
                break
            cv.put_text(x, cy0, "■", col)
            cv.put_text(x + 1, cy0, txt, ch.TEXT)
            x += len(txt) + 3
        box = (x0 + 1, (cy0 + 1) * 2, x1 - 1, y1 - 1)
        if box[3] - box[1] < 2:
            return
        ch.grid(cv, box, 4)
        ch.area(cv, box, h.get("sm", ()), 100, ch.GREEN)
        ch.line(cv, box, h.get("occ", ()), 100, ch.CYAN)
        ch.line(cv, box, h.get("tensor", ()), 100, ch.VIOLET)
        clk = h.get("smclk", ())
        cmax = snap.max_clk_sm or ch.nice_max(max(clk, default=0) * 1.1, 1000)
        ch.line(cv, box, clk, cmax, shade(ch.YELLOW, 0.9))
