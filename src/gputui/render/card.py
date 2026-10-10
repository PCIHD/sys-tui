"""Card scene: the cooler seen from the fan side, fans spinning at their live RPM."""

from __future__ import annotations

import math

from ..boards import BoardProfile, Component
from ..canvas import Canvas, mix, shade
from ..chips import SkuSpec
from ..telemetry import GpuSnapshot
from . import Scene
from . import sprites as sp

SHROUD = {"fe": (0xB9BDC2, 0x1A1B1E), "default": (0x2B2E33, 0x3F444C)}


class CardScene(Scene):
    def __init__(self, profile: BoardProfile, sku: SkuSpec, w: int, h: int, labels: bool = True):
        super().__init__(w, h)
        self.profile, self.sku, self.labels = profile, sku, labels
        self.title = f"{profile.short} — card (fan side)"
        L, H = profile.card_mm
        cv = self.static
        self.bracket_mm = 4.0
        cable_mm = 16.0
        ext_w, ext_h = L + self.bracket_mm + 2, H + cable_mm + 2
        s = min((cv.pw - 4) / ext_w, (cv.ph - 4) / ext_h)
        self.scale = s
        self.ox = (cv.pw - ext_w * s) / 2 + self.bracket_mm * s
        self.oy = (cv.ph - ext_h * s) / 2 + cable_mm * s
        self.fans: list[tuple[str, float, float, float, list]] = []
        self._draw()

    def P(self, xmm: float, ymm: float) -> tuple[float, float]:
        return self.ox + xmm * self.scale, self.oy + ymm * self.scale

    def _draw(self) -> None:
        cv, p, s = self.static, self.profile, self.scale
        L, H = p.card_mm
        cv.gradient_v(0, 0, cv.pw, cv.ph, 0x0B0E14, 0x111822)
        # bracket (edge-on) with port openings
        bx0, by0 = self.P(-self.bracket_mm, -6)
        bx1, by1 = self.P(-1, H + 4)
        cv.fill(int(bx0), int(by0), int(bx1), int(by1), sp.SILVER_DARK)
        cv.fill(int(bx0), int(by0), int(bx0) + 1, int(by1), sp.SILVER)
        body, accent = SHROUD["fe"] if p.kind == "fe" else SHROUD["default"]
        x0, y0 = self.P(0, 0)
        x1, y1 = self.P(L, H)
        X0, Y0, X1, Y1 = int(x0), int(y0), int(x1), int(y1)
        cv.fill(X0, Y0, X1, Y1, body)
        # chamfered corners
        r = max(1, int(4 * s))
        for i in range(r):
            for (cx, cy, dx, dy) in ((X0, Y0, 1, 1), (X1 - 1, Y0, -1, 1), (X0, Y1 - 1, 1, -1), (X1 - 1, Y1 - 1, -1, -1)):
                for j in range(r - i):
                    cv.set(cx + dx * j, cy + dy * i, 0x0E121A)
        cv.frame(X0, Y0, X1, Y1, shade(body, 0.7))
        if p.kind == "fe":
            cv.fill(X0 + int(8 * s), Y0 + int(6 * s), X1 - int(8 * s), Y1 - int(6 * s), accent)
        else:
            # angular accent strokes
            for k in range(3):
                ax, ay = self.P(L * (0.05 + 0.3 * k), H * 0.08)
                bx, by = self.P(L * (0.12 + 0.3 * k), H * 0.92)
                cv.line(ax, ay, bx, by, accent)
        # PCB "x-ray" outline: where the board ends and the flow-through begins
        pw_mm, ph_mm = p.pcb_mm
        py0 = (H - ph_mm) / 2
        ax, ay = self.P(0, py0)
        bx, by = self.P(pw_mm, py0 + ph_mm)
        for x in range(int(ax), int(bx), 2):
            cv.set(x, int(ay), 0x2E6B47)
            cv.set(x, int(by), 0x2E6B47)
        for y in range(int(ay), int(by), 2):
            cv.set(int(bx), y, 0x2E6B47)
        if p.flow_through or pw_mm < L - 40:
            fx0, _ = self.P(pw_mm + 4, 0)
            for x in range(int(fx0), X1 - 2, 2):
                cv.fill(x, Y0 + 2, x + 1, Y1 - 2, mix(body, 0x000000, 0.35))
        # fans
        n = p.fans
        R = min(p.fan_mm / 2, H / 2 - 4)
        if p.kind == "fe":
            centers = [R + 10, L - R - 10][:n]
        else:
            span = L - 12
            centers = [6 + span * (i + 0.5) / n for i in range(n)]
        for i, cxmm in enumerate(centers):
            cx, cy = self.P(cxmm, H / 2)
            rp = R * s
            ring = []
            for y in range(int(cy - rp - 1), int(cy + rp + 2)):
                for x in range(int(cx - rp - 1), int(cx + rp + 2)):
                    d = math.hypot(x + 0.5 - cx, y + 0.5 - cy) / rp
                    if d <= 1 and 0 <= x < cv.pw and 0 <= y < cv.ph:
                        ring.append((y * cv.pw + x, d, math.atan2(y + 0.5 - cy, x + 0.5 - cx)))
                        cv.set(x, y, 0x0D0E10 if d < 0.93 else shade(accent, 1.2))
            fid = f"FAN{i + 1}"
            k = max(1, len(self._fan_ctrls_hint()))
            comp = Component(fid, "fan", (0, 0, 0, 0), label=f"Fan {i + 1}", channel=min(i * k // n, k - 1),
                             part=f"{p.fan_mm:g} mm axial fan", conf="inferred" if k != n else "measured")
            self.comps[fid] = comp
            self.boxes[fid] = (int(cx - rp), int(cy - rp), int(cx + rp), int(cy + rp))
            self.fans.append((fid, cx, cy, rp, ring))
        # power connector + cable on the top edge, above where it sits on the PCB
        conn = next(iter(p.of_kind("power")), None)
        if conn:
            cxmm = conn.cx / 100 * pw_mm
            cw = 20 if "12V" in (conn.part or "") else 22
            a0, a1 = self.P(cxmm - cw / 2, -3)
            b0, b1 = self.P(cxmm + cw / 2, 1)
            self.conn_box = (int(a0), int(a1), int(b0), int(b1))
            cv.fill(*self.conn_box, 0x151515)
            ca, cb = self.P(cxmm - cw / 2 + 2, -16)
            cv.fill(int(ca), int(cb), int(b0) - int(2 * s), int(a1), 0x1C1C1C)
            for x in range(int(ca), int(b0) - int(2 * s), 2):
                cv.fill(x, int(cb), x + 1, int(a1), 0x2A2A2A)
            self.comps[conn.id] = conn
            self.boxes[conn.id] = self.conn_box
        # cooler as a whole (click anywhere on the shroud)
        self.comps["COOLER"] = Component("COOLER", "cooler", (0, 0, 100, 100), label="Cooler", part=p.cooler or "cooler")
        self.boxes["COOLER"] = (X0, Y0, X1, Y1)
        if self.labels:
            name = p.short
            cv.put_text(X0 + 2, min(cv.h - 1, (Y1 - 2) // 2), name[: max(0, X1 - X0 - 4)], 0xC9D1D9, None)

    def _fan_ctrls_hint(self) -> list[int]:
        return getattr(self, "_ctrls", [0, 1])

    def set_controllers(self, count: int) -> None:
        self._ctrls = list(range(max(1, count)))
        n = self.profile.fans
        for i, (fid, *_rest) in enumerate(self.fans):
            self.comps[fid].channel = min(i * count // n, count - 1)

    def draw_live(self, cv: Canvas, t: float, snap: GpuSnapshot | None, mode: str) -> None:
        dt = self.dt(t)
        if not hasattr(self, "_phase"):
            self._phase = [0.0] * len(self.fans)
        if snap is not None and snap.fans and len(self._fan_ctrls_hint()) != len(snap.fans):
            self.set_controllers(len(snap.fans))
        blades = 11 if self.profile.kind != "fe" else 7
        px = cv.px
        body = SHROUD["fe"][0] if self.profile.kind == "fe" else SHROUD["default"][1]
        for i, (fid, cx, cy, rp, ring) in enumerate(self.fans):
            rpm = 0
            if snap is not None and snap.fans:
                f = snap.fans[min(self.comps[fid].channel or 0, len(snap.fans) - 1)]
                rpm = f.rpm if f.rpm is not None else (f.speed_pct or 0) * 30
            rev_s = min(0.6, rpm / 3000 * 0.6)
            self._phase[i] = (self._phase[i] + dt * rev_s) % 1.0
            ph = self._phase[i]
            for idx, d, ang in ring:
                if d >= 0.93:
                    continue
                if d < 0.3:
                    px[idx] = 0x1B1C20 if d > 0.26 else (0x2B2E33 if d > 0.12 else shade(body, 1.1))
                    continue
                a = (ang / (2 * math.pi) + ph + d * 0.12) * blades
                frac = a - math.floor(a)
                if frac < 0.62:
                    lit = 0.8 + 0.35 * (1 - frac / 0.62) * (1 - d * 0.4)
                    px[idx] = shade(0x2A2C31, lit)
                else:
                    px[idx] = 0x08090B if (idx % 3) else 0x1E2024  # heatsink fins between blades
            if self.labels and snap is not None and snap.fans:
                f = snap.fans[min(self.comps[fid].channel or 0, len(snap.fans) - 1)]
                row = int(cy + rp) // 2 + 1
                txt = f"{f.rpm} rpm · {f.speed_pct}%" if f.rpm is not None else f"{f.speed_pct}%"
                cv.put_text_center(int(cx - rp), int(cx + rp), min(cv.h - 1, row), txt, 0xC9D1D9, None)
        if snap is not None and getattr(self, "conn_box", None) and snap.power_frac is not None:
            x0, y0, x1, y1 = self.conn_box
            glow = mix(0x151515, 0xFFB347, min(1.0, snap.power_frac) * (0.75 + 0.25 * math.sin(t * 5)))
            cv.fill(x0 + 1, y0 + 1, x1 - 1, y1 - 1, glow)
            if self.labels and snap.power_w is not None:
                cv.put_text_center(x0 - 6, x1 + 6, max(0, y0 // 2 - 1), f"{snap.power_w:.0f} W", 0xFFD84A, None, bold=True)
