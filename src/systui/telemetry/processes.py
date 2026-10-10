"""Top processes. psutil caches Process objects across ``process_iter`` calls, so cpu_percent
is the share since the previous poll."""

from __future__ import annotations

import re

import psutil

from . import ProcStat, Rates

ATTRS = ["pid", "name", "username", "cpu_percent", "memory_info", "memory_percent", "num_threads",
         "status", "io_counters", "cmdline", "cpu_num"]


def _disks(p) -> list[str]:
    names: list[str] = []
    try:
        files = p.open_files()
    except (psutil.Error, OSError):
        return names
    seen: set[str] = set()
    for f in files or []:
        path = f.path
        if path.startswith("/dev/nvme") or path.startswith("/dev/sd") or path.startswith("/dev/vd"):
            base = path.split("/")[-1]
            base = base.split("p")[0] if base.startswith("nvme") else re.sub(r"\d+$", "", base)
            if base and base not in seen:
                seen.add(base)
                names.append(base)
    return names


class ProcTracker:
    def __init__(self, keep: int = 200) -> None:
        self.keep = keep
        self.rates = Rates()

    def collect(self, now: float) -> list[ProcStat]:
        out = []
        alive = set()
        for p in psutil.process_iter(ATTRS, ad_value=None):
            info = p.info
            pid = info["pid"]
            if pid is None:
                continue
            alive.add(pid)
            mem = info.get("memory_info")
            io = info.get("io_counters")
            io_bps = rd = wr = None
            if io is not None:
                rd = self.rates.rate(f"p{pid}:r", io.read_bytes, now)
                wr = self.rates.rate(f"p{pid}:w", io.write_bytes, now)
                io_bps = None if rd is None or wr is None else rd + wr
            cmdl = info.get("cmdline") or []
            cmd = " ".join(cmdl)[:220] if isinstance(cmdl, (list, tuple)) else ""
            last = info.get("cpu_num")
            if not isinstance(last, int) or last < 0:
                last = None
            cpu = info.get("cpu_percent") or 0.0
            if not isinstance(cpu, (int, float)):
                cpu = 0.0
            rss = mem.rss if mem is not None else 0
            mem_pct = info.get("memory_percent") or 0.0
            if not isinstance(mem_pct, (int, float)):
                mem_pct = 0.0
            disks = _disks(p) if cpu >= 5 or (io_bps or 0) > 1e6 else []
            out.append(ProcStat(pid, info.get("name") or "?", info.get("username") or "",
                                float(cpu), rss, float(mem_pct), info.get("num_threads") or 0,
                                info.get("status") or "", io_bps, cmd, last, disks=disks,
                                io_read_bps=rd, io_write_bps=wr))
        for k in [k for k in self.rates.prev if int(k[1:].split(":")[0]) not in alive]:
            del self.rates.prev[k]
        out.sort(key=lambda s: (s.cpu, s.rss), reverse=True)
        return out[: self.keep]
