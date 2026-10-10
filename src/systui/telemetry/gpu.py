"""GPUs: NVIDIA through NVML (optional ``gpu`` extra), AMD through amdgpu sysfs/hwmon."""

from __future__ import annotations

from . import GpuProc, GpuStat, SysFS
from .sensors import Chip, chips_named


def _try(fn, *a, default=None):
    try:
        return fn(*a)
    except Exception:
        return default


class Nvml:
    def __init__(self) -> None:
        self.nv = None
        self.handles = []
        self.driver = ""
        try:
            import pynvml as nv

            nv.nvmlInit()
            self.nv = nv
            self.handles = [nv.nvmlDeviceGetHandleByIndex(i) for i in range(nv.nvmlDeviceGetCount())]
            self.driver = _try(nv.nvmlSystemGetDriverVersion, default="") or ""
            self.names = [_try(nv.nvmlDeviceGetName, h, default="NVIDIA GPU") for h in self.handles]
            self.pci = [_try(lambda h: nv.nvmlDeviceGetPciInfo(h).busId, h, default="") for h in self.handles]
        except Exception:
            self.nv = None
            self.handles = []

    @property
    def available(self) -> bool:
        return self.nv is not None

    def collect(self) -> list[GpuStat]:
        nv = self.nv
        if nv is None:
            return []
        out = []
        for i, h in enumerate(self.handles):
            g = GpuStat(i, self.names[i], "nvidia", driver=self.driver)
            u = _try(nv.nvmlDeviceGetUtilizationRates, h)
            if u is not None:
                g.util, g.mem_util = float(u.gpu), float(u.memory)
            p = _try(nv.nvmlDeviceGetPowerUsage, h)
            g.power_w = None if p is None else p / 1000
            lim = _try(nv.nvmlDeviceGetEnforcedPowerLimit, h)
            g.power_limit_w = None if lim is None else lim / 1000
            g.temp = _try(nv.nvmlDeviceGetTemperature, h, nv.NVML_TEMPERATURE_GPU)
            m = _try(nv.nvmlDeviceGetMemoryInfo, h)
            if m is not None:
                g.vram_used, g.vram_total = int(m.used), int(m.total)
            g.fan_pct = _try(nv.nvmlDeviceGetFanSpeed, h)
            g.fan_rpm = _try(lambda h: nv.nvmlDeviceGetFanSpeedRPM(h).speed, h)
            g.clk_mhz = _try(nv.nvmlDeviceGetClockInfo, h, nv.NVML_CLOCK_GRAPHICS)
            g.clk_mem_mhz = _try(nv.nvmlDeviceGetClockInfo, h, nv.NVML_CLOCK_MEM)
            g.pci = (self.pci[i] or "").lower()
            if g.pci.startswith("0000") and len(g.pci) > 12:
                g.pci = g.pci[4:]  # NVML uses an 8-digit domain
            g.pcie_gen = _try(nv.nvmlDeviceGetCurrPcieLinkGeneration, h)
            g.pcie_width = _try(nv.nvmlDeviceGetCurrPcieLinkWidth, h)
            g.pcie_max_gen = _try(nv.nvmlDeviceGetMaxPcieLinkGeneration, h)
            g.pcie_max_width = _try(nv.nvmlDeviceGetMaxPcieLinkWidth, h)
            rx = _try(nv.nvmlDeviceGetPcieThroughput, h, nv.NVML_PCIE_UTIL_RX_BYTES)
            tx = _try(nv.nvmlDeviceGetPcieThroughput, h, nv.NVML_PCIE_UTIL_TX_BYTES)
            g.pcie_rx_bps = None if rx is None else rx * 1024.0  # NVML reports KB/s over a 20 ms window
            g.pcie_tx_bps = None if tx is None else tx * 1024.0
            g.vf_gpc = _try(nv.nvmlDeviceGetGpcClkVfOffset, h)
            g.vf_mem = _try(nv.nvmlDeviceGetMemClkVfOffset, h)
            r = _try(nv.nvmlDeviceGetGpcClkMinMaxVfOffset, h)
            g.vf_gpc_range = None if r is None else (int(r[0]), int(r[1]))
            r = _try(nv.nvmlDeviceGetMemClkMinMaxVfOffset, h)
            g.vf_mem_range = None if r is None else (int(r[0]), int(r[1]))
            g.processes = _gpu_procs(nv, h)
            ids, names = [], []
            for name in ("NVML_FI_DEV_PCIE_L0_TO_RECOVERY_COUNTER",
                         "NVML_FI_DEV_PCIE_COUNT_CORRECTABLE_ERRORS",
                         "NVML_FI_DEV_PCIE_COUNT_NAKS_RECEIVED"):
                if hasattr(nv, name):
                    ids.append(getattr(nv, name))
                    names.append(name)
            if ids:
                fv = _try(nv.nvmlDeviceGetFieldValues, h, ids)
                if fv:
                    by = {n: v.value.ullVal for n, v in zip(names, fv) if getattr(v, "nvmlReturn", 1) == 0}
                    g.pcie_l0_recovery = by.get("NVML_FI_DEV_PCIE_L0_TO_RECOVERY_COUNTER")
                    g.pcie_corr = by.get("NVML_FI_DEV_PCIE_COUNT_CORRECTABLE_ERRORS")
                    g.pcie_nak = by.get("NVML_FI_DEV_PCIE_COUNT_NAKS_RECEIVED")
            out.append(g)
        return out

    def close(self) -> None:
        if self.nv is not None:
            _try(self.nv.nvmlShutdown)
            self.nv = None


def _gpu_procs(nv, h) -> list[GpuProc]:
    found: dict[int, GpuProc] = {}
    for kind, fn in (("C", nv.nvmlDeviceGetComputeRunningProcesses),
                     ("G", nv.nvmlDeviceGetGraphicsRunningProcesses)):
        for p in _try(fn, h, default=[]) or []:
            used = getattr(p, "usedGpuMemory", None)
            if used is not None and used > 1 << 60:
                used = None
            if p.pid in found:
                found[p.pid].kind = "C+G"
                if used and not found[p.pid].vram_bytes:
                    found[p.pid].vram_bytes = used
                continue
            name = f"pid {p.pid}"
            try:
                import psutil
                name = psutil.Process(p.pid).name()
            except Exception:
                pass
            found[p.pid] = GpuProc(p.pid, name, kind, used)
    return sorted(found.values(), key=lambda p: -(p.vram_bytes or 0))


def amd(fs: SysFS, chips: list[Chip]) -> list[GpuStat]:
    out = []
    for i, c in enumerate(chips_named(chips, "amdgpu")):
        dev = fs.p(c.device)
        g = GpuStat(100 + i, "AMD Radeon (iGPU)", "amd", driver="amdgpu")
        g.pci = c.device.rsplit("/", 1)[-1]
        busy = fs.read_int(dev / "gpu_busy_percent")
        g.util = None if busy is None else float(busy)
        for r in c.readings:
            if r.kind == "temp" and g.temp is None:
                g.temp = r.value
            elif r.kind == "power" and g.power_w is None:
                g.power_w = r.value
            elif r.kind == "freq" and r.label == "sclk":
                g.clk_mhz = r.value
            elif r.kind == "in" and r.label == "vddgfx":
                g.volt = r.value
        g.vram_used = fs.read_int(dev / "mem_info_vram_used")
        g.vram_total = fs.read_int(dev / "mem_info_vram_total")
        out.append(g)
    return out
