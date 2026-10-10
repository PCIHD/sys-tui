"""Die scene: a Blackwell floorplan generated from the chip spec, with fused-off units greyed.

This is a schematic floorplan (block placement follows NVIDIA's published block diagrams),
not a die shot. SM lighting uses GPM SM activity when available, which NVML reports as an
aggregate — the per-SM variation is a visual spread of that aggregate, not per-SM counters.
"""

from __future__ import annotations

import math

from ..boards import BoardProfile, Component
from ..canvas import Canvas, mix, shade
from ..chips import SkuSpec, enabled_sm_map
from ..telemetry import GpuSnapshot
from . import Scene
from . import charts as ch
from .pcb import pcie_level

BG_DIE = 0x1A1D24
GPC_BG = 0x202733
GPC_EDGE = 0x3A4658
SM_OFF = 0x2A2C30
SM_BASE = 0x23473A
SM_LIT = 0x7DFFB0
TENSOR = 0xB08CFF
L2_BASE = 0x233459
L2_LIT = 0x6FA8FF
MC_BASE = 0x3B2A52
MC_LIT = 0xC58CFF
PHY = 0xB08D3C
MEDIA_BASE = 0x4A3A22
MEDIA_LIT = 0xFFB347
HOST_BASE = 0x1F4452
TEXT = 0xD8DEE9


def _hash(i: int, j: int) -> int:
    h = (i * 2654435761 ^ j * 40503) & 0xFFFFFFFF
    h ^= h >> 15
    return (h * 2246822519) & 0xFFFF


class DieScene(Scene):
    def __init__(self, profile: BoardProfile, sku: SkuSpec, w: int, h: int, labels: bool = True):
        super().__init__(w, h)
        self.profile, self.sku, self.labels = profile, sku, labels
        chip = sku.chip_spec
        self.chip = chip
        self.title = f"{sku.variant} die floorplan — {sku.sms}/{chip.sms} SMs enabled"
        self.sms: list[tuple[int, int, tuple[int, int, int, int], bool]] = []  # (gpc, sm, box, enabled)
        self.blocks: dict[str, tuple[str, tuple[int, int, int, int]]] = {}  # id -> (role, box)
        self._draw()

    def _add(self, cid: str, kind: str, box, label: str, part: str | None = None, channel: int | None = None,
             note: str | None = None) -> None:
        self.comps[cid] = Component(cid, kind, (0, 0, 0, 0), label=label, part=part, channel=channel, note=note)
        self.boxes[cid] = box

    def _draw(self) -> None:
        cv, chip, sku = self.static, self.chip, self.sku
        cv.gradient_v(0, 0, cv.pw, cv.ph, 0x0B0E14, 0x111822)
        side = math.sqrt(chip.die_mm2)
        dw, dh = side * 1.02, side * 0.98
        s = min((cv.pw - 4) / dw, (cv.ph - 4) / dh)
        W, H = int(dw * s), int(dh * s)
        X0, Y0 = (cv.pw - W) // 2, (cv.ph - H) // 2
        cv.fill(X0, Y0, X0 + W, Y0 + H, BG_DIE)
        cv.frame(X0, Y0, X0 + W, Y0 + H, 0x4A5160)
        # seal ring
        cv.frame(X0 + 1, Y0 + 1, X0 + W - 1, Y0 + H - 1, 0x2A2F3A)

        def R(fx0, fy0, fx1, fy1):
            return (X0 + int(fx0 * W), Y0 + int(fy0 * H), X0 + int(fx1 * W), Y0 + int(fy1 * H))

        mc_w = 0.085
        top_h, bot_h, l2_h = 0.085, 0.085, 0.10
        # --- memory controllers + PHY on the left/right edges
        n_mc = chip.mem_controllers
        used_mc = sku.bus_width // 32
        left = (n_mc + 1) // 2
        right = n_mc - left
        for side_i, (count, fx) in enumerate(((left, 0.012), (right, 1 - 0.012 - mc_w))):
            for k in range(count):
                idx = k if side_i == 0 else left + k
                fy0 = top_h + 0.02 + k * (1 - top_h - bot_h - 0.04) / count
                fy1 = top_h + 0.02 + (k + 1) * (1 - top_h - bot_h - 0.04) / count - 0.012
                box = R(fx, fy0, fx + mc_w, fy1)
                on = idx < used_mc
                cv.fill(*box, MC_BASE if on else SM_OFF)
                phx = box[0] if side_i == 0 else box[2] - max(1, (box[2] - box[0]) // 4)
                cv.fill(phx, box[1], phx + max(1, (box[2] - box[0]) // 4), box[3], PHY if on else 0x3A3A3A)
                self._add(f"MC{idx}", "mc", box, f"MC{idx}", part="32-bit memory controller + PHY",
                          channel=idx, note=None if on else "Present on die, disabled on this SKU")
                self.blocks[f"MC{idx}"] = ("mc" if on else "off", box)
        # --- top band: PCIe host, GigaThread, display
        ix0, ix1 = mc_w + 0.03, 1 - mc_w - 0.03
        span = ix1 - ix0
        tb = [("PCIE_HOST", "host", 0.36, "PCIe 5.0 host", f"PCIe 5.0 x{sku.pcie_lanes} host interface"),
              ("GIGATHREAD", "gigathread", 0.34, "GigaThread", "GigaThread engine (work distributor)"),
              ("DISPLAY", "display", 0.30, "Display", "Display engine (DP 2.1b / HDMI 2.1b)")]
        fx = ix0
        for cid, kind, frac, label, part in tb:
            box = R(fx, 0.018, fx + span * frac - 0.01, top_h)
            cv.fill(*box, HOST_BASE)
            cv.frame(*box, shade(HOST_BASE, 1.5))
            self._add(cid, kind, box, label, part)
            self.blocks[cid] = (kind, box)
            fx += span * frac
        # --- bottom band: media engines
        media = ([("NVENC", i, "nvenc") for i in range(chip.nvenc)] + [("NVDEC", i, "nvdec") for i in range(chip.nvdec)]
                 + [("OFA", 0, "ofa"), ("NVJPG", 0, "nvjpg")])
        mw = span / len(media)
        for k, (name, i, kind) in enumerate(media):
            box = R(ix0 + k * mw, 1 - bot_h, ix0 + (k + 1) * mw - 0.01, 1 - 0.018)
            enabled = not ((kind == "nvenc" and i >= sku.nvenc) or (kind == "nvdec" and i >= sku.nvdec))
            cv.fill(*box, MEDIA_BASE if enabled else SM_OFF)
            cid = f"{name}{i}" if kind in ("nvenc", "nvdec") else name
            gen = {"nvenc": "9th-gen NVENC", "nvdec": "6th-gen NVDEC", "ofa": "Optical-flow accelerator", "nvjpg": "JPEG decoder"}[kind]
            self._add(cid, kind, box, cid, gen, channel=i, note=None if enabled else "Fused off on this SKU")
            self.blocks[cid] = (kind if enabled else "off", box)
        # --- GPCs in two rows around the L2 band
        grid = enabled_sm_map(chip, sku)
        g_top = (chip.gpcs + 1) // 2
        g_bot = chip.gpcs - g_top
        mid_y0 = top_h + 0.02
        row_h = (1 - top_h - bot_h - l2_h - 0.06) / 2
        l2_y0 = mid_y0 + row_h + 0.01
        l2_box = R(ix0, l2_y0, ix1, l2_y0 + l2_h)
        cv.fill(*l2_box, L2_BASE)
        # L2 slice texture
        slices = max(8, chip.l2_mb // 4)
        for k in range(1, slices):
            x = l2_box[0] + int(k * (l2_box[2] - l2_box[0]) / slices)
            cv.fill(x, l2_box[1], x + 1, l2_box[3], shade(L2_BASE, 0.7))
        self._add("L2", "l2", l2_box, f"L2 {sku.l2_mb} MB", f"L2 cache ({sku.l2_mb} of {chip.l2_mb} MB enabled)")
        self.blocks["L2"] = ("l2", l2_box)
        for gi in range(chip.gpcs):
            if gi < g_top:
                cols, k, fy0 = g_top, gi, mid_y0
            else:
                cols, k, fy0 = g_bot, gi - g_top, l2_y0 + l2_h + 0.01
            gw = span / cols
            box = R(ix0 + k * gw, fy0, ix0 + (k + 1) * gw - 0.012, fy0 + row_h)
            gon = any(grid[gi])
            cv.fill(*box, GPC_BG if gon else 0x1E1F22)
            cv.frame(*box, GPC_EDGE if gon else 0x2C2E33)
            n_on = sum(grid[gi])
            self._add(f"GPC{gi}", "gpc", box, f"GPC{gi}",
                      f"Graphics processing cluster: raster engine, {chip.tpcs_per_gpc} TPCs × {chip.sms_per_tpc} SMs",
                      channel=gi, note=f"{n_on}/{len(grid[gi])} SMs enabled")
            # raster engine strip
            bx0, by0, bx1, by1 = box
            rh = max(1, (by1 - by0) // 9)
            cv.fill(bx0 + 1, by0 + 1, bx1 - 1, by0 + 1 + rh, 0x3A3350 if gon else 0x26262A)
            # SMs: TPCs in 2 columns, each TPC = 2 SMs side by side
            tpc_cols = 2
            tpc_rows = math.ceil(chip.tpcs_per_gpc / tpc_cols)
            ax0, ay0 = bx0 + 2, by0 + 2 + rh
            aw, ah = (bx1 - 2) - ax0, (by1 - 2) - ay0
            sm_cols = tpc_cols * chip.sms_per_tpc
            for t in range(chip.tpcs_per_gpc):
                tr, tc = divmod(t, tpc_cols)
                for sidx in range(chip.sms_per_tpc):
                    col = tc * chip.sms_per_tpc + sidx
                    x0 = ax0 + int(col * aw / sm_cols) + (1 if sidx == 0 and tc > 0 else 0)
                    x1 = ax0 + int((col + 1) * aw / sm_cols) - 1
                    y0 = ay0 + int(tr * ah / tpc_rows)
                    y1 = ay0 + int((tr + 1) * ah / tpc_rows) - 1
                    en = grid[gi][t * chip.sms_per_tpc + sidx]
                    cv.fill(x0, y0, max(x1, x0 + 1), max(y1, y0 + 1), SM_BASE if en else SM_OFF)
                    if not en and x1 - x0 >= 3 and y1 - y0 >= 3:
                        cv.line(x0, y0, x1 - 1, y1 - 1, 0x44464B)
                        cv.line(x1 - 1, y0, x0, y1 - 1, 0x44464B)
                    self.sms.append((gi, t * chip.sms_per_tpc + sidx, (x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)), en))
        if self.labels:
            for cid, c in self.comps.items():
                x0, y0, x1, y1 = self.boxes[cid]
                row = ((y0 + y1) // 2) // 2
                if c.kind == "gpc":
                    row = (y0 + 1) // 2
                for t in (c.label, c.label[:5], c.label[:3]):
                    if cv.put_text_center(x0, x1, row, t, TEXT, None, bold=c.kind in ("gpc", "l2")):
                        break

    def draw_live(self, cv: Canvas, t: float, snap: GpuSnapshot | None, mode: str) -> None:
        self.dt(t)
        if snap is None or mode == "off":
            return
        g = snap.gpm
        act = snap.sm_activity or 0.0
        tensor = g.get("ANY_TENSOR_UTIL", 0.0) / 100
        dram = snap.dram_activity or 0.0
        frame = int(t * 8)
        for n, (gi, si, (x0, y0, x1, y1), en) in enumerate(self.sms):
            if not en:
                continue
            h = _hash(n, frame)
            v = act * (0.65 + 0.35 * h / 65535)
            cv.fill(x0, y0, x1, y1, mix(SM_BASE, SM_LIT, v))
            if tensor > 0.01 and y1 - y0 >= 3:
                cv.fill(x0, y1 - 1, x1, y1, mix(SM_BASE, TENSOR, min(1.0, tensor * 1.4)))
        for cid, (role, box) in self.blocks.items():
            x0, y0, x1, y1 = box
            if role == "l2":
                lit = mix(L2_BASE, L2_LIT, dram * 0.8)
                w = x1 - x0
                for x in range(x0, x1):
                    wave = 0.5 + 0.5 * math.sin((x - x0) / max(1, w) * 12 - t * 6)
                    cv.fill(x, y0 + 1, x + 1, y1 - 1, mix(L2_BASE, lit, 0.4 + 0.6 * wave * dram))
            elif role == "mc":
                cv.fill(x0 + (x1 - x0) // 4, y0, x1 - (x1 - x0) // 4, y1,
                        mix(MC_BASE, MC_LIT, dram * (0.7 + 0.3 * math.sin(t * 7 + y0))))
            elif role in ("nvenc", "nvdec", "ofa", "nvjpg"):
                c = self.comps[cid]
                key = {"nvenc": f"NVENC_{c.channel}_UTIL", "nvdec": f"NVDEC_{c.channel}_UTIL",
                       "ofa": "NVOFA_0_UTIL", "nvjpg": "NVJPG_0_UTIL"}[role]
                u = g.get(key)
                if u is None and role == "nvenc":
                    u = snap.util_enc
                if u is None and role == "nvdec":
                    u = snap.util_dec
                cv.fill(x0 + 1, y0 + 1, x1 - 1, y1 - 1, mix(MEDIA_BASE, MEDIA_LIT, (u or 0) / 100))
            elif role == "host":
                # left half lit by card → host traffic, right half by host → card
                pulse = 0.7 + 0.3 * math.sin(t * 9)
                xm = (x0 + x1) // 2
                cv.fill(x0 + 1, y0 + 1, xm, y1 - 1, mix(HOST_BASE, ch.IN, pcie_level(snap.pcie_tx_kbs) * pulse))
                cv.fill(xm, y0 + 1, x1 - 1, y1 - 1, mix(HOST_BASE, ch.OUT, pcie_level(snap.pcie_rx_kbs) * pulse))
            elif role == "gigathread":
                cv.fill(x0 + 1, y0 + 1, x1 - 1, y1 - 1, mix(HOST_BASE, SM_LIT, act * 0.6))
            elif role == "display":
                on = any(c.connected for c in snap.displays)
                cv.fill(x0 + 1, y0 + 1, x1 - 1, y1 - 1, mix(HOST_BASE, 0x3DFF7A, 0.45 if on else 0.0))
