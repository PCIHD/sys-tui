"""Motherboard scene: the board as laid out in its profile, animated from live telemetry."""

from __future__ import annotations

import math
import re

from .. import bindings as B
from ..boards import BoardProfile, Component
from ..canvas import HEAT, LOAD, Canvas, line_points, mix, polyline_points, ramp, shade
from ..telemetry import SystemSnapshot
from . import Scene
from . import charts as ch
from . import sprites as sp

POWER = 0xFFD84A
RAIL_COLORS = {"slot": 0xFFD84A, "dimm": 0xFF6B5A, "storage": 0xFFA03C}  # 12 V · 5 V · 3.3 V
RAIL_REF_W = {"slot": 66.0, "dimm": 18.0, "storage": 30.0}
READ = ch.IN  # towards the CPU: disk reads, network receive, card → host
WRITE = ch.OUT  # away from the CPU
SPARK = 0x7DFFB0
SAT = [(0.0, 0x2FD18A), (0.5, 0xF2C14E), (0.8, 0xF07F3C), (1.0, 0xFF3B30)]  # link load: green → red
CARD = 0x262A31
CARD_EDGE = 0x5E6670


def _hash(i: int, j: int) -> int:
    h = (i * 2654435761 ^ j * 40503) & 0xFFFFFFFF
    h ^= h >> 15
    return (h * 2246822519) & 0xFFFF


def heat01(v: float | None, lo: float, hi: float) -> float:
    return 0.0 if v is None else max(0.0, min(1.0, (v - lo) / (hi - lo)))


level = B.level


def cpu_short(model: str) -> str:
    m = re.search(r"(\d{4,5}[A-Z0-9]*(?:X3D)?)\b", model or "")
    return m.group(1) if m else "CPU"


def gpu_short(name: str) -> str:
    return re.sub(r"^(NVIDIA )?(GeForce )?", "", name or "GPU")


class BoardScene(Scene):
    def __init__(self, profile: BoardProfile, w: int, h: int, labels: bool = True,
                 hint: SystemSnapshot | None = None, card: bool = True):
        super().__init__(w, h)
        self.profile, self.labels, self.hint = profile, labels, hint
        x16 = next((c for c in profile.of_kind("pcie") if c.bind == "gpu:x16"), None)
        self.x16 = x16
        self.card = bool(card and x16 and hint is not None and B.gpu(hint) is not None)
        self.title = profile.short
        self._phase: dict[str, float] = {}
        self._fan_phase: dict[str, float] = {}
        self._card_angle = 0.0
        self._card_alpha = 1.0
        self._focus: set[str] = set()
        self._layout()
        self._draw_static()

    # ---------------------------------------------------------------------- geometry
    def _layout(self) -> None:
        p = self.profile
        bw, bh = p.pcb_mm
        pad = 1
        xmin, ymin, ymax = -2.0, -1.5, 101.5
        tight = 101.5
        full = 126.0 if self.card else tight

        def scale(xmax):
            ew, eh = (xmax - xmin) / 100 * bw, (ymax - ymin) / 100 * bh
            return min((self.static.pw - 2 * pad) / ew, (self.static.ph - 2 * pad) / eh), ew, eh

        s_full, ew, eh = scale(full)
        s_tight, _, _ = scale(tight)
        xmax = full
        if self.card and s_full < 0.88 * s_tight:  # not enough width: crop the card's overhang
            xmax = 104.0
            s_full, ew, eh = scale(xmax)
        self.xmin, self.xmax, self.ymin, self.ymax = xmin, xmax, ymin, ymax
        self.scale = s_full
        self.ox = (self.static.pw - ew * s_full) / 2
        self.oy = (self.static.ph - eh * s_full) / 2
        self.px_per_pct = bw / 100 * s_full

    def X(self, x: float) -> float:
        return self.ox + (x - self.xmin) / 100 * self.profile.pcb_mm[0] * self.scale

    def Y(self, y: float) -> float:
        return self.oy + (y - self.ymin) / 100 * self.profile.pcb_mm[1] * self.scale

    def P(self, x: float, y: float) -> tuple[float, float]:
        return self.X(x), self.Y(y)

    def box(self, r) -> tuple[int, int, int, int]:
        x0, y0 = int(round(self.X(r[0]))), int(round(self.Y(r[1])))
        x1, y1 = int(round(self.X(r[2]))), int(round(self.Y(r[3])))
        return x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)

    def mm(self, v: float) -> float:
        return v * self.scale

    # ------------------------------------------------------------------------ static
    def _draw_static(self) -> None:
        cv, p = self.static, self.profile
        cv.gradient_v(0, 0, cv.pw, cv.ph, 0x0B0E14, 0x111822)
        bx0, by0, bx1, by1 = self.box((0, 0, 100, 100))
        self.board_box = (bx0, by0, bx1, by1)
        cv.fill(bx0, by0, bx1, by1, sp.PCB)
        for i in range(bx0 + by0 * cv.pw, len(cv.px), 5):
            if cv.px[i] == sp.PCB and _hash(i, 3) < 9000:
                cv.px[i] = sp.PCB_DARK if _hash(i, 5) & 1 else sp.PCB_ALT
        cv.frame(bx0, by0, bx1, by1, sp.PCB_EDGE)
        self.comps = {c.id: c for c in p.components}
        self._routes()
        self._traces(cv)
        r = max(0.8, self.mm(2.0))
        for hx, hy in p.holes:
            sp.draw_hole(cv, self.X(hx), self.Y(hy), r)

        populated = self._populated()
        self.hot: dict[str, list[int]] = {}
        self.cpu: dict | None = None
        for c in sorted(p.components, key=sp.z_key):
            b = self.box(c.rect)
            self.boxes[c.id] = b
            k = c.kind
            if k == "socket":
                self.cpu = sp.draw_socket(cv, b, c)
                self.cpu_id = c.id
                hot = self.cpu["hot"]
            elif k == "heatsink":
                hot = sp.draw_heatsink(cv, b, c)
            elif k == "choke":
                hot = sp.draw_choke(cv, b, c)
            elif k == "power":
                hot = sp.draw_power(cv, b, c)
            elif k == "dimm":
                hot = sp.draw_dimm(cv, b, c, populated.get(c.id, True))
            elif k == "pcie":
                hot = sp.draw_pcie(cv, b, c)
            elif k == "m2":
                hot = sp.draw_m2(cv, b, c, populated.get(c.id, True), self.px_per_pct)
            elif k == "chipset":
                hot = sp.draw_chipset(cv, b, c)
            elif k == "sata":
                hot = sp.draw_sata(cv, b, c)
            elif k == "fan":
                hot = sp.draw_fan_header(cv, b, c)
            elif k == "header":
                hot = sp.draw_header(cv, b, c)
            elif k == "jumper":
                hot = sp.draw_jumper(cv, b, c)
            elif k == "chip":
                hot = sp.draw_chip(cv, b, c)
            elif k == "io":
                hot = sp.draw_io(cv, b, c)
            elif k == "wifi":
                hot = sp.draw_wifi(cv, b, c)
            elif k == "battery":
                hot = sp.draw_battery(cv, b, c)
            else:
                cv.fill(*b, 0x333333)
                hot = []
            if hot:
                self.hot[c.id] = hot
        self._smd_dust(cv)
        self.bare = bytearray(1 if v in sp.BARE else 0 for v in cv.px)
        self.base = {cid: [cv.px[i] for i in idx] for cid, idx in self.hot.items()}
        self.ihs_base = list(self.base.get(getattr(self, "cpu_id", ""), []))
        if self.card:
            self._card_setup()
        if self.labels:
            self._labels(cv)

    def _populated(self) -> dict[str, bool]:
        s = self.hint
        out = {}
        if s is None:
            return out
        for c in self.profile.of_kind("dimm"):
            out[c.id] = B.dimm(s, c) is not None if s.mem.dimms else True
        for c in self.profile.of_kind("m2"):
            out[c.id] = B.nvme(s, c) is not None
        return out

    def _smd_dust(self, cv: Canvas) -> None:
        hubs = [self.boxes[c.id] for c in self.profile.components if c.kind in ("socket", "chipset", "choke", "chip")]
        if not hubs:
            return
        x0b, y0b, x1b, y1b = self.board_box
        for y in range(max(0, y0b), min(cv.ph, y1b)):
            for x in range(max(0, x0b), min(cv.pw, x1b)):
                i = y * cv.pw + x
                if cv.px[i] not in (sp.PCB, sp.PCB_DARK, sp.PCB_ALT):
                    continue
                near = 0.0
                for (a0, b0, a1, b1) in hubs:
                    d = max(a0 - x, 0, x - a1) + max(b0 - y, 0, y - b1)
                    if d < 4:
                        near = max(near, 1 - d / 4)
                pr = 0.004 + 0.06 * near
                h = _hash(i, 11)
                if h < pr * 65535:
                    cv.px[i] = (0x5E4B33, 0x2A2C30, 0x6B5A3E, 0x34363B)[h & 3]

    # ------------------------------------------------------------------------ routes
    def _path(self, pts_pct: list[tuple[float, float]]) -> list[tuple[int, int]]:
        return polyline_points([self.P(x, y) for x, y in pts_pct])

    def _lanes(self, pts_pct: list[tuple[float, float]]) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
        """Two parallel 1-px lanes along an orthogonal route: inbound on the route itself,
        outbound one pixel to its left, mitred at the corners."""
        pts = [(round(x), round(y)) for x, y in (self.P(x, y) for x, y in pts_pct)]
        pts = [q for i, q in enumerate(pts) if i == 0 or q != pts[i - 1]]
        normals = []
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            dx, dy = (bx > ax) - (bx < ax), (by > ay) - (by < ay)
            normals.append((dy, -dx))
        if not normals:
            return pts, pts
        out = []
        for i, (x, y) in enumerate(pts):
            near = normals[max(0, i - 1): i + 1]
            ox = max(-1, min(1, sum(n[0] for n in near)))
            oy = max(-1, min(1, sum(n[1] for n in near)))
            out.append((x + ox, y + oy))
        return polyline_points(pts), polyline_points(out)

    def _routes(self) -> None:
        """Pixel paths for power (EPS → VRM → socket) and data (devices ↔ CPU / chipset)."""
        p = self.profile
        g = lambda cid: p.by_id(cid)  # noqa: E731
        self.power_paths: list[list[tuple[int, int]]] = []
        # (inbound lane, outbound lane) per route; lanes run device-first
        self.data_paths: dict[str, list[tuple[list[tuple[int, int]], list[tuple[int, int]]]]] = {}
        cpu = next(iter(p.of_kind("socket")), None)
        if cpu is None:
            return
        cx0, cy0, cx1, cy1 = cpu.rect
        ccx = (cx0 + cx1) / 2
        left = sorted([c for c in p.of_kind("choke") if c.group == "L"], key=lambda c: c.rect[1])
        top = sorted([c for c in p.of_kind("choke") if c.group == "LT"], key=lambda c: c.rect[0])
        eps = sorted(p.of_kind("power"), key=lambda c: c.rect[0])
        eps = [c for c in eps if (c.pins or 0) <= 8] or eps[:1]
        if eps and left:
            e = eps[0]
            ex, ey = e.cx, e.rect[3]
            lx = sum(c.cx for c in left) / len(left)
            ly0 = left[0].rect[1]
            for frac in (0.3, 0.75):
                ty = left[0].rect[1] + frac * (left[-1].rect[3] - left[0].rect[1])
                self.power_paths.append(self._path([(ex, ey), (ex, ey + 0.9), (lx, ey + 0.9), (lx, ly0), (lx, ty), (cx0 + 0.8, ty)]))
        if eps and top:
            e = eps[-1]
            ex, ey = e.cx, e.rect[3]
            ty = sum(c.cy for c in top) / len(top)
            for frac in (0.3, 0.7):
                tx = top[0].rect[0] + frac * (top[-1].rect[2] - top[0].rect[0])
                self.power_paths.append(self._path([(ex, ey), (ex, ey + 0.6), (tx, ey + 0.6), (tx, ty), (tx, cy0 + 0.8)]))

        self._atx_routes()

        def add(key, pts):
            self.data_paths.setdefault(key, []).append(self._lanes(pts))

        m21, m22 = g("M2_1"), g("M2_2")
        if m21:
            x, y = m21.rect[0] + 1.6, m21.rect[1]
            add("M2_1", [(x, y), (x, y - 2.0), (ccx - 8, y - 2.0), (ccx - 8, cy1)])
        if m22:
            x, y = m22.rect[0], m22.cy
            add("M2_2", [(x, y), (x - 1.7, y), (x - 1.7, 57.0), (cx0 - 3.3, 57.0), (cx0 - 3.3, cy1 - 2), (cx0, cy1 - 2)])
        if self.x16:
            sx = min(ccx, self.x16.rect[2] - 6)
            add("GPU", [(sx, cy1), (sx, self.x16.rect[1])])
        dimms = p.of_kind("dimm")
        dx = min((d.rect[0] for d in dimms), default=0.0)
        if dimms and dx > cx1:  # one lane per memory channel, across the fan-out to the slots
            for frac in (0.35, 0.65):
                y = cy0 + frac * (cy1 - cy0)
                add("MEM", [(dx, y), (cx1, y)])
        pch = next(iter(p.of_kind("chipset")), None)
        if pch:
            px0, py0, px1, py1 = pch.rect
            pcx = (px0 + px1) / 2
            add("UPLINK", [(pcx, py0), (pcx, py0 - 4), (px0 - 3.7, py0 - 4), (px0 - 3.7, cy1 + 2.6), (cx1 - 2, cy1 + 2.6), (cx1 - 2, cy1)])
            for c in p.of_kind("sata"):
                if c.rect[0] >= px1 - 1:  # right edge, beside the chipset
                    add(c.id, [(px1, (c.rect[1] + c.rect[3]) / 2), (c.rect[0], (c.rect[1] + c.rect[3]) / 2)])
                else:
                    yb = c.rect[1] - 3.2
                    sx = min(max(c.cx, px0 + 1), px1 - 1)
                    add(c.id, [(sx, py1), (sx, yb), (c.cx, yb), (c.cx, c.rect[1])])
            lan, wifi = g("LAN"), g("WIFI")
            trunk = [(px0, py1 - 1.5), (px0 - 2.7, py1 - 1.5), (px0 - 2.7, py1), (29.8, py1), (29.8, 71.6), (22.5, 71.6), (22.5, 60.6)]
            if lan:
                add("LAN", trunk + [(22.5, lan.cy), (lan.rect[2], lan.cy)])
                io = g("IO_LAN")
                if io:
                    add("LAN", [(lan.rect[0], lan.rect[1] - 0.6), (io.rect[2], lan.rect[1] - 0.6)])
            if wifi:
                add("WIFI", trunk + [(15.0, 60.6), (15.0, wifi.cy), (wifi.rect[2], wifi.cy)])

    def _atx_routes(self) -> None:
        """24-pin → DIMMs (5 V), → PCIe x16 slot (12 V), → chipset and M.2 (3.3 V)."""
        p = self.profile
        self.atx_paths: dict[str, list[list[tuple[int, int]]]] = {}
        atx = next((c for c in p.of_kind("power") if (c.pins or 0) > 8), None)
        dimms = p.of_kind("dimm")
        if atx is None or not dimms:
            return
        self.atx = atx
        ax0 = atx.rect[0]
        sx = (max(d.rect[2] for d in dimms) + ax0) / 2  # spine between the DIMMs and the connector
        ty = atx.cy
        trunk = [(ax0, ty), (sx, ty)]
        add = lambda rail, pts: self.atx_paths.setdefault(rail, []).append(self._path(pts))  # noqa: E731
        top = min(d.rect[1] for d in dimms) + 3
        bottom = max(d.rect[3] for d in dimms) - 3
        add("dimm", trunk + [(sx, top)])
        add("dimm", trunk + [(sx, bottom)])
        if self.x16:
            ym = self.x16.rect[1] - 2.4
            xe = self.x16.rect[2] - 1
            add("slot", trunk + [(sx, ym), (xe, ym), (xe, self.x16.rect[1])])
        pch = next(iter(p.of_kind("chipset")), None)
        if pch:
            add("storage", trunk + [(sx, pch.cy), (pch.rect[2], pch.cy)])
        m21, m22 = p.by_id("M2_1"), p.by_id("M2_2")
        if m21 and self.x16:
            ym = self.x16.rect[1] - 2.4
            xm = m21.rect[2] + 0.8
            add("storage", trunk + [(sx, ym), (xm, ym), (xm, m21.cy), (m21.rect[2], m21.cy)])
        if m22 and pch:
            yb = pch.rect[3] + 1.5
            xm = m22.rect[2] + 0.8
            add("storage", trunk + [(sx, yb), (xm, yb), (xm, m22.cy), (m22.rect[2], m22.cy)])

    def _traces(self, cv: Canvas) -> None:
        """Static copper: DIMM fan-out and the data/power paths as faint traces."""
        p = self.profile
        cpu = next(iter(p.of_kind("socket")), None)
        dimms = p.of_kind("dimm")
        if cpu and dimms:
            x0 = self.X(cpu.rect[2])
            x1 = self.X(max(d.rect[2] for d in dimms))
            ya, yb = self.Y(cpu.rect[1] + 3), self.Y(cpu.rect[3] - 3)
            y = ya
            while y < yb:
                cv.line(x0, y, x1, y, sp.PCB_TRACE)
                y += 2
        for lanes in self.data_paths.values():
            for pair in lanes:
                for path in pair:
                    for x, y in path:
                        cv.set(x, y, sp.PCB_TRACE)
        for path in self.power_paths + [q for paths in self.atx_paths.values() for q in paths]:
            for x, y in path:
                cv.set(x, y, sp.PCB_PLANE)

    # ------------------------------------------------------------------------ card
    def _card_setup(self) -> None:
        """Ghost of the graphics card's footprint over the x16 slot (cooler side faces down)."""
        s = self.x16
        cy = (s.rect[1] + s.rect[3]) / 2
        gpu = B.gpu(self.hint) if self.hint else None
        length = 304 / self.profile.pcb_mm[0] * 100
        thick = 61 / self.profile.pcb_mm[1] * 100
        rect = (-1.6, cy - 2.2, min(-1.6 + length, self.xmax - 0.6), min(cy - 2.2 + thick, 99.4))
        self.card_rect = rect
        c = Component("GPU", "gpu", rect, label=gpu_short(gpu.name) if gpu else "GPU", part=gpu.name if gpu else None,
                      bind="gpu:x16", conf="inferred", note="Footprint drawn from a 304 × 61 mm triple-slot card.")
        self.comps["GPU"] = c
        self.boxes["GPU"] = self.box(rect)
        x0, y0, x1, y1 = self.boxes["GPU"]
        cv = self.static
        self.card_px = sp.box_indices(cv, x0, y0, x1, y1)
        # the card only shows in the gaps: bare PCB, traces and the overhang past the board edge
        bx0, by0, bx1, by1 = self.board_box
        self.card_gap = bytearray(len(cv.px))
        for i in self.card_px:
            x, y = i % cv.pw, i // cv.pw
            if self.bare[i] or not (bx0 <= x < bx1 and by0 <= y < by1):
                self.card_gap[i] = 1
        self.card_body = [i for i in self.card_px if self.card_gap[i]]
        edge = set()
        for x in range(x0, x1):
            edge.add((x, y0))
            edge.add((x, y1 - 1))
        for y in range(y0, y1):
            edge.add((x0, y))
            edge.add((x1 - 1, y))
        self.card_edge = [sp.idx(cv, x, y) for x, y in sorted(edge) if sp.idx(cv, x, y) is not None]
        self.card_dash = [sp.idx(cv, x, y) for x, y in sorted(edge)
                          if sp.idx(cv, x, y) is not None and (x + y) // 2 % 2 == 0]
        H = y1 - y0
        L = x1 - x0
        fr = max(1.5, H * 0.40)
        n = 3 if L > 6 * fr else 2
        self.card_fans = [(x0 + L * (k + 0.5) / n, y0 + H / 2, fr) for k in range(n)]

    def frame(self, t, snap=None, selected=None, hover=None, mode="live", highlight=None):
        self._focus = {selected, hover} - {None}
        return super().frame(t, snap, selected, hover, mode, highlight)

    def _card_target(self) -> float:
        """Card opacity: solid when it is the subject, near-invisible while peeking at a part under it."""
        if "GPU" in self._focus:
            return 1.0
        cx0, cy0, cx1, cy1 = self.boxes["GPU"]
        for cid in self._focus:
            b = self.boxes.get(cid)
            if b and b[0] < cx1 and cx0 < b[2] and b[1] < cy1 and cy0 < b[3]:
                return 0.15
        return 0.85

    def _card_plot(self, cv: Canvas, x: int, y: int, c: int, a: float) -> None:
        if 0 <= x < cv.pw and 0 <= y < cv.ph and self.card_gap[y * cv.pw + x]:
            cv.blend(x, y, c, a)

    def _draw_card(self, cv: Canvas, t: float, dt: float, snap: SystemSnapshot | None, mode: str) -> None:
        gpu = B.gpu(snap) if snap else None
        px = cv.px
        target = self._card_target()
        self._card_alpha += (target - self._card_alpha) * (min(1.0, dt * 10) if dt else 1.0)
        al = self._card_alpha
        tint = CARD
        if mode == "thermal" and gpu is not None:
            tint = mix(CARD, ramp(heat01(gpu.temp, 30, 88), HEAT), 0.55)
        body = 0.72 * al
        for i in self.card_body:
            px[i] = mix(px[i], tint, body)
        util = (gpu.util or 0) / 100 if gpu else 0.0
        ec = mix(CARD_EDGE, 0x76B900, 0.15 + 0.45 * util) if mode == "live" else CARD_EDGE
        ea = 0.35 + 0.65 * al
        for i in self.card_edge if al > 0.5 else self.card_dash:  # dashed while peeking underneath
            px[i] = mix(px[i], ec, ea)
        x0, y0, x1, y1 = self.boxes["GPU"]
        bx = int(self.X(-1.6))
        cv.fill(bx, y0, bx + max(1, int(self.mm(1.6))), y1, sp.SILVER)
        speed = ((gpu.fan_pct or 0) / 100 if gpu else 0.0)
        self._card_angle += dt * speed * 9.0
        for k, (fx, fy, fr) in enumerate(self.card_fans):
            for a in range(0, 360, 12):
                ang = math.radians(a)
                self._card_plot(cv, int(fx + fr * math.cos(ang)), int(fy + fr * math.sin(ang)), 0x4A515B, 0.6 * al)
            blades = 7
            for j in range(blades):
                ang = self._card_angle * (1 if k % 2 == 0 else -1) + 2 * math.pi * j / blades
                for x, y in line_points(fx + 0.25 * fr * math.cos(ang), fy + 0.25 * fr * math.sin(ang),
                                        fx + 0.92 * fr * math.cos(ang + 0.35), fy + 0.92 * fr * math.sin(ang + 0.35)):
                    self._card_plot(cv, x, y, 0x8C96A2, 0.32 * al)
            hub = max(0.6, fr * 0.2)
            for y in range(int(fy - hub - 1), int(fy + hub + 2)):
                for x in range(int(fx - hub - 1), int(fx + hub + 2)):
                    if (x + 0.5 - fx) ** 2 + (y + 0.5 - fy) ** 2 <= hub * hub:
                        self._card_plot(cv, x, y, 0x30343B, al)
        if self.labels and gpu is not None:
            parts = [gpu_short(gpu.name)]
            if gpu.temp is not None:
                parts.append(f"{gpu.temp:.0f}°C")
            if gpu.power_w is not None:
                parts.append(f"{gpu.power_w:.0f}W")
            if gpu.util is not None:
                parts.append(f"{gpu.util:.0f}%")
            row = min(cv.h - 1, (y1 - 2) // 2)
            for txt in (" · ".join(parts), " ".join(parts[:2]), parts[0]):
                if cv.put_text_center(x0, x1, row, txt, 0xE6E9ED, None, bold=True):
                    break

    # ------------------------------------------------------------------------ labels
    def _labels(self, cv: Canvas) -> None:
        for cid, c in self.comps.items():
            if c.kind in ("dimm", "choke", "fan", "header", "jumper", "battery", "io", "gpu", "socket"):
                continue
            text = c.label
            if not text:
                continue
            x0, y0, x1, y1 = self.boxes[cid]
            row = ((y0 + y1) // 2) // 2
            fg, bg = sp.label_colors(c.kind)
            if c.kind == "m2":
                x0 += max(1, round(3.1 * self.px_per_pct))
            if c.kind == "pcie":
                fg = sp.SILK if (c.lanes or 1) < 16 else 0x8FB8DE
                row = (y1 + 1) // 2 if (y1 - y0) < 4 else row
            if c.kind == "heatsink":
                continue  # live text goes there
            cands = [text]
            if c.kind == "power":
                cands = [text, text.replace("CPU_", "").replace("ATX_", ""), "EPS" if (c.pins or 0) <= 8 else "ATX"]
                row = (y1 + 1) // 2 if (y1 - y0) < 6 else row
                x0, x1 = x0 + 1, x1 - 1
            for t in cands:
                if cv.put_text_center(x0, x1, row, t, fg, bg):
                    break
        # silk-screen board name between socket and the first M.2 slot
        cpu = next(iter(self.profile.of_kind("socket")), None)
        if cpu is not None:
            y = self.Y(cpu.rect[3] + 4.2)
            name = self.profile.name
            x0, x1 = int(self.X(cpu.rect[0] - 4)), int(self.X(cpu.rect[2] + 7))
            for t in (name, name.replace("MSI ", ""), name.split()[-1]):
                if cv.put_text_center(x0, x1, int(y) // 2, t, 0x7D8590):
                    break
        # DIMM slot names under the slots when they fit
        for c in self.profile.of_kind("dimm"):
            x0, y0, x1, y1 = self.boxes[c.id]
            if (x1 - x0) >= len(c.label or "") and c.label:
                cv.put_text_center(x0, x1, min(cv.h - 1, (y1 + 1) // 2), c.label, sp.SILK)

    # ------------------------------------------------------------------------- live
    def draw_live(self, cv: Canvas, t: float, snap: SystemSnapshot | None, mode: str) -> None:
        dt = self.dt(t)
        self._mode = mode
        if snap is None or mode == "off":
            if self.card:
                self._draw_card(cv, t, dt, snap, "off")
            return
        frame = int(t * 12)
        px = cv.px
        cpu = snap.cpu
        pkg = cpu.package_w or 0.0
        pf = max(0.0, min(1.0, pkg / 120))
        util = cpu.util / 100
        vrm_t = B.superio_reading(snap, "VRM MOS", "temp")
        live = mode == "live"
        atx = B.atx_estimate(snap)
        self._atx_w = atx

        for cid, idx in self.hot.items():
            c = self.comps[cid]
            base = self.base[cid]
            k = c.kind
            if k == "socket":
                self._cpu(cv, frame, snap, mode)
            elif k == "choke":
                amt = pf * (0.6 if live else 0.9)
                tint = 0xFF9A3C if live else ramp(heat01(vrm_t.value if vrm_t else None, 30, 95), HEAT)
                if live:
                    amt *= 0.85 + 0.15 * math.sin(t * 9 + (hash(cid) % 7))
                else:
                    amt = 0.75
                for i, b in zip(idx, base):
                    px[i] = mix(b, tint, amt)
            elif k == "heatsink" and not live:
                tc = ramp(heat01(vrm_t.value if vrm_t else None, 30, 95), HEAT)
                for i, b in zip(idx, base):
                    px[i] = mix(b, tc, 0.55)
            elif k == "power":
                amt = pf if (c.pins or 0) <= 8 else min(1.0, sum(atx.values()) / 150)
                for i, b in zip(idx, base):
                    px[i] = mix(b, 0xFFF2A0, amt * 0.9)
            elif k == "dimm":
                self._dimm(cv, c, idx, base, snap, live)
            elif k == "m2":
                d = B.nvme(snap, c)
                if d is None:
                    continue
                if live:
                    rl, wl = level(d.read_bps), level(d.write_bps)
                    for n, (i, b) in enumerate(zip(idx, base)):
                        h = _hash(i, frame)
                        if h < rl * 0.5 * 65535:
                            px[i] = mix(b, READ, 0.8)
                        elif h > 65535 - wl * 0.5 * 65535:
                            px[i] = mix(b, WRITE, 0.8)
                else:
                    tc = ramp(heat01(d.temp, 25, 80), HEAT)
                    for i, b in zip(idx, base):
                        px[i] = mix(b, tc, 0.7)
            elif k == "sata" and live:
                disks = B.sata(snap, c)
                lv = max((level(B.disk_activity(d)) for d in disks), default=0.0)
                on = bool(disks)
                for i, b in zip(idx, base):
                    if on and _hash(i, frame) < (0.08 + lv) * 40000:
                        px[i] = mix(b, READ, 0.85)
                    elif on:
                        px[i] = mix(b, 0x2F6F8A, 0.35)
            elif k == "pcie" and c.bind == "gpu:x16" and live:
                g = B.gpu(snap)
                lv = (g.util or 0) / 100 if g else 0.0
                for i, b in zip(idx, base):
                    if _hash(i, frame) < lv * 0.7 * 65535:
                        px[i] = 0xFFF3B0
            elif k == "chipset":
                if live:
                    lv = level(B.chipset_traffic(snap, B.chipset_uplink(snap)))
                    for i, b in zip(idx, base):
                        if _hash(i, frame) < lv * 0.35 * 65535:
                            px[i] = mix(b, SPARK, 0.6)
                else:
                    tr = B.temp(snap, c)
                    tc = ramp(heat01(tr.value if tr else None, 30, 90), HEAT)
                    for i, b in zip(idx, base):
                        px[i] = mix(b, tc, 0.6)
            elif k in ("io", "wifi", "chip") and c.bind and c.bind.startswith("net:"):
                n = B.net(snap, c)
                rx, tx = (level(n.rx_bps), level(n.tx_bps)) if n else (0.0, 0.0)
                for j, i in enumerate(idx if k != "chip" else idx[:1]):
                    blink = _hash(j, frame)
                    if n is None or not n.up:
                        px[i] = 0x333333
                    elif j % 2 == 0:  # link LED, flashing with receive
                        px[i] = READ if rx > 0 and blink < (0.1 + rx) * 50000 else sp.LED_GREEN
                    else:  # activity LED, flashing with transmit
                        px[i] = WRITE if tx > 0 and blink < (0.1 + tx) * 50000 else 0x3A2A10

        if live:
            self._flows(cv, t, dt, snap, pf)
            self._atx_flows(cv, dt, atx)
        self._fans(cv, dt, snap)
        if self.card:
            self._draw_card(cv, t, dt, snap, mode)
        if self.labels:
            self._live_labels(cv, snap, vrm_t)

    # ---------------------------------------------------------------- live helpers
    def _cpu(self, cv: Canvas, frame: int, snap: SystemSnapshot, mode: str) -> None:
        if not self.cpu:
            return
        px = cv.px
        cpu = snap.cpu
        hot = self.cpu["hot"]
        heat = heat01(cpu.tctl, 30, 95)
        hc = ramp(heat, HEAT)
        if mode == "thermal":
            ix0, iy0, ix1, iy1 = self.cpu["ihs"]
            cv.fill(ix0, iy0, ix1, iy1, sp.SUBSTRATE)
            # IOD
            a0, b0, a1, b1 = self.cpu["iod"]
            cv.fill(a0, b0, a1, b1, mix(0x3A4150, hc, 0.45))
            cv.frame(a0, b0, a1, b1, 0x1A1D22)
            # CCD: 8 core tiles in two columns around the L3; fused cores greyed
            a0, b0, a1, b1 = self.cpu["ccd"]
            cv.fill(a0, b0, a1, b1, 0x1A1D22)
            w, h = a1 - a0, b1 - b0
            l3 = mix(0x3A4150, ramp(heat01(cpu.tccd[0] if cpu.tccd else cpu.tctl, 30, 95), HEAT), 0.5)
            cv.fill(a0 + w // 2 - max(0, w // 10), b0, a0 + w // 2 + max(1, w // 10), b1, l3)
            slots = 8 if cpu.cores <= 8 else 16
            rows = slots // 2
            for k in range(slots):
                col, row = k % 2, k // 2
                tx0 = a0 + (0 if col == 0 else w // 2 + max(1, w // 10))
                tx1 = a0 + (w // 2 - max(0, w // 10) if col == 0 else w)
                ty0 = b0 + h * row // rows
                ty1 = b0 + h * (row + 1) // rows
                if k < cpu.cores:
                    u = cpu.core_util(k) / 100
                    c = ramp(u, LOAD)
                else:
                    c = 0x2A2D33
                cv.fill(tx0, ty0, max(tx0 + 1, tx1), max(ty0 + 1, ty1 - (1 if ty1 - ty0 > 2 else 0)), c)
            return
        thr = int(cpu.util / 100 * 0.55 * 65535)
        for i, b in zip(hot, self.ihs_base):
            h = _hash(i, frame)
            if h < thr:
                px[i] = mix(b, SPARK, 0.25 + 0.4 * ((h >> 4) & 15) / 15)
            else:
                px[i] = mix(b, hc, 0.22)

    def _dimm(self, cv: Canvas, c: Component, idx: list[int], base: list[int], snap: SystemSnapshot, live: bool) -> None:
        px = cv.px
        d = B.dimm(snap, c)
        if live:
            frac = snap.mem.used_frac
            n = len(idx)
            lit = int(round(frac * n))
            for j, (i, b) in enumerate(zip(idx, base)):
                if j < lit:
                    px[i] = mix(b, ramp(j / max(1, n - 1), LOAD), 0.85)
        else:
            tc = ramp(heat01(d.temp if d else None, 25, 85), HEAT)
            x0, y0, x1, y1 = self.boxes[c.id]
            for i in sp.box_indices(cv, x0, y0 + 1, x1, y1 - 1):
                px[i] = mix(px[i], tc, 0.7)

    def _flow(self, cv: Canvas, path, phase: float, lv: float, color: int, reverse: bool) -> None:
        n = len(path)
        if n < 2 or lv <= 0.003:
            return
        count = max(1, int(n / 7 * (0.15 + lv)))
        spacing = n / count
        pw = cv.pw
        for k in range(count):
            pos = (phase + k * spacing) % n
            i = int(pos)
            for tail in range(3):
                j = (n - 1 - i + tail) if reverse else (i - tail)
                if 0 <= j < n:
                    x, y = path[j]
                    if 0 <= x < pw and 0 <= y < cv.ph and self.bare[y * pw + x]:
                        cv.blend(x, y, color, 1.0 - tail * 0.35)

    def _glow(self, cv: Canvas, paths, sat: float, t: float) -> None:
        """Light the trace itself by link load: faint green when lightly used, thick red and
        pulsing when saturated. The dots on top still show direction and raw rate."""
        if sat < 0.03:
            return
        col = ramp(sat, SAT)
        a = 0.15 + 0.7 * sat
        if sat >= 0.9:
            a *= 0.7 + 0.3 * math.sin(t * 10)
        pw, ph, bare = cv.pw, cv.ph, self.bare
        offsets = ((0, 0), (1, 0), (0, 1), (-1, 0), (0, -1)) if sat >= 0.75 else \
            ((0, 0), (1, 0), (0, 1)) if sat >= 0.5 else ((0, 0),)
        for path in paths:
            for x, y in path:
                for k, (ox, oy) in enumerate(offsets):
                    xx, yy = x + ox, y + oy
                    if 0 <= xx < pw and 0 <= yy < ph and bare[yy * pw + xx]:
                        cv.blend(xx, yy, col, a if k == 0 else a * 0.5)

    def _advance(self, key: str, dt: float, lv: float) -> float:
        ph = self._phase.get(key, 0.0) + dt * (4 + 50 * lv)
        self._phase[key] = ph
        return ph

    def _flows(self, cv: Canvas, t: float, dt: float, snap: SystemSnapshot, pf: float) -> None:
        ph = self._advance("power", dt, pf)
        for path in self.power_paths:
            n = len(path)
            if n < 2:
                continue
            count = max(1, int(n / 8 * (0.15 + pf)))
            spacing = n / count
            for k in range(count):
                i = int((ph + k * spacing) % n)
                for tail in range(3):
                    j = i - tail
                    if 0 <= j < n:
                        x, y = path[j]
                        cv.blend(x, y, POWER, 1.0 - tail * 0.35)
        for key, lanes in self.data_paths.items():
            rd, wr, sat, _ = self._link(snap, key)
            if sat:
                self._glow(cv, [q for pair in lanes for q in pair], sat, t)
            lr, lw = level(rd), level(wr)
            pr = self._advance(key + ":r", dt, lr)
            pw_ = self._advance(key + ":w", dt, lw)
            for in_lane, out_lane in lanes:
                # inbound travels device → CPU (lanes are drawn device-first), outbound the other way
                self._flow(cv, in_lane, pr, lr, READ, reverse=False)
                self._flow(cv, out_lane, pw_, lw, WRITE, reverse=True)

    def _link(self, snap: SystemSnapshot, key: str) -> tuple[float, float, float | None, bool]:
        """(towards CPU, away from CPU, share of link capacity or None, measured) for a data route."""
        p = self.profile
        rd = wr = 0.0
        sat = None  # share of the link's capacity in use, where we know the capacity
        measured = True
        if key in ("M2_1", "M2_2"):
            c = p.by_id(key)
            d = B.nvme(snap, c) if c else None
            rd, wr = (d.read_bps or 0.0, d.write_bps or 0.0) if d else (0.0, 0.0)
            sat = B.disk_link_load(d)
            measured = d is not None
        elif key == "GPU":
            g = B.gpu(snap)
            if g and (g.pcie_rx_bps is not None or g.pcie_tx_bps is not None):
                rd, wr = g.pcie_tx_bps or 0.0, g.pcie_rx_bps or 0.0  # tx = card → CPU
                sat = B.gpu_pcie_load(g)
            else:  # no throughput counters: guess from utilisation
                rd = wr = ((g.util or 0) / 100) * 4e8 if g else 0.0
                measured = False
        elif key == "MEM":
            rd, wr = snap.mem.read_bps or 0.0, snap.mem.write_bps or 0.0
            sat = B.mem_load(snap)
            measured = snap.mem.read_bps is not None or snap.mem.write_bps is not None
        elif key == "UPLINK":
            up = B.chipset_uplink(snap)
            rd, wr = B.chipset_split(snap, up)
            sat = B.uplink_load(snap, up)
            measured = up is not None
        elif key in ("LAN", "WIFI"):
            n = snap.net_of_kind("ethernet" if key == "LAN" else "wifi")
            rd, wr = ((n.rx_bps or 0.0), (n.tx_bps or 0.0)) if n else (0.0, 0.0)
            sat = B.net_link_load(n) if key == "LAN" else None
            measured = n is not None
        else:
            c = p.by_id(key)
            disks = B.sata(snap, c) if c is not None and c.kind == "sata" else []
            for d in disks:
                rd += d.read_bps or 0.0
                wr += d.write_bps or 0.0
                sat = max(sat or 0.0, B.disk_link_load(d) or 0.0)
            measured = bool(disks)
        return rd, wr, sat, measured

    def _atx_flows(self, cv: Canvas, dt: float, watts: dict[str, float]) -> None:
        for rail, paths in self.atx_paths.items():
            lv = max(0.0, min(1.0, watts.get(rail, 0.0) / RAIL_REF_W[rail]))
            ph = self._advance("atx:" + rail, dt, lv)
            col = RAIL_COLORS[rail]
            for path in paths:
                n = len(path)
                if n < 2:
                    continue
                count = max(1, int(n / 9 * (0.2 + lv)))
                spacing = n / count
                for k in range(count):
                    i = int((ph + k * spacing) % n)
                    for tail in range(3):
                        j = i - tail
                        if 0 <= j < n:
                            x, y = path[j]
                            cv.blend(x, y, col, 1.0 - tail * 0.35)

    def _fans(self, cv: Canvas, dt: float, snap: SystemSnapshot) -> None:
        for c in self.profile.of_kind("fan"):
            r = B.fan(snap, c)
            rpm = r.value if r else 0.0
            ph = self._fan_phase.get(c.id, 0.0) + dt * rpm / 120
            self._fan_phase[c.id] = ph
            x0, y0, x1, y1 = self.boxes[c.id]
            row = ((y0 + y1) // 2) // 2
            col = (x0 + x1) // 2
            if rpm > 0:
                cv.put_text(col, row, sp.spinner(ph * 4), 0x6FE3FF, None, bold=True)
            elif r is not None:
                cv.put_text(col, row, "·", 0x55606E)

    def _live_labels(self, cv: Canvas, snap: SystemSnapshot, vrm_t) -> None:
        cpu = snap.cpu
        if self.cpu:
            ix0, iy0, ix1, iy1 = self.cpu["ihs"]
            row = ((iy0 + iy1) // 2) // 2
            name = cpu_short(cpu.model)
            temp = f"{cpu.tctl:.0f}°C" if cpu.tctl is not None else ""
            watts = f"{cpu.package_w:.0f}W" if cpu.package_w is not None else ""
            if self._mode == "thermal":
                bx0, by0, bx1, by1 = self.boxes[self.cpu_id]
                cv.put_text_center(bx0, bx1, min(cv.h - 1, (by1 + 1) // 2), f"Tctl {temp}", 0xE6E9ED, None, bold=True)
            else:
                for s in (f"{name} {temp}".strip(), temp, name):
                    if s and cv.put_text_center(ix0, ix1, row, s, 0x101215, None, bold=True):
                        break
                if watts and iy1 - iy0 >= 8:
                    cv.put_text_center(ix0, ix1, row + 1, f"{watts} {cpu.util:.0f}%", 0x30343B)
        atx = getattr(self, "atx", None)
        if atx is not None and self._mode == "live":
            x0, y0, x1, y1 = self.boxes[atx.id]
            tot = sum(getattr(self, "_atx_w", {}).values())
            row = (y1 + 1) // 2
            if row < cv.h:
                cv.put_text_center(x0 - 3, min(cv.w, x1 + 2), row, f"≈{tot:.0f}W", 0xF2C14E, None, True)
        vcore = B.volt(snap, "CPU Vcore")
        for cid in ("HS_T", "HS_L"):
            c = self.comps.get(cid)
            if not c:
                continue
            x0, y0, x1, y1 = self.boxes[cid]
            row = ((y0 + y1) // 2) // 2
            if cid == "HS_T":
                txt = [f"Vcore {vcore:.2f}V" if vcore else "VRM", "VRM"]
            else:
                txt = [f"{vrm_t.value:.0f}°C" if vrm_t else "VRM", "VRM"]
            for s in txt:
                if cv.put_text_center(x0, x1, row, s, 0xE6E9ED, None, bold=True):
                    break
        for c in self.profile.of_kind("m2"):
            d = B.nvme(snap, c)
            if d is None:
                continue
            x0, y0, x1, y1 = self.boxes[c.id]
            x0 += max(1, round(3.1 * self.px_per_pct))
            row = ((y0 + y1) // 2) // 2
            for x in range(x0, x1):  # replace the static slot label
                cv.text.pop((x, row), None)
            parts = [c.label or c.id]
            if d.temp is not None:
                parts.append(f"{d.temp:.0f}°C")
            if (d.read_bps or 0) + (d.write_bps or 0) > 0:
                parts.append(ch.human_rate_pair(d.read_bps, d.write_bps))
            for s in (" ".join(parts), " ".join(parts[:2]), parts[0]):
                if cv.put_text_center(x0, x1, row, s, 0xE6E9ED, None):
                    break
        if self._mode == "live":
            self._traffic_key(cv, snap)

    def _traffic_key(self, cv: Canvas, snap: SystemSnapshot) -> None:
        """Legend for the two-lane data traces plus each link's ▲in ▼out rate, in the empty space
        right of the board; just the legend below or above the board when that space is missing."""
        bx0, by0, bx1, by1 = self.board_box
        legend = [("▲ to CPU", READ), ("  ", None), ("▼ from CPU", WRITE)]
        rows = []
        for key in self.data_paths:
            rd, wr, _, measured = self._link(snap, key)
            if measured:
                c = self.profile.by_id(key)
                name = {"GPU": "GPU", "MEM": "DRAM", "UPLINK": "Chipset", "WIFI": "Wi-Fi"}.get(
                    key, c.label if c is not None and c.label else key)
                rows.append((name, rd, wr))
        x0 = bx1 + 2
        nw = max((len(r[0]) for r in rows), default=0) + 1
        bottom = self.boxes["GPU"][1] // 2 if self.card else cv.h
        if cv.w - x0 >= nw + 20 and bottom > 3:
            self._put_runs(cv, x0, 1, legend)
            for y, (name, rd, wr) in enumerate(rows, start=2):
                if y >= bottom:
                    break
                cv.put_text(x0, y, name, 0x8FB8DE)
                cv.put_text(x0 + nw, y, "▲" + ch.human_bytes(rd, True), READ)
                cv.put_text(x0 + nw + 11, y, "▼" + ch.human_bytes(wr, True), WRITE)
            return
        width = sum(len(t) for t, _ in legend)
        below, above = (by1 + 1) // 2, by0 // 2 - 1
        row = below if below < cv.h else above if above >= 0 else None
        if row is not None:
            self._put_runs(cv, max(0, bx1 - width), row, legend)

    @staticmethod
    def _put_runs(cv: Canvas, x: int, y: int, runs) -> None:
        for txt, col in runs:
            if col is not None:
                cv.put_text(x, y, txt, col, None, True)
            x += len(txt)
