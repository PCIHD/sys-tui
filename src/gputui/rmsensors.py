"""Rail voltages, memory junction temperature, and the root-only die and GDDR7 sensors.

The ioctl sequence and the register decode are adapted from Philip Langdale's
nvidia-gpu-sensors (MIT, Copyright (c) 2026 Philip Langdale). The driver
publishes the memory junction temperature and the NVVDD and MSVDD voltages to
any caller. The on-die hot spot and the per-module GDDR7 registers sit behind
the RM register allowlist, so they answer only when the process is root.
"""

from __future__ import annotations

import ctypes
import mmap
import os
import struct
import time
from dataclasses import dataclass, field

NV_IOCTL_MAGIC = ord("F")
NV_ESC_REGISTER_FD = 201
NV_ESC_RM_MAP_MEMORY = 0x4E
NV_ESC_RM_FREE = 0x29
NV_ESC_RM_CONTROL = 0x2A
NV_ESC_RM_ALLOC = 0x2B

NV01_ROOT_CLIENT = 0x00000041
NV01_DEVICE_0 = 0x00000080
NV20_SUBDEVICE_0 = 0x00002080
RM_USER_SHARED_DATA = 0x000000DE

NV0000_CTRL_CMD_GPU_GET_PROBED_IDS = 0x00000214
NV0000_CTRL_CMD_GPU_ATTACH_IDS = 0x00000215
NV0000_CTRL_CMD_GPU_GET_ID_INFO_V2 = 0x00000205
NV00DE_CTRL_CMD_REQUEST_DATA_POLL = 0x00DE0001
NV2080_CTRL_CMD_VOLT_VOLT_RAILS_GET_INFO = 0x2080B201
NV2080_CTRL_CMD_VOLT_VOLT_RAILS_GET_STATUS = 0x2080B202
NV2080_CTRL_CMD_GPU_EXEC_REG_OPS = 0x20800122

POLL_ALL = 0x7F
RUSD_SIZE = 3152
RUSD_TEMPS_OFF = 2312
RUSD_TEMP_STRIDE = 16
RUSD_TEMP_VALUE = 8
SENSOR_MEMORY = 1
NV_GPU_INVALID_ID = 0xFFFFFFFF
NV_MAX_DEVICES = 32

VOLT_RAIL_BASE = 0x20
VOLT_STATUS_MAX = 0xD20
VOLT_UV_MIN = 100_000
VOLT_UV_MAX = 3_000_000
# (status params size, stride, voltage offset). 615.xx first, then the older layout.
VOLT_LAYOUTS = (
    (VOLT_RAIL_BASE + 32 * 0x68, 0x68, 0x04),
    (VOLT_RAIL_BASE + 32 * 100, 100, 0x08),
)

NV_PMC_BOOT_0 = 0x0
NV_THERM_SCAN_FIRST = 0xAD0A80
NV_THERM_SCAN_LAST = 0xAD0AFC
NV_THERM_COUNT = ((NV_THERM_SCAN_LAST - NV_THERM_SCAN_FIRST) // 4) + 1
NV_THERM_TEMP_MAX = 130.0

FBPA_DQR_DQ = 0x009024C0
FBPA_DQR_VLD = 0x009024D0
FBPA_STRIDE = 0x00004000
FBPA_MAX_PARTITIONS = 16
FBPA_DQR_REGS = 4
REGOPS_MAX_OPS = FBPA_MAX_PARTITIONS * (FBPA_DQR_REGS + 1)

GDDR_MRCODE_MIN = 10
GDDR_MRCODE_MAX = 80

ROOT_NOTE = "hot spot and per-module readings need root"


class _NVOS21(ctypes.Structure):
    _fields_ = [
        ("hRoot", ctypes.c_uint32), ("hObjectParent", ctypes.c_uint32), ("hObjectNew", ctypes.c_uint32),
        ("hClass", ctypes.c_uint32), ("pAllocParms", ctypes.c_uint64),
        ("paramsSize", ctypes.c_uint32), ("status", ctypes.c_uint32),
    ]


class _NVOS54(ctypes.Structure):
    _fields_ = [
        ("hClient", ctypes.c_uint32), ("hObject", ctypes.c_uint32),
        ("cmd", ctypes.c_uint32), ("flags", ctypes.c_uint32),
        ("params", ctypes.c_uint64), ("paramsSize", ctypes.c_uint32), ("status", ctypes.c_uint32),
    ]


class _NVOS00(ctypes.Structure):
    _fields_ = [
        ("hRoot", ctypes.c_uint32), ("hObjectParent", ctypes.c_uint32),
        ("hObjectOld", ctypes.c_uint32), ("status", ctypes.c_uint32),
    ]


class _NVOS33(ctypes.Structure):
    _fields_ = [
        ("hClient", ctypes.c_uint32), ("hDevice", ctypes.c_uint32), ("hMemory", ctypes.c_uint32),
        ("_pad0", ctypes.c_uint32),
        ("offset", ctypes.c_uint64), ("length", ctypes.c_uint64), ("pLinearAddress", ctypes.c_uint64),
        ("status", ctypes.c_uint32), ("flags", ctypes.c_uint32),
        ("fd", ctypes.c_int32), ("pad", ctypes.c_int32),
    ]


class _NV0000(ctypes.Structure):
    _fields_ = [
        ("hClient", ctypes.c_uint32), ("processID", ctypes.c_uint32),
        ("processName", ctypes.c_char * 100), ("_pad", ctypes.c_uint32),
        ("pOsPidInfo", ctypes.c_uint64),
    ]


class _NV0080(ctypes.Structure):
    _fields_ = [
        ("deviceId", ctypes.c_uint32),
        ("hClientShare", ctypes.c_uint32), ("hTargetClient", ctypes.c_uint32), ("hTargetDevice", ctypes.c_uint32),
        ("flags", ctypes.c_uint32), ("_pad", ctypes.c_uint32),
        ("vaSpaceSize", ctypes.c_uint64), ("vaStartInternal", ctypes.c_uint64), ("vaLimitInternal", ctypes.c_uint64),
        ("vaMode", ctypes.c_uint32), ("_pad2", ctypes.c_uint32),
    ]


class _NV2080(ctypes.Structure):
    _fields_ = [("subDeviceId", ctypes.c_uint32)]


class _NV00DE(ctypes.Structure):
    _fields_ = [("polledDataMask", ctypes.c_uint64)]


class _IdInfo(ctypes.Structure):
    _fields_ = [
        ("gpuId", ctypes.c_uint32), ("gpuFlags", ctypes.c_uint32),
        ("deviceInstance", ctypes.c_uint32), ("subDeviceInstance", ctypes.c_uint32),
        ("sliStatus", ctypes.c_uint32), ("boardId", ctypes.c_uint32), ("gpuInstance", ctypes.c_uint32),
        ("numaId", ctypes.c_int32),
    ]


class _Probed(ctypes.Structure):
    _fields_ = [
        ("gpuIds", ctypes.c_uint32 * NV_MAX_DEVICES),
        ("excludedGpuIds", ctypes.c_uint32 * NV_MAX_DEVICES),
        ("gpuFlags", ctypes.c_uint32 * NV_MAX_DEVICES),
    ]


class _Attach(ctypes.Structure):
    _fields_ = [("gpuIds", ctypes.c_uint32 * NV_MAX_DEVICES), ("failedId", ctypes.c_uint32)]


class _RegOp(ctypes.Structure):
    _fields_ = [
        ("regOp", ctypes.c_uint8), ("regType", ctypes.c_uint8),
        ("regStatus", ctypes.c_uint8), ("regQuad", ctypes.c_uint8),
        ("regGroupMask", ctypes.c_uint32), ("regSubGroupMask", ctypes.c_uint32), ("regOffset", ctypes.c_uint32),
        ("regValueHi", ctypes.c_uint32), ("regValueLo", ctypes.c_uint32),
        ("regAndNMaskHi", ctypes.c_uint32), ("regAndNMaskLo", ctypes.c_uint32),
    ]


class _GrRoute(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint32), ("_pad", ctypes.c_uint32), ("route", ctypes.c_uint64)]


class _ExecRegOps(ctypes.Structure):
    _fields_ = [
        ("hClientTarget", ctypes.c_uint32), ("hChannelTarget", ctypes.c_uint32),
        ("bNonTransactional", ctypes.c_uint32), ("reserved0", ctypes.c_uint32), ("reserved1", ctypes.c_uint32),
        ("regOpCount", ctypes.c_uint32), ("regOps", ctypes.c_uint64), ("grRouteInfo", _GrRoute),
    ]


class _RegFd(ctypes.Structure):
    _fields_ = [("ctl_fd", ctypes.c_int)]


assert ctypes.sizeof(_NVOS21) == 32
assert ctypes.sizeof(_NVOS54) == 32
assert ctypes.sizeof(_NVOS33) == 56
assert ctypes.sizeof(_NV0000) == 120
assert ctypes.sizeof(_NV0080) == 56
assert ctypes.sizeof(_RegOp) == 32
assert ctypes.sizeof(_ExecRegOps) == 48

_libc = ctypes.CDLL(None, use_errno=True)
_libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
_libc.ioctl.restype = ctypes.c_int


def _ioc(nr: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (NV_IOCTL_MAGIC << 8) | nr


def _ioctl(fd: int, nr: int, arg: ctypes.Structure) -> None:
    rc = _libc.ioctl(fd, _ioc(nr, ctypes.sizeof(arg)), ctypes.byref(arg))
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def therm_c(word: int) -> float | None:
    """Decode one NV_THERM word. Valid readings are tagged 0x4000 and scaled by 1/256."""
    if (word >> 16) != 0x4000:
        return None
    if ((word >> 8) & 0xFF) in (0, 0xFF):
        return None
    t = (word & 0xFFFF) / 256.0
    if t > NV_THERM_TEMP_MAX:
        return None
    return t


def gddr_c(dq: int, vld: int, slot: int) -> float | None:
    """One GDDR7 DQ slot. The mode-register code steps by 2 °C, with code 20 at 0 °C."""
    if not ((vld >> (24 + slot)) & 1):
        return None
    if (dq & 0xFFFF0000) == 0xBADF0000:
        return None
    code = (dq >> 16) & 0xFF
    if not GDDR_MRCODE_MIN <= code <= GDDR_MRCODE_MAX:
        return None
    return float((code - 20) * 2)


def collapse_modules(slots: list[tuple[int, int, float]]) -> list[float]:
    """Controller order, one temperature per 32-bit channel.

    Each partition has four DQ slots. Slot 0 and slot 2 are the two IC readbacks
    of subpartition 0; slot 1 and slot 3 are subpartition 1. When the pair agrees
    they are the same chip reported twice, and one reading is kept.
    """
    by = {(fbpa, slot): temp for fbpa, slot, temp in slots}
    out: list[float] = []
    for fbpa in sorted({fbpa for fbpa, _, _ in slots}):
        for sub in (0, 1):
            a = by.get((fbpa, sub))
            b = by.get((fbpa, 2 + sub))
            if a is None and b is None:
                continue
            if a is not None and b is not None and a == b:
                out.append(a)
            else:
                if a is not None:
                    out.append(a)
                if b is not None:
                    out.append(b)
    return out


@dataclass
class SensorSample:
    temp_mem: float | None = None
    temp_hot: float | None = None
    die_c: list[float] = field(default_factory=list)
    mem_modules: list[float] = field(default_factory=list)
    volt_nvvdd: float | None = None
    volt_msvdd: float | None = None
    note: str | None = None
    ok: bool = False


def _rusd_temp(buf: mmap.mmap, sensor: int) -> float | None:
    base = RUSD_TEMPS_OFF + sensor * RUSD_TEMP_STRIDE
    ts = 0
    raw = 0
    for _ in range(200):
        ts = struct.unpack_from("<Q", buf, base)[0]
        if ts == 0xFFFFFFFFFFFFFFFF:
            time.sleep(0.001)
            continue
        raw = struct.unpack_from("<i", buf, base + RUSD_TEMP_VALUE)[0]
        if struct.unpack_from("<Q", buf, base)[0] == ts:
            break
    else:
        return None
    if ts == 0:
        return None
    return raw / 256.0


class RmSensors:
    """One RM client for a single GPU. ``sample`` never raises."""

    def __init__(self, index: int = 0):
        self.index = index
        self._ctl = -1
        self._dev = -1
        self._mapfd = -1
        self._rusd: mmap.mmap | None = None
        self._client = 0
        self._sub = 0
        self._rail_mask = 0
        self._volt: tuple[int, int, int] | None = None
        self._raw = False
        self._note: str | None = None
        self._last = SensorSample()
        self.ok = False

    def open(self) -> None:
        try:
            self._open()
        except Exception:
            self.close()
            self.ok = False

    def _open(self) -> None:
        self._ctl = os.open("/dev/nvidiactl", os.O_RDWR | os.O_CLOEXEC)
        client = self._rm_alloc(0, 0, 0xCB000001, NV01_ROOT_CLIENT,
                                _NV0000(hClient=0xCB000001, processID=os.getpid(), processName=b"gputui"))
        self._client = client

        probed = _Probed()
        self._rm_control(client, client, NV0000_CTRL_CMD_GPU_GET_PROBED_IDS, probed)
        ids = [int(probed.gpuIds[i]) for i in range(NV_MAX_DEVICES) if probed.gpuIds[i] != NV_GPU_INVALID_ID]
        if self.index >= len(ids):
            raise RuntimeError(f"GPU index {self.index} not in the RM probe list")
        att = _Attach()
        for i in range(NV_MAX_DEVICES):
            att.gpuIds[i] = NV_GPU_INVALID_ID
        for i, gid in enumerate(ids):
            att.gpuIds[i] = gid
        self._rm_control(client, client, NV0000_CTRL_CMD_GPU_ATTACH_IDS, att)

        info = _IdInfo(gpuId=ids[self.index])
        self._rm_control(client, client, NV0000_CTRL_CMD_GPU_GET_ID_INFO_V2, info)
        self._dev = os.open(f"/dev/nvidia{info.deviceInstance}", os.O_RDWR | os.O_CLOEXEC)
        reg = _RegFd(ctl_fd=self._ctl)
        try:
            _ioctl(self._dev, NV_ESC_REGISTER_FD, reg)
        except OSError:
            pass

        device = self._rm_alloc(client, client, 0xCB000002 + self.index * 0x10, NV01_DEVICE_0,
                                _NV0080(deviceId=info.deviceInstance))
        self._sub = self._rm_alloc(client, device, 0xCB000003 + self.index * 0x10, NV20_SUBDEVICE_0,
                                   _NV2080(subDeviceId=info.subDeviceInstance))

        rusd_h = 0
        try:
            rusd_h = self._rm_alloc(client, self._sub, 0xCB000004 + self.index * 0x10, RM_USER_SHARED_DATA,
                                    _NV00DE(polledDataMask=POLL_ALL))
        except OSError:
            rusd_h = 0
        if rusd_h:
            self._mapfd = os.open("/dev/nvidiactl", os.O_RDWR | os.O_CLOEXEC)
            m = _NVOS33(hClient=client, hDevice=device, hMemory=rusd_h, length=RUSD_SIZE, fd=self._mapfd)
            self._rm_control_raw(NV_ESC_RM_MAP_MEMORY, m)
            if m.status != 0:
                raise OSError(f"RM_MAP_MEMORY status {m.status:#x}")
            self._rusd = mmap.mmap(self._mapfd, RUSD_SIZE, prot=mmap.PROT_READ)
            poll = (ctypes.c_uint32 * 2)(POLL_ALL, 0)
            try:
                self._rm_control(client, rusd_h, NV00DE_CTRL_CMD_REQUEST_DATA_POLL, poll)
            except OSError:
                pass

        info_buf = (ctypes.c_uint8 * 2444)()
        if self._control_buf(NV2080_CTRL_CMD_VOLT_VOLT_RAILS_GET_INFO, info_buf) == 0:
            self._rail_mask = struct.unpack_from("<I", bytes(info_buf), 4)[0]
        self._volt = self._probe_volt()
        self._raw = os.geteuid() == 0
        self._note = None if self._raw else ROOT_NOTE
        self.ok = self._rusd is not None or self._volt is not None

    def _probe_volt(self) -> tuple[int, int, int] | None:
        if not self._rail_mask:
            return None
        rail0 = (self._rail_mask & -self._rail_mask).bit_length() - 1
        for size, stride, off in VOLT_LAYOUTS:
            buf = (ctypes.c_uint8 * size)()
            struct.pack_into("<I", buf, 4, self._rail_mask)
            if self._control_buf(NV2080_CTRL_CMD_VOLT_VOLT_RAILS_GET_STATUS, buf) != 0:
                continue
            uv = struct.unpack_from("<I", bytes(buf), VOLT_RAIL_BASE + rail0 * stride + off)[0]
            if VOLT_UV_MIN <= uv <= VOLT_UV_MAX:
                return (size, stride, off)
        return None

    def sample(self) -> SensorSample:
        if not self.ok:
            return self._last
        try:
            got = self._sample()
        except Exception:
            return self._last
        self._last = got
        return got

    def _sample(self) -> SensorSample:
        s = SensorSample(note=self._note, ok=True)
        if self._rusd is not None:
            s.temp_mem = _rusd_temp(self._rusd, SENSOR_MEMORY)
        if self._volt and self._rail_mask:
            size, stride, off = self._volt
            buf = (ctypes.c_uint8 * size)()
            struct.pack_into("<I", buf, 4, self._rail_mask)
            if self._control_buf(NV2080_CTRL_CMD_VOLT_VOLT_RAILS_GET_STATUS, buf) == 0:
                raw = bytes(buf)
                if self._rail_mask & 1:
                    s.volt_nvvdd = struct.unpack_from("<I", raw, VOLT_RAIL_BASE + off)[0] / 1e6
                if self._rail_mask & 2:
                    s.volt_msvdd = struct.unpack_from("<I", raw, VOLT_RAIL_BASE + stride + off)[0] / 1e6
        if self._raw:
            self._read_raw(s)
        return s

    def _read_raw(self, s: SensorSample) -> None:
        boot = self._regops([NV_PMC_BOOT_0])
        if boot is None:
            s.note = "register read failed"
            return
        chip = boot[0] >> 20
        if not 0x1B0 <= chip <= 0x1BF:
            s.note = f"chip 0x{chip:03X} has no Blackwell sensor window"
            return
        therm_offs = [NV_THERM_SCAN_FIRST + i * 4 for i in range(NV_THERM_COUNT)]
        therm = self._regops(therm_offs)
        if therm:
            s.die_c = [t for w in therm if (t := therm_c(w)) is not None]
            if s.die_c:
                s.temp_hot = max(s.die_c)
        offs: list[int] = []
        for fbpa in range(FBPA_MAX_PARTITIONS):
            for slot in range(FBPA_DQR_REGS):
                offs.append(FBPA_DQR_DQ + fbpa * FBPA_STRIDE + slot * 4)
            offs.append(FBPA_DQR_VLD + fbpa * FBPA_STRIDE)
        vals = self._regops(offs)
        if not vals:
            return
        slots: list[tuple[int, int, float]] = []
        n = 0
        for fbpa in range(FBPA_MAX_PARTITIONS):
            dqs = vals[n:n + FBPA_DQR_REGS]
            n += FBPA_DQR_REGS
            vld = vals[n]
            n += 1
            for slot, dq in enumerate(dqs):
                t = gddr_c(dq, vld, slot)
                if t is not None:
                    slots.append((fbpa, slot, t))
        s.mem_modules = collapse_modules(slots)
        if s.die_c or s.mem_modules:
            s.note = None

    def _regops(self, offs: list[int]) -> list[int] | None:
        if not offs or len(offs) > REGOPS_MAX_OPS:
            return None
        ops = (_RegOp * len(offs))()
        for i, off in enumerate(offs):
            ops[i].regOffset = off
        params = _ExecRegOps(bNonTransactional=1, regOpCount=len(offs), regOps=ctypes.addressof(ops))
        try:
            self._rm_control(self._client, self._sub, NV2080_CTRL_CMD_GPU_EXEC_REG_OPS, params)
        except OSError:
            return None
        return [int(ops[i].regValueLo) for i in range(len(offs))]

    def close(self) -> None:
        if self._rusd is not None:
            self._rusd.close()
            self._rusd = None
        if self._client:
            free = _NVOS00(hRoot=self._client, hObjectOld=self._client)
            try:
                _ioctl(self._ctl, NV_ESC_RM_FREE, free)
            except OSError:
                pass
            self._client = 0
        for fd in (self._mapfd, self._dev, self._ctl):
            if fd >= 0:
                os.close(fd)
        self._mapfd = self._dev = self._ctl = -1
        self.ok = False

    def _rm_alloc(self, root: int, parent: int, new: int, cls: int, params: ctypes.Structure) -> int:
        call = _NVOS21(hRoot=root, hObjectParent=parent, hObjectNew=new, hClass=cls,
                       pAllocParms=ctypes.addressof(params), paramsSize=ctypes.sizeof(params))
        _ioctl(self._ctl, NV_ESC_RM_ALLOC, call)
        if call.status != 0:
            raise OSError(f"RM_ALLOC class {cls:#x} status {call.status:#x}")
        return int(call.hObjectNew)

    def _rm_control(self, client: int, obj: int, cmd: int, params: ctypes.Structure) -> None:
        call = _NVOS54(hClient=client, hObject=obj, cmd=cmd,
                       params=ctypes.addressof(params), paramsSize=ctypes.sizeof(params))
        _ioctl(self._ctl, NV_ESC_RM_CONTROL, call)
        if call.status != 0:
            raise OSError(f"RM_CONTROL {cmd:#x} status {call.status:#x}")

    def _rm_control_raw(self, nr: int, params: ctypes.Structure) -> None:
        _ioctl(self._ctl, nr, params)

    def _control_buf(self, cmd: int, buf: ctypes.Array) -> int:
        call = _NVOS54(hClient=self._client, hObject=self._sub, cmd=cmd,
                       params=ctypes.addressof(buf), paramsSize=ctypes.sizeof(buf))
        try:
            _ioctl(self._ctl, NV_ESC_RM_CONTROL, call)
        except OSError:
            return -1
        return int(call.status)
