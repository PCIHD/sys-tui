"""Dashboard panels: each draws one pixel-art visual into a Canvas sized to its widget."""

from __future__ import annotations

import math

from .. import bindings as B
from ..canvas import HEAT, Canvas, mix, ramp, shade
from ..history import History
from ..telemetry import SystemSnapshot
from . import charts as ch

BG = 0x0D1117
TILE = 0x131A23
TILE_EDGE = 0x243041


class Panel:
    title = ""
    animated = False

    def draw(self, cv: Canvas, s: SystemSnapshot | None, h: History, t: float) -> None:
        raise NotImplementedError

    def canvas(self, w: int, hgt: int) -> Canvas:
        return Canvas(w, hgt, bg=BG)


def _best_grid(n: int, W: int, H: int, aspect: float = 1.6) -> tuple[int, int]:
    best, best_s = (n, 1), -1.0
    for cols in range(1, n + 1):
        rows = math.ceil(n / cols)
        tw, th = W / cols, H / rows
        s = min(tw / aspect, th)
        if s > best_s:
            best, best_s = (cols, rows), s
    return best


def _tile(cv: Canvas, box, accent: int, amt: float = 0.15) -> None:
    x0, y0, x1, y1 = box
    cv.fill(x0, y0, x1, y1, mix(TILE, accent, amt))
    cv.frame(x0, y0, x1, y1, mix(TILE_EDGE, accent, 0.55))


def _held(values, width: int) -> list[float]:
    """Last ``width`` samples, with the oldest one held across the part of the window not yet filled."""
    vals = list(values)[-width:]
    if not vals or width <= 0:
        return vals
    return [vals[0]] * (width - len(vals)) + vals


def _clock_line(s: SystemSnapshot) -> str:
    m = s.mem
    parts = []
    if m.memclk_mhz:
        parts.append(f"DDR5-{m.memclk_mhz * 2}")
        parts.append(f"{m.memclk_mhz} MHz")
    dram = B.volt(s, "DRAM")
    if dram:
        parts.append(f"{dram:.3f} V")
    if m.peak_bps and m.read_bps is None and m.write_bps is None:
        parts.append(f"peak {m.peak_bps / 1e9:.1f} GB/s")
    return " · ".join(parts)


def _cells(box) -> tuple[int, int, int, int]:
    """Pixel box → the cell rows fully inside it: (cx0, cy0, cx1, cy1)."""
    x0, y0, x1, y1 = box
    return x0, (y0 + 1) // 2, x1, y1 // 2


def _waiting(cv: Canvas) -> None:
    cv.put_text_center(0, cv.w, cv.h // 2, "waiting for data…", ch.DIM)


# ============================================================================ CPU
class CoreGrid(Panel):
    title = "Cores"

    def draw(self, cv, s, h, t):
        if s is None or not s.cpu.cores:
            return _waiting(cv)
        c = s.cpu
        n = c.cores
        W, H = cv.pw, cv.ph
        cols, rows = _best_grid(n, W, H, aspect=1.7)
        for k in range(n):
            col, row = k % cols, k // cols
            x0, x1 = W * col // cols + 1, W * (col + 1) // cols - 1
            y0, y1 = H * row // rows + 1, H * (row + 1) // rows - 1
            u = c.core_util(k) / 100
            acc = ramp(u, ch.LOAD_STOPS)
            _tile(cv, (x0, y0, x1, y1), acc, 0.08 + 0.22 * u)
            cx0, cy0, cx1, cy1 = _cells((x0, y0, x1, y1))
            # header: core id + clock
            cv.put_text(cx0 + 1, cy0, f"Core {k}", 0xE6EDF3, None, True)
            mhz = c.core_mhz(k)
            if mhz:
                ch.text_right(cv, cx1 - 1, cy0 * 2, f"{mhz / 1000:.2f} GHz", 0x8FB8DE)
            # history inside the tile
            gy0, gy1 = (cy0 + 1) * 2, (cy1 - 2) * 2
            if gy1 - gy0 >= 4:
                ch.grid(cv, (x0 + 1, gy0, x1 - 1, gy1), 2, mix(TILE, 0x2A3442, 0.6))
                ch.area(cv, (x0 + 1, gy0, x1 - 1, gy1), h.get(f"cpu:c{k}"), 100, acc)
            # big % in the tile centre
            big = f"{u * 100:.0f}%"
            cv.put_text_center(cx0, cx1, max(cy0 + 1, (cy0 + cy1) // 2 - 1), big, 0xFFFFFF, None, True)
            # thread bars + watts
            ths = c.core_threads(k)
            by = (cy1 - 2) * 2
            bw = (x1 - x0 - 2 - (len(ths) - 1)) // max(1, len(ths))
            for j, tidx in enumerate(ths):
                bx = x0 + 1 + j * (bw + 1)
                ch.hbar(cv, (bx, by, bx + bw, by + 2), c.per_thread[tidx] / 100)
            w = c.core_w[k] if k < len(c.core_w) else None
            foot = f"T{','.join(map(str, ths))}"
            cv.put_text(cx0 + 1, cy1 - 1, foot, ch.DIM)
            if w is not None:
                ch.text_right(cv, cx1 - 1, (cy1 - 1) * 2, f"{w:.1f} W", 0xF2C14E)


class Threads(Panel):
    title = "Threads (history)"

    def draw(self, cv, s, h, t):
        if s is None:
            return _waiting(cv)
        n = s.cpu.threads
        W, H = cv.pw, cv.ph
        cols, rows = _best_grid(n, W, H, aspect=3.0)
        for i in range(n):
            col, row = i % cols, i // cols
            x0, x1 = W * col // cols + 1, W * (col + 1) // cols - 1
            y0, y1 = H * row // rows, H * (row + 1) // rows
            y0 += y0 % 2
            v = s.cpu.per_thread[i]
            acc = ramp(v / 100, ch.LOAD_STOPS)
            cv.fill(x0, y0, x1, y1 - 1, TILE)
            ch.area(cv, (x0, y0 + 2, x1, y1 - 1), h.get(f"cpu:t{i}"), 100, acc)
            cv.put_text(x0, y0 // 2, f"T{i}", 0x8FB8DE, TILE)
            ch.text_right(cv, x1, y0, f"{v:3.0f}%", 0xE6EDF3)


class Package(Panel):
    title = "Package"

    def draw(self, cv, s, h, t):
        if s is None:
            return _waiting(cv)
        c = s.cpu
        W, H = cv.pw, cv.ph
        box = (1, 4, W - 1, H - 4)
        ch.grid(cv, box, 4)
        ch.area(cv, box, h.get("cpu"), 100, 0x2FD18A)
        wmax = ch.nice_max(h.peak("cpu:w") * 1.1, 20)
        ch.line(cv, box, h.get("cpu:w"), wmax, ch.YELLOW)
        ch.line(cv, box, h.get("cpu:tctl"), 100, ch.RED, vmin=20)
        # legend
        x = 1
        for txt, col in ((f"util {c.util:.0f}%", 0x2FD18A),
                         (f"━ {c.package_w:.0f} W" if c.package_w is not None else "━ — W", ch.YELLOW),
                         (f"━ Tctl {c.tctl:.0f}°C" if c.tctl is not None else "━ Tctl —", ch.RED)):
            cv.put_text(x, 0, txt, col, None, True)
            x += len(txt) + 2
        ch.text_right(cv, W - 1, 0, f"0–{wmax:.0f} W · 20–100 °C", ch.DIM)
        mhz = [m for m in c.mhz if m]
        avg = sum(mhz) / len(mhz) if mhz else 0
        foot = (f"load {c.load[0]:.2f} {c.load[1]:.2f} {c.load[2]:.2f} · {avg / 1000:.2f} GHz avg"
                f" · ctx {(c.ctx_rate or 0) / 1000:.0f}k/s · irq {(c.irq_rate or 0) / 1000:.0f}k/s · {c.procs} procs")
        if c.tccd:
            foot += " · " + " ".join(f"CCD{i} {v:.0f}°C" for i, v in enumerate(c.tccd))
        cv.put_text(1, cv.h - 1, foot[: cv.w - 2], 0x9FB0C3)


# ============================================================================ memory
class Memory(Panel):
    title = "Memory"

    def draw(self, cv, s, h, t):
        if s is None:
            return _waiting(cv)
        m = s.mem
        W = cv.pw
        G = 2**30
        app = max(0, m.total - m.available)
        cache = m.cached + m.buffers
        tot = m.total or 1
        cv.put_text(1, 0, f"RAM {app / G:.1f} / {m.total / G:.1f} GiB", 0xE6EDF3, None, True)
        ch.text_right(cv, W - 1, 0, f"{m.used_frac * 100:.0f}% used", ramp(m.used_frac, ch.LOAD_STOPS), True)
        ch.stacked(cv, (1, 2, W - 1, 6), [(app / tot, ch.VIOLET), (min(cache, m.available) / tot, 0x3B6EA8)])
        legend = f"■ apps {app / G:.1f}G  ■ cache/buffers {cache / G:.1f}G  ■ available {m.available / G:.1f}G"
        cv.put_text(1, 3, legend[: cv.w - 2], 0x9FB0C3)
        cv.put_text(1, 3, "■", ch.VIOLET)
        i = legend.find("■", 2)
        cv.put_text(1 + i, 3, "■", 0x3B6EA8)
        y = 8
        clock = _clock_line(s)
        if clock:
            cv.put_text(1, y // 2, clock[: cv.w - 2], 0xE6EDF3, None, True)
            y += 2
        have_bw = m.read_bps is not None or m.write_bps is not None
        bw = "Bandwidth " + (f"▲ read {ch.human_bytes(m.read_bps, True)} · ▼ write {ch.human_bytes(m.write_bps, True)}"
                             if have_bw else f"N/A ({m.bw_note or 'no memory-controller counters'})")
        if have_bw and m.peak_bps:
            bw += f" · {((m.read_bps or 0) + (m.write_bps or 0)) / m.peak_bps * 100:.0f}% of peak"
        cv.put_text(1, y // 2, bw[: cv.w - 2], 0xE6EDF3 if have_bw else ch.DIM)
        y += 2
        if m.swap_total:
            sf = m.swap_used / m.swap_total
            cv.put_text(1, y // 2, f"Swap {m.swap_used / G:.1f} / {m.swap_total / G:.1f} GiB", 0xE6EDF3)
            io = f"in {ch.human_bytes(m.swap_in_rate, True)} · out {ch.human_bytes(m.swap_out_rate, True)}"
            ch.text_right(cv, W - 1, y, io, ch.DIM)
            ch.hbar(cv, (1, y + 2, W - 1, y + 4), sf, color=ch.ORANGE)
            y += 6
        for z in m.zram:
            ratio = z.orig / z.compr if z.compr else 0
            cv.put_text(1, y // 2, f"{z.name} ({z.algo}): {z.orig / G:.2f}G stored in {z.used / G:.2f}G RAM · {ratio:.1f}×",
                        0x9FB0C3)
            y += 2
        box = (1, y + 2, W - 1, cv.ph - 1)
        if box[3] - box[1] >= 6:
            width = box[2] - box[0]
            ch.grid(cv, box, 4)
            # Hold the oldest sample across the unfilled window. A used-% gauge that
            # zero-pads looks like an empty grid until five minutes of history exist.
            ch.area(cv, box, _held(h.get("mem"), width), 100, ch.VIOLET)
            ch.line(cv, box, _held(h.get("mem:cache"), width), 100, 0x5B8FD0)
            ch.line(cv, box, _held(h.get("swap"), width), 100, ch.ORANGE)
            cv.put_text(2, (y + 2) // 2, "used % ", ch.VIOLET, None, True)
            cv.put_text(9, (y + 2) // 2, "━ cache ", 0x5B8FD0)
            cv.put_text(17, (y + 2) // 2, "━ swap", ch.ORANGE)


class Dimms(Panel):
    title = "DIMMs"

    def __init__(self, profile=None) -> None:
        self.profile = profile

    def _slots(self, s: SystemSnapshot) -> list[tuple[str, int | None]]:
        if self.profile is not None:
            out = []
            for c in self.profile.of_kind("dimm"):
                b = c.bind_kind()
                out.append((c.id.replace("DIMM", ""), int(b[1], 16) if b else None))
            if out:
                return out
        return [(f"0x{d.addr:02x}", d.addr) for d in s.mem.dimms]

    def draw(self, cv, s, h, t):
        if s is None:
            return _waiting(cv)
        slots = self._slots(s)
        if not slots:
            cv.put_text_center(0, cv.w, cv.h // 2, "no DIMM temperature sensors (spd5118 / jc42)", ch.DIM)
            return
        W, H = cv.pw, cv.ph - 2
        n = len(slots)
        present = [a for _, a in slots if a is not None and s.mem.dimm(a)]
        each = s.mem.total / len(present) if present else None
        gib = 2 ** round(math.log2(max(1.0, each / 2**30))) if each else None
        chans = s.mem.channel_bps
        # one scale for every channel, so the columns compare at a glance
        bw_max = ch.nice_max(max((max(h.get(f"mem:ch{i}:{k}"), default=0) for i in range(len(chans)) for k in ("rd", "wr")),
                                 default=0) * 1.1, 1e8)
        for k, (name, addr) in enumerate(slots):
            x0, x1 = W * k // n + 1, W * (k + 1) // n - 1
            d = s.mem.dimm(addr) if addr is not None else None
            temp = d.temp if d else None
            heat = ch_heat(temp, 25, 85)
            hc = ramp(heat, HEAT) if temp is not None else 0x3A3F48
            _tile(cv, (x0, 0, x1, H), hc, 0.10 if d else 0.0)
            cells = _cells((x0, 0, x1, H))
            cv.put_text(cells[0] + 1, 0, name, 0x8FB8DE, None, True)
            ch.text_right(cv, x1 - 1, 0, f"{temp:.1f}°C" if temp is not None else "empty", hc if d else ch.DIM, True)
            if d is None:
                continue
            sw = max(5, (x1 - x0) - 4)
            sprite_h = min(24, max(10, H // 4))
            sy0 = 3
            sx0 = x0 + max(1, ((x1 - x0) - sw) // 2)
            dimm_sprite(cv, sx0, sy0, sw, sprite_h, temp, populated=True)
            label_cell = (sy0 + sprite_h + 1) // 2
            label = f"{gib}G" if gib else ""
            if d.speed and len(label) + 1 + len(d.speed) <= cells[2] - cells[0]:
                label = f"{label} {d.speed}".strip()
            cv.put_text_center(cells[0], cells[2], label_cell, label, 0xE6EDF3, None, True)
            # Below the label: the stick's details, then its channel's traffic at the bottom.
            # Short columns lose the last details first; the traffic keeps at least 6 rows.
            ci = _channel(name, len(chans))
            row0, last = label_cell + 1, H // 2 - 1
            lines = _dimm_lines(d, h.minmax.get(("dimm", f"{addr:#x}", "temp")), hc, cells[2] - cells[0] - 2)
            room = last - row0 - (6 if ci is not None else 0)
            for i, (text, fg) in enumerate(lines[: max(0, room)]):
                cv.put_text(cells[0] + 1, row0 + i, text, fg)
            top = row0 + max(0, min(len(lines), room)) + 1
            if ci is not None and last - top >= 4:
                busy = s.mem.channel_busy[ci] if ci < len(s.mem.channel_busy) else None
                _channel_traffic(cv, (x0 + 2, top * 2, x1 - 2, last * 2), cells, ci, chans[ci], busy, h, bw_max)
            elif not chans and s.mem.bw_note and last - top >= 3 and name[:1].isalpha():
                mid = (top + last) // 2
                cv.put_text_center(cells[0], cells[2], mid, f"ch {name[0].upper()} read/write", ch.DIM)
                cv.put_text_center(cells[0], cells[2], mid + 1, "n/a, see footer", ch.DIM)
        dram = B.volt(s, "DRAM")
        bits = []
        if s.mem.memclk_mhz:
            bits.append(f"DDR5-{s.mem.memclk_mhz * 2} · {s.mem.memclk_mhz} MHz")
        if dram:
            bits.append(f"DRAM {dram:.3f} V")
        if not bits:
            bits.append("SPD hub temperature")
        watts = [d.power_w for d in s.mem.dimms if d.power_w is not None]
        if watts:
            bits.append(f"DIMMs {sum(watts):.1f} W")
        elif s.mem.dimm_power_note:
            bits.append(f"DIMM power: {s.mem.dimm_power_note}")
        if any(d.vdd is not None for d in s.mem.dimms):
            bits.append("V = VDD/VDDQ/VPP set")
        if not chans and s.mem.bw_note:
            bits.append(f"channel R/W: {s.mem.bw_note}")
        cv.put_text(1, cv.h - 1, "  ·  ".join(bits)[: cv.w - 2], ch.DIM)


def _channel(name: str, n: int) -> int | None:
    """Memory channel of a slot from its label (A1/A2 → 0, B1/B2 → 1); amd_umc_i drives channel i."""
    k = ord(name[0].upper()) - ord("A") if name[:1].isalpha() else -1
    return k if 0 <= k < n else None


def _dimm_lines(d, seen: tuple[float, float] | None, hc: int, width: int) -> list[tuple[str, int]]:
    """Detail lines for one stick, most useful first: (text, colour). Each line takes the
    longest of its wordings that fits ``width``."""
    rows: list[tuple[list[str], int]] = []
    if d.rail_w:
        w = "/".join(f"{x:.2f}" for x in d.rail_w)
        rows.append(([f"{d.power_w:.2f} W · {w}", f"{d.power_w:.2f} W now", f"{d.power_w:.1f}W"], ch.YELLOW))
    if d.maker:
        if d.dram_maker == d.maker:
            rows.append(([f"{d.maker} · own dies", f"{d.maker}, own dies", d.maker], 0xC9D1D9))
        elif d.dram_maker:
            rows.append(([f"{d.maker} · {d.dram_maker} dies", f"{d.maker}/{d.dram_maker}", d.maker], 0xC9D1D9))
        else:
            rows.append(([d.maker], 0xC9D1D9))
    org = f"{d.ranks}R x{d.width}" if d.ranks and d.width else ""
    die = f"{d.die_gbit}Gb" if d.die_gbit else ""
    if org or die:
        rows.append(([f"{org} · {die} dies", f"{org} {die}"] if org and die else [org or f"{die} dies"], 0x9FB0C3))
    if d.timings:
        rows.append(([f"JEDEC {d.timings}", d.timings], 0x9FB0C3))
    if d.temp is not None:
        span = f"{seen[0]:.0f}–{seen[1]:.0f}" if seen else f"{d.temp:.0f}"
        warn = f"{d.temp_max:.0f}" if d.temp_max is not None else ""
        opts = [f"{span}°C seen · warn {warn}", f"{span}° warn {warn}"] if warn else []
        rows.append((opts + [f"{span}°C seen", f"{span}°C"], hc))
    volts = "/".join(f"{v:.2f}" for v in (d.vdd, d.vddq, d.vpp) if v is not None)
    if volts:
        rows.append(([f"V {volts}", volts], 0x9FB0C3))
    if d.made:
        rows.append(([f"made {d.made}", d.made], ch.DIM))
    return [(next((o for o in opts if len(o) <= width), opts[-1][:width]), fg) for opts, fg in rows]


def _channel_traffic(cv: Canvas, box, cells, ci: int, bps, busy: float | None, h: History, vmax: float) -> None:
    """Channel ``ci``: ``▲read  ch A  ▼write`` label, a data-bus busy bar, then read (up) / write (down) history."""
    x0, y0, x1, y1 = box
    rd, wr = bps
    row = y0 // 2
    r, w = f"▲{ch.human_bytes(rd)}", f"▼{ch.human_bytes(wr)}"
    cv.put_text(cells[0] + 1, row, r, ch.IN, None, True)
    ch.text_right(cv, cells[2] - 1, y0, w, ch.OUT, True)
    tag = f"ch {chr(ord('A') + ci)}"
    if len(r) + len(w) + len(tag) + 4 <= cells[2] - cells[0]:
        cv.put_text_center(cells[0], cells[2], row, tag, ch.DIM)
    y0 += 2
    if busy is not None:
        txt = f"busy {busy * 100:3.0f}%"
        cv.put_text(cells[0] + 1, row + 1, txt, ramp(busy, ch.LOAD_STOPS))
        bx0 = cells[0] + len(txt) + 2
        if x1 - bx0 >= 3:
            ch.hbar(cv, (bx0, y0, x1, y0 + 2), busy)
        y0 += 2
    if y1 - y0 >= 6:
        width = x1 - x0
        ch.mirrored(cv, (x0, y0, x1, y1), list(h.get(f"mem:ch{ci}:rd"))[-width:],
                    list(h.get(f"mem:ch{ci}:wr"))[-width:], vmax, ch.IN, ch.OUT)


def dimm_sprite(cv: Canvas, x0: int, y0: int, w: int, hgt: int, temp: float | None, populated: bool) -> None:
    y1 = y0 + hgt
    if not populated:
        cv.frame(x0, y0, x0 + w, y1, 0x2A3038)
        return
    heat = ch_heat(temp, 25, 85)
    cv.fill(x0, y0, x0 + w, y1, 0x0E3B24)
    cv.frame(x0, y0, x0 + w, y1, 0x0A2A1A)
    fy = y1 - max(2, hgt // 18)
    for x in range(x0 + 1, x0 + w - 1, 2):
        cv.fill(x, fy, x + 1, y1, ch.YELLOW)
    cv.set(x0 + w // 2, fy, 0x0D1117)  # key notch
    chip_c = mix(0x1C1E22, ramp(heat, HEAT), 0.55)
    n = 8
    span = fy - 2 - (y0 + 2)
    cw0, cw1 = x0 + max(1, w // 6), x0 + w - max(1, w // 6)
    for k in range(n):
        a = y0 + 2 + span * k // n
        b = y0 + 2 + span * (k + 1) // n - 1
        if k == n // 2:  # PMIC + SPD hub sit mid-module on DDR5
            cv.fill(cw0, a, cw0 + max(1, (cw1 - cw0) // 3), b, 0x3A3F48)
            cv.fill(cw1 - max(1, (cw1 - cw0) // 4), a, cw1, b, mix(0x30343B, ramp(heat, HEAT), 0.85))
            continue
        cv.fill(cw0, a, cw1, max(a + 1, b), chip_c)
        if b - a >= 2:
            cv.fill(cw0, a, cw1, a + 1, shade(chip_c, 1.3))


def ch_heat(v, lo, hi):
    return 0.0 if v is None else max(0.0, min(1.0, (v - lo) / (hi - lo)))


# ============================================================================ storage / network
class Disk(Panel):
    def __init__(self, name: str) -> None:
        self.name = name
        self.title = name

    def draw(self, cv, s, h, t):
        d = s.disk(self.name) if s else None
        if d is None:
            return _waiting(cv)
        W = cv.pw
        bits = [d.model or d.kind, ch.human_bytes(d.size, digits=0) if d.size else ""]
        if d.link:
            bits.append(d.link + (f" port {d.sata_port}" if d.sata_port else ""))
        cv.put_text(1, 0, " · ".join(b for b in bits if b)[: cv.w - 16], 0xE6EDF3, None, True)
        right = []
        if d.temp is not None:
            right.append(f"{d.temp:.0f}°C")
        if d.busy is not None:
            right.append(f"busy {d.busy:.0f}%")
        rs = "  ".join(right)
        ch.text_right(cv, W - 1, 0, rs, ramp(ch_heat(d.temp, 30, 75), HEAT) if d.temp is not None else ch.DIM, True)
        r, w = h.get(f"disk:{d.name}:r"), h.get(f"disk:{d.name}:w")
        vmax = ch.nice_max(max(max(r, default=0), max(w, default=0)) * 1.1, 1e5)
        box = (1, 2, W - 1, cv.ph - 2)
        ch.mirrored(cv, box, r, w, vmax, ch.IN, ch.OUT)
        cv.put_text(1, 1, f"▲ read {ch.human_bytes(d.read_bps, True)}", ch.IN, None, True)
        if d.read_iops is not None:
            ch.text_right(cv, W - 1, 2, f"{d.read_iops:.0f} IOPS", 0x5E8FA8)
        cv.put_text(1, cv.h - 1, f"▼ write {ch.human_bytes(d.write_bps, True)}", ch.OUT, None, True)
        if d.write_iops is not None:
            ch.text_right(cv, W - 1, (cv.h - 1) * 2, f"{d.write_iops:.0f} IOPS · scale {ch.human_bytes(vmax, True)}", 0xA8774A)


class Partitions(Panel):
    title = "Filesystems"

    def draw(self, cv, s, h, t):
        if s is None:
            return _waiting(cv)
        W = cv.pw
        mw = min(24, max((len(p.mount) for p in s.parts), default=4) + 1)
        for i, p in enumerate(s.parts[: cv.h]):
            cv.put_text(1, i, p.mount[: mw - 1], 0x8FB8DE)
            val = f"{ch.human_bytes(p.used, digits=0)}/{ch.human_bytes(p.total, digits=0)} {p.frac * 100:3.0f}% {p.fstype}"
            bx0, bx1 = 1 + mw, W - len(val) - 2
            if bx1 - bx0 >= 4:
                ch.hbar(cv, (bx0, i * 2, bx1, i * 2 + 2), p.frac)
            ch.text_right(cv, W - 1, i * 2, val, 0xC9D1D9)


class Net(Panel):
    def __init__(self, name: str) -> None:
        self.name = name
        self.title = name

    def draw(self, cv, s, h, t):
        n = s.net(self.name) if s else None
        if n is None:
            return _waiting(cv)
        W = cv.pw
        kind = {"ethernet": "Ethernet", "wifi": "Wi-Fi"}.get(n.kind, n.kind)
        bits = [kind, n.driver, n.addr or ("up" if n.up else "down")]
        if n.speed_mbps:
            bits.append(f"{n.speed_mbps / 1000:g} Gb/s" if n.speed_mbps >= 1000 else f"{n.speed_mbps} Mb/s")
        cv.put_text(1, 0, " · ".join(b for b in bits if b)[: cv.w - 20], 0xE6EDF3, None, True)
        right = []
        if n.signal_dbm is not None:
            right.append(f"{n.signal_dbm:.0f} dBm")
        if n.temp is not None:
            right.append(f"{n.temp:.0f}°C")
        right.append("● up" if n.up else "○ down")
        ch.text_right(cv, W - 1, 0, "  ".join(right), 0x3DDC84 if n.up else ch.DIM, True)
        rx, tx = h.get(f"net:{n.name}:rx"), h.get(f"net:{n.name}:tx")
        vmax = ch.nice_max(max(max(rx, default=0), max(tx, default=0)) * 1.1, 1e4)
        ch.mirrored(cv, (1, 2, W - 1, cv.ph - 2), rx, tx, vmax, ch.IN, ch.OUT)
        cv.put_text(1, 1, f"▲ rx {ch.human_bits(n.rx_bps)}", ch.IN, None, True)
        ch.text_right(cv, W - 1, 2, f"total {ch.human_bytes(n.rx_total)}", 0x5E8FA8)
        cv.put_text(1, cv.h - 1, f"▼ tx {ch.human_bits(n.tx_bps)}", ch.OUT, None, True)
        ch.text_right(cv, W - 1, (cv.h - 1) * 2, f"total {ch.human_bytes(n.tx_total)} · scale {ch.human_bits(vmax)}", 0xA8774A)


# ============================================================================ fans / overview
class Fans(Panel):
    title = "Fans"
    animated = True

    def __init__(self) -> None:
        self.angle: dict[str, float] = {}
        self._t: float | None = None

    def draw(self, cv, s, h, t):
        if s is None:
            return _waiting(cv)
        dt = 0.0 if self._t is None else max(0.0, min(0.5, t - self._t))
        self._t = t
        fans = [r for r in s.sensors if r.kind == "fan"]
        if not fans:
            cv.put_text_center(0, cv.w, cv.h // 2, "no fan sensors exposed by hwmon", ch.DIM)
            return
        W, H = cv.pw, cv.ph
        cols, rows = _best_grid(len(fans), W, H, aspect=3.2)
        for k, r in enumerate(fans):
            col, row = k % cols, k // cols
            x0, x1 = W * col // cols + 1, W * (col + 1) // cols - 1
            y0, y1 = H * row // rows, H * (row + 1) // rows
            y0 += y0 % 2
            y1 -= 1
            on = r.value > 0
            key = f"fan:{r.chip}:{r.label}"
            peak = max(2000.0, h.peak(key) * 1.1)
            _tile(cv, (x0, y0, x1, y1), ch.CYAN if on else 0x3A3F48, 0.06 if on else 0.0)
            side = max(4, min(y1 - y0 - 2, (x1 - x0) // 2))
            fx0, fy0 = x0 + 1, y0 + 1
            a = self.angle.get(key, 0.0) + dt * r.value / 60 * 2 * math.pi / 8
            self.angle[key] = a
            fan_sprite(cv, fx0, fy0, side, a, on)
            cells = _cells((fx0 + side + 1, y0, x1, y1))
            label = r.label.replace("System Fan #", "SYS").replace(" Fan", "")
            tx = cells[0] + 1
            cv.put_text(tx, cells[1], label, 0xE6EDF3 if on else ch.DIM, None, True)
            ch.text_right(cv, x1 - 1, cells[1] * 2, f"{r.value:.0f} rpm", 0x9BE7FF if on else ch.DIM, on)
            if cells[3] - cells[1] >= 2:
                by = (cells[1] + 1) * 2
                ch.hbar(cv, (tx, by, x1 - 1, by + 2), r.value / peak,
                        stops=[(0.0, 0x2F7DE1), (0.6, 0x39C5FF), (1.0, 0x9BE7FF)])
            gy0, gy1 = (cells[1] + 2) * 2, y1 - 1
            if gy1 - gy0 >= 2:
                ch.area(cv, (tx, gy0, x1 - 1, gy1), h.get(key), peak, 0x39C5FF)


def fan_sprite(cv: Canvas, x0: int, y0: int, side: int, angle: float, on: bool, blades: int = 7) -> None:
    """Square fan frame with shaded, swept blades rotated by ``angle``."""
    frame = 0x2A313B if on else 0x22262D
    cv.fill(x0, y0, x0 + side, y0 + side, frame)
    cx, cy = x0 + side / 2, y0 + side / 2
    r = side / 2 - 0.6
    hub = r * 0.28
    blade = 0x6FC8F0 if on else 0x3A3F48
    for y in range(y0, y0 + side):
        for x in range(x0, x0 + side):
            dx, dy = x + 0.5 - cx, y + 0.5 - cy
            d = math.hypot(dx, dy)
            if d > r:
                continue
            if d <= hub:
                cv.set(x, y, 0x1A1F27)
                continue
            th = math.atan2(dy, dx) - angle + 0.9 * (d / r)  # swept blades
            f = (th * blades / (2 * math.pi)) % 1.0
            if f < 0.5:
                cv.set(x, y, shade(blade, 0.7 + 0.5 * (1 - f * 2)))
            else:
                cv.set(x, y, 0x0F1318)
    for (sx, sy) in ((x0, y0), (x0 + side - 1, y0), (x0, y0 + side - 1), (x0 + side - 1, y0 + side - 1)):
        cv.set(sx, sy, 0x0D1117)


class Overview(Panel):
    """Stacked mini charts for the board side panel."""

    title = "Overview"

    # traffic rows take an (in, out) key pair and draw mirrored: ▲ towards the CPU above the axis
    ROWS = (
        ("CPU", "cpu", 100, 0x2FD18A, "%"),
        ("MEM", "mem", 100, 0xA78BFA, "%"),
        ("MEM BW", ("mem:rd", "mem:wr"), None, None, "B/s"),
        ("CPU W", "cpu:w", None, 0xF2C14E, "W"),
        ("GPU", "gpu0:util", 100, 0x76B900, "%"),
        ("GPU W", "gpu0:w", None, 0xFF9A3C, "W"),
        ("PCIe", ("gpu0:pcie:in", "gpu0:pcie:out"), None, None, "B/s"),
        ("DISK", ("disk:all:r", "disk:all:w"), None, None, "B/s"),
        ("NET", ("net:all:rx", "net:all:tx"), None, None, "B/s"),
    )

    def draw(self, cv, s, h, t):
        if s is None:
            return _waiting(cv)
        W = cv.pw
        first = lambda key: key[0] if isinstance(key, tuple) else key  # noqa: E731
        rows = [r for r in self.ROWS if first(r[1]) in h.data or not first(r[1]).startswith(("gpu", "mem:"))]
        n = len(rows)
        per = max(2, cv.h // max(1, n))
        lw = 13
        for i, (label, key, vmax, col, unit) in enumerate(rows):
            cy0 = i * per
            if cy0 >= cv.h:
                break
            y0, y1 = cy0 * 2, min(cv.ph, (cy0 + per) * 2 - 1)
            cv.fill(lw, y0, W - 1, y1, 0x111821)
            cv.put_text(0, cy0, f"{label:<6}", 0x8FB8DE, None, True)
            if isinstance(key, tuple):
                kin, kout = key
                vm = ch.nice_max(max(h.peak(kin), h.peak(kout)) * 1.1, 10)
                ch.mirrored(cv, (lw, y0, W - 1, y1), h.get(kin), h.get(kout), vm)
                a, b = h.last(kin), h.last(kout)
                if per > 2:
                    cv.put_text(0, cy0 + 1, ("▲" + ch.human_bytes(a, True))[:lw - 1], ch.IN, None, True)
                    cv.put_text(0, cy0 + 2, ("▼" + ch.human_bytes(b, True))[:lw - 1], ch.OUT, None, True)
                else:  # one line: the label row carries ▲, the next ▼, both right-aligned under the label
                    ch.text_right(cv, lw - 1, cy0 * 2, "▲" + ch.human_bytes(a, digits=0), ch.IN, True)
                    ch.text_right(cv, lw - 1, (cy0 + 1) * 2, "▼" + ch.human_bytes(b, digits=0), ch.OUT, True)
                continue
            vm = vmax or ch.nice_max(h.peak(key) * 1.1, 10)
            last = h.last(key)
            ch.area(cv, (lw, y0, W - 1, y1), h.get(key), vm, col)
            val = ch.human_bytes(last, True) if unit == "B/s" else f"{last:5.0f}{unit}"
            cv.put_text(0, cy0 + 1 if per > 1 else cy0, val[:lw - 1].rjust(7), col, None, True)
