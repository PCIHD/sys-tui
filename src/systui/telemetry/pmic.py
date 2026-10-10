"""DDR5 module PMICs over the SMBus: rail setpoints (read-only) and, opt-in, live rail current.

Each DDR5 DIMM carries its own PMIC at 0x48 | (SPD address & 7) on the same bus as its SPD hub.
Its buck setpoints are 7-bit VID codes in bits 7:1 of R21 (SWA, VDD), R25 (SWB, VDDQ) and
R27 (SWC, VPP), 5 mV a step from 0.8 V (VDD/VDDQ) or 1.5 V (VPP) — JESD301 PMIC5100.

These are what the PMIC was told to output (what EXPO/XMP or the BIOS set), not a measurement.
Reads use I2C_SMBUS byte-data transfers on /dev/i2c-N, which needs read/write access to the
device node (root, or an ACL as OpenRGB installs).

Live current (``Meter``, opt-in with ``--dimm-power``) needs the PMIC's ADC switched on, which
is the one register write this module makes: R30 (ADC enable / select / averaging window). R30
sits outside the range secure mode write-protects (0x15-0x2F, 0x32) and controls measurement
only. The meter is left reporting current (R1B[6] = 0, its power-on default) because changing
that bit is a write into the protected configuration range; watts are current × rail setpoint.
R30 is put back as found on exit. Register map: JESD301 PMIC5100, as in Fitipower's FP9968.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import re

from . import SysFS

I2C_SLAVE = 0x0703  # not I2C_SLAVE_FORCE: never talk over a kernel driver bound to the address
I2C_SMBUS = 0x0720
I2C_SMBUS_READ = 1
I2C_SMBUS_BYTE_DATA = 2
RAILS = ((0x21, 0.8), (0x25, 0.8), (0x27, 1.5))  # (register, base volts): VDD, VDDQ, VPP
I2C_SMBUS_WRITE = 0
R_ADC = 0x30
ADC_ON = 0x80 | 0b11  # ADC enabled, SWA selected, 8 ms averaging window for the current meter
METER = ((0x0C, 0xFF), (0x0E, 0x3F), (0x0F, 0x3F))  # SWA/SWB/SWC output current, 125 mA a step
AMPS_PER_STEP = 0.125

_cache: dict[tuple[str, int], tuple[float | None, float | None, float | None]] = {}


class _Data(ctypes.Union):
    _fields_ = [("byte", ctypes.c_uint8), ("word", ctypes.c_uint16), ("block", ctypes.c_uint8 * 34)]


class _Args(ctypes.Structure):
    _fields_ = [("read_write", ctypes.c_uint8), ("command", ctypes.c_uint8), ("size", ctypes.c_uint32),
                ("data", ctypes.POINTER(_Data))]


def _read_byte(fd: int, reg: int) -> int:
    data = _Data()
    fcntl.ioctl(fd, I2C_SMBUS, _Args(I2C_SMBUS_READ, reg, I2C_SMBUS_BYTE_DATA, ctypes.pointer(data)))
    return data.byte


def _write_adc(fd: int, value: int) -> None:
    """The only write this module makes, and only ever to R30 (measurement control)."""
    data = _Data()
    data.byte = value
    fcntl.ioctl(fd, I2C_SMBUS, _Args(I2C_SMBUS_WRITE, R_ADC, I2C_SMBUS_BYTE_DATA, ctypes.pointer(data)))


def setpoints(fs: SysFS, spd_device: str, spd_addr: int) -> tuple[float | None, float | None, float | None]:
    """(VDD, VDDQ, VPP) volts for the module whose SPD hub is ``spd_device``; Nones if unreadable.

    Read once per module and cached: setpoints only change across a reboot.
    """
    m = re.search(r"(\d+)-00[0-9a-fA-F]{2}$", spd_device)
    if not m:
        return None, None, None
    node = fs.p(f"dev/i2c-{m.group(1)}")
    key = (str(node), spd_addr)
    if key not in _cache:
        _cache[key] = _read(str(node), 0x48 | (spd_addr & 0x07))
    return _cache[key]


def _read(node: str, addr: int) -> tuple[float | None, float | None, float | None]:
    try:
        fd = os.open(node, os.O_RDWR)
    except OSError:
        return None, None, None
    try:
        fcntl.ioctl(fd, I2C_SLAVE, addr)
        out = []
        for reg, base in RAILS:
            vid = _read_byte(fd, reg) >> 1
            out.append(round(base + vid * 0.005, 3) if vid else None)
        return out[0], out[1], out[2]
    except OSError:
        return None, None, None
    finally:
        os.close(fd)


class Meter:
    """Live VDD/VDDQ/VPP watts per module from each PMIC's current meter."""

    def __init__(self, fs: SysFS) -> None:
        self.fs = fs
        self.restore: dict[tuple[str, int], int | None] = {}  # R30 as found; None = we left it alone
        self.dead: set[tuple[str, int]] = set()  # meter would not start: no readings, no retries
        self.note = ""

    def fill(self, dimms) -> None:
        for d in dimms:
            # No setpoints means the PMIC didn't answer reads: never write to it then.
            if d.bus is None or d.vdd is None or d.vddq is None or d.vpp is None:
                continue
            node, addr = str(self.fs.p(f"dev/i2c-{d.bus}")), 0x48 | (d.addr & 0x07)
            if (node, addr) in self.dead:
                continue
            try:
                fd = os.open(node, os.O_RDWR)
            except OSError as e:
                self.note = f"no access to {node} ({e.strerror})"
                continue
            try:
                fcntl.ioctl(fd, I2C_SLAVE, addr)
                if (node, addr) not in self.restore:
                    try:
                        self._start(fd, node, addr)
                    except OSError:
                        self.dead.add((node, addr))
                        raise
                amps = [(_read_byte(fd, reg) & mask) * AMPS_PER_STEP for reg, mask in METER]
                d.rail_w = (amps[0] * d.vdd, amps[1] * d.vddq, amps[2] * d.vpp)
            except OSError as e:
                self.note = f"PMIC 0x{addr:02x}: {e.strerror}"
            finally:
                os.close(fd)

    def _start(self, fd: int, node: str, addr: int) -> None:
        was = _read_byte(fd, R_ADC)
        if was & 0x80:  # someone else already runs the ADC: use it as is, and leave it on
            self.restore[(node, addr)] = None
            return
        _write_adc(fd, ADC_ON)
        self.restore[(node, addr)] = was
        if _read_byte(fd, R_ADC) != ADC_ON:
            raise OSError(0, "ADC did not switch on")

    def close(self) -> None:
        for (node, addr), was in self.restore.items():
            if was is None:
                continue
            try:
                fd = os.open(node, os.O_RDWR)
            except OSError:
                continue
            try:
                fcntl.ioctl(fd, I2C_SLAVE, addr)
                _write_adc(fd, was)
            except OSError:
                pass
            finally:
                os.close(fd)
        self.restore = {}
