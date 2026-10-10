"""Resolve a component's ``bind`` to the live data behind it."""

from __future__ import annotations

import math
import re

from .boards import Component
from .telemetry import Dimm, DiskStat, GpuStat, NetStat, ProcStat, Reading, SystemSnapshot

SUPERIO = ("nct6687", "nct6683", "nct6775", "nct6779", "nct6791", "nct6792", "nct6793", "nct6795", "nct6796",
           "nct6797", "nct6798", "nct6799", "it87", "asus_ec_sensors", "gigabyte_wmi")


def superio_reading(s: SystemSnapshot, label: str, kind: str | None = None) -> Reading | None:
    for r in s.sensors:
        if r.chip in SUPERIO and (r.label == label or r.key == label) and (kind is None or r.kind == kind):
            return r
    return None


def superio_chip(s: SystemSnapshot) -> str | None:
    return next((r.chip for r in s.sensors if r.chip in SUPERIO), None)


def dimm(s: SystemSnapshot, c: Component) -> Dimm | None:
    b = c.bind_kind()
    if not b or b[0] != "dimm":
        return None
    return s.mem.dimm(int(b[1], 16))


def nvme(s: SystemSnapshot, c: Component) -> DiskStat | None:
    b = c.bind_kind()
    if not b or b[0] != "nvme":
        return None
    return s.disk_on_port(b[1])


def sata(s: SystemSnapshot, c: Component) -> list[DiskStat]:
    b = c.bind_kind()
    if not b or b[0] != "sata":
        return []
    out = []
    for p in b[1].split(","):
        d = s.disk_on_sata(int(p))
        if d:
            out.append(d)
    return out


def sata_ports(c: Component) -> list[int]:
    b = c.bind_kind()
    return [int(p) for p in b[1].split(",")] if b and b[0] == "sata" else []


def fan(s: SystemSnapshot, c: Component) -> Reading | None:
    b = c.bind_kind()
    if not b or b[0] != "fan":
        return None
    return superio_reading(s, b[1], "fan")


def fan_pwm(s: SystemSnapshot, c: Component) -> Reading | None:
    f = fan(s, c)
    if f is None:
        return None
    n = f.key[3:]
    return next((r for r in s.sensors if r.chip == f.chip and r.key == f"pwm{n}"), None)


def temp(s: SystemSnapshot, c: Component) -> Reading | None:
    b = c.bind_kind()
    if not b or b[0] != "temp":
        return None
    r = superio_reading(s, b[1], "temp")
    return r if r is not None and r.value > 0 else None


def volt(s: SystemSnapshot, label: str) -> float | None:
    r = superio_reading(s, label, "in")
    return r.value if r is not None and r.value > 0 else None


def net(s: SystemSnapshot, c: Component) -> NetStat | None:
    b = c.bind_kind()
    if not b or b[0] != "net":
        return None
    return s.net_of_kind(b[1])


def gpu(s: SystemSnapshot, c: Component | None = None) -> GpuStat | None:
    return s.gpu("nvidia") or next((g for g in s.gpus if g.vendor != "amd"), None)


def disk_activity(d: DiskStat | None) -> float:
    return 0.0 if d is None else (d.read_bps or 0.0) + (d.write_bps or 0.0)


def net_activity(n: NetStat | None) -> float:
    return 0.0 if n is None else (n.rx_bps or 0.0) + (n.tx_bps or 0.0)


def chipset_traffic(s: SystemSnapshot, uplink: str | None) -> float:
    """Bytes/s through the chipset: disks and NICs whose PCI path passes its uplink port."""
    if not uplink:
        return 0.0
    tot = 0.0
    for d in s.physical_disks():
        if d.pci and d.pci[0] == uplink:
            tot += disk_activity(d)
    for n in s.physical_nets():
        if n.pci and n.pci[0] == uplink:
            tot += net_activity(n)
    return tot


def chipset_uplink(s: SystemSnapshot) -> str | None:
    """Root port the chipset hangs off: the one shared by devices behind ≥2 bridges."""
    counts: dict[str, int] = {}
    for chain in [d.pci for d in s.disks] + [n.pci for n in s.nets]:
        if len(chain) >= 3:
            counts[chain[0]] = counts.get(chain[0], 0) + 1
    return max(counts, key=counts.get) if counts else None


def level(bps: float | None) -> float:
    """0..1 activity from a byte rate, log-scaled: ~1 kB/s → 0.05, ~1 GB/s → 1."""
    if not bps or bps <= 0:
        return 0.0
    return max(0.0, min(1.0, math.log10(1 + bps / 1000) / 6))


# usable bytes/s per lane, per direction, after line encoding (8b/10b to Gen2, 128b/130b from Gen3)
PCIE_LANE_BPS = {1: 250e6, 2: 500e6, 3: 984.6e6, 4: 1969.2e6, 5: 3938.5e6, 6: 7877e6}
SATA_BPS = 600e6  # SATA III after 8b/10b
UPLINK_LINK = (4, 4)  # AM5 chipset uplink: PCIe 4.0 x4


def pcie_bps(gen: int | None, width: int | None) -> float | None:
    return PCIE_LANE_BPS[gen] * width if gen in PCIE_LANE_BPS and width else None


def link_bps(link: str) -> float | None:
    """Per-direction capacity of a DiskStat.link string: "PCIe 4.0 x4" or "SATA"."""
    if link == "SATA":
        return SATA_BPS
    m = re.match(r"PCIe (\d)\.0 x(\d+)", link or "")
    return pcie_bps(int(m.group(1)), int(m.group(2))) if m else None


def clients(s: SystemSnapshot, profile, proc: ProcStat) -> list[str]:
    """Board component ids this process maps onto. DIMMs are never attributed per-stick."""
    out: list[str] = []
    if proc.last_cpu is not None:
        out.append("CPU")
    if proc.gpu:
        out.append("GPU")
        for c in profile.of_kind("pcie"):
            if c.bind == "gpu:x16":
                out.append(c.id)
    for name in proc.disks:
        for c in profile.components:
            d = nvme(s, c)
            if d is not None and d.name == name:
                out.append(c.id)
            if any(x.name == name for x in sata(s, c)):
                out.append(c.id)
    seen: set[str] = set()
    uniq = []
    for cid in out:
        if cid not in seen:
            seen.add(cid)
            uniq.append(cid)
    return uniq


def procs_for(s: SystemSnapshot, profile, cid: str) -> list[ProcStat]:
    return [p for p in s.procs if cid in clients(s, profile, p)]


def load(up: float | None, down: float | None, cap: float | None, duplex: bool = True) -> float | None:
    """0..1 share of a link's capacity in use. A full-duplex link is as busy as its busier direction."""
    if not cap or (up is None and down is None):
        return None
    used = max(up or 0.0, down or 0.0) if duplex else (up or 0.0) + (down or 0.0)
    return min(1.0, used / cap)


def gpu_pcie_load(g: GpuStat | None) -> float | None:
    """Against the link as currently trained; cards drop to Gen1 at idle and retrain under load."""
    return None if g is None else load(g.pcie_tx_bps, g.pcie_rx_bps, pcie_bps(g.pcie_gen, g.pcie_width))


def disk_link_load(d: DiskStat | None) -> float | None:
    return None if d is None else load(d.read_bps, d.write_bps, link_bps(d.link), duplex=d.link != "SATA")


def net_link_load(n: NetStat | None) -> float | None:
    return None if n is None or not n.speed_mbps else load(n.rx_bps, n.tx_bps, n.speed_mbps * 1e6 / 8)


def mem_load(s: SystemSnapshot) -> float | None:
    """DRAM data bus share, against the theoretical peak; reads and writes share the bus."""
    m = s.mem
    return load(m.read_bps, m.write_bps, m.peak_bps, duplex=False)


def chipset_split(s: SystemSnapshot, uplink: str | None) -> tuple[float, float]:
    """(towards CPU, from CPU) bytes/s through the chipset uplink."""
    up = down = 0.0
    if uplink:
        for d in s.physical_disks():
            if d.pci and d.pci[0] == uplink:
                up, down = up + (d.read_bps or 0.0), down + (d.write_bps or 0.0)
        for n in s.physical_nets():
            if n.pci and n.pci[0] == uplink:
                up, down = up + (n.rx_bps or 0.0), down + (n.tx_bps or 0.0)
    return up, down


def uplink_load(s: SystemSnapshot, uplink: str | None) -> float | None:
    return load(*chipset_split(s, uplink), pcie_bps(*UPLINK_LINK)) if uplink else None


SLOT_SHARE = 0.10  # share of a 12V-2x6 card's power assumed to come through the slot
SLOT_12V_MAX = 66.0  # PCIe CEM limit for the slot's 12 V pins


def atx_estimate(s: SystemSnapshot) -> dict[str, float]:
    """Rough watts the 24-pin delivers per destination. Nothing measures the 24-pin, so every
    figure here is an estimate from what the loads are doing (shown as ◐ derived)."""
    g = gpu(s)
    slot = min(SLOT_12V_MAX, SLOT_SHARE * (g.power_w or 0.0)) if g else 0.0
    n_dimm = len(s.mem.dimms) or 2
    dimm = n_dimm * (1.5 + 3.0 * s.cpu.util / 100)  # DDR5 PMIC input, 5 V
    storage = 5.0 + 1.5 * level(chipset_traffic(s, chipset_uplink(s)))  # chipset
    for d in s.physical_disks():
        act = level(disk_activity(d))
        storage += (1.5 + 2.0 * act) if d.kind == "hdd" else (1.0 + 5.0 * act)
    fans = 0.0
    for r in s.sensors:
        if r.kind == "fan" and r.chip in SUPERIO and r.value > 0:
            pump = "pump" in r.label.lower()
            fans += (4.0 if pump else 2.5) * (r.value / (3000 if pump else 2000)) ** 3
    return {"slot": slot, "dimm": dimm, "storage": storage, "fans": fans}
