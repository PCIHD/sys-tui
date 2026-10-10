"""DRAM bandwidth from AMD memory-controller (UMC) perf counters, when the kernel lets us open them.

``amd_umc_N`` PMUs (Zen 4+, Linux 6.7+) count CAS commands per controller; each CAS moves one
64-byte line, which is how perf's ``umc_mem_read_bandwidth`` metric is defined. They are uncore
(system-wide) events, so an unprivileged process needs ``kernel.perf_event_paranoid <= 0`` or
CAP_PERFMON; ``systui-root`` runs it as root. Without that the counters stay closed and ``note`` says why.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import struct

from . import Rates, SysFS

NR_PERF_EVENT_OPEN = {"x86_64": 298}.get(platform.machine())
UMC_MEM_CLK = 0x00  # tools/perf/pmu-events/arch/x86/amdzen4/memory-controller.json
UMC_CAS_CMD = 0x0A
UMC_DATA_SLOT_CLKS = 0x14  # clocks the data bus carried data; a burst fills two per memory clock
RD, WR = 1, 2  # rdwrmask, config bits 8-9
LINE = 64
CHANNEL_BYTES = 8  # each UMC drives one 64-bit channel (two 32-bit DDR5 subchannels)
ATTR_SIZE = 128
READ_FORMAT = 1 | 2  # TOTAL_TIME_ENABLED | TOTAL_TIME_RUNNING: tells us if the counter ever got a slot


def perf_event_open(type_: int, config: int, cpu: int) -> int:
    """fd of a counting (not sampling) event on ``cpu`` for all processes; raises OSError."""
    attr = bytearray(ATTR_SIZE)
    struct.pack_into("IIQ", attr, 0, type_, ATTR_SIZE, config)
    struct.pack_into("Q", attr, 32, READ_FORMAT)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    fd = libc.syscall(NR_PERF_EVENT_OPEN, ctypes.c_char_p(bytes(attr)), -1, cpu, -1, 0)
    if fd < 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    return fd


class UmcBandwidth:
    def __init__(self, fs: SysFS) -> None:
        self.fds: list[tuple[int, int]] = []  # (read fd, write fd) per memory controller
        self.clk_fds: list[int] = []  # memory clock per controller, for the peak; optional
        self.slot_fds: list[int] = []  # data-bus clocks per controller, for how busy it is; optional
        self.note = ""
        pmus = fs.glob("sys/bus/event_source/devices/amd_umc_*")
        if not pmus:
            self.note = "no AMD memory-controller PMU (amd_umc)"
            return
        if NR_PERF_EVENT_OPEN is None:
            self.note = f"perf_event_open not wired up for {platform.machine()}"
            return
        try:
            for pmu in pmus:
                type_ = fs.read_int(pmu / "type")
                cpu = int((fs.read(pmu / "cpumask") or "0").split(",")[0].split("-")[0])
                if type_ is None:
                    raise OSError(errno.ENOENT, f"{pmu.name}/type unreadable")
                rd = perf_event_open(type_, UMC_CAS_CMD | RD << 8, cpu)
                try:
                    wr = perf_event_open(type_, UMC_CAS_CMD | WR << 8, cpu)
                except OSError:
                    os.close(rd)
                    raise
                self.fds.append((rd, wr))
                try:
                    self.clk_fds.append(perf_event_open(type_, UMC_MEM_CLK, cpu))
                except OSError:
                    pass
            if len(self.clk_fds) != len(self.fds):  # a peak from some channels only would be wrong
                self._close(self.clk_fds)
                self.clk_fds = []
            # Opened last, so it only gets a counter if one is left after read/write/clock.
            for pmu in pmus if self.clk_fds else []:
                type_ = fs.read_int(pmu / "type")
                cpu = int((fs.read(pmu / "cpumask") or "0").split(",")[0].split("-")[0])
                try:
                    self.slot_fds.append(perf_event_open(type_, UMC_DATA_SLOT_CLKS, cpu))
                except OSError:
                    self._drop_busy()
                    break
        except OSError as e:
            self.close()
            if e.errno in (errno.EACCES, errno.EPERM):
                self.note = "needs root (./systui-root) or perf_event_paranoid ≤ 0"
            else:
                self.note = f"perf counters unavailable ({e.strerror})"

    @property
    def available(self) -> bool:
        return bool(self.fds)

    def sample(self, rates: Rates, now: float) -> tuple[float | None, float | None, float | None,
                                                        list[tuple[float | None, float | None]], list[float | None]]:
        """(read, write, peak, per-channel (read, write) bytes/s, per-channel busy 0..1).

        Rates are None until two samples exist. Peak is the theoretical DRAM rate at the current
        memory clock: MEMCLK × 2 (DDR) × 8 bytes per channel. Zen's fabric caps real throughput
        well below it (roughly 60-70 %). Busy is perf's umc_data_bus_utilization: data-bus clocks
        ÷ 2 ÷ memory clocks; empty when there was no counter left for it.
        """
        if not self.fds:
            return None, None, None, [], []
        try:
            per = [(_count(r) * LINE, _count(w) * LINE) for r, w in self.fds]
        except OSError:
            return None, None, None, [], []
        try:
            clks = [_count(c) for c in self.clk_fds]
        except OSError:
            clks = []
        hz = rates.rate("umc:clk", sum(clks) if clks else None, now)
        channels = []
        rd = wr = 0
        for i, (r, w) in enumerate(per):
            rb, wb = rates.rate(f"umc:{i}:rd", r, now), rates.rate(f"umc:{i}:wr", w, now)
            channels.append((rb, wb))
            rd += r
            wr += w
        return (rates.rate("umc:rd", rd, now), rates.rate("umc:wr", wr, now),
                hz * 2 * CHANNEL_BYTES if hz else None, channels, self._busy(rates, now, clks))

    def _busy(self, rates: Rates, now: float, clks: list[int]) -> list[float | None]:
        if not self.slot_fds or len(clks) != len(self.slot_fds):
            return []
        try:
            slots = [_count(f, whole=True) for f in self.slot_fds]
        except OSError:  # it had to share a counter: drop it rather than let it time-slice the others
            self._drop_busy()
            return []
        out = []
        for i, (sl, ck) in enumerate(zip(slots, clks)):
            s_hz, c_hz = rates.rate(f"umc:{i}:slot", sl, now), rates.rate(f"umc:{i}:clk", ck, now)
            out.append(min(1.0, s_hz / 2 / c_hz) if s_hz is not None and c_hz else None)
        return out

    def _drop_busy(self) -> None:
        self._close(self.slot_fds)
        self.slot_fds = []

    def close(self) -> None:
        self._close([fd for pair in self.fds for fd in pair] + self.clk_fds + self.slot_fds)
        self.fds, self.clk_fds, self.slot_fds = [], [], []

    @staticmethod
    def _close(fds: list[int]) -> None:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


def _count(fd: int, whole: bool = False) -> int:
    """Counter value; raises OSError if the PMU never scheduled it (no free hardware counter),
    or with ``whole``, if it was ever off its counter (perf time-slicing it with others)."""
    value, enabled, running = struct.unpack("QQQ", os.read(fd, 24))
    if enabled and (running < enabled if whole else not running):
        raise OSError(errno.EBUSY, "counter not scheduled")
    return value
