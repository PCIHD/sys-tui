"""Clickable Zen CCD/IOD floorplan. Schematic: CCD left, IOD right, fused cores greyed."""

from __future__ import annotations

from ..boards import Component
from ..canvas import HEAT, LOAD, Canvas, mix, ramp, shade
from ..telemetry import SystemSnapshot
from . import Scene
from . import charts as ch

BG = 0x0B0E14
CCD = 0x1A1D24
IOD = 0x1C2230
FUSED = 0x2A2D33
L3 = 0x3A4150
TEXT = 0xE6EDF3


class CpuDieScene(Scene):
    title = "CPU die"

    def __init__(self, w: int, h: int, snap: SystemSnapshot | None = None, labels: bool = True):
        super().__init__(w, h)
        self.labels = labels
        self.n_live = snap.cpu.cores if snap and snap.cpu.cores else 6
        self.slots = 8 if self.n_live <= 8 else 16
        self._draw_static()

    def _box(self, cid, kind, pix, label, part=None):
        self.comps[cid] = Component(cid, kind, (0, 0, 0, 0), label=label, part=part)
        self.boxes[cid] = pix

    def _draw_static(self):
        cv = self.static
        cv.gradient_v(0, 0, cv.pw, cv.ph, BG, 0x111822)
        pad = 4
        W, H = cv.pw - 2 * pad, cv.ph - 2 * pad
        x0, y0 = pad, pad
        # package
        cv.fill(x0, y0, x0 + W, y0 + H, 0x2A2D33)
        cv.frame(x0, y0, x0 + W, y0 + H, 0x5A6270)
        # CCD (left 55%) and IOD (right)
        cx1 = x0 + int(W * 0.55)
        ccd = (x0 + 2, y0 + 2, cx1 - 2, y0 + H - 2)
        iod = (cx1 + 2, y0 + 2, x0 + W - 2, y0 + H - 2)
        cv.fill(*ccd, CCD)
        cv.fill(*iod, IOD)
        cv.frame(*ccd, 0x3A4150)
        cv.frame(*iod, 0x3A4150)
        self._box("IOD", "chip", iod, "IOD", "I/O die (infinity fabric, memory, PCIe)")
        # L3 strip down the CCD centre
        a0, b0, a1, b1 = ccd
        lw = max(2, (a1 - a0) // 10)
        l3 = (a0 + (a1 - a0) // 2 - lw, b0 + 2, a0 + (a1 - a0) // 2 + lw, b1 - 2)
        cv.fill(*l3, L3)
        self._box("L3", "chip", l3, "L3", "CCD L3 cache")
        rows = self.slots // 2
        for k in range(self.slots):
            col, row = k % 2, k // 2
            tx0 = a0 + 2 if col == 0 else l3[2] + 1
            tx1 = l3[0] - 1 if col == 0 else a1 - 2
            ty0 = b0 + 2 + (b1 - b0 - 4) * row // rows
            ty1 = b0 + 2 + (b1 - b0 - 4) * (row + 1) // rows - 1
            box = (tx0, ty0, max(tx0 + 2, tx1), max(ty0 + 2, ty1))
            live = k < self.n_live
            cv.fill(*box, 0x23473A if live else FUSED)
            cv.frame(*box, 0x3A4658)
            cid = f"CORE{k}" if live else f"FUSED{k}"
            self._box(cid, "core" if live else "chip", box, f"Core {k}" if live else f"Fused {k}",
                      None if live else "fused off on this SKU")
            if self.labels:
                cx = (box[0] + box[2]) // 2
                cy = ((box[1] + box[3]) // 2) // 2
                cv.put_text_center(box[0] // 1, box[2], cy, str(k) if live else "×", TEXT if live else 0x6E7A8A, None, True)
        if self.labels:
            cv.put_text(ccd[0] + 1, (ccd[1] // 2), "CCD", 0x8FB8DE)
            cv.put_text((iod[0] + iod[2]) // 2 - 1, (iod[1] // 2), "IOD", 0x8FB8DE)

    def draw_live(self, cv: Canvas, t: float, snap: SystemSnapshot | None, mode: str) -> None:
        if snap is None:
            return
        cpu = snap.cpu
        for k in range(self.n_live):
            cid = f"CORE{k}"
            if cid not in self.boxes:
                continue
            x0, y0, x1, y1 = self.boxes[cid]
            u = cpu.core_util(k) / 100
            if mode == "thermal":
                w = cpu.core_w[k] if k < len(cpu.core_w) else None
                acc = ramp(0 if w is None else min(1.0, w / 20), HEAT)
            else:
                acc = ramp(u, LOAD)
            cv.fill(x0 + 1, y0 + 1, x1 - 1, y1 - 1, mix(0x23473A, acc, 0.35 + 0.5 * u))
            cy = ((y0 + y1) // 2) // 2
            cv.put_text_center(x0, x1, cy, f"{u * 100:.0f}%", TEXT, None, True)
            mhz = cpu.core_mhz(k)
            if mhz and (y1 - y0) > 10:
                ch.text_right(cv, x1 - 1, y1 - 2, f"{mhz / 1000:.2f}", 0x8FB8DE)
        if "IOD" in self.boxes and cpu.package_w:
            x0, y0, x1, y1 = self.boxes["IOD"]
            uncore = cpu.package_w - sum(w or 0 for w in cpu.core_w)
            acc = ramp(min(1.0, max(0.0, uncore / 40)), HEAT)
            cv.fill(x0 + 1, y0 + 1, x1 - 1, y1 - 1, mix(IOD, acc, 0.35))
