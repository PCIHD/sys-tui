"""PCB front/back scene: the board as laid out in its profile, with live power, memory and PCIe flow."""

from __future__ import annotations

import math

from ..boards import BoardProfile, Component
from ..canvas import HEAT, Canvas, mix, polyline_points, ramp, shade
from ..chips import SkuSpec
from ..components import kbs
from ..telemetry import GpuSnapshot
from . import Scene
from . import charts as ch
from . import sprites as sp

FINGER_MM = 8.5
BRACKET_GAP_MM = 3.0
BRACKET_MM = 1.6
POWER = 0xFFD84A
DATA = 0x4FD1FF
LED_ON = 0x3DFF7A
SILK = 0xD6E2D6
TRACE = 0x1E7046  # copper under solder mask: a shade lighter than the bare board
MEM_TEMP = (45.0, 100.0)  # GDDR7 °C range of the bus colour scale (throttle is ~105)
# cool → hot without passing through green, which is the die's SM-activity colour
MEM_HEAT = [(0.0, DATA), (0.45, 0x9FE3C8), (0.6, 0xF2C14E), (0.8, 0xF07F3C), (1.0, 0xE5383B)]
# usable bytes/s per lane, per direction, after line encoding (8b/10b to Gen2, 128b/130b from Gen3)
PCIE_LANE_BPS = {1: 250e6, 2: 500e6, 3: 984.6e6, 4: 1969.2e6, 5: 3938.5e6, 6: 7877e6}
_PCIE_ERR = (("replay", "pcie_replay"), ("L0", "pcie_l0_recovery"), ("corr", "pcie_corr"), ("NAK", "pcie_nak"))


def _hash(i: int, j: int) -> int:
    h = (i * 2654435761 ^ j * 40503) & 0xFFFFFFFF
    h ^= h >> 15
    return (h * 2246822519) & 0xFFFF


class PcbScene(Scene):
    def __init__(self, profile: BoardProfile, sku: SkuSpec, w: int, h: int, side: str = "front",
                 labels: bool = True):
        super().__init__(w, h)
        self.profile, self.sku, self.side, self.labels = profile, sku, side, labels
        self.title = f"{profile.short} — PCB {side}"
        self._layout()
        self._draw_static()

    # ---------------------------------------------------------------------- geometry
    def _layout(self) -> None:
        p = self.profile
        pw_mm, ph_mm = p.pcb_mm
        fh = FINGER_MM / ph_mm * 100
        xs = [0.0, 100.0]
        ys = [0.0, 100.0 + (fh if p.fingers and p.fingers.board == "main" else 0)]
        for a in p.aux_boards:
            xs += [a.rect[0], a.rect[2]]
            ys += [a.rect[1], a.rect[3] + (fh if p.fingers and p.fingers.board == a.name else 0)]
        bracket_pct = (BRACKET_GAP_MM + BRACKET_MM) / pw_mm * 100
        xmin, xmax = min(xs), max(xs)
        if self.side == "front":
            xmin -= bracket_pct
        else:
            xmax += bracket_pct
        ymin, ymax = min(ys) - 3, max(ys) + 1
        self.fh = fh
        self.xmin, self.xmax, self.ymin, self.ymax = xmin, xmax, ymin, ymax
        ext_w = (xmax - xmin) / 100 * pw_mm
        ext_h = (ymax - ymin) / 100 * ph_mm
        pad = 2
        s = min((self.static.pw - 2 * pad) / ext_w, (self.static.ph - 2 * pad) / ext_h)
        self.scale = s  # px per mm
        used_w, used_h = ext_w * s, ext_h * s
        self.ox = (self.static.pw - used_w) / 2
        self.oy = (self.static.ph - used_h) / 2

    def _mx(self, x: float) -> float:
        """Mirror for the back view (bracket moves to the right)."""
        return 100 - x if self.side == "back" else x

    def X(self, x: float) -> float:
        x = self._mx(x)
        lo = self.xmin if self.side == "front" else 100 - self.xmax
        return self.ox + (x - lo) / 100 * self.profile.pcb_mm[0] * self.scale

    def Y(self, y: float) -> float:
        return self.oy + (y - self.ymin) / 100 * self.profile.pcb_mm[1] * self.scale

    def box(self, r: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
        xa, xb = sorted((self.X(r[0]), self.X(r[2])))
        x0, y0 = int(round(xa)), int(round(self.Y(r[1])))
        x1, y1 = int(round(xb)), int(round(self.Y(r[3])))
        return x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)

    def mm(self, v: float) -> float:
        return v * self.scale

    # ------------------------------------------------------------------------ static
    def _draw_static(self) -> None:
        cv, p = self.static, self.profile
        cv.gradient_v(0, 0, cv.pw, cv.ph, 0x0B0E14, 0x111822)
        self._draw_bracket(cv)
        for a in p.aux_boards:
            x0, y0, x1, y1 = self.box(a.rect)
            cv.fill(x0, y0, x1, y1, 0x0F4A2A)
            cv.frame(x0, y0, x1, y1, sp.PCB_EDGE)
        pts = [(self.X(x), self.Y(y)) for x, y in p.outline_points()]
        cv.polygon(pts, sp.PCB)
        self._texture(cv, pts)
        for a, b in zip(pts, pts[1:] + pts[:1]):
            cv.line(a[0], a[1], b[0], b[1], sp.PCB_EDGE)
        self._fingers(cv)
        gpu = next(iter(p.of_kind("gpu")), None)
        self.gpu_box = self.box(gpu.rect) if gpu else None
        front = self.side == "front"
        self.mem_buses: list[tuple[str, int | None, list[list[tuple[int, int]]]]] = []
        self.pcie_lanes: list[tuple[int, list[tuple[int, int]]]] = []
        if front:
            self._planes(cv)
            self._pcie_traces(cv)
            self._mem_traces(cv)
        self._mount_holes(cv)

        comps = [c for c in p.components if c.side == self.side]
        ghosts = [c for c in p.components if c.side != self.side and c.kind in ("gpu", "mem", "choke", "power")]
        for c in ghosts:  # parts on the other side, shown as silk outlines
            x0, y0, x1, y1 = self.box(c.rect)
            cv.frame(x0, y0, x1, y1, 0x2E6B47)
        self.hot: dict[str, list[int]] = {}
        self.die_idx: list[int] = []
        for c in sorted(comps, key=sp.z_key):
            b = self.box(c.rect)
            kw = {}
            if c.kind == "gpu":
                kw["die_box"] = self._die_box(c, b)
            hot = sp.draw_component(cv, b, c, **kw)
            self.boxes[c.id] = b
            self.comps[c.id] = c
            if c.kind == "gpu":
                self.die_idx = hot
                self.die_box = kw["die_box"]
            elif hot:
                self.hot[c.id] = hot
        if p.fingers:
            fid = "PCIE"
            elec = "" if p.fingers.electrical == p.fingers.physical else f" · x{p.fingers.electrical} wired"
            fc = Component(fid, "fingers", self._finger_rect(), label=f"PCIe x{p.fingers.physical}{elec}",
                           part=f"PCIe 5.0 x{p.fingers.physical} edge"
                                + (f" (x{p.fingers.electrical} electrical)" if p.fingers.electrical != p.fingers.physical else ""))
            self.comps[fid] = fc
            self.boxes[fid] = self.box(fc.rect)
        self._smd_dust(cv)
        self.base = {cid: [cv.px[i] for i in idx] for cid, idx in self.hot.items()}
        self.die_base = [cv.px[i] for i in self.die_idx]
        self._halo(cv)
        self._routes()
        if self.labels:
            self._labels(cv)

    def _smd_dust(self, cv: Canvas) -> None:
        """Scatter of 0402/0603 passives, densest around the GPU and memory, on bare mask only."""
        bare = {sp.PCB, sp.PCB_DARK, 0x125A31, sp.PCB_PLANE}
        hubs = [self.boxes[c.id] for c in self.comps.values() if c.kind in ("gpu", "mem", "ctrl", "mcu")]
        if not hubs:
            return
        for y in range(cv.ph):
            for x in range(cv.pw):
                i = y * cv.pw + x
                if cv.px[i] not in bare:
                    continue
                near = 0.0
                for (x0, y0, x1, y1) in hubs:
                    dx = max(x0 - x, 0, x - x1)
                    dy = max(y0 - y, 0, y - y1)
                    d = dx + dy
                    if d < 5:
                        near = max(near, 1 - d / 5)
                p = 0.015 + 0.16 * near
                h = _hash(i, 11)
                if h < p * 65535:
                    cv.px[i] = (0x8B6D45, 0x1E1E1E, 0x9C8156, 0x2A2A2A)[h & 3]

    def _draw_bracket(self, cv: Canvas) -> None:
        pw_mm = self.profile.pcb_mm[0]
        xs = [0.0] + [a.rect[0] for a in self.profile.aux_boards]
        left = min(xs) - (BRACKET_GAP_MM + BRACKET_MM) / pw_mm * 100
        right = left + BRACKET_MM / pw_mm * 100
        x0, y0, x1, y1 = self.box((left, -2.5, right, 104))
        for x in range(x0, x1):
            f = 0.75 + 0.35 * (x - x0) / max(1, x1 - x0)
            cv.fill(x, y0, x + 1, y1, shade(sp.SILVER, f))

    def _texture(self, cv: Canvas, pts) -> None:
        # faint via field / speckle so the mask doesn't read as flat colour
        for i in range(0, len(cv.px), 7):
            if cv.px[i] == sp.PCB and _hash(i, 3) < 5200:
                cv.px[i] = sp.PCB_DARK if _hash(i, 5) & 1 else 0x125A31

    def _finger_rect(self) -> tuple[float, float, float, float]:
        f = self.profile.fingers
        base = 100.0
        if f.board != "main":
            aux = next(a for a in self.profile.aux_boards if a.name == f.board)
            base = aux.rect[3]
        return f.x0, base, f.x1, base + self.fh

    def _fingers(self, cv: Canvas) -> None:
        self.finger_cols: list[tuple[int, int, int, bool]] = []
        f = self.profile.fingers
        if not f:
            return
        x0, y0, x1, y1 = self.box(self._finger_rect())
        cv.fill(x0, y0, x1, y1, 0x0D4224)
        length_mm = (f.x1 - f.x0) / 100 * self.profile.pcb_mm[0]
        key_mm = 11.65
        pitch = max(1.0, self.mm(1.0))
        n = int((x1 - x0) / pitch)
        elec_frac = f.electrical / f.physical
        for k in range(n):
            xa = x0 + int(k * pitch)
            pos_mm = (k * pitch) / max(1, x1 - x0) * length_mm
            if self.side == "back":
                pos_mm = length_mm - pos_mm
            if key_mm - 0.8 <= pos_mm <= key_mm + 1.2:
                cv.fill(xa, y0 + (y1 - y0) // 3, xa + max(1, int(pitch)), y1, 0x0B0E14)
                continue
            if pitch >= 2 or k % 2 == 0:
                wired = pos_mm / length_mm <= elec_frac + 0.02
                cv.fill(xa, y0 + 1, xa + max(1, int(pitch * 0.7)), y1, sp.GOLD if wired else sp.GOLD_DIM)
                self.finger_cols.append((xa, y0 + 1, y1, wired))

    def _die_box(self, c: Component, b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        if c.die:
            return self.box(c.die)
        x0, y0, x1, y1 = b
        side = math.sqrt(self.sku.chip_spec.die_mm2)
        w, h = self.mm(side * 0.95), self.mm(side * 1.05)
        w = min(w, (x1 - x0) * 0.72)
        h = min(h, (y1 - y0) * 0.72)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        return int(round(cx - w / 2)), int(round(cy - h / 2)), int(round(cx + w / 2)), int(round(cy + h / 2))

    def _planes(self, cv: Canvas) -> None:
        """Copper pours between the NVVDD choke groups and the GPU."""
        if not self.gpu_box:
            return
        gx0, gy0, gx1, gy1 = self.gpu_box
        for g in self._choke_groups():
            bx0, by0, bx1, by1 = g
            if by1 - by0 >= bx1 - bx0:  # vertical column: pour horizontally toward the GPU
                ya, yb = max(by0, gy0 - 4), min(by1, gy1 + 4)
                if yb <= ya:
                    ya, yb = gy0, gy1
                xa, xb = (bx1, gx0) if bx1 <= gx0 else (gx1, bx0)
                cv.fill(xa, ya, xb, yb, sp.PCB_PLANE)
            else:
                xa, xb = max(bx0, gx0 - 4), min(bx1, gx1 + 4)
                ya, yb = (by1, gy0) if by1 <= gy0 else (gy1, by0)
                cv.fill(xa, ya, xb, yb, sp.PCB_PLANE)

    def _choke_groups(self) -> list[tuple[int, int, int, int]]:
        groups: dict[str, list[tuple[int, int, int, int]]] = {}
        for c in self.profile.of_kind("choke", side=self.side):
            groups.setdefault(c.group or c.id, []).append(self.box(c.rect))
        out = []
        for boxes in groups.values():
            if len(boxes) < 3:
                continue
            out.append((min(b[0] for b in boxes), min(b[1] for b in boxes),
                        max(b[2] for b in boxes), max(b[3] for b in boxes)))
        return out

    def _gpu_edge_point(self, tx: float, ty: float) -> tuple[float, float]:
        gx0, gy0, gx1, gy1 = self.gpu_box
        cx, cy = (gx0 + gx1) / 2, (gy0 + gy1) / 2
        dx, dy = tx - cx, ty - cy
        if dx == 0 and dy == 0:
            return cx, cy
        sx = (gx1 - gx0) / 2 / abs(dx) if dx else math.inf
        sy = (gy1 - gy0) / 2 / abs(dy) if dy else math.inf
        s = min(sx, sy)
        return cx + dx * s, cy + dy * s

    def _mem_traces(self, cv: Canvas) -> None:
        """A bundle of parallel traces per memory package, from the GPU's facing edge to the chip.

        Bundles run straight where the chip faces the die and jog at 45° where it is offset,
        as a real fan-out does. Each path is kept, GPU end first, for the live overlay. Paths
        start at the die, not the package edge: the package is drawn over that stretch, so it
        only shows when lit, and at terminal sizes the chips sit right against the package.
        """
        if not self.gpu_box:
            return
        gx0, gy0, gx1, gy1 = self.gpu_box
        dx0, dy0, dx1, dy1 = self._gpu_die()
        for c in self.profile.of_kind("mem", side="front"):
            x0, y0, x1, y1 = self.box(c.rect)
            gap_x = max(gx0 - x1, x0 - gx1, 0)
            gap_y = max(gy0 - y1, y0 - gy1, 0)
            vertical = gap_y >= gap_x  # chip above/below the GPU: traces leave its top/bottom edge
            if vertical:
                face, lo, hi = (x1 - x0), dx0, dx1
                a0, a1, die_a = (gy0, y1, dy0) if y1 <= gy0 else (gy1, y0, dy1)
                centre = (x0 + x1) / 2
            else:
                face, lo, hi = (y1 - y0), dy0, dy1
                a0, a1, die_a = (gx0, x1, dx0) if x1 <= gx0 else (gx1, x0, dx1)
                centre = (y0 + y1) / 2
            n = max(2, min(5, face // 2))
            half = (n - 1)  # pitch 2: one dark pixel between traces
            at_gpu = min(max(centre, lo + half + 1), hi - half - 1)
            paths = []
            for k in range(n):
                off = 2 * k - half
                pts = [(die_a, at_gpu + off)] + _dogleg(a0, at_gpu + off, a1, centre + off)
                pts = [(b, a) for a, b in pts] if vertical else pts
                path = polyline_points(pts)
                for x, y in path:
                    cv.set(x, y, TRACE)
                paths.append(path)
            self.mem_buses.append((c.id, c.channel, paths))

    def _pcie_traces(self, cv: Canvas) -> None:
        """Lanes from the edge fingers up to the GPU's bottom edge, one path per drawn lane.

        At terminal sizes there aren't pixels for all 16 pairs, so each path stands for
        ``physical / len(paths)`` lanes. Fingers to GPU first, for the live overlay.
        """
        wired = [col for col in self.finger_cols if col[3]]
        if not self.gpu_box or not wired:
            return
        gx0, gy0, gx1, gy1 = self.gpu_box
        span = (gx1 - gx0) * 0.7
        n = max(2, min(self.profile.fingers.physical, len(wired), int(span // 3)))
        for k in range(n):
            fx, fy0, _, _ = wired[round(k * (len(wired) - 1) / max(1, n - 1))]
            gx = gx0 + (gx1 - gx0 - span) / 2 + span * k / max(1, n - 1)
            path = polyline_points([(x, y) for y, x in _dogleg(fy0 - 1, fx, gy1, gx)])
            for x, y in path:
                cv.set(x, y, TRACE)
            self.pcie_lanes.append((k, path))

    def _gpu_die(self) -> tuple[int, int, int, int]:
        gpu = next(iter(self.profile.of_kind("gpu")), None)
        return self._die_box(gpu, self.gpu_box) if gpu else self.gpu_box

    def _mount_holes(self, cv: Canvas) -> None:
        if not self.gpu_box:
            return
        gx0, gy0, gx1, gy1 = self.gpu_box
        d = max(2.0, self.mm(4))
        r = max(0.8, self.mm(1.6))
        for x, y in ((gx0 - d, gy0 - d), (gx1 + d, gy0 - d), (gx0 - d, gy1 + d), (gx1 + d, gy1 + d)):
            if cv.get(int(x), int(y)) in (sp.PCB, sp.PCB_DARK, 0x125A31, sp.PCB_PLANE, TRACE):
                cv.disc(x, y, r, sp.GOLD_DIM)
                cv.disc(x, y, max(0.5, r * 0.55), 0x0B0E14)

    def _halo(self, cv: Canvas) -> None:
        self.halo: list[tuple[int, float]] = []
        if not self.gpu_box:
            return
        gx0, gy0, gx1, gy1 = self.gpu_box
        cx, cy = (gx0 + gx1) / 2, (gy0 + gy1) / 2
        rad = max(gx1 - gx0, gy1 - gy0) * 1.1
        for y in range(int(cy - rad), int(cy + rad)):
            for x in range(int(cx - rad), int(cx + rad)):
                d = math.hypot(x - cx, y - cy) / rad
                if d < 1 and 0 <= x < cv.pw and 0 <= y < cv.ph:
                    self.halo.append((y * cv.pw + x, (1 - d) ** 2))

    def _routes(self) -> None:
        """Pixel paths for animated power (connector → VRM → GPU), and the trace paths cut
        down to the pixels that show: copper not covered by a part, plus the memory buses'
        run across the package substrate (between die and package edge)."""
        self._visible_traces()
        self.power_paths: list[list[tuple[int, int]]] = []
        if not self.gpu_box or self.side != "front":
            return
        conn = next(iter(self.profile.of_kind("power", side="front")), None)
        gx0, gy0, gx1, gy1 = self.gpu_box
        gcx, gcy = (gx0 + gx1) / 2, (gy0 + gy1) / 2
        if conn:
            b = self.box(conn.rect)
            px, py = (b[0] + b[2]) / 2, b[3]
            for (x0, y0, x1, y1) in self._choke_groups():
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                if y1 - y0 >= x1 - x0:
                    ex, ey = self._gpu_edge_point(cx, gcy)
                    pts = [(px, py), (px, y0 - 1), (cx, y0 - 1), (cx, gcy), (ex, gcy)]
                else:
                    ex, ey = self._gpu_edge_point(gcx, cy)
                    pts = [(px, py), (px, cy), (x1, cy), (x0, cy), (gcx, cy), (gcx, ey)]
                self.power_paths.append(polyline_points(pts))

    def _visible_traces(self) -> None:
        px, pw = self.static.px, self.static.pw
        gx0, gy0, gx1, gy1 = self.gpu_box or (0, 0, 0, 0)
        dx0, dy0, dx1, dy1 = getattr(self, "die_box", (0, 0, 0, 0))

        def shows(x: int, y: int) -> bool:
            if gx0 <= x < gx1 and gy0 <= y < gy1:
                return not (dx0 <= x < dx1 and dy0 <= y < dy1)
            return px[y * pw + x] == TRACE

        self.mem_buses = [(cid, chn, [[q for q in path if shows(*q)] for path in paths])
                          for cid, chn, paths in self.mem_buses]
        self.pcie_lanes = [(k, [q for q in path if px[q[1] * pw + q[0]] == TRACE]) for k, path in self.pcie_lanes]

    def _labels(self, cv: Canvas) -> None:
        for cid, c in self.comps.items():
            x0, y0, x1, y1 = self.boxes[cid]
            if c.kind in ("gpu", "stage", "cap", "mlcc", "fuse", "shunt", "pad"):
                continue
            text = {
                "mem": c.label, "choke": c.marking, "power": "12V-2x6" if "12V" in (c.part or "") else "8-pin",
                "output": c.label, "fingers": c.label,
            }.get(c.kind, c.label)
            if not text:
                continue
            fg, bg = sp.label_colors(c.kind)
            rows = list(range((y0 + 1) // 2, (y1 - 1) // 2 + 1)) or [y0 // 2]
            row = rows[len(rows) // 2]
            if c.kind == "fingers":
                row = min(cv.h - 1, (y1 + 1) // 2)
            candidates = [text]
            if c.kind == "power":
                candidates += ["12V", "⚡"]
            if c.kind == "output" and text == "HDMI":
                candidates += ["HD", "H"]
            if c.kind == "mem":
                candidates = [text]
            if c.kind == "choke" and x1 - x0 <= len(text):
                continue  # adjacent choke markings would run together
            for t in candidates:
                if cv.put_text_center(x0, x1, row, t, fg, bg if c.kind != "fingers" else None, bold=c.kind == "mem"):
                    break
        for a in self.profile.aux_boards:
            x0, y0, x1, y1 = self.box(a.rect)
            cv.put_text_center(x0, x1, (y0 + y1) // 4, a.name, SILK)

    # ------------------------------------------------------------------------- live
    def draw_live(self, cv: Canvas, t: float, snap: GpuSnapshot | None, mode: str) -> None:
        dt = self.dt(t)
        if snap is None or mode == "off":
            return
        px = cv.px
        act = snap.sm_activity or 0.0
        dram = snap.dram_activity or 0.0
        pf = snap.power_frac or 0.0
        slow = snap.temp_slowdown or 90
        heat = 0.0 if snap.temp_gpu is None else max(0.0, min(1.0, (snap.temp_gpu - 25) / (slow - 25)))
        frame = int(t * 12)

        # GPU die: thermal colour, or SM-activity sparkle
        if mode == "thermal":
            hc = ramp(heat, HEAT)
            for i, (idx, base) in enumerate(zip(self.die_idx, self.die_base)):
                px[idx] = mix(base, hc, 0.75)
            for idx, wgt in self.halo:
                px[idx] = mix(px[idx], hc, 0.45 * wgt * (0.3 + heat))
        else:
            hc = ramp(heat, HEAT)
            thr = int(act * 65535)
            for idx, base in zip(self.die_idx, self.die_base):
                h = _hash(idx, frame)
                if h < thr:
                    px[idx] = mix(base, 0x7DFFB0, 0.35 + 0.5 * ((h >> 4) & 15) / 15)
                else:
                    px[idx] = mix(base, hc, 0.18)

        # VRM chokes / stages warm with load; memory with bandwidth
        for cid, idx in self.hot.items():
            c = self.comps[cid]
            base = self.base[cid]
            if c.kind in ("choke", "stage"):
                amt = pf * (0.55 if mode == "live" else 0.9) * (1.0 if c.kind == "choke" else 0.2)
                tint = 0xFF9A3C if mode == "live" else ramp(pf, HEAT)
                if mode == "live":
                    amt *= 0.85 + 0.15 * math.sin(t * 9 + hash(cid) % 7)
                for i, b in zip(idx, base):
                    px[i] = mix(b, tint, amt)
            elif c.kind == "mem":
                tint = 0x2A7FA8 if mode == "live" else ramp(dram, HEAT)
                amt = dram * (0.45 if mode == "live" else 0.85)
                for i, b in zip(idx, base):
                    px[i] = mix(b, tint, amt)
            elif c.kind == "power":
                for i, b in zip(idx, base):
                    px[i] = mix(b, 0xFFF2A0, pf * 0.8)

        if mode == "live":
            self._pcie_glow(cv, snap, dt)
        self._mem_glow(cv, snap, dt, dram, mode)
        if mode == "live":
            self._particles(cv, t, dt, pf)
            self._pcie(cv, snap, frame)

        # live label on the die
        if self.labels and self.die_idx:
            x0, y0, x1, y1 = self.die_box
            row = ((y0 + y1) // 2) // 2
            name = self.sku.chip
            temp = f"{snap.temp_gpu}°C" if snap.temp_gpu is not None else ""
            for s in (f"{name} {temp}".strip(), temp, name):
                if s and cv.put_text_center(x0, x1, row, s, 0xFFFFFF, None, bold=True):
                    break

        # display outputs: connected-port LED
        conns = snap.displays
        outs = [c for c in self.comps.values() if c.kind == "output"]
        for c in outs:
            con = output_connector(c, outs, conns)
            x0, y0, x1, y1 = self.boxes[c.id]
            on = bool(con and con.connected)
            cv.set(x1 - 1 if self.side == "front" else x0, y0, LED_ON if on else 0x333333)

        if mode == "live" and self.labels:
            self._rate_labels(cv, snap)

    def _mem_glow(self, cv: Canvas, snap: GpuSnapshot, dt: float, dram: float, mode: str) -> None:
        """Memory buses: brightness and pulses follow DRAM activity, colour follows each chip's
        temperature when the per-module sensors are readable (root), else plain data cyan.

        In thermal view the colour is the whole story: steady, and from the junction
        temperature when per-module readings are missing. A quiet live bus stays the static
        trace. NVML reports one activity figure, so the alternating directions are a convention.
        """
        raw = snap.dram_activity
        active = raw is not None and raw > 0.01
        self._mp = getattr(self, "_mp", 0.0) + dt * (6 + 60 * dram)
        px, pw = cv.px, cv.pw
        for b, (cid, channel, paths) in enumerate(self.mem_buses):
            temp = snap.module_temp(channel)
            if temp is None and mode == "thermal":
                temp = snap.temp_mem
            if temp is not None:
                col = ramp((temp - MEM_TEMP[0]) / (MEM_TEMP[1] - MEM_TEMP[0]), MEM_HEAT)
            else:
                col = DATA
            if mode != "thermal" and not active:
                continue
            amt = 0.7 if mode == "thermal" else 0.12 + 0.6 * dram
            for path in paths:
                for x, y in path:
                    i = y * pw + x
                    px[i] = mix(px[i], col, amt)
            if mode != "live" or not active:
                continue
            hot = mix(col, 0xFFFFFF, 0.45)
            for k, path in enumerate(paths):
                # alternate traces carry the two directions: reads come in, writes go out
                _pulses(cv, path, self._mp + 2.3 * b + 1.1 * k, 0.06 + 0.25 * dram, hot, reverse=k % 2 == 0)

    def _pcie_glow(self, cv: Canvas, snap: GpuSnapshot, dt: float) -> None:
        """PCIe lanes: alternate paths carry card → host (NVML TX, cyan, flowing down to the
        fingers) and host → card (RX, orange, flowing up to the GPU), each lit by its own rate.
        Paths beyond the negotiated link width stay dark."""
        if not self.pcie_lanes:
            return
        lv_up, lv_down = pcie_level(snap.pcie_tx_kbs), pcie_level(snap.pcie_rx_kbs)
        self._lp = getattr(self, "_lp", 0.0) + dt * 30
        phys = self.profile.fingers.physical
        live = len(self.pcie_lanes) * min(1.0, (snap.pcie_width or phys) / phys)
        px, pw = cv.px, cv.pw
        for k, path in self.pcie_lanes:
            if k >= round(live):
                continue
            to_host = k % 2 == 0
            level, col = (lv_up, ch.IN) if to_host else (lv_down, ch.OUT)
            if level <= 0:
                continue
            for x, y in path:
                i = y * pw + x
                px[i] = mix(px[i], col, 0.1 + 0.5 * level)
            _pulses(cv, path, self._lp * (0.3 + level) + 3.7 * k, 0.03 + 0.12 * level, mix(col, 0xFFFFFF, 0.4),
                    reverse=to_host)

    def _particles(self, cv: Canvas, t: float, dt: float, pf: float) -> None:
        self._pp = getattr(self, "_pp", 0.0) + dt * (8 + 70 * pf)
        for path in self.power_paths:
            n = len(path)
            if n < 2:
                continue
            count = max(1, int(n / 9 * (0.15 + pf)))
            spacing = n / count
            for k in range(count):
                pos = (self._pp + k * spacing) % n
                i = int(pos)
                for tail in range(3):
                    j = i - tail
                    if 0 <= j < n:
                        x, y = path[j]
                        cv.blend(x, y, POWER, 1.0 - tail * 0.35)

    def _pcie(self, cv: Canvas, snap: GpuSnapshot, frame: int) -> None:
        if not self.finger_cols:
            return
        # alternate fingers carry the two directions: card → host (NVML TX) and host → card (RX)
        lv_in, lv_out = pcie_level(snap.pcie_tx_kbs), pcie_level(snap.pcie_rx_kbs)
        width = snap.pcie_width or 0
        phys = self.profile.fingers.physical
        for k, (x, y0, y1, wired) in enumerate(self.finger_cols):
            lane_frac = k / max(1, len(self.finger_cols))
            active = wired and lane_frac <= width / phys + 0.02
            level, col = (lv_in, ch.IN) if k % 2 == 0 else (lv_out, ch.OUT)
            if active and _hash(k, frame) < level * 65535:
                cv.fill(x, y0, x + 1, y1, col)

    # --------------------------------------------------------------------- rate labels
    def _rate_labels(self, cv: Canvas, snap: GpuSnapshot) -> None:
        self._gddr_label(cv, snap)
        self._pcie_label(cv, snap)

    def _gddr_gbs(self, snap: GpuSnapshot, dram: float) -> float:
        """Device GB/s. NVML's memory clock is half the GDDR data rate; without it, use the rated peak."""
        clk = snap.clk_mem
        if clk:
            peak = self.sku.bus_width / 8 * (clk * 2 / 1000)
        else:
            peak = self.sku.bandwidth_gbs
        return peak * dram

    def _gddr_label(self, cv: Canvas, snap: GpuSnapshot) -> None:
        dram = snap.dram_activity
        if dram is None or dram <= 0.01 or not self.gpu_box:
            return
        gbs = self._gddr_gbs(snap, dram)
        texts = (f"GDDR ≈{gbs:.0f} GB/s · {dram * 100:.0f}%", f"≈{gbs:.0f} GB/s")
        gcx = (self.gpu_box[0] + self.gpu_box[2]) / 2
        chips = [c for c in self.profile.of_kind("mem", side="front") if c.id in self.boxes]
        for text in texts:
            for c in chips:
                x0, y0, x1, y1 = self.boxes[c.id]
                row = ((y0 + y1) // 2) // 2
                x = x1 if (x0 + x1) / 2 >= gcx else x0 - len(text)
                if self._label_fits(cv, x, row, len(text), boxes=True):
                    cv.put_text(x, row, text, DATA, None, True)
                    return

    def _pcie_label(self, cv: Canvas, snap: GpuSnapshot) -> None:
        token = self._pcie_error_token(snap)
        box = self.boxes.get("PCIE")
        if box is None:
            return
        x0, _, x1, y1 = box
        row = min(cv.h - 1, (y1 + 1) // 2)
        mid = (x0 + x1) / 2
        for runs in self._pcie_runs(snap, token):
            width = sum(len(t) for t, _ in runs) + len(runs) - 1
            placed = self._place_row(cv, row, width, mid, gap=True)
            if placed is None:
                placed = self._place_row(cv, row, width, mid, gap=False)
            if placed is not None:
                self._put_runs(cv, placed, row, runs)
                return

    def _pcie_runs(self, snap: GpuSnapshot, token: str | None) -> list[list[tuple[str, int]]]:
        link = None
        if snap.pcie_gen and snap.pcie_width:
            link = f"Gen{snap.pcie_gen} x{snap.pcie_width}"
        elif snap.pcie_gen:
            link = f"Gen{snap.pcie_gen}"
        elif snap.pcie_width:
            link = f"x{snap.pcie_width}"
        dirs = []
        if snap.pcie_tx_kbs is not None:
            dirs.append(("▲" + kbs(snap.pcie_tx_kbs), ch.IN))
        if snap.pcie_rx_kbs is not None:
            dirs.append(("▼" + kbs(snap.pcie_rx_kbs), ch.OUT))
        pct = self._link_share(snap)
        items: list[tuple[str, tuple[str, int]]] = []
        if link:
            items.append(("link", (link, SILK)))
        for d in dirs:
            items.append(("dir", d))
        if pct:
            items.append(("pct", (pct, SILK)))
        if token:
            items.append(("tok", (token, SILK)))
        current = items
        out: list[list[tuple[str, int]]] = []
        seen: set[tuple[str, ...]] = set()
        dir_dropped = False
        while current:
            key = tuple(text for _, (text, _) in current)
            if key not in seen:
                seen.add(key)
                out.append([run for _, run in current])
            nxt, dir_dropped = _drop_pcie_piece(current, dir_dropped)
            if nxt is current:
                break
            current = nxt
        return out

    def _link_share(self, snap: GpuSnapshot) -> str | None:
        lane = PCIE_LANE_BPS.get(snap.pcie_gen or 0)
        if not lane or not snap.pcie_width:
            return None
        rates = [v * 1024 for v in (snap.pcie_tx_kbs, snap.pcie_rx_kbs) if v is not None]
        if not rates:
            return None
        return f"{min(100, round(max(rates) / (lane * snap.pcie_width) * 100))}%"

    def _pcie_error_token(self, snap: GpuSnapshot) -> str | None:
        cur = {name: getattr(snap, attr) for name, attr in _PCIE_ERR}
        prev = getattr(self, "_err_prev", None)
        self._err_prev = cur
        if prev is None:
            return None
        for name, _attr in _PCIE_ERR:
            was, now = prev[name], cur[name]
            if was is not None and now is not None and now > was:
                return name
        return None

    def _place_row(self, cv: Canvas, row: int, width: int, mid: float, gap: bool) -> int | None:
        """Left edge of a free run on ``row``, nearest the fingers. ``gap`` keeps it off neighbouring text."""
        best: tuple[float, int] | None = None
        for x in range(0, cv.w - width + 1):
            if not self._label_fits(cv, x, row, width, boxes=False):
                continue
            if gap and any(0 <= edge < cv.w and (edge, row) in cv.text for edge in (x - 1, x + width)):
                continue
            dist = abs(x + width / 2 - mid)
            if best is None or dist < best[0]:
                best = (dist, x)
        return None if best is None else best[1]

    def _label_fits(self, cv: Canvas, x: int, row: int, n: int, boxes: bool) -> bool:
        if n <= 0 or row < 0 or row >= cv.h or x < 0 or x + n > cv.w:
            return False
        for i in range(n):
            if (x + i, row) in cv.text:
                return False
            if boxes and any(_cell_hits(x + i, row, b) for b in self.boxes.values()):
                return False
        return True

    @staticmethod
    def _put_runs(cv: Canvas, x: int, row: int, runs: list[tuple[str, int]]) -> None:
        for i, (text, col) in enumerate(runs):
            piece = text if i == len(runs) - 1 else text + " "
            cv.put_text(x, row, piece, col, None, True)
            x += len(piece)


def _drop_pcie_piece(items: list[tuple[str, tuple[str, int]]], dir_dropped: bool
                     ) -> tuple[list[tuple[str, tuple[str, int]]], bool]:
    """Shorten a PCIe caption: error token, then the capacity share, then one direction, then the gen."""
    def without(tag: str, last: bool = False) -> list[tuple[str, tuple[str, int]]] | None:
        idxs = [i for i, (t, _) in enumerate(items) if t == tag]
        if not idxs:
            return None
        i = idxs[-1] if last else idxs[0]
        return items[:i] + items[i + 1:]

    nxt = without("tok")
    if nxt is not None:
        return nxt, dir_dropped
    nxt = without("pct")
    if nxt is not None:
        return nxt, dir_dropped
    if not dir_dropped:
        nxt = without("dir", last=True)
        if nxt is not None:
            return nxt, True
    nxt = without("link")
    if nxt is not None:
        return nxt, dir_dropped
    nxt = without("dir", last=True)
    if nxt is not None:
        return nxt, True
    return items, dir_dropped


def _cell_hits(x: int, row: int, box: tuple[int, int, int, int]) -> bool:
    bx0, by0, bx1, by1 = box
    if x < bx0 or x >= bx1:
        return False
    return row * 2 < by1 and row * 2 + 2 > by0


def _dogleg(a0: float, b0: float, a1: float, b1: float) -> list[tuple[float, float]]:
    """Route from (a0, b0) to (a1, b1), points as (a, b): straight along a, a 45° jog across
    b in the middle, straight again. Too little run for the jog: one straight segment."""
    run, jog = a1 - a0, b1 - b0
    if abs(jog) < 0.5 or abs(jog) >= abs(run):
        return [(a0, b0), (a1, b1)]
    sa = 1 if run > 0 else -1
    lead = (abs(run) - abs(jog)) / 2
    return [(a0, b0), (a0 + sa * lead, b0), (a0 + sa * (lead + abs(jog)), b1), (a1, b1)]


def _pulses(cv: Canvas, path: list[tuple[int, int]], phase: float, density: float, color: int,
            reverse: bool = False) -> None:
    """Packets with a two-pixel tail running along ``path`` (backwards with ``reverse``)."""
    n = len(path)
    if n < 2:
        return
    count = max(1, int(n * density))
    spacing = n / count
    for k in range(count):
        pos = int((phase + k * spacing) % n)
        for tail in range(3):
            j = pos - tail
            if 0 <= j < n:
                x, y = path[n - 1 - j] if reverse else path[j]
                cv.blend(x, y, color, 1.0 - tail * 0.35)


def pcie_level(kbs: int | None) -> float:
    """0..1 on a log scale: 1 MB/s is faint, ~20 GB/s is full."""
    mbs = (kbs or 0) / 1024
    return 0.0 if mbs <= 0 else min(1.0, math.log10(1 + mbs) / 4.3)


def output_connector(comp: Component, outs: list[Component], conns):
    """Best-effort mapping of a bracket port to a DRM connector (same type, same order)."""
    kind = "HDMI" if (comp.label or "").startswith("HD") else "DP"
    same = [c for c in outs if ("HDMI" if (c.label or "").startswith("HD") else "DP") == kind]
    same.sort(key=lambda c: c.cy)
    pos = same.index(comp)
    drm = [c for c in conns if c.kind == kind]
    return drm[pos] if pos < len(drm) else None
