"""Memory: usage, swap, zram, and per-DIMM temperatures from SPD hub sensors."""

from __future__ import annotations

import re

import psutil

from . import Dimm, MemStat, Rates, SysFS, ZramStat, pmic
from .sensors import Chip, chips_named

DIMM_DRIVERS = ("spd5118", "jc42")
_SPEED = re.compile(r"(\d{4})\s*CL(\d+)")
_MHZ = re.compile(r"(\d+)\s*Mhz", re.IGNORECASE)


def read_memclk(fs: SysFS) -> int | None:
    """Current DRAM clock in MHz from the AMD iGPU ``pp_dpm_mclk`` table.

    The starred line is the active state. DDR5-5600 is a 2800 MHz clock. The discrete
    NVIDIA card has no such file; its driver is skipped.
    """
    for dev in fs.glob("sys/class/drm/card*/device"):
        try:
            if (dev / "driver").readlink().name != "amdgpu":
                continue
        except OSError:
            continue
        text = fs.read(dev / "pp_dpm_mclk")
        if not text:
            continue
        starred = last = None
        for line in text.splitlines():
            m = _MHZ.search(line)
            if not m:
                continue
            last = int(m.group(1))
            if "*" in line:
                starred = last
        if starred or last:
            return starred or last
    return None


def fill_clock(fs: SysFS, mem: MemStat) -> None:
    """Record the running DRAM clock, and the theoretical peak when the UMC counters did not."""
    mem.memclk_mhz = read_memclk(fs)
    if mem.peak_bps is not None or not mem.memclk_mhz:
        return
    channels = len(fs.glob("sys/bus/event_source/devices/amd_umc_*"))
    if channels:
        mem.peak_bps = mem.memclk_mhz * 1_000_000 * 2 * 8 * channels


# JEDEC JEP106 IDs as (continuation bank, code) with the parity bits dropped
VENDORS = {(0, 0x2C): "Micron", (0, 0xCE): "Samsung", (0, 0xAD): "SK hynix", (1, 0x98): "Kingston",
           (2, 0x9E): "Corsair", (3, 0x0B): "Nanya", (4, 0xCB): "ADATA", (4, 0xCD): "G.Skill", (4, 0xEF): "TeamGroup"}
DIE_GBIT = {1: 4, 2: 8, 3: 12, 4: 16, 5: 24, 6: 32, 7: 48, 8: 64}


def _vendor(lo: int, hi: int) -> str:
    if lo == 0 and hi == 0:
        return ""
    return VENDORS.get((lo & 0x7F, hi), f"0x{lo:02X}{hi:02X}")


def _bcd(b: int) -> int:
    return (b >> 4) * 10 + (b & 0x0F)


def _nck(t_ps: int, tck_ps: int) -> int:
    """Clocks for a time, with the SPD spec's rounding (a 0.3 % guard band before rounding up)."""
    return (t_ps * 1000 // tck_ps + 997) // 1000


def decode_spd(data: bytes) -> dict:
    """Identity and organisation from a DDR5 SPD dump (JESD400-5).

    Part/serial live in the manufacturer block; EXPO/XMP are flags. Speed bins in bytes 640+
    are a vendor string some modules (Crucial) carry, not a JEDEC field.
    """
    info: dict = {"part": "", "serial": "", "speed": "", "expo": b"EXPO" in data, "xmp": b"XMP" in data,
                  "maker": "", "dram_maker": "", "ranks": None, "width": None, "die_gbit": None,
                  "timings": "", "made": ""}
    if len(data) < 560:
        return info
    part = data[521:551].decode("ascii", "replace").strip(" \x00")
    info["part"] = re.sub(r"\s+", " ", part).split(" ")[0]
    serial = data[553:569].decode("ascii", "replace").strip(" \x00,")
    info["serial"] = re.sub(r"[^A-Za-z0-9]", "", serial)
    blob = data[640:720].decode("ascii", "replace")
    m = _SPEED.search(blob)
    if m:
        info["speed"] = f"{m.group(1)} CL{m.group(2)}"
    if data[2] != 0x12:  # not DDR5: the rest of the layout differs
        return info
    info["maker"] = _vendor(data[512], data[513])
    info["dram_maker"] = _vendor(data[552], data[553])
    info["die_gbit"] = DIE_GBIT.get(data[4] & 0x1F)
    info["width"] = 4 << ((data[6] >> 5) & 0x07)
    info["ranks"] = ((data[234] >> 3) & 0x07) + 1
    year, week = _bcd(data[515]), _bcd(data[516])
    if 0 < week <= 53:
        info["made"] = f"{2000 + year} wk{week:02d}"
    tck = int.from_bytes(data[20:22], "little")
    if tck:
        taa, trcd, trp, tras = (int.from_bytes(data[o:o + 2], "little") for o in (30, 32, 34, 36))
        cl = _nck(taa, tck)
        cl += cl & 1  # DDR5 CAS latencies are even
        info["timings"] = f"{cl}-{_nck(trcd, tck)}-{_nck(trp, tck)}-{_nck(tras, tck)}"
    return info


def _limit(fs: SysFS, hwmon: str, name: str) -> float | None:
    v = fs.read_int(f"sys/class/hwmon/{hwmon}/{name}")
    return None if v is None else v / 1000


def dimms(chips: list[Chip], fs: SysFS | None = None) -> list[Dimm]:
    out = []
    for c in chips_named(chips, *DIMM_DRIVERS):
        m = re.search(r"\d+-00([0-9a-fA-F]{2})$", c.device)
        if not m:
            continue
        t = c.first("temp")
        d = Dimm(int(m.group(1), 16), None if t is None else t.value, c.hwmon, c.name)
        bus = re.search(r"(\d+)-00[0-9a-fA-F]{2}$", c.device)
        d.bus = int(bus.group(1)) if bus else None
        if fs is not None:
            raw = fs.read_bytes(c.device.lstrip("/") + "/eeprom")
            if raw:
                info = decode_spd(raw)
                for k in ("part", "serial", "speed", "expo", "xmp", "maker", "dram_maker", "ranks", "width",
                          "die_gbit", "timings", "made"):
                    setattr(d, k, info[k])
            d.temp_max, d.temp_crit = _limit(fs, c.hwmon, "temp1_max"), _limit(fs, c.hwmon, "temp1_crit")
            d.vdd, d.vddq, d.vpp = pmic.setpoints(fs, c.device, d.addr)
        out.append(d)
    out.sort(key=lambda d: d.addr)
    return out


def zram(fs: SysFS) -> list[ZramStat]:
    out = []
    for d in fs.glob("sys/block/zram*"):
        mm = (fs.read(d / "mm_stat") or "").split()
        size = fs.read_int(d / "disksize") or 0
        if len(mm) < 3 or not size:
            continue
        algo = fs.read(d / "comp_algorithm") or ""
        m = re.search(r"\[(\w+)\]", algo)
        out.append(ZramStat(d.name, size, int(mm[0]), int(mm[1]), int(mm[2]), m.group(1) if m else algo))
    return out


def collect(fs: SysFS, rates: Rates, chips: list[Chip], now: float) -> MemStat:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    s = MemStat(total=vm.total, available=vm.available, used=vm.used, free=vm.free,
                buffers=getattr(vm, "buffers", 0), cached=getattr(vm, "cached", 0),
                shared=getattr(vm, "shared", 0), swap_total=sw.total, swap_used=sw.used)
    s.swap_in_rate = rates.rate("swin", sw.sin, now)
    s.swap_out_rate = rates.rate("swout", sw.sout, now)
    s.dimms = dimms(chips, fs)
    s.zram = zram(fs)
    return s
