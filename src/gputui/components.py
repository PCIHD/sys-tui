"""Per-component detail: what we know about one physical part, tagged live / derived / spec."""

from __future__ import annotations

from pixboard.charts import human_bytes
from pixboard.rows import DERIVED, LIVE, SPEC, Row

from .boards import BoardProfile, Component
from .chips import SkuSpec, enabled_sm_map
from .telemetry import GpuSnapshot

CONNECTOR_RATING_W = {"12V-2x6": 600, "8-pin": 150}


def _hot(s: GpuSnapshot) -> str:
    if s.temp_hot is not None:
        text = f"{s.temp_hot:.1f} °C"
        if s.die_c:
            text += "  (" + " ".join(f"{t:.1f}" for t in s.die_c) + ")"
        return text
    return "N/A" + (f"  ({s.sensor_note})" if s.sensor_note else "")


def _rail_volt(s: GpuSnapshot, rail: str | None) -> float | None:
    return {"NVVDD": s.volt_nvvdd, "MSVDD": s.volt_msvdd}.get(rail or "")


def _mem_temps(c, s: GpuSnapshot) -> list[Row]:
    own = s.module_temp(c.channel)
    rows = []
    if own is not None:
        rows.append(Row("Temperature", f"{own:.0f} °C", LIVE))
    elif s.temp_mem is not None:
        rows.append(Row("Temperature", f"{s.temp_mem:.1f} °C  (hottest module)", LIVE))
    else:
        rows.append(Row("Temperature", "N/A" + (f"  ({s.sensor_note})" if s.sensor_note else ""), LIVE))
    if own is not None and s.temp_mem is not None and abs(s.temp_mem - own) >= 1:
        rows.append(Row("Hottest module", f"{s.temp_mem:.1f} °C", LIVE))
    return rows


def na(v, fmt: str = "{}", unit: str = "") -> str:
    if v is None:
        return "N/A"
    return fmt.format(v) + unit


def kbs(v: int | None) -> str:
    """An NVML KB/s PCIe counter as a byte rate."""
    return "N/A" if v is None else human_bytes(v * 1024, True)


def gib(b: int | float | None) -> str:
    return "N/A" if b is None else f"{b / 2**30:.2f} GiB"


def rows_for(c: Component, profile: BoardProfile, sku: SkuSpec, s: GpuSnapshot | None) -> list[Row]:
    fn = _BUILDERS.get(c.kind, _generic)
    rows = fn(c, profile, sku, s) if s is not None else fn(c, profile, sku, GpuSnapshot())
    common = []
    if c.part and not any(r.key == "Part" for r in rows):
        common.append(Row("Part", c.part, SPEC))
    if c.rating and not any(r.key == "Rating" for r in rows):
        common.append(Row("Rating", c.rating, SPEC))
    if c.note and c.kind != "gpc":
        common.append(Row("Note", c.note, SPEC))
    if c.kind in _BOARD_KINDS:
        common.append(Row("Position", _position(c), SPEC))
        common.append(Row("Source", "measured from PCB photo" if c.conf == "measured" else "inferred", SPEC))
    return rows + common


_BOARD_KINDS = {"gpu", "mem", "pad", "choke", "stage", "ctrl", "power", "shunt", "fuse", "cap", "mlcc",
                "header", "mcu", "switch", "output", "b2b"}


def _position(c: Component) -> str:
    x, y = c.cx, c.cy
    h = "left" if x < 33 else "right" if x > 66 else "centre"
    v = "top" if y < 33 else "bottom" if y > 66 else "middle"
    return f"{v}-{h} ({c.side}), x {x:.0f}% y {y:.0f}%"


def _gpu(c, p, sku, s):
    chip = sku.chip_spec
    r = [
        Row("Chip", f"{sku.variant} ({chip.arch}, {chip.process})", SPEC),
        Row("Temperature", na(s.temp_gpu, unit=" °C") + (f"  (slowdown {s.temp_slowdown} °C, shutdown {s.temp_shutdown} °C)" if s.temp_slowdown else ""), LIVE),
        Row("Hot spot", _hot(s), LIVE),
        Row("Thermal margin", "N/A" if s.temp_margin is None else f"{s.temp_margin} °C under the limit", LIVE),
        Row("Graphics / SM clock", f"{na(s.clk_graphics)} / {na(s.clk_sm)} MHz  (max {na(s.max_clk_graphics)})", LIVE),
        Row("GPU utilization", na(s.util_gpu, unit=" %"), LIVE),
    ]
    g = s.gpm
    if g:
        r.append(Row("SM active / occupancy", f"{g.get('SM_UTIL', 0):.1f} % / {g.get('SM_OCCUPANCY', 0):.1f} %", LIVE))
        r.append(Row("Tensor / FP32 / FP16 / INT", f"{g.get('ANY_TENSOR_UTIL', 0):.0f} / {g.get('FP32_UTIL', 0):.0f} / {g.get('FP16_UTIL', 0):.0f} / {g.get('INTEGER_UTIL', 0):.0f} %", LIVE))
    r += [
        Row("P-state", na(s.pstate, "P{}"), LIVE),
        Row("Clock limiters", ", ".join(s.throttle) if s.throttle else "none", LIVE),
        Row("VF offset", ("N/A" if s.vf_gpc is None else f"GPC {s.vf_gpc:+d} MHz")
            + ("" if s.vf_mem is None else f" · MEM {s.vf_mem:+d} MHz"), LIVE),
        Row("SMs", f"{sku.sms} of {chip.sms} enabled ({sku.gpcs}/{chip.gpcs} GPCs)", SPEC),
        Row("CUDA / Tensor / RT", f"{sku.cuda_cores:,} / {sku.tensor_cores} (5th gen) / {sku.rt_cores} (4th gen)", SPEC),
        Row("ROPs / TMUs", f"{sku.rops} / {sku.tmus}", SPEC),
        Row("L2 cache", f"{sku.l2_mb} MB (of {chip.l2_mb} MB on die)", SPEC),
        Row("Die", f"{chip.die_mm2:g} mm²" + (f", {chip.transistors_b:g} B transistors" if chip.transistors_b else ""), SPEC),
    ]
    return r


def _mem(c, p, sku, s):
    mems = p.memory
    n = len(mems) or 1
    gbps = s.clk_mem * 2 / 1000 if s.clk_mem else None  # NVML reports half the GDDR7 data rate
    r = [
        Row("Part", p.mem_part or f"{sku.mem_type}", SPEC),
        Row("Capacity", f"{p.mem_gb_each:g} GB", SPEC),
        Row("Channel", f"32-bit controller #{c.channel} of {sku.bus_width // 32}" + (" (clamshell pair)" if n > sku.bus_width // 32 else ""), DERIVED),
        Row("Memory clock", f"{na(s.clk_mem)} MHz" + (f"  ≈ {gbps:.1f} Gbps/pin" if gbps else ""), LIVE),
        *_mem_temps(c, s),
    ]
    if s.mem_used is not None:
        r.append(Row("Share of used VRAM", f"≈ {gib(s.mem_used / n)} (VRAM interleaves across all chips)", DERIVED))
    dram = s.dram_activity
    if dram is not None:
        per = sku.bandwidth_gbs * dram / n
        src = "GPM DRAM" if "DRAM_BW_UTIL" in s.gpm else "mem util"
        r.append(Row("Bandwidth now", f"≈ {per:.1f} GB/s of {sku.bandwidth_gbs / n:.0f} ({src} {dram * 100:.0f} %)", DERIVED))
    return r


def _vrm(c, p, sku, s):
    rail = c.rail or "?"
    same = [x for x in p.components if x.kind == c.kind and x.rail == rail]
    phases = len(same) if c.kind == "choke" else len([x for x in p.components if x.kind == "choke" and x.rail == rail])
    desc = {"NVVDD": "GPU core", "FBVDD": "memory", "MSVDD": "GPU misc/SRAM"}.get(rail, rail)
    r = [Row("Rail", f"{rail} — {desc}, {phases} phase{'s' if phases != 1 else ''} on this board", SPEC)]
    if c.marking:
        r.append(Row("Marking", c.marking, SPEC))
    ctrl = p.gpu_vrm if rail == "NVVDD" else p.mem_vrm
    if ctrl:
        r.append(Row("VRM design", ctrl, SPEC))
    volt = _rail_volt(s, rail)
    if volt is not None:
        r.append(Row("Rail voltage", f"{volt:.3f} V", LIVE))
    if s.power_w is not None and phases:
        share = 0.82 if rail == "NVVDD" else 0.12
        per = s.power_w * share / phases
        r.append(Row("Est. load / phase", f"≈ {per:.1f} W ({share * 100:.0f} % of board power ÷ {phases})", DERIVED))
    r.append(Row("Per-phase current", "unavailable", SPEC))
    return r


def _power(c, p, sku, s):
    typ = "12V-2x6" if "12V" in (c.part or "") else "8-pin"
    rating = CONNECTOR_RATING_W[typ]
    r = [
        Row("Board power", na(s.power_w, "{:.1f}", " W") + ("" if s.power_instant_w is None else f"  (instant {s.power_instant_w:.1f} W)"), LIVE),
        Row("Power limit", f"{na(s.power_limit_w, '{:.0f}')} W  (default {na(s.power_default_w, '{:.0f}')}, range {na(s.power_min_w, '{:.0f}')}–{na(s.power_max_w, '{:.0f}')} W)", LIVE),
        Row("Connector load", "N/A" if s.power_w is None else f"≈ {s.power_w / rating * 100:.0f} % of {rating} W rating (incl. slot share)", DERIVED),
        Row("Energy since driver load", "N/A" if s.energy_j is None else f"{s.energy_j / 3.6e6:.3f} kWh", LIVE),
        Row("Per-pin current", "not exposed by NVML", SPEC),
    ]
    return r


def _fingers(c, p, sku, s):
    f = p.fingers
    return [
        Row("Link", f"PCIe Gen{na(s.pcie_gen)} x{na(s.pcie_width)}", LIVE),
        Row("Max (GPU / this slot)", f"Gen{na(s.pcie_gen_max_gpu)} / Gen{na(s.pcie_gen_max)} x{na(s.pcie_width_max)}", LIVE),
        Row("Traffic", f"▲ card → host {kbs(s.pcie_tx_kbs)} · ▼ host → card {kbs(s.pcie_rx_kbs)}", LIVE),
        Row("Replay counter", na(s.pcie_replay), LIVE),
        Row("Edge", f"x{f.physical} physical, x{f.electrical} electrical" if f else "separate PCIe board", SPEC),
        Row("Slot power", "up to 75 W (not separately metered)", SPEC),
    ]


def _output(c, p, sku, s):
    from .render.pcb import output_connector

    outs = [x for x in p.components if x.kind == "output"]
    con = output_connector(c, outs, s.displays)
    r = [Row("Standard", c.part or c.label or "", SPEC)]
    if con is None:
        r.append(Row("DRM connector", "N/A", LIVE))
    else:
        r += [
            Row("DRM connector", f"{con.name} (port order approximate)", LIVE),
            Row("Status", "connected" if con.connected else "disconnected", LIVE),
            Row("Monitor", con.monitor or "—", LIVE),
            Row("Mode", con.mode or "—", LIVE),
        ]
    return r


def _header(c, p, sku, s):
    r = []
    if "fan" in (c.part or "").lower() or (c.label or "") == "FAN":
        for f in s.fans:
            r.append(Row(f"Fan controller {f.index}", f"{na(f.speed_pct)} % (target {na(f.target_pct)} %), {na(f.rpm)} RPM, {f.policy or 'N/A'}", LIVE))
    return r


def _switch(c, p, sku, s):
    return [Row("VBIOS", na(s.vbios), LIVE), Row("InfoROM", na(s.inforom), LIVE)]


def _ctrl(c, p, sku, s):
    r = []
    if c.rail:
        desc = {"NVVDD": "GPU core", "FBVDD": "memory", "MSVDD": "GPU misc/SRAM"}.get(c.rail, c.rail)
        r.append(Row("Drives", f"{c.rail} — {desc}", SPEC))
        volt = _rail_volt(s, c.rail)
        if volt is not None:
            r.append(Row("Rail voltage", f"{volt:.3f} V", LIVE))
        r.append(Row("Board power", na(s.power_w, "{:.1f}", " W"), LIVE))
    return r


def _pad(c, p, sku, s):
    return [Row("State", "unpopulated memory position (this SKU uses a narrower bus)", SPEC)]


def _generic(c, p, sku, s):
    return []


def _fan(c, p, sku, s):
    idx = getattr(c, "channel", None) or 0
    fans = s.fans
    r = [Row("Fan size", f"{p.fan_mm:g} mm", SPEC)]
    if fans:
        f = fans[min(idx, len(fans) - 1)]
        shared = len(fans) < p.fans
        r += [
            Row("Controller", f"#{f.index}" + (f" (NVML reports {len(fans)} controllers for {p.fans} fans)" if shared else ""), LIVE),
            Row("Speed", f"{na(f.speed_pct)} % (target {na(f.target_pct)} %)", LIVE),
            Row("RPM", na(f.rpm), LIVE),
            Row("Policy", f.policy or "N/A", LIVE),
            Row("Allowed range", f"{na(s.fan_min_pct)}–{na(s.fan_max_pct)} %", LIVE),
        ]
    else:
        r.append(Row("Speed", "N/A", LIVE))
    return r


def _die_block(c, p, sku, s):
    g = s.gpm
    k = c.kind
    r = []
    if k == "gpc":
        chip = sku.chip_spec
        n_on = sum(enabled_sm_map(chip, sku)[c.channel]) if c.channel is not None else 0
        r.append(Row("SMs", c.note or "", SPEC))
        if n_on:
            r.append(Row("Cores in this GPC", f"{n_on * chip.fp32_per_sm} CUDA · {n_on * chip.tensor_per_sm} tensor · {n_on} RT", SPEC))
        r.append(Row("SM clock", f"{na(s.clk_sm)} / {na(s.max_clk_sm)} MHz", LIVE))
        r.append(Row("SM activity (all SMs)", f"{g['SM_UTIL']:.1f} % (GPM, device-wide)" if "SM_UTIL" in g else na(s.util_gpu, unit=" % (GPU util)"), LIVE))
        if "SM_OCCUPANCY" in g:
            r.append(Row("Warp occupancy", f"{g['SM_OCCUPANCY']:.1f} %", LIVE))
        if "ANY_TENSOR_UTIL" in g:
            r.append(Row("Tensor pipe", f"{g['ANY_TENSOR_UTIL']:.1f} %", LIVE))
        r.append(Row("Per-GPC counters", "not exposed by NVML — lighting spreads the device-wide value", SPEC))
    elif k == "l2":
        r.append(Row("DRAM bandwidth", f"{g['DRAM_BW_UTIL']:.1f} % (GPM)" if "DRAM_BW_UTIL" in g else na(s.util_mem, unit=" % (mem util)"), LIVE))
    elif k == "mc":
        mem = next((m for m in p.memory if m.channel == c.channel), None)
        r.append(Row("Width", "32-bit", SPEC))
        r.append(Row("Attached module", f"{mem.label} ({mem.id})" if mem else "none on this board", DERIVED))
        r.append(Row("DRAM bandwidth (all channels)", f"{s.dram_activity * 100:.0f} %" if s.dram_activity is not None else "N/A", LIVE))
    elif k in ("nvenc", "nvdec", "ofa", "nvjpg"):
        key = {"nvenc": f"NVENC_{c.channel}_UTIL", "nvdec": f"NVDEC_{c.channel}_UTIL", "ofa": "NVOFA_0_UTIL", "nvjpg": "NVJPG_0_UTIL"}[k]
        r.append(Row("Engine utilization", f"{g[key]:.1f} % (GPM, this engine)" if key in g else "N/A", LIVE))
        if k == "nvenc":
            r.append(Row("All encoders", na(s.util_enc, unit=" %") + f", {na(s.enc_sessions)} sessions, {na(s.enc_fps)} fps", LIVE))
        if k == "nvdec":
            r.append(Row("All decoders", na(s.util_dec, unit=" %"), LIVE))
    elif k == "host":
        return _fingers(c, p, sku, s)[:4]
    elif k == "gigathread":
        r.append(Row("GPU utilization", na(s.util_gpu, unit=" %"), LIVE))
        if "GRAPHICS_UTIL" in g:
            r.append(Row("Graphics engine active", f"{g['GRAPHICS_UTIL']:.1f} %", LIVE))
    elif k == "display":
        r.append(Row("Display active", na(s.display_active), LIVE))
        r.append(Row("Connected", ", ".join(f"{d.name}: {d.monitor or '?'}" for d in s.displays if d.connected) or "none", LIVE))
    return r


def _pipes(c, p, sku, s):
    g = s.gpm
    r = []
    labels = {"SM_UTIL": "SM active", "SM_OCCUPANCY": "Warp occupancy", "ANY_TENSOR_UTIL": "Tensor pipe",
              "FP32_UTIL": "FP32 pipe", "FP16_UTIL": "FP16 pipe", "FP64_UTIL": "FP64 pipe",
              "INTEGER_UTIL": "INT pipe", "GRAPHICS_UTIL": "Graphics engine active", "DRAM_BW_UTIL": "DRAM bandwidth"}
    for key, lab in labels.items():
        r.append(Row(lab, f"{g[key]:.1f} %" if key in g else "N/A", LIVE))
    act = s.sm_activity
    if act is not None:
        r.append(Row("≈ busy CUDA cores", f"{act * sku.cuda_cores:.0f} of {sku.cuda_cores}", DERIVED))
    if not g:
        r.append(Row("GPM", "unsupported here: SM activity falls back to GPU util", SPEC))
    r.append(Row("Scope", "device-wide: NVML has no per-SM or per-GPC counters", SPEC))
    return r


def _cooler(c, p, sku, s):
    r = [Row("Design", p.cooler or "N/A", SPEC), Row("Fans", f"{p.fans} × {p.fan_mm:g} mm", SPEC),
         Row("Card size", f"{p.card_mm[0]:g} × {p.card_mm[1]:g} mm", SPEC),
         Row("GPU temperature", na(s.temp_gpu, unit=" °C"), LIVE)]
    for f in s.fans:
        r.append(Row(f"Fan controller {f.index}", f"{na(f.rpm)} RPM, {na(f.speed_pct)} %", LIVE))
    return r


_BUILDERS = {
    "gpu": _gpu, "mem": _mem, "choke": _vrm, "stage": _vrm, "power": _power, "fingers": _fingers,
    "output": _output, "header": _header, "switch": _switch, "ctrl": _ctrl, "pad": _pad, "fan": _fan,
    "shunt": _power, "fuse": _power, "cooler": _cooler,
    "gpc": _die_block, "l2": _die_block, "mc": _die_block, "nvenc": _die_block, "nvdec": _die_block,
    "ofa": _die_block, "nvjpg": _die_block, "host": _die_block, "gigathread": _die_block, "display": _die_block,
    "pipes": _pipes,
}


def live_summary(c: Component, sku: SkuSpec, s: GpuSnapshot | None) -> str:
    """One-line live value for the Components table."""
    if s is None:
        return ""
    k = c.kind
    if k == "gpu":
        hot = f" · hot {s.temp_hot:.0f} °C" if s.temp_hot is not None else ""
        return f"{na(s.temp_gpu)} °C{hot} · {na(s.clk_graphics)} MHz · {na(s.util_gpu)} %"
    if k == "mem":
        d = s.dram_activity
        own = s.module_temp(c.channel)
        temp = f"{own:.0f} °C · " if own is not None else (f"{s.temp_mem:.0f} °C · " if s.temp_mem is not None else "")
        return temp + (f"{na(s.clk_mem)} MHz · bw {d * 100:.0f} %" if d is not None else f"{na(s.clk_mem)} MHz")
    if k in ("power", "shunt", "fuse"):
        return f"{na(s.power_w, '{:.0f}')} / {na(s.power_limit_w, '{:.0f}')} W"
    if k in ("choke", "stage", "ctrl"):
        volt = _rail_volt(s, c.rail)
        base = f"board {na(s.power_w, '{:.0f}')} W"
        return f"{volt:.3f} V · {base}" if volt is not None else base
    if k == "fingers":
        return f"Gen{na(s.pcie_gen)} x{na(s.pcie_width)}"
    if k == "fan" and s.fans:
        f = s.fans[min(c.channel or 0, len(s.fans) - 1)]
        return f"{na(f.rpm)} RPM · {na(f.speed_pct)} %"
    if k == "output":
        return ""
    return ""
