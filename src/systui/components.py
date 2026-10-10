"""Per-part detail: what we know about one board component, tagged live / derived / spec."""

from __future__ import annotations

import math
from pixboard.rows import DERIVED, LIVE, SPEC, Row

from . import bindings as B
from .boards import BoardProfile, Component
from .render.charts import human_bits, human_bytes
from .telemetry import SystemSnapshot


def na(v, fmt: str = "{}", unit: str = "") -> str:
    return "N/A" if v is None else fmt.format(v) + unit


def pct(frac: float | None) -> str:
    return "N/A" if frac is None else f"{frac * 100:.0f} %"


def gib(b) -> str:
    return "N/A" if b is None else f"{b / 2**30:.1f} GiB"


def _spec_rows(c: Component, p: BoardProfile) -> list[Row]:
    rows = []
    if c.part:
        rows.append(Row("Part", c.part, SPEC))
    for k, v in c.spec.items():
        rows.append(Row(k, v, SPEC))
    if c.note:
        rows.append(Row("Note", c.note, SPEC))
    rows.append(Row("Position", "measured (manual figure)" if c.conf == "measured" else "inferred", SPEC))
    return rows


def _core(c, p, s) -> list[Row]:
    k = int("".join(ch for ch in c.id if ch.isdigit()) or "0")
    cpu = s.cpu
    if c.kind != "core":
        return [Row("Status", "fused off on this SKU", SPEC)]
    rows = [Row("Core", str(k), LIVE), Row("Utilisation", f"{cpu.core_util(k):.1f} %", LIVE)]
    mhz = cpu.core_mhz(k)
    if mhz:
        rows.append(Row("Clock", f"{mhz:.0f} MHz", LIVE))
    if k < len(cpu.core_w) and cpu.core_w[k] is not None:
        rows.append(Row("Power", f"{cpu.core_w[k]:.1f} W", LIVE))
    ths = cpu.core_threads(k)
    rows.append(Row("Threads", "T" + ",".join(map(str, ths)), LIVE))
    if cpu.prefcore:
        ranks = [cpu.prefcore[t] for t in ths if t < len(cpu.prefcore) and cpu.prefcore[t] is not None]
        if ranks:
            rows.append(Row("Prefcore rank", str(max(ranks)), SPEC))
    if cpu.tccd:
        rows.append(Row("Tccd", f"{cpu.tccd[0]:.1f} °C", LIVE))
    return rows


def _socket(c, p, s) -> list[Row]:
    cpu = s.cpu
    rows = [Row("CPU", cpu.model, LIVE), Row("Cores / threads", f"{cpu.cores} / {cpu.threads}", LIVE),
            Row("Utilisation", f"{cpu.util:.1f} %", LIVE)]
    mhz = [m for m in cpu.mhz if m]
    if mhz:
        rows.append(Row("Clock", f"avg {sum(mhz) / len(mhz):.0f} · max {max(mhz):.0f} MHz", LIVE))
    if cpu.max_mhz:
        rows.append(Row("Boost limit", f"{cpu.max_mhz:.0f} MHz", SPEC))
    rows.append(Row("Tctl", na(cpu.tctl, "{:.1f}", " °C"), LIVE))
    for i, v in enumerate(cpu.tccd):
        rows.append(Row(f"Tccd{i + 1}", f"{v:.1f} °C", LIVE))
    rows.append(Row("Package power", na(cpu.package_w, "{:.1f}", " W") + (f"  ({cpu.power_source})" if cpu.power_source else ""), LIVE))
    if any(w is not None for w in cpu.core_w):
        rows.append(Row("Per-core power", " ".join("—" if w is None else f"{w:.1f}" for w in cpu.core_w) + " W", LIVE))
        uncore = (cpu.package_w or 0) - sum(w or 0 for w in cpu.core_w)
        if cpu.package_w is not None:
            rows.append(Row("SoC / uncore", f"{uncore:.1f} W", DERIVED))
    for lab in ("CPU Vcore", "CPU Soc"):
        v = B.volt(s, lab)
        if v:
            rows.append(Row(lab, f"{v:.3f} V", LIVE))
    sock = B.superio_reading(s, "CPU Socket", "temp")
    if sock:
        rows.append(Row("Socket temp", f"{sock.value:.1f} °C", LIVE))
    rows.append(Row("Load average", " ".join(f"{v:.2f}" for v in cpu.load), LIVE))
    if cpu.governor:
        rows.append(Row("Governor / EPP", f"{cpu.governor}" + (f" / {cpu.epp}" if cpu.epp else ""), LIVE))
    if cpu.boost is not None:
        rows.append(Row("Boost", "on" if cpu.boost else "off", LIVE))
    if cpu.prefcore:
        best = [i for i, v in enumerate(cpu.prefcore) if v == max(x for x in cpu.prefcore if x is not None)]
        rows.append(Row("Preferred cores", "T" + ",".join(str(i) for i in best[:8]), SPEC))
    if cpu.cstates:
        rows.append(Row("C-states", "  ".join(f"{n} {f * 100:.0f}%" for n, f in cpu.cstates), LIVE))
    if s.psi:
        psi = s.psi
        rows.append(Row("PSI some (cpu/mem/io)", f"{psi.cpu.some10:.2f} / {psi.memory.some10:.2f} / {psi.io.some10:.2f}  (avg10)", LIVE))
    rows.append(Row("Socket", p.socket or "—", SPEC))
    return rows


def _vrm(c, p, s) -> list[Row]:
    rows = []
    vrm_t = B.superio_reading(s, "VRM MOS", "temp")
    rows.append(Row("VRM MOS temp", na(vrm_t.value if vrm_t else None, "{:.1f}", " °C"), LIVE))
    vcore, vsoc = B.volt(s, "CPU Vcore"), B.volt(s, "CPU Soc")
    rows.append(Row("Vcore", na(vcore, "{:.3f}", " V"), LIVE))
    rows.append(Row("VSOC", na(vsoc, "{:.3f}", " V"), LIVE))
    pkg = s.cpu.package_w
    rows.append(Row("CPU package", na(pkg, "{:.1f}", " W"), LIVE))
    vcore_phases = len([x for x in p.of_kind("choke") if x.rail == "Vcore"]) or None
    core_w = sum(w or 0 for w in s.cpu.core_w) if any(s.cpu.core_w) else None
    if c.kind == "choke":
        rows.insert(0, Row("Rail", c.rail or "—", SPEC))
    if vcore_phases and core_w is not None:
        rows.append(Row("≈ W per Vcore phase", f"{core_w / vcore_phases:.1f} W  ({vcore_phases} phases)", DERIVED))
        if vcore:
            rows.append(Row("≈ Vcore current", f"{core_w / vcore:.1f} A total", DERIVED))
    rows.append(Row("Topology", p.vrm or "—", SPEC))
    rows.append(Row("Per-phase current", "N/A (not exposed by the Super I/O)", LIVE))
    return rows


def _power(c, p, s) -> list[Row]:
    rows = []
    if (c.pins or 0) <= 8:
        v12 = B.volt(s, "+12V")
        rows.append(Row("+12V rail", na(v12, "{:.3f}", " V"), LIVE))
        pkg = s.cpu.package_w
        if pkg is not None:
            n = len([x for x in p.of_kind("power") if (x.pins or 0) <= 8]) or 1
            rows.append(Row("≈ EPS input", f"{pkg / 0.88:.0f} W total (CPU / 88% VRM eff.), ~{pkg / 0.88 / n:.0f} W here", DERIVED))
            if v12:
                rows.append(Row("≈ EPS current", f"{pkg / 0.88 / v12 / n:.1f} A here", DERIVED))
        rows.append(Row("Rating", "8-pin EPS: ~336 W (7 A × 4 × 12 V)", SPEC))
    else:
        for lab in ("+12V", "+5V", "+3.3V", "AVCC3"):
            v = B.volt(s, lab)
            if v:
                rows.append(Row(lab, f"{v:.3f} V", LIVE))
        est = B.atx_estimate(s)
        rows.append(Row("≈ Total", f"{sum(est.values()):.0f} W  (not measured: estimated from its loads)", DERIVED))
        rows.append(Row("≈ 12 V → PCIe x16 slot", f"{est['slot']:.0f} W  ({B.SLOT_SHARE:.0%} of card power, ≤{B.SLOT_12V_MAX:.0f} W)", DERIVED))
        rows.append(Row("≈ 5 V → DIMM PMICs", f"{est['dimm']:.1f} W", DERIVED))
        rows.append(Row("≈ 3.3 V → chipset + drives", f"{est['storage']:.1f} W", DERIVED))
        rows.append(Row("≈ 12 V → fans / pump", f"{est['fans']:.1f} W", DERIVED))
        rows.append(Row("Animation", "yellow 12 V · red 5 V · orange 3.3 V, speed ∝ estimate", SPEC))
    return rows


def _dimm(c, p, s) -> list[Row]:
    d = B.dimm(s, c)
    b = c.bind_kind()
    rows = [Row("SPD hub address", b[1] if b else "—", SPEC)]
    if d is None:
        rows.append(Row("Module", "empty (no SPD hub answering)" if s.mem.dimms else "unknown (no SPD sensors)", LIVE))
        return rows
    if d.part:
        rows.append(Row("Module", d.part + ("  EXPO" if d.expo else "  XMP" if d.xmp else ""), SPEC))
    if d.serial:
        rows.append(Row("Serial", d.serial, SPEC))
    if d.speed:
        rows.append(Row("Rated", d.speed, SPEC))
    if s.mem.memclk_mhz:
        rows.append(Row("Running clock", f"DDR5-{s.mem.memclk_mhz * 2} · {s.mem.memclk_mhz} MHz", LIVE))
    rows.append(Row("Temperature", na(d.temp, "{:.2f}", " °C"), LIVE))
    rows.append(Row("Sensor", f"{d.driver} ({d.hwmon})", LIVE))
    n = len(s.mem.dimms)
    each = s.mem.total / n / 2**30
    rows.append(Row("≈ Capacity", f"{2 ** round(math.log2(max(1.0, each)))} GiB (MemTotal ÷ {n}, rounded)", DERIVED))
    dram = B.volt(s, "DRAM")
    if dram:
        rows.append(Row("DRAM voltage", f"{dram:.3f} V", LIVE))
    rows.append(Row("System memory used", f"{s.mem.used_frac * 100:.1f} % of {gib(s.mem.total)}", LIVE))
    rows.append(Row("System memory bandwidth", mem_bandwidth(s.mem), LIVE))
    if s.mem.peak_bps:
        rows.append(Row("Memory bus load", f"{pct(B.mem_load(s))} of {human_bytes(s.mem.peak_bps, True)} theoretical peak "
                        "(Zen tops out near 60-70 %)", DERIVED))
    rows.append(Row("Slot mapping", "A1/A2/B1/B2 ↔ 0x50..0x53 (usual AM5 order)", DERIVED))
    return rows


def mem_bandwidth(m) -> str:
    if m.read_bps is None and m.write_bps is None:
        return f"N/A ({m.bw_note})" if m.bw_note else "N/A"
    out = f"▲ read {human_bytes(m.read_bps, True)} · ▼ write {human_bytes(m.write_bps, True)}"
    if m.peak_bps:
        out += f" · {((m.read_bps or 0) + (m.write_bps or 0)) / m.peak_bps * 100:.0f} % of peak"
    return out


def _disk_rows(d, prefix: str = "") -> list[Row]:
    rows = [Row(prefix + "Drive", f"{d.name} · {d.model}", LIVE),
            Row(prefix + "Capacity", f"{d.size / 1e9:.0f} GB", LIVE)]
    if d.link:
        rows.append(Row(prefix + "Link", d.link + (f" · port {d.sata_port}" if d.sata_port else ""), LIVE))
    for k, v in d.temps.items():
        rows.append(Row(prefix + f"Temp ({k})", f"{v:.1f} °C", LIVE))
    rows.append(Row(prefix + "▲ Read", f"{human_bytes(d.read_bps, True)} · {d.read_iops or 0:.0f} IOPS", LIVE))
    rows.append(Row(prefix + "▼ Write", f"{human_bytes(d.write_bps, True)} · {d.write_iops or 0:.0f} IOPS", LIVE))
    rows.append(Row(prefix + "Busy", na(d.busy, "{:.0f}", " %"), LIVE))
    if B.link_bps(d.link):
        rows.append(Row(prefix + "Link load", f"{pct(B.disk_link_load(d))} of {human_bytes(B.link_bps(d.link), True)}", DERIVED))
    if d.firmware:
        rows.append(Row(prefix + "Firmware", d.firmware, LIVE))
    if d.pci:
        rows.append(Row(prefix + "PCI path", " → ".join(a[5:] for a in d.pci), LIVE))
    return rows


def _m2(c, p, s) -> list[Row]:
    d = B.nvme(s, c)
    b = c.bind_kind()
    rows = []
    if d is None:
        rows.append(Row("Drive", "empty (no NVMe behind " + (b[1] if b else "?") + ")", LIVE))
    else:
        rows += _disk_rows(d)
        parts = [pt for pt in s.parts if pt.device.startswith(f"/dev/{d.name}")]
        for pt in parts:
            rows.append(Row(f"  {pt.mount}", f"{pt.used / 1e9:.0f}/{pt.total / 1e9:.0f} GB ({pt.frac * 100:.0f}%) {pt.fstype}", LIVE))
    if b:
        rows.append(Row("Slot ↔ root port", f"{b[1][5:]} (inferred)", DERIVED))
    return rows


def _gpu(c, p, s) -> list[Row]:
    g = B.gpu(s)
    rows = []
    if c.kind == "pcie":
        rows.append(Row("Lanes", f"x{c.lanes} from {c.source or '?'}", SPEC))
    if g is None:
        rows.append(Row("Card", "none detected (NVML unavailable or no NVIDIA GPU)", LIVE))
        return rows
    rows += [Row("Card", g.name, LIVE), Row("Utilisation", na(g.util, "{:.0f}", " %"), LIVE),
             Row("Temperature", na(g.temp, "{:.0f}", " °C"), LIVE),
             Row("Power", f"{na(g.power_w, '{:.0f}')} / {na(g.power_limit_w, '{:.0f}')} W", LIVE),
             Row("VRAM", f"{gib(g.vram_used)} / {gib(g.vram_total)}", LIVE),
             Row("Clocks", f"{na(g.clk_mhz, '{:.0f}')} MHz core · {na(g.clk_mem_mhz, '{:.0f}')} MHz mem", LIVE),
             Row("Fan", f"{na(g.fan_pct, '{:.0f}')} %" + (f" · {g.fan_rpm} rpm" if g.fan_rpm else ""), LIVE)]
    if g.pcie_gen:
        rows.append(Row("PCIe link", f"Gen{g.pcie_gen} x{g.pcie_width} (max Gen{g.pcie_max_gen} x{g.pcie_max_width})", LIVE))
    if g.pcie_rx_bps is not None or g.pcie_tx_bps is not None:
        rows.append(Row("PCIe traffic", f"▲ card → CPU {human_bytes(g.pcie_tx_bps, True)} · ▼ CPU → card "
                        f"{human_bytes(g.pcie_rx_bps, True)}", LIVE))
        cap = B.pcie_bps(g.pcie_gen, g.pcie_width)
        if cap:
            rows.append(Row("PCIe link load", f"{pct(B.gpu_pcie_load(g))} of {human_bytes(cap, True)} each way "
                            f"(Gen{g.pcie_gen} x{g.pcie_width} as trained now)", DERIVED))
    rows.append(Row("Driver", g.driver, LIVE))
    rows.append(Row("More", "press G for the PCB-level view (gputui)", SPEC))
    return rows


def _pcie_x1(c, p, s) -> list[Row]:
    rows = [Row("Lanes", f"x{c.lanes} from {c.source or '?'}", SPEC)]
    t = B.temp(s, c)
    if t:
        rows.append(Row("Nearby temp sensor", f"{t.value:.1f} °C ({t.label})", LIVE))
    return rows


def _chipset(c, p, s) -> list[Row]:
    rows = []
    t = B.temp(s, c)
    rows.append(Row("Temperature", na(t.value if t else None, "{:.1f}", " °C"), LIVE))
    v = B.volt(s, "Chipset")
    if v:
        rows.append(Row("Chipset voltage", f"{v:.3f} V", LIVE))
    up = B.chipset_uplink(s)
    if up:
        rows.append(Row("Uplink", f"CPU root port {up[5:]} (PCIe 4.0 x4)", DERIVED))
        rows.append(Row("Traffic through it", human_bytes(B.chipset_traffic(s, up), True), DERIVED))
        rows.append(Row("Uplink load", f"{pct(B.uplink_load(s, up))} of {human_bytes(B.pcie_bps(*B.UPLINK_LINK), True)} each way",
                        DERIVED))
        for d in s.physical_disks():
            if d.pci and d.pci[0] == up:
                rows.append(Row(f"  {d.name}", f"{d.model} · {human_bytes(B.disk_activity(d), True)}", LIVE))
        for n in s.physical_nets():
            if n.pci and n.pci[0] == up:
                rows.append(Row(f"  {n.name}", f"{n.driver} · {human_bits(B.net_activity(n))}", LIVE))
    rows.append(Row("Chipset", p.chipset or "—", SPEC))
    return rows


def _sata(c, p, s) -> list[Row]:
    disks = B.sata(s, c)
    ports = B.sata_ports(c)
    rows = [Row("Ports", ", ".join(f"SATA_{n}" for n in ports) + " (port ↔ ataN order inferred)", DERIVED)]
    if not disks:
        rows.append(Row("Drive", "none connected", LIVE))
    for d in disks:
        rows += _disk_rows(d)
    return rows


def _fan(c, p, s) -> list[Row]:
    r = B.fan(s, c)
    if r is None:
        return [Row("Speed", "N/A (no matching fan sensor)", LIVE)]
    rows = [Row("Speed", f"{r.value:.0f} rpm" if r.value else "0 rpm (stopped or nothing connected)", LIVE),
            Row("Sensor", f"{r.chip} {r.key} “{r.label}”", LIVE)]
    pwm = B.fan_pwm(s, c)
    if pwm:
        rows.append(Row("Duty", f"{pwm.value:.0f} %", LIVE))
    rows.append(Row("Header ↔ sensor", "driver order (CPU, Pump, SYS1..)", DERIVED))
    return rows


def _net(c, p, s) -> list[Row]:
    n = B.net(s, c)
    if n is None:
        return [Row("Interface", "none found", LIVE)]
    rows = [Row("Interface", f"{n.name} ({n.driver})" if n.driver else n.name, LIVE),
            Row("State", ("up" if n.up else "down") + (f" · {n.speed_mbps} Mb/s" if n.speed_mbps else ""), LIVE),
            Row("Address", n.addr or "—", LIVE),
            Row("▲ Receive", human_bits(n.rx_bps) + f" · total {human_bytes(n.rx_total)}", LIVE),
            Row("▼ Transmit", human_bits(n.tx_bps) + f" · total {human_bytes(n.tx_total)}", LIVE)]
    if n.kind == "ethernet" and n.speed_mbps:
        rows.append(Row("Link load", f"{pct(B.net_link_load(n))} of {n.speed_mbps} Mb/s", DERIVED))
    if n.signal_dbm is not None:
        rows.append(Row("Signal", f"{n.signal_dbm:.0f} dBm", LIVE))
    if n.temp is not None:
        rows.append(Row("Controller temp", f"{n.temp:.1f} °C", LIVE))
    if n.errors:
        rows.append(Row("Errors / drops", str(n.errors), LIVE))
    if n.pci:
        rows.append(Row("PCI path", " → ".join(a[5:] for a in n.pci), LIVE))
    return rows


def _superio(c, p, s) -> list[Row]:
    chip = B.superio_chip(s)
    if chip is None:
        return [Row("Sensors", "no Super I/O driver loaded", LIVE)]
    rows = [Row("Driver", chip, LIVE)]
    for r in s.sensors:
        if r.chip == chip and r.kind in ("in", "temp") and r.value > 0:
            rows.append(Row(r.label, f"{r.value:.3f} V" if r.kind == "in" else f"{r.value:.1f} °C", LIVE))
    return rows


def _battery(c, p, s) -> list[Row]:
    v = B.volt(s, "VBat")
    return [Row("Voltage", na(v, "{:.3f}", " V") if v else "N/A (sensor reads 0)", LIVE)]


def rows_for(c: Component, p: BoardProfile, s: SystemSnapshot | None) -> list[Row]:
    if s is None:
        return _spec_rows(c, p)
    k = c.kind
    if k == "core":
        rows = _core(c, p, s)
    elif k == "socket":
        rows = _socket(c, p, s)
    elif k in ("choke", "heatsink"):
        rows = _vrm(c, p, s)
    elif k == "power":
        rows = _power(c, p, s)
    elif k == "dimm":
        rows = _dimm(c, p, s)
    elif k == "m2":
        rows = _m2(c, p, s)
    elif k == "gpu" or (k == "pcie" and c.bind == "gpu:x16"):
        rows = _gpu(c, p, s)
    elif k == "pcie":
        rows = _pcie_x1(c, p, s)
    elif k == "chipset":
        rows = _chipset(c, p, s)
    elif k == "sata":
        rows = _sata(c, p, s)
    elif k == "fan":
        rows = _fan(c, p, s)
    elif c.bind and c.bind.startswith("net:"):
        rows = _net(c, p, s)
    elif c.id == "SIO":
        rows = _superio(c, p, s)
    elif k == "battery":
        rows = _battery(c, p, s)
    else:
        rows = []
    return rows + _spec_rows(c, p)
