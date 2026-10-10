"""Network interfaces: rx/tx rates, link, Wi-Fi signal, NIC temperature."""

from __future__ import annotations

import socket

import psutil

from . import NetStat, Rates, SysFS
from .sensors import Chip, chip_under
from .storage import pci_chain


def wireless_signal(fs: SysFS) -> dict[str, float]:
    out = {}
    for line in (fs.read("proc/net/wireless") or "").splitlines()[2:]:
        parts = line.split()
        if len(parts) >= 4:
            try:
                out[parts[0].rstrip(":")] = float(parts[3].rstrip("."))
            except ValueError:
                pass
    return out


def kind_of(fs: SysFS, name: str) -> str:
    if name == "lo":
        return "loopback"
    base = f"sys/class/net/{name}"
    if fs.p(base + "/wireless").exists() or fs.p(base + "/phy80211").exists():
        return "wifi"
    if fs.p(base + "/device").exists():
        return "ethernet"
    return "virtual"


def collect(fs: SysFS, rates: Rates, chips: list[Chip], now: float) -> list[NetStat]:
    try:
        io = psutil.net_io_counters(pernic=True)
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
    except OSError:
        return []
    sig = wireless_signal(fs)
    out = []
    for name, c in io.items():
        kind = kind_of(fs, name)
        st = stats.get(name)
        n = NetStat(name, kind=kind, up=bool(st and st.isup), rx_total=c.bytes_recv, tx_total=c.bytes_sent)
        n.speed_mbps = st.speed if st and st.speed else None
        n.rx_bps = rates.rate(f"n:{name}:rx", c.bytes_recv, now)
        n.tx_bps = rates.rate(f"n:{name}:tx", c.bytes_sent, now)
        n.errors = c.errin + c.errout + c.dropin + c.dropout
        for a in addrs.get(name, []):
            if a.family == socket.AF_INET and not n.addr:
                n.addr = a.address
            elif a.family == psutil.AF_LINK:
                n.mac = a.address
        n.signal_dbm = sig.get(name)
        dev = fs.real(f"sys/class/net/{name}/device") if kind in ("ethernet", "wifi") else ""
        n.pci = pci_chain(dev)
        drv = fs.real(f"sys/class/net/{name}/device/driver") if dev else ""
        n.driver = drv.rsplit("/", 1)[-1] if drv else ""
        chip = chip_under(chips, dev)
        if chip and chip.first("temp"):
            n.temp = chip.first("temp").value
        out.append(n)
    order = {"ethernet": 0, "wifi": 1, "virtual": 2, "loopback": 3}
    out.sort(key=lambda n: (order[n.kind], n.name))
    return out
