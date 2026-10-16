"""Rolling windows of every charted series, plus per-sensor min/max since start."""

from __future__ import annotations

from collections import deque

from .telemetry import SystemSnapshot


class History:
    def __init__(self, n: int = 300) -> None:
        self.n = n
        self.data: dict[str, deque[float]] = {}
        self.minmax: dict[tuple[str, str, str], tuple[float, float]] = {}
        self.samples = 0

    def get(self, key: str) -> deque[float]:
        d = self.data.get(key)
        if d is None:
            d = self.data[key] = deque(maxlen=self.n)
        return d

    def push(self, key: str, v: float | None) -> None:
        self.get(key).append(float(v or 0.0))

    def last(self, key: str, default: float = 0.0) -> float:
        d = self.data.get(key)
        return d[-1] if d else default

    def peak(self, key: str, window: int | None = None) -> float:
        d = self.data.get(key)
        if not d:
            return 0.0
        vals = list(d)[-window:] if window else d
        return max(vals)

    def ingest(self, s: SystemSnapshot) -> None:
        self.samples += 1
        c = s.cpu
        self.push("cpu", c.util)
        for i, v in enumerate(c.per_thread):
            self.push(f"cpu:t{i}", v)
        for k in range(c.cores):
            self.push(f"cpu:c{k}", c.core_util(k))
            if k < len(c.core_w):
                self.push(f"cpu:cw{k}", c.core_w[k])
        self.push("cpu:w", c.package_w)
        self.push("cpu:tctl", c.tctl)
        mhz = [m for m in c.mhz if m]
        self.push("cpu:mhz", sum(mhz) / len(mhz) if mhz else 0)
        m = s.mem
        self.push("mem", m.used_frac * 100)
        self.push("mem:cache", (m.cached + m.buffers) / m.total * 100 if m.total else 0)
        self.push("swap", m.swap_used / m.swap_total * 100 if m.swap_total else 0)
        if m.read_bps is not None or m.write_bps is not None:
            self.push("mem:rd", m.read_bps)
            self.push("mem:wr", m.write_bps)
        for i, (rd, wr) in enumerate(m.channel_bps):
            self.push(f"mem:ch{i}:rd", rd)
            self.push(f"mem:ch{i}:wr", wr)
        for d in m.dimms:
            self.push(f"dimm:{d.addr:#x}", d.temp)
            if d.temp is not None:
                key = ("dimm", f"{d.addr:#x}", "temp")
                lo, hi = self.minmax.get(key, (d.temp, d.temp))
                self.minmax[key] = (min(lo, d.temp), max(hi, d.temp))
        for d in s.disks:
            self.push(f"disk:{d.name}:r", d.read_bps)
            self.push(f"disk:{d.name}:w", d.write_bps)
            self.push(f"disk:{d.name}:busy", d.busy)
        for n in s.nets:
            self.push(f"net:{n.name}:rx", n.rx_bps)
            self.push(f"net:{n.name}:tx", n.tx_bps)
        # whole-machine totals over physical devices only (no loopback, bridges or partitions)
        self.push("disk:all:r", sum(d.read_bps or 0 for d in s.physical_disks()))
        self.push("disk:all:w", sum(d.write_bps or 0 for d in s.physical_disks()))
        self.push("net:all:rx", sum(n.rx_bps or 0 for n in s.physical_nets()))
        self.push("net:all:tx", sum(n.tx_bps or 0 for n in s.physical_nets()))
        for g in s.gpus:
            p = f"gpu{g.index}"
            self.push(f"{p}:util", g.util)
            self.push(f"{p}:w", g.power_w)
            self.push(f"{p}:temp", g.temp)
            self.push(f"{p}:vram", (g.vram_frac or 0) * 100)
            if g.pcie_rx_bps is not None or g.pcie_tx_bps is not None:
                self.push(f"{p}:pcie:in", g.pcie_tx_bps)  # card → host
                self.push(f"{p}:pcie:out", g.pcie_rx_bps)
        if s.ipc is not None:
            # not per process: History never drops a key, and pids churn
            for node in s.ipc.nodes:
                if node.kind in ("container", "vm", "peer", "wan"):
                    self.push(f"ipc:{node.id}:in", node.in_bps)
                    self.push(f"ipc:{node.id}:out", node.out_bps)
            self.push("ipc:k:retrans", s.ipc.kernel.get("Tcp.RetransSegs"))
            self.push("ipc:k:rsts", s.ipc.kernel.get("Tcp.OutRsts"))
        for r in s.sensors:
            if r.kind in ("energy",):
                continue
            key = (r.chip, r.hwmon, r.key)
            lo, hi = self.minmax.get(key, (r.value, r.value))
            self.minmax[key] = (min(lo, r.value), max(hi, r.value))
            if r.kind == "fan":
                self.push(f"fan:{r.chip}:{r.label}", r.value)
