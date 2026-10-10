"""Structured tables (Rich) for the bottom tab strip and ``--once`` output."""

from __future__ import annotations

from rich import box
from rich.table import Table
from rich.text import Text

from pixboard.charts import IN, OUT

from .components import gib, kbs, live_summary, na
from .identify import Match
from .telemetry import GpuSnapshot

TABS = ["Overview", "Components", "Architecture", "Clocks", "Memory", "Power & Thermal", "PCIe", "Displays", "Processes"]
KEY_STYLE = "bold #8fb8de"


def _kv(title: str | None = None) -> Table:
    t = Table(box=box.SIMPLE_HEAD, expand=True, show_header=False, title=title, pad_edge=False)
    t.add_column("Field", style=KEY_STYLE, no_wrap=True, ratio=1)
    t.add_column("Value", ratio=3)
    return t


def _pair(*tables: Table) -> Table:
    g = Table.grid(expand=True, padding=(0, 2))
    for _ in tables:
        g.add_column(ratio=1)
    g.add_row(*tables)
    return g


def _pct_bar(v: float | None, width: int = 20) -> Text:
    if v is None:
        return Text("N/A", style="dim")
    v = max(0.0, min(1.0, v))
    fill = int(round(v * width))
    color = "#3fae7a" if v < 0.6 else "#f2c14e" if v < 0.85 else "#e5383b"
    t = Text("█" * fill, style=color)
    t.append("░" * (width - fill), style="#3b4252")
    t.append(f" {v * 100:5.1f} %")
    return t


def overview(s: GpuSnapshot, m: Match | None) -> Table:
    a = _kv()
    a.add_row("Name", na(s.name))
    if m:
        a.add_row("Board", m.profile.name)
        a.add_row("Match", m.level_text)
        a.add_row("Board maker", f"{m.vendor_name or 'unknown'} (0x{s.pci_subsys_vendor or 0:04X})")
    a.add_row("PCI IDs", f"{0x10DE:04X}:{s.pci_device_id or 0:04X}  subsystem {s.pci_subsys_vendor or 0:04X}:{s.pci_subsys_device or 0:04X}")
    a.add_row("Bus ID", na(s.bus_id))
    a.add_row("UUID", na(s.uuid))
    a.add_row("Serial", na(s.serial))
    b = _kv()
    b.add_row("Driver", na(s.driver))
    b.add_row("CUDA (driver)", na(s.cuda_driver))
    b.add_row("NVML", na(s.nvml_version))
    b.add_row("VBIOS", na(s.vbios))
    b.add_row("InfoROM", na(s.inforom))
    b.add_row("GSP firmware", na(s.gsp_firmware))
    b.add_row("Compute capability", na(s.compute_cap))
    b.add_row("Persistence / compute mode", f"{'on' if s.persistence else 'off' if s.persistence is not None else 'N/A'} / {na(s.compute_mode)}")
    b.add_row("Display active", na(s.display_active))
    extra = ""
    if s.gpm:
        extra += " + GPM counters"
    if s.volt_nvvdd is not None or s.temp_mem is not None or s.mem_modules:
        extra += " + driver sensors"
    b.add_row("Telemetry", f"{s.source}{extra}")
    return _pair(a, b)


def components_table(s: GpuSnapshot | None, m: Match, selected: str | None, extra=None) -> Table:
    t = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
    ell = {"no_wrap": True, "overflow": "ellipsis"}
    for col, kw in (("ID", {"no_wrap": True}), ("Type", ell), ("Part", {"ratio": 3, **ell}), ("Rail/Ch", ell),
                    ("Side", ell), ("Src", ell), ("Live", {"ratio": 2, **ell})):
        t.add_column(col, **kw)
    comps = list(m.profile.components) + list(extra or [])
    # collapse identical grouped parts (e.g. 10 NVVDD chokes) into one row
    rows: list[tuple[list, object]] = []
    for c in comps:
        key = (c.group, c.kind, c.part, c.rail, c.marking, c.side) if c.group and c.kind != "mem" else None
        if key and rows and rows[-1][1] == key:
            rows[-1][0].append(c)
        else:
            rows.append(([c], key))
    for group, _ in rows:
        c = group[0]
        cid = c.id if len(group) == 1 else f"{group[0].id}–{group[-1].id} ×{len(group)}"
        rc = c.rail or (f"ch{c.channel}" if c.channel is not None and c.kind == "mem" else "")
        style = "reverse" if any(g.id == selected for g in group) else None
        t.add_row(cid, c.kind_name, c.part or c.marking or c.label or "", rc, c.side,
                  "meas" if c.conf == "measured" else "infer", live_summary(c, m.sku, s), style=style)
    return t


def architecture(s: GpuSnapshot, m: Match | None) -> Table:
    a = _kv()
    if m is None:
        a.add_row("SKU", "unknown")
        return a
    sku, chip = m.sku, m.sku.chip_spec
    a.add_row("GPU", f"{sku.variant} ({chip.arch})")
    a.add_row("Process / die", f"{chip.process}, {chip.die_mm2:g} mm²" + (f", {chip.transistors_b:g} B transistors" if chip.transistors_b else ""))
    a.add_row("GPC / TPC / SM", f"{sku.gpcs} / {sku.sms // 2} / {sku.sms}  (full die {chip.gpcs} / {chip.gpcs * chip.tpcs_per_gpc} / {chip.sms})")
    a.add_row("CUDA cores", f"{sku.cuda_cores:,}" + (f"  (NVML: {s.num_cores:,})" if s.num_cores else ""))
    a.add_row("Tensor cores", f"{sku.tensor_cores} (5th gen)")
    a.add_row("RT cores", f"{sku.rt_cores} (4th gen)")
    b = _kv()
    b.add_row("ROPs / TMUs", f"{sku.rops} / {sku.tmus}")
    b.add_row("L2 cache", f"{sku.l2_mb} MB")
    b.add_row("Memory", f"{'/'.join(str(g) for g in sku.mem_gb)} GB {sku.mem_type} @ {sku.mem_gbps:g} Gbps")
    b.add_row("Bus / bandwidth", f"{sku.bus_width}-bit" + (f" (NVML: {s.bus_width})" if s.bus_width else "") + f", {sku.bandwidth_gbs:.0f} GB/s")
    b.add_row("NVENC / NVDEC", f"{sku.nvenc}× 9th gen / {sku.nvdec}× 6th gen")
    b.add_row("TGP / connector", f"{sku.tgp_w} W / {sku.power_connector}")
    b.add_row("PCIe", f"5.0 x{sku.pcie_lanes}")
    if sku.notes:
        b.add_row("Note", sku.notes)
    return _pair(a, b)


def clocks(s: GpuSnapshot, m: Match | None) -> Table:
    t = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
    t.add_column("Domain", style=KEY_STYLE)
    t.add_column("Current", justify="right")
    t.add_column("Max", justify="right")
    t.add_column("", ratio=2)
    for name, cur, mx in (("Graphics", s.clk_graphics, s.max_clk_graphics), ("SM", s.clk_sm, s.max_clk_sm),
                          ("Memory", s.clk_mem, s.max_clk_mem), ("Video", s.clk_video, s.max_clk_video)):
        t.add_row(name, f"{na(cur)} MHz", f"{na(mx)} MHz", _pct_bar(cur / mx if cur is not None and mx else None))
    t.add_row("P-state", na(s.pstate, "P{}"), "", "")
    t.add_row("Clock limiters", ", ".join(s.throttle) or "none", "", "")
    off = "N/A" if s.vf_gpc is None else f"GPC {s.vf_gpc:+d} MHz"
    if s.vf_mem is not None:
        off += f" · MEM {s.vf_mem:+d} MHz"
    t.add_row("VF offset", off, "", "")
    return t


def memory(s: GpuSnapshot, m: Match | None) -> Table:
    a = _kv()
    a.add_row("Total", gib(s.mem_total))
    a.add_row("Used", gib(s.mem_used))
    a.add_row("Free", gib(s.mem_free))
    a.add_row("Reserved (driver)", gib(s.mem_reserved))
    a.add_row("Used", _pct_bar(s.mem_used / s.mem_total if s.mem_used is not None and s.mem_total else None))
    b = _kv()
    b.add_row("BAR1", f"{gib(s.bar1_used)} of {gib(s.bar1_total)}")
    b.add_row("Controller util", _pct_bar(None if s.util_mem is None else s.util_mem / 100))
    if "DRAM_BW_UTIL" in s.gpm:
        b.add_row("DRAM bandwidth (GPM)", _pct_bar(s.gpm["DRAM_BW_UTIL"] / 100))
    if m:
        mods = m.profile.memory
        b.add_row("Modules", f"{len(mods)} × {m.profile.mem_gb_each:g} GB — {m.profile.mem_part or m.sku.mem_type}")
        b.add_row("Theoretical", f"{m.sku.bandwidth_gbs:.0f} GB/s ({m.sku.bus_width}-bit × {m.sku.mem_gbps:g} Gbps)")
    b.add_row("Memory junction", "N/A" if s.temp_mem is None else f"{s.temp_mem:.1f} °C  (hottest module)")
    if s.mem_modules:
        n = (s.bus_width or 0) // 32
        aligned = bool(n) and len(s.mem_modules) == n
        cells = "  ".join(f"{'M' + str(i) + ' ' if aligned else ''}{t:.0f}°C" for i, t in enumerate(s.mem_modules))
        b.add_row("Per module", cells)
    elif s.sensor_note:
        b.add_row("Per module", f"N/A  ({s.sensor_note})")
    return _pair(a, b)


def power_thermal(s: GpuSnapshot, m: Match | None) -> Table:
    a = _kv()
    a.add_row("Board power", na(s.power_w, "{:.1f}", " W"))
    a.add_row("Instant power", na(s.power_instant_w, "{:.1f}", " W"))
    rails = []
    if s.volt_nvvdd is not None:
        rails.append(f"NVVDD {s.volt_nvvdd:.3f} V")
    if s.volt_msvdd is not None:
        rails.append(f"MSVDD {s.volt_msvdd:.3f} V")
    a.add_row("Rail voltage", "  ".join(rails) if rails else "N/A")
    a.add_row("Enforced limit", na(s.power_limit_w, "{:.0f}", " W"))
    a.add_row("Default / min / max", f"{na(s.power_default_w, '{:.0f}')} / {na(s.power_min_w, '{:.0f}')} / {na(s.power_max_w, '{:.0f}')} W")
    a.add_row("Load", _pct_bar(s.power_frac))
    a.add_row("Energy since load", "N/A" if s.energy_j is None else f"{s.energy_j / 3.6e6:.3f} kWh")
    if m:
        a.add_row("VRM (GPU)", m.profile.gpu_vrm or "N/A")
        a.add_row("VRM (memory)", m.profile.mem_vrm or "N/A")
    b = _kv()
    b.add_row("GPU temperature", na(s.temp_gpu, unit=" °C"))
    hot = "N/A" if s.temp_hot is None else f"{s.temp_hot:.1f} °C"
    if s.temp_hot is None and s.sensor_note:
        hot += f"  ({s.sensor_note})"
    b.add_row("Hot spot", hot)
    b.add_row("Thermal margin", "N/A" if s.temp_margin is None else f"{s.temp_margin} °C under the limit")
    b.add_row("Target max / slowdown / shutdown", f"{na(s.temp_gpu_max)} / {na(s.temp_slowdown)} / {na(s.temp_shutdown)} °C")
    for f in s.fans:
        b.add_row(f"Fan {f.index}", f"{na(f.speed_pct)} % → target {na(f.target_pct)} %, {na(f.rpm)} RPM, {f.policy or 'N/A'}")
    b.add_row("Fan range", f"{na(s.fan_min_pct)}–{na(s.fan_max_pct)} %")
    b.add_row("Clock limiters", ", ".join(s.throttle) or "none")
    return _pair(a, b)


def pcie(s: GpuSnapshot, m: Match | None) -> Table:
    a = _kv()
    a.add_row("Current link", f"Gen{na(s.pcie_gen)} x{na(s.pcie_width)}")
    a.add_row("Max (this slot)", f"Gen{na(s.pcie_gen_max)} x{na(s.pcie_width_max)}")
    a.add_row("Max (GPU)", f"Gen{na(s.pcie_gen_max_gpu)}")
    if s.pcie_gen_max and s.pcie_gen_max_gpu and s.pcie_gen_max < s.pcie_gen_max_gpu:
        a.add_row("Note", Text(f"slot/platform limits the link to Gen{s.pcie_gen_max}", style="#f2c14e"))
    b = _kv()
    b.add_row("▲ To host", Text.assemble((kbs(s.pcie_tx_kbs), f"#{IN:06x}"), (" card → host (TX)", "dim")))
    b.add_row("▼ To card", Text.assemble((kbs(s.pcie_rx_kbs), f"#{OUT:06x}"), (" host → card (RX)", "dim")))
    if "PCIE_TX_PER_SEC" in s.gpm:
        b.add_row("▲/▼ GPM", Text.assemble((f"{s.gpm['PCIE_TX_PER_SEC']:.1f}", f"#{IN:06x}"), " / ",
                                                (f"{s.gpm.get('PCIE_RX_PER_SEC', 0):.1f}", f"#{OUT:06x}"), " MB/s"))
    b.add_row("Replay counter", na(s.pcie_replay))
    b.add_row("L0 → recovery", na(s.pcie_l0_recovery))
    b.add_row("Correctable / NAK", f"{na(s.pcie_corr)} / {na(s.pcie_nak)}")
    if m and m.profile.fingers:
        f = m.profile.fingers
        b.add_row("Edge connector", f"x{f.physical} physical / x{f.electrical} electrical")
    return _pair(a, b)


def displays_table(s: GpuSnapshot, m: Match | None) -> Table:
    t = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
    for col in ("Connector", "Type", "Status", "Monitor", "Mode"):
        t.add_column(col)
    for c in s.displays:
        t.add_row(c.name, c.kind, Text("connected", style="#3dff7a") if c.connected else Text("—", style="dim"),
                  c.monitor or "", c.mode or "")
    if not s.displays:
        t.add_row("—", "", "no DRM connectors found for this GPU", "", "")
    return t


def processes(s: GpuSnapshot, m: Match | None) -> Table:
    t = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
    for col, kw in (("PID", {"justify": "right"}), ("Name", {"ratio": 2}), ("User", {}), ("Type", {}),
                    ("VRAM", {"justify": "right"}), ("SM %", {"justify": "right"})):
        t.add_column(col, **kw)
    for p in s.processes:
        sm = "N/A" if p.sm_pct is None else f"{p.sm_pct:.0f}"
        t.add_row(str(p.pid), p.name, p.user or "", p.kind, gib(p.vram_bytes) if p.vram_bytes else "N/A", sm)
    if not s.processes:
        t.add_row("", "no processes", "", "", "", "")
    return t


BUILDERS = {
    "Overview": overview, "Architecture": architecture, "Clocks": clocks, "Memory": memory,
    "Power & Thermal": power_thermal, "PCIe": pcie, "Displays": displays_table, "Processes": processes,
}
