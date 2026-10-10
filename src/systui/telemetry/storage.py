"""Block devices: throughput/IOPS/busy, model, PCIe link, temperatures; filesystem usage."""

from __future__ import annotations

import re

import psutil

from . import DiskStat, PartStat, Rates, SysFS
from .sensors import Chip, chip_under

PCI_ADDR = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]")
GT_TO_GEN = {"2.5": 1, "5.0": 2, "5": 2, "8.0": 3, "8": 3, "16.0": 4, "16": 4, "32.0": 5, "32": 5, "64.0": 6, "64": 6}
SKIP_FS = {"squashfs", "tmpfs", "devtmpfs", "overlay", "composefs", "erofs", "iso9660", "ramfs", "efivarfs"}


def pci_chain(path: str) -> list[str]:
    """PCI addresses along a device path, root port first, without the host bridge."""
    return PCI_ADDR.findall(path)


def pci_dir(path: str) -> str | None:
    """Directory of the deepest PCI device in a resolved sysfs path."""
    last = None
    for m in PCI_ADDR.finditer(path):
        last = m
    return path[: last.end()] if last else None


def pcie_link(fs: SysFS, pdir: str | None) -> str:
    if not pdir:
        return ""
    sp = fs.read(fs.p(pdir) / "current_link_speed") or ""
    wd = fs.read(fs.p(pdir) / "current_link_width") or ""
    m = re.match(r"([\d.]+)", sp)
    gen = GT_TO_GEN.get(m.group(1)) if m else None
    if not gen or not wd:
        return ""
    return f"PCIe {gen}.0 x{wd}"


def sata_port(fs: SysFS, dev: str) -> int | None:
    """1-based port of a libata disk on its controller (ataN numbering is global)."""
    m = re.search(r"/ata(\d+)/", dev)
    ctrl = pci_dir(dev)
    if not m or not ctrl:
        return None
    mine = int(m.group(1))
    ports = []
    for p in fs.glob("sys/class/ata_port/ata*"):
        real = fs.real(p)
        if pci_dir(real) == ctrl:
            ports.append(int(p.name[3:]))
    ports.sort()
    return ports.index(mine) + 1 if mine in ports else None


def kind_of(fs: SysFS, name: str) -> str:
    if name.startswith("zram"):
        return "zram"
    if name.startswith(("dm-", "md", "loop", "ram", "nbd")):
        return "virtual"
    if name.startswith("nvme"):
        return "nvme"
    return "hdd" if fs.read_int(f"sys/block/{name}/queue/rotational") == 1 else "ssd"


def collect(fs: SysFS, rates: Rates, chips: list[Chip], now: float) -> tuple[list[DiskStat], list[PartStat]]:
    try:
        io = psutil.disk_io_counters(perdisk=True) or {}
    except (OSError, RuntimeError):
        io = {}
    disks: list[DiskStat] = []
    for d in fs.glob("sys/block/*"):
        name = d.name
        if name.startswith(("loop", "ram", "sr", "fd")):
            continue
        kind = kind_of(fs, name)
        dev = fs.real(d / "device") if (d / "device").exists() else ""
        s = DiskStat(name, kind=kind)
        s.model = (fs.read(d / "device/model") or "").strip()
        if kind == "zram":
            s.model = "zram (compressed RAM)"
        elif kind == "virtual":
            s.model = fs.read(d / "dm/name") or s.model or "virtual"
        s.size = (fs.read_int(d / "size") or 0) * 512
        s.firmware = (fs.read(d / "device/firmware_rev") or fs.read(d / "device/rev") or "").strip()
        s.serial = (fs.read(d / "device/serial") or "").strip()
        s.pci = pci_chain(dev)
        if kind == "nvme":
            s.link = pcie_link(fs, pci_dir(dev))
        elif kind in ("ssd", "hdd") and dev:
            s.sata_port = sata_port(fs, dev)
            s.link = "SATA" if s.sata_port else ""
        chip = chip_under(chips, dev)
        if chip:
            for r in chip.readings:
                if r.kind == "temp" and r.value > 0:
                    s.temps[r.label] = r.value
            s.temp = s.temps.get("Composite", next(iter(s.temps.values()), None))
        c = io.get(name)
        if c is not None:
            s.read_bps = rates.rate(f"d:{name}:rb", c.read_bytes, now)
            s.write_bps = rates.rate(f"d:{name}:wb", c.write_bytes, now)
            s.read_iops = rates.rate(f"d:{name}:rc", c.read_count, now)
            s.write_iops = rates.rate(f"d:{name}:wc", c.write_count, now)
            busy = getattr(c, "busy_time", None)
            r = rates.rate(f"d:{name}:busy", busy, now)
            s.busy = None if r is None else min(100.0, r / 10)  # ms per s → %
        disks.append(s)
    return disks, partitions()


def partitions() -> list[PartStat]:
    best: dict[str, PartStat] = {}
    try:
        parts = psutil.disk_partitions(all=False)
    except OSError:
        return []
    for p in parts:
        if p.fstype in SKIP_FS or not p.device.startswith("/dev/"):
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        prev = best.get(p.device)
        if prev is None or len(p.mountpoint) < len(prev.mount):
            best[p.device] = PartStat(p.device, p.mountpoint, p.fstype, u.total, u.used)
    return sorted(best.values(), key=lambda s: s.mount)
