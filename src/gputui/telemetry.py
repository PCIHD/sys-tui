"""GPU telemetry: one immutable snapshot per poll, from NVML or a synthetic demo source.

Every NVML call is individually guarded — an unsupported query yields ``None`` (shown as
N/A), never an exception that takes the app down.
"""

from __future__ import annotations

import math
import os
import random
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from . import displays

THROTTLE_REASONS = {
    0x1: "GPU idle",
    0x2: "Application clocks",
    0x4: "SW power cap",
    0x8: "HW slowdown",
    0x10: "Sync boost",
    0x20: "SW thermal",
    0x40: "HW thermal",
    0x80: "HW power brake",
    0x100: "Display clock",
    0x200: "Board limit",
    0x400: "Reliability (voltage)",
}

GPM_METRICS = [
    "SM_UTIL", "SM_OCCUPANCY", "ANY_TENSOR_UTIL", "DRAM_BW_UTIL", "FP32_UTIL", "FP16_UTIL",
    "FP64_UTIL", "INTEGER_UTIL", "GRAPHICS_UTIL", "NVENC_0_UTIL", "NVENC_1_UTIL", "NVENC_2_UTIL",
    "NVDEC_0_UTIL", "NVDEC_1_UTIL", "NVOFA_0_UTIL", "NVJPG_0_UTIL", "PCIE_TX_PER_SEC",
    "PCIE_RX_PER_SEC",
]


@dataclass
class FanInfo:
    index: int
    speed_pct: int | None = None
    target_pct: int | None = None
    rpm: int | None = None
    policy: str | None = None


@dataclass
class ProcessInfo:
    pid: int
    name: str
    user: str | None
    kind: str  # "C" compute, "G" graphics, "C+G"
    vram_bytes: int | None
    sm_pct: float | None = None
    mem_pct: float | None = None
    enc_pct: float | None = None
    dec_pct: float | None = None


@dataclass
class GpuSnapshot:
    index: int = 0
    ts: float = 0.0
    source: str = "nvml"
    # identity (static)
    name: str | None = None
    uuid: str | None = None
    serial: str | None = None
    vbios: str | None = None
    inforom: str | None = None
    gsp_firmware: str | None = None
    driver: str | None = None
    cuda_driver: str | None = None
    nvml_version: str | None = None
    arch: str | None = None
    compute_cap: str | None = None
    num_cores: int | None = None
    bus_width: int | None = None
    pci_device_id: int | None = None  # 0xDDDD (device only)
    pci_subsys_vendor: int | None = None
    pci_subsys_device: int | None = None
    bus_id: str | None = None
    board_id: int | None = None
    minor: int | None = None
    multi_gpu_board: bool | None = None
    persistence: bool | None = None
    compute_mode: str | None = None
    display_active: bool | None = None
    display_mode: bool | None = None
    power_default_w: float | None = None
    power_min_w: float | None = None
    power_max_w: float | None = None
    max_clk_graphics: int | None = None
    max_clk_sm: int | None = None
    max_clk_mem: int | None = None
    max_clk_video: int | None = None
    temp_shutdown: int | None = None
    temp_slowdown: int | None = None
    temp_gpu_max: int | None = None
    fan_min_pct: int | None = None
    fan_max_pct: int | None = None
    pcie_gen_max_gpu: int | None = None  # what the GPU supports
    pcie_gen_max: int | None = None  # max for this GPU+slot combination
    pcie_width_max: int | None = None
    mem_total: int | None = None
    bar1_total: int | None = None
    gpm_supported: bool = False
    # dynamic
    util_gpu: int | None = None
    util_mem: int | None = None
    util_enc: int | None = None
    util_dec: int | None = None
    enc_sessions: int | None = None
    enc_fps: int | None = None
    mem_used: int | None = None
    mem_free: int | None = None
    mem_reserved: int | None = None
    bar1_used: int | None = None
    clk_graphics: int | None = None
    clk_sm: int | None = None
    clk_mem: int | None = None
    clk_video: int | None = None
    temp_gpu: int | None = None
    temp_mem: float | None = None
    temp_hot: float | None = None
    temp_margin: int | None = None
    die_c: list[float] = field(default_factory=list)
    mem_modules: list[float] = field(default_factory=list)
    volt_nvvdd: float | None = None
    volt_msvdd: float | None = None
    sensor_note: str | None = None
    power_w: float | None = None
    power_instant_w: float | None = None
    power_limit_w: float | None = None
    energy_j: float | None = None
    pstate: int | None = None
    throttle: list[str] = field(default_factory=list)
    fans: list[FanInfo] = field(default_factory=list)
    pcie_gen: int | None = None
    pcie_width: int | None = None
    pcie_tx_kbs: int | None = None
    pcie_rx_kbs: int | None = None
    pcie_replay: int | None = None
    pcie_l0_recovery: int | None = None
    pcie_corr: int | None = None
    pcie_nak: int | None = None
    vf_gpc: int | None = None
    vf_mem: int | None = None
    vf_gpc_range: tuple[int, int] | None = None
    vf_mem_range: tuple[int, int] | None = None
    sample_util: list[float] = field(default_factory=list)
    sample_power: list[float] = field(default_factory=list)
    processes: list[ProcessInfo] = field(default_factory=list)
    gpm: dict[str, float] = field(default_factory=dict)
    displays: list[displays.Connector] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # convenience accessors -------------------------------------------------------------
    @property
    def sm_activity(self) -> float | None:
        """0..1 SM activity, preferring the real GPM counter over aggregate util."""
        if "SM_UTIL" in self.gpm:
            return self.gpm["SM_UTIL"] / 100
        return None if self.util_gpu is None else self.util_gpu / 100

    @property
    def dram_activity(self) -> float | None:
        if "DRAM_BW_UTIL" in self.gpm:
            return self.gpm["DRAM_BW_UTIL"] / 100
        return None if self.util_mem is None else self.util_mem / 100

    @property
    def power_frac(self) -> float | None:
        if self.power_w is None or not self.power_limit_w:
            return None
        return self.power_w / self.power_limit_w

    def module_temp(self, channel: int | None) -> float | None:
        """Temperature of one 32-bit memory channel, when the sensor list lines up with the bus."""
        n = (self.bus_width or 0) // 32
        if channel is None or not self.mem_modules or not n or len(self.mem_modules) != n:
            return None
        if not 0 <= channel < n:
            return None
        return self.mem_modules[channel]


def _nvml_samples(n, h, kind, scale: float = 1.0) -> list[float]:
    if kind is None:
        return []
    got = _safe(n.nvmlDeviceGetSamples, h, kind, 0)
    if not got:
        return []
    _, samples = got
    out = []
    for s in samples:
        try:
            out.append(float(s.sampleValue.uiVal) * scale)
        except Exception:
            continue
    return out


def _safe(fn: Callable, *args, default=None):
    try:
        v = fn(*args)
    except Exception:
        return default
    if isinstance(v, bytes):
        v = v.decode(errors="replace")
    return v


class NvmlSource:
    """Reads GPU state through NVML (nvidia-ml-py)."""

    def __init__(self, index: int = 0):
        import pynvml

        self.n = pynvml
        pynvml.nvmlInit()
        self.count = pynvml.nvmlDeviceGetCount()
        if not 0 <= index < self.count:
            raise ValueError(f"GPU index {index} out of range (found {self.count})")
        self.index = index
        self.h = pynvml.nvmlDeviceGetHandleByIndex(index)
        self._static = self._read_static()
        self._gpm_prev = None
        self._gpm_samples = None
        if self._static.get("gpm_supported"):
            try:
                self._gpm_samples = [pynvml.nvmlGpmSampleAlloc(), pynvml.nvmlGpmSampleAlloc()]
                pynvml.nvmlGpmSampleGet(self.h, self._gpm_samples[0])
                self._gpm_prev = 0
            except Exception:
                self._gpm_samples = None
                self._static["gpm_supported"] = False
        self._psutil_cache: dict[int, tuple[str, str | None]] = {}
        from .rmsensors import RmSensors

        self._rm = RmSensors(self.index)
        self._rm.open()

    # --------------------------------------------------------------------------- static
    def _read_static(self) -> dict[str, Any]:
        n, h = self.n, self.h
        s: dict[str, Any] = {}
        s["name"] = _safe(n.nvmlDeviceGetName, h)
        s["uuid"] = _safe(n.nvmlDeviceGetUUID, h)
        serial = _safe(n.nvmlDeviceGetSerial, h)
        s["serial"] = serial if serial not in (None, "", "0") else None
        s["vbios"] = _safe(n.nvmlDeviceGetVbiosVersion, h)
        s["inforom"] = _safe(n.nvmlDeviceGetInforomImageVersion, h)
        s["gsp_firmware"] = _safe(n.nvmlDeviceGetGspFirmwareVersion, h)
        s["driver"] = _safe(n.nvmlSystemGetDriverVersion)
        cuda = _safe(n.nvmlSystemGetCudaDriverVersion_v2)
        s["cuda_driver"] = f"{cuda // 1000}.{cuda % 1000 // 10}" if cuda else None
        s["nvml_version"] = _safe(n.nvmlSystemGetNVMLVersion)
        arch = _safe(n.nvmlDeviceGetArchitecture, h)
        s["arch"] = {7: "Ampere", 8: "Ada Lovelace", 9: "Hopper", 10: "Blackwell"}.get(arch, None if arch is None else f"arch {arch}")
        cc = _safe(n.nvmlDeviceGetCudaComputeCapability, h)
        s["compute_cap"] = f"{cc[0]}.{cc[1]}" if cc else None
        s["num_cores"] = _safe(n.nvmlDeviceGetNumGpuCores, h)
        s["bus_width"] = _safe(n.nvmlDeviceGetMemoryBusWidth, h)
        pci = _safe(n.nvmlDeviceGetPciInfo_v3, h) or _safe(n.nvmlDeviceGetPciInfo, h)
        if pci is not None:
            s["pci_device_id"] = pci.pciDeviceId >> 16
            s["pci_subsys_vendor"] = pci.pciSubSystemId & 0xFFFF
            s["pci_subsys_device"] = pci.pciSubSystemId >> 16
            bus = pci.busId
            s["bus_id"] = bus.decode() if isinstance(bus, bytes) else bus
        s["board_id"] = _safe(n.nvmlDeviceGetBoardId, h)
        s["minor"] = _safe(n.nvmlDeviceGetMinorNumber, h)
        mgb = _safe(n.nvmlDeviceGetMultiGpuBoard, h)
        s["multi_gpu_board"] = None if mgb is None else bool(mgb)
        dl = _safe(n.nvmlDeviceGetPowerManagementDefaultLimit, h)
        s["power_default_w"] = dl / 1000 if dl else None
        cons = _safe(n.nvmlDeviceGetPowerManagementLimitConstraints, h)
        if cons:
            s["power_min_w"], s["power_max_w"] = cons[0] / 1000, cons[1] / 1000
        for key, clk in (("graphics", n.NVML_CLOCK_GRAPHICS), ("sm", n.NVML_CLOCK_SM),
                         ("mem", n.NVML_CLOCK_MEM), ("video", n.NVML_CLOCK_VIDEO)):
            s[f"max_clk_{key}"] = _safe(n.nvmlDeviceGetMaxClockInfo, h, clk)
        s["temp_shutdown"] = _safe(n.nvmlDeviceGetTemperatureThreshold, h, n.NVML_TEMPERATURE_THRESHOLD_SHUTDOWN)
        s["temp_slowdown"] = _safe(n.nvmlDeviceGetTemperatureThreshold, h, n.NVML_TEMPERATURE_THRESHOLD_SLOWDOWN)
        s["temp_gpu_max"] = _safe(n.nvmlDeviceGetTemperatureThreshold, h, n.NVML_TEMPERATURE_THRESHOLD_GPU_MAX)
        mm = _safe(n.nvmlDeviceGetMinMaxFanSpeed, h)
        if mm:
            s["fan_min_pct"], s["fan_max_pct"] = mm
        s["pcie_gen_max_gpu"] = _safe(n.nvmlDeviceGetGpuMaxPcieLinkGeneration, h)
        s["pcie_gen_max"] = _safe(n.nvmlDeviceGetMaxPcieLinkGeneration, h)
        s["pcie_width_max"] = _safe(n.nvmlDeviceGetMaxPcieLinkWidth, h)
        mem = _safe(n.nvmlDeviceGetMemoryInfo, h)
        s["mem_total"] = mem.total if mem else None
        bar1 = _safe(n.nvmlDeviceGetBAR1MemoryInfo, h)
        s["bar1_total"] = bar1.bar1Total if bar1 else None
        sup = _safe(n.nvmlGpmQueryDeviceSupport, h)
        s["gpm_supported"] = bool(sup and sup.isSupportedDevice)
        s["_num_fans"] = _safe(n.nvmlDeviceGetNumFans, h) or 0
        return s

    # -------------------------------------------------------------------------- dynamic
    def _fan_rpm(self, fan: int) -> int | None:
        n = self.n
        try:
            from ctypes import byref

            info = n.c_nvmlFanSpeedInfo_t()
            info.fan = fan
            info.version = n.nvmlFanSpeedInfo_v1
            fn = n._nvmlGetFunctionPointer("nvmlDeviceGetFanSpeedRPM")
            n._nvmlCheckReturn(fn(self.h, byref(info)))
            return int(info.speed)
        except Exception:
            return None

    def _gpm(self) -> dict[str, float]:
        if not self._gpm_samples:
            return {}
        n = self.n
        cur = 1 - self._gpm_prev
        try:
            n.nvmlGpmSampleGet(self.h, self._gpm_samples[cur])
            mg = n.c_nvmlGpmMetricsGet_t()
            mg.version = n.NVML_GPM_METRICS_GET_VERSION
            mg.sample1 = self._gpm_samples[self._gpm_prev]
            mg.sample2 = self._gpm_samples[cur]
            ids = [m for m in GPM_METRICS if hasattr(n, "NVML_GPM_METRIC_" + m)]
            mg.numMetrics = len(ids)
            for i, m in enumerate(ids):
                mg.metrics[i].metricId = getattr(n, "NVML_GPM_METRIC_" + m)
            n.nvmlGpmMetricsGet(mg)
        except Exception:
            return {}
        finally:
            self._gpm_prev = cur
        out = {}
        for i, m in enumerate(ids):
            if mg.metrics[i].nvmlReturn == 0 and math.isfinite(mg.metrics[i].value):
                out[m] = float(mg.metrics[i].value)
        return out

    def _proc_meta(self, pid: int) -> tuple[str, str | None]:
        if pid in self._psutil_cache:
            return self._psutil_cache[pid]
        try:
            import psutil

            p = psutil.Process(pid)
            meta = (p.name(), p.username())
        except Exception:
            meta = (f"pid {pid}", None)
        self._psutil_cache[pid] = meta
        return meta

    def _processes(self) -> list[ProcessInfo]:
        n, h = self.n, self.h
        found: dict[int, ProcessInfo] = {}
        for kind, fn in (("C", n.nvmlDeviceGetComputeRunningProcesses),
                         ("G", n.nvmlDeviceGetGraphicsRunningProcesses)):
            for p in _safe(fn, h, default=[]) or []:
                used = getattr(p, "usedGpuMemory", None)
                if used is not None and used > 1 << 60:  # NVML_VALUE_NOT_AVAILABLE
                    used = None
                if p.pid in found:
                    found[p.pid].kind = "C+G"
                    if used and not found[p.pid].vram_bytes:
                        found[p.pid].vram_bytes = used
                    continue
                name, user = self._proc_meta(p.pid)
                found[p.pid] = ProcessInfo(p.pid, name, user, kind, used)
        util = _safe(n.nvmlDeviceGetProcessUtilization, h, 0, default=[]) or []
        for u in util:
            pid = getattr(u, "pid", None)
            if pid in found:
                found[pid].sm_pct = getattr(u, "smUtil", None)
                found[pid].mem_pct = getattr(u, "memUtil", None)
                found[pid].enc_pct = getattr(u, "encUtil", None)
                found[pid].dec_pct = getattr(u, "decUtil", None)
        return sorted(found.values(), key=lambda p: -(p.vram_bytes or 0))

    def collect(self) -> GpuSnapshot:
        n, h = self.n, self.h
        s = GpuSnapshot(index=self.index, ts=time.time(), source="nvml",
                        **{k: v for k, v in self._static.items() if not k.startswith("_")})
        util = _safe(n.nvmlDeviceGetUtilizationRates, h)
        if util is not None:
            s.util_gpu, s.util_mem = util.gpu, util.memory
        enc = _safe(n.nvmlDeviceGetEncoderUtilization, h)
        s.util_enc = enc[0] if enc else None
        dec = _safe(n.nvmlDeviceGetDecoderUtilization, h)
        s.util_dec = dec[0] if dec else None
        es = _safe(n.nvmlDeviceGetEncoderStats, h)
        if es:
            s.enc_sessions, s.enc_fps = es[0], es[1]
        mem = _safe(lambda: n.nvmlDeviceGetMemoryInfo(h, version=n.nvmlMemory_v2)) or _safe(n.nvmlDeviceGetMemoryInfo, h)
        if mem is not None:
            s.mem_used, s.mem_free = mem.used, mem.free
            s.mem_reserved = getattr(mem, "reserved", None)
            s.mem_total = mem.total
        bar1 = _safe(n.nvmlDeviceGetBAR1MemoryInfo, h)
        s.bar1_used = bar1.bar1Used if bar1 else None
        s.clk_graphics = _safe(n.nvmlDeviceGetClockInfo, h, n.NVML_CLOCK_GRAPHICS)
        s.clk_sm = _safe(n.nvmlDeviceGetClockInfo, h, n.NVML_CLOCK_SM)
        s.clk_mem = _safe(n.nvmlDeviceGetClockInfo, h, n.NVML_CLOCK_MEM)
        s.clk_video = _safe(n.nvmlDeviceGetClockInfo, h, n.NVML_CLOCK_VIDEO)
        s.temp_gpu = _safe(n.nvmlDeviceGetTemperature, h, n.NVML_TEMPERATURE_GPU)
        s.temp_margin = _safe(n.nvmlDeviceGetMarginTemperature, h)
        fv = _safe(n.nvmlDeviceGetFieldValues, h, [n.NVML_FI_DEV_MEMORY_TEMP, n.NVML_FI_DEV_POWER_INSTANT])
        if fv and fv[0].nvmlReturn == 0:
            s.temp_mem = float(fv[0].value.uiVal)
        if fv and len(fv) > 1 and fv[1].nvmlReturn == 0:
            s.power_instant_w = fv[1].value.uiVal / 1000
        pw = _safe(n.nvmlDeviceGetPowerUsage, h)
        s.power_w = pw / 1000 if pw is not None else None
        rm = self._rm.sample()
        if rm.ok:
            if s.temp_mem is None:
                s.temp_mem = rm.temp_mem
            s.temp_hot = rm.temp_hot
            s.die_c = rm.die_c
            s.mem_modules = rm.mem_modules
            s.volt_nvvdd = rm.volt_nvvdd
            s.volt_msvdd = rm.volt_msvdd
            s.sensor_note = rm.note
        pl = _safe(n.nvmlDeviceGetEnforcedPowerLimit, h)
        s.power_limit_w = pl / 1000 if pl else None
        en = _safe(n.nvmlDeviceGetTotalEnergyConsumption, h)
        s.energy_j = en / 1000 if en is not None else None
        s.pstate = _safe(n.nvmlDeviceGetPerformanceState, h)
        reasons = _safe(n.nvmlDeviceGetCurrentClocksEventReasons, h)
        if reasons is None:
            reasons = _safe(n.nvmlDeviceGetCurrentClocksThrottleReasons, h)
        s.throttle = [txt for bit, txt in THROTTLE_REASONS.items() if reasons and reasons & bit]
        for f in range(self._static.get("_num_fans", 0)):
            pol = _safe(n.nvmlDeviceGetFanControlPolicy_v2, h, f)
            s.fans.append(FanInfo(
                index=f,
                speed_pct=_safe(n.nvmlDeviceGetFanSpeed_v2, h, f),
                target_pct=_safe(n.nvmlDeviceGetTargetFanSpeed, h, f),
                rpm=self._fan_rpm(f),
                policy=None if pol is None else ("auto" if pol == 0 else "manual"),
            ))
        s.pcie_gen = _safe(n.nvmlDeviceGetCurrPcieLinkGeneration, h)
        s.pcie_width = _safe(n.nvmlDeviceGetCurrPcieLinkWidth, h)
        s.pcie_tx_kbs = _safe(n.nvmlDeviceGetPcieThroughput, h, n.NVML_PCIE_UTIL_TX_BYTES)
        s.pcie_rx_kbs = _safe(n.nvmlDeviceGetPcieThroughput, h, n.NVML_PCIE_UTIL_RX_BYTES)
        s.pcie_replay = _safe(n.nvmlDeviceGetPcieReplayCounter, h)
        s.vf_gpc = _safe(n.nvmlDeviceGetGpcClkVfOffset, h)
        s.vf_mem = _safe(n.nvmlDeviceGetMemClkVfOffset, h)
        r = _safe(n.nvmlDeviceGetGpcClkMinMaxVfOffset, h)
        s.vf_gpc_range = None if r is None else (int(r[0]), int(r[1]))
        r = _safe(n.nvmlDeviceGetMemClkMinMaxVfOffset, h)
        s.vf_mem_range = None if r is None else (int(r[0]), int(r[1]))
        ids, names = [], []
        for name in ("NVML_FI_DEV_PCIE_L0_TO_RECOVERY_COUNTER",
                     "NVML_FI_DEV_PCIE_COUNT_CORRECTABLE_ERRORS",
                     "NVML_FI_DEV_PCIE_COUNT_NAKS_RECEIVED"):
            if hasattr(n, name):
                ids.append(getattr(n, name))
                names.append(name)
        if ids:
            fv = _safe(n.nvmlDeviceGetFieldValues, h, ids)
            if fv:
                by = {nm: v.value.ullVal for nm, v in zip(names, fv) if getattr(v, "nvmlReturn", 1) == 0}
                s.pcie_l0_recovery = by.get("NVML_FI_DEV_PCIE_L0_TO_RECOVERY_COUNTER")
                s.pcie_corr = by.get("NVML_FI_DEV_PCIE_COUNT_CORRECTABLE_ERRORS")
                s.pcie_nak = by.get("NVML_FI_DEV_PCIE_COUNT_NAKS_RECEIVED")
        s.sample_util = _nvml_samples(n, h, getattr(n, "NVML_GPU_UTILIZATION_SAMPLES", None))
        s.sample_power = _nvml_samples(n, h, getattr(n, "NVML_TOTAL_POWER_SAMPLES", None), scale=0.001)
        pm = _safe(n.nvmlDeviceGetPersistenceMode, h)
        s.persistence = None if pm is None else bool(pm)
        cm = _safe(n.nvmlDeviceGetComputeMode, h)
        s.compute_mode = {0: "Default", 1: "Exclusive thread", 2: "Prohibited", 3: "Exclusive process"}.get(cm)
        da = _safe(n.nvmlDeviceGetDisplayActive, h)
        s.display_active = None if da is None else bool(da)
        dm = _safe(n.nvmlDeviceGetDisplayMode, h)
        s.display_mode = None if dm is None else bool(dm)
        s.processes = self._processes()
        s.gpm = self._gpm()
        s.displays = displays.read_connectors(s.bus_id)
        return s

    def close(self) -> None:
        try:
            for smp in self._gpm_samples or []:
                self.n.nvmlGpmSampleFree(smp)
            self.n.nvmlShutdown()
        except Exception:
            pass
        rm = getattr(self, "_rm", None)
        if rm is not None:
            rm.close()


class DemoSource:
    """Synthetic, time-varying telemetry for any SKU — for tests, demos and the board browser."""

    def __init__(self, sku_key: str = "5080", subsys_vendor: int = 0x19DA, subsys_device: int = 0x1765,
                 mem_gb: int | None = None, seed: int = 7):
        from .chips import SKU_BY_KEY

        self.sku = SKU_BY_KEY[sku_key]
        self.subsys_vendor = subsys_vendor
        self.subsys_device = subsys_device
        self.mem_gb = mem_gb or self.sku.mem_gb[-1]
        self.count = 1
        self.index = 0
        self.t0 = time.time()
        self.rng = random.Random(seed)
        self._energy = 1.0e6

    def collect(self, t: float | None = None) -> GpuSnapshot:
        sku = self.sku
        now = time.time() if t is None else t
        t = now - self.t0
        load = 0.55 + 0.4 * math.sin(t / 7) * math.sin(t / 3.1 + 1)
        load = max(0.02, min(1.0, load + self.rng.uniform(-0.04, 0.04)))
        memload = max(0.0, min(1.0, 0.8 * load + 0.1 * math.sin(t / 2)))
        total = self.mem_gb * 2**30
        used = int(total * (0.35 + 0.4 * (0.5 + 0.5 * math.sin(t / 23))))
        power = sku.tgp_w * (0.12 + 0.82 * load)
        self._energy += power
        temp = int(38 + 38 * load)
        fan = int(30 + 45 * max(0.0, load - 0.2))
        return GpuSnapshot(
            index=0, ts=now, source="demo",
            name=f"NVIDIA {sku.name}", uuid="GPU-00000000-demo-0000-0000-000000000000",
            vbios="98.00.00.00.00", inforom="G005.0000.98.01", driver="615.71.09", cuda_driver="13.4",
            nvml_version="13.615.71.09", arch="Blackwell", compute_cap="12.0",
            num_cores=sku.cuda_cores, bus_width=sku.bus_width, pci_device_id=sku.device_ids[0],
            pci_subsys_vendor=self.subsys_vendor, pci_subsys_device=self.subsys_device,
            bus_id="00000000:01:00.0", board_id=0x100, minor=0, multi_gpu_board=False,
            persistence=False, compute_mode="Default", display_active=True, display_mode=True,
            power_default_w=sku.tgp_w, power_min_w=sku.tgp_w * 0.7, power_max_w=sku.tgp_w * 1.05,
            max_clk_graphics=3120, max_clk_sm=3120, max_clk_mem=int(sku.mem_gbps * 500), max_clk_video=3090,
            temp_shutdown=93, temp_slowdown=90, temp_gpu_max=88, fan_min_pct=30, fan_max_pct=100,
            pcie_gen_max_gpu=5, pcie_gen_max=5, pcie_width_max=sku.pcie_lanes,
            mem_total=total, bar1_total=total, gpm_supported=True,
            util_gpu=int(load * 100), util_mem=int(memload * 100), util_enc=int(20 * (0.5 + 0.5 * math.sin(t / 5))),
            util_dec=0, enc_sessions=1, enc_fps=60,
            mem_used=used, mem_free=total - used, mem_reserved=512 * 2**20, bar1_used=64 * 2**20,
            clk_graphics=int(900 + 1900 * load), clk_sm=int(900 + 1900 * load),
            clk_mem=int(sku.mem_gbps * 500) if load > 0.1 else 810, clk_video=int(1500 + 1300 * load),
            temp_gpu=temp, temp_mem=float(temp + 9), temp_hot=float(temp + 9), temp_margin=max(0, 88 - temp),
            die_c=[float(temp - 4 + i) for i in range(6)],
            mem_modules=[float(temp + 6 + (i % 4)) for i in range(sku.bus_width // 32)],
            volt_nvvdd=round(0.85 + 0.20 * load, 3), volt_msvdd=0.800,
            power_w=round(power, 1), power_instant_w=round(power * 1.03, 1), power_limit_w=float(sku.tgp_w),
            energy_j=self._energy, pstate=0 if load > 0.5 else 2 if load > 0.1 else 8,
            throttle=["SW power cap"] if load > 0.93 else (["GPU idle"] if load < 0.05 else []),
            fans=[FanInfo(i, fan, fan, int(fan * 33), "auto") for i in range(2)],
            pcie_gen=5 if load > 0.1 else 1, pcie_width=sku.pcie_lanes,
            pcie_tx_kbs=int(400_000 * load), pcie_rx_kbs=int(1_200_000 * memload), pcie_replay=0,
            processes=[
                ProcessInfo(4242, "python3", os.environ.get("USER"), "C", int(used * 0.8)),
                ProcessInfo(1337, "Xwayland", os.environ.get("USER"), "G", 180 * 2**20),
            ],
            gpm={
                "SM_UTIL": load * 92, "SM_OCCUPANCY": load * 40, "ANY_TENSOR_UTIL": load * 55,
                "DRAM_BW_UTIL": memload * 80, "FP32_UTIL": load * 20, "FP16_UTIL": load * 12,
                "INTEGER_UTIL": load * 30, "GRAPHICS_UTIL": load * 100,
                "NVENC_0_UTIL": 20 * (0.5 + 0.5 * math.sin(t / 5)), "NVENC_1_UTIL": 0.0,
                "NVDEC_0_UTIL": 0.0, "NVDEC_1_UTIL": 0.0, "NVOFA_0_UTIL": 0.0,
                "PCIE_TX_PER_SEC": 400 * load, "PCIE_RX_PER_SEC": 1200 * memload,
            },
            displays=[displays.Connector("DP-1", "DP", 1, True, "DELL S2725DS", "2560x1440", True),
                      displays.Connector("DP-2", "DP", 2, False, None, None, False),
                      displays.Connector("DP-3", "DP", 3, False, None, None, False),
                      displays.Connector("HDMI-A-1", "HDMI", 1, False, None, None, False)],
        )

    def close(self) -> None:
        pass
