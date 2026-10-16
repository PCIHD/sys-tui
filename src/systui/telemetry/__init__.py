"""System telemetry: one ``SystemSnapshot`` per poll, built from psutil, sysfs/hwmon and NVML.

Every collector reads sysfs through a ``SysFS`` rooted at ``/`` (tests point it at a fake
tree). Counters (energy, disk/net bytes, context switches) become rates through ``Rates``,
which remembers the previous sample per key.
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


class SysFS:
    def __init__(self, root: str | Path = "/") -> None:
        self.root = Path(root)

    def p(self, rel: str) -> Path:
        return self.root / rel.lstrip("/")

    def read(self, rel: str | Path) -> str | None:
        path = rel if isinstance(rel, Path) else self.p(rel)
        try:
            return path.read_text().strip()
        except (OSError, UnicodeDecodeError):
            return None

    def read_bytes(self, rel: str | Path) -> bytes | None:
        path = rel if isinstance(rel, Path) else self.p(rel)
        try:
            return path.read_bytes()
        except OSError:
            return None

    def read_int(self, rel: str | Path) -> int | None:
        s = self.read(rel)
        try:
            return int(s) if s is not None else None
        except ValueError:
            return None

    def glob(self, pattern: str) -> list[Path]:
        return sorted(self.root.glob(pattern.lstrip("/")), key=_natural)

    def real(self, rel: str | Path) -> str:
        """Resolved path with the fake root stripped, so it looks like a real /sys path."""
        path = rel if isinstance(rel, Path) else self.p(rel)
        try:
            r = str(path.resolve())
        except OSError:
            return ""
        root = str(self.root.resolve())
        return r[len(root):] if root != "/" and r.startswith(root) else r


def _natural(p: Path):
    import re

    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(p))]


class Rates:
    """Per-key counter → rate (units per second). Counter resets and wraps yield None."""

    def __init__(self) -> None:
        self.prev: dict[str, tuple[float, float]] = {}

    def rate(self, key: str, value: float | None, now: float) -> float | None:
        if value is None:
            return None
        last = self.prev.get(key)
        self.prev[key] = (value, now)
        if last is None:
            return None
        dv, dt = value - last[0], now - last[1]
        if dt <= 0 or dv < 0:
            return None
        return dv / dt


# ------------------------------------------------------------------------- data model
@dataclass
class Reading:
    chip: str  # hwmon name, e.g. "nct6687"
    key: str  # "temp3", "fan1", "in4"
    label: str  # driver label or key
    kind: str  # temp | fan | in | power | curr | freq | energy | pwm
    value: float  # normalised: °C, rpm, V, W, A, MHz, J, %
    unit: str
    hwmon: str  # "hwmon2"
    device: str  # resolved device path


@dataclass
class CpuStat:
    model: str = ""
    threads: int = 0
    cores: int = 0
    util: float = 0.0  # %
    per_thread: list[float] = field(default_factory=list)  # %
    mhz: list[float | None] = field(default_factory=list)
    max_mhz: float | None = None
    base_mhz: float | None = None
    core_of_thread: list[int] = field(default_factory=list)  # thread → core index (0..cores-1)
    tctl: float | None = None
    tccd: list[float] = field(default_factory=list)
    package_w: float | None = None
    core_w: list[float | None] = field(default_factory=list)
    load: tuple[float, float, float] = (0.0, 0.0, 0.0)
    ctx_rate: float | None = None
    irq_rate: float | None = None
    procs: int = 0
    power_source: str = ""  # "zenergy" | "rapl" | ""
    prefcore: list[int | None] = field(default_factory=list)  # per thread, amd_pstate ranking
    governor: str = ""
    epp: str = ""  # energy_performance_preference
    boost: bool | None = None
    cstates: list[tuple[str, float]] = field(default_factory=list)  # (name, time-share 0..1)

    def core_threads(self, core: int) -> list[int]:
        return [t for t, c in enumerate(self.core_of_thread) if c == core]

    def core_util(self, core: int) -> float:
        ts = self.core_threads(core)
        return sum(self.per_thread[t] for t in ts) / len(ts) if ts else 0.0

    def core_mhz(self, core: int) -> float | None:
        vals = [self.mhz[t] for t in self.core_threads(core) if t < len(self.mhz) and self.mhz[t]]
        return max(vals) if vals else None


@dataclass
class Dimm:
    addr: int  # SPD i2c address, 0x50..0x57
    temp: float | None
    hwmon: str = ""
    driver: str = "spd5118"
    part: str = ""
    serial: str = ""
    speed: str = ""  # e.g. "5600 CL46"
    expo: bool = False
    xmp: bool = False
    maker: str = ""  # module vendor, from the SPD's JEDEC ID
    dram_maker: str = ""  # who made the DRAM dies
    ranks: int | None = None
    width: int | None = None  # DRAM I/O width (x4/x8/x16)
    die_gbit: int | None = None
    timings: str = ""  # JEDEC base profile, "CL-tRCD-tRP-tRAS"
    made: str = ""  # "2025 wk10"
    temp_max: float | None = None  # SPD hub's warning / critical limits
    temp_crit: float | None = None
    vdd: float | None = None  # PMIC rail setpoints, volts
    vddq: float | None = None
    vpp: float | None = None
    bus: int | None = None  # i2c bus the SPD hub (and PMIC) sit on
    rail_w: tuple[float, float, float] | None = None  # live VDD/VDDQ/VPP watts, with --dimm-power

    @property
    def power_w(self) -> float | None:
        return sum(self.rail_w) if self.rail_w else None


@dataclass
class ZramStat:
    name: str
    disksize: int
    orig: int
    compr: int
    used: int
    algo: str = ""


@dataclass
class MemStat:
    total: int = 0
    available: int = 0
    used: int = 0
    free: int = 0
    buffers: int = 0
    cached: int = 0
    shared: int = 0
    swap_total: int = 0
    swap_used: int = 0
    swap_in_rate: float | None = None  # bytes/s
    swap_out_rate: float | None = None
    dimms: list[Dimm] = field(default_factory=list)
    zram: list[ZramStat] = field(default_factory=list)
    hugepages: int = 0
    read_bps: float | None = None  # DRAM traffic from memory-controller counters
    write_bps: float | None = None
    peak_bps: float | None = None  # theoretical DRAM rate at the current memory clock
    bw_note: str = ""  # why read_bps/write_bps are None
    memclk_mhz: int | None = None  # DRAM clock (DDR MT/s is twice this); from the iGPU DPM table
    channel_bps: list[tuple[float | None, float | None]] = field(default_factory=list)  # per UMC (read, write)
    channel_busy: list[float | None] = field(default_factory=list)  # per UMC, share of clocks the data bus moved data
    dimm_power_note: str = ""  # why --dimm-power gave no readings

    @property
    def used_frac(self) -> float:
        return (self.total - self.available) / self.total if self.total else 0.0

    def dimm(self, addr: int) -> Dimm | None:
        return next((d for d in self.dimms if d.addr == addr), None)


@dataclass
class DiskStat:
    name: str  # nvme0n1, sda
    model: str = ""
    kind: str = "disk"  # nvme | ssd | hdd | zram | virtual
    size: int = 0
    read_bps: float | None = None
    write_bps: float | None = None
    read_iops: float | None = None
    write_iops: float | None = None
    busy: float | None = None  # %
    temp: float | None = None
    temps: dict[str, float] = field(default_factory=dict)
    pci: list[str] = field(default_factory=list)  # PCI path, root port first
    serial: str = ""
    firmware: str = ""
    link: str = ""  # e.g. "PCIe 4.0 x4" or "SATA"
    sata_port: int | None = None  # 1-based port on its SATA controller

    @property
    def root_port(self) -> str | None:
        return self.pci[0] if self.pci else None


@dataclass
class PartStat:
    device: str
    mount: str
    fstype: str
    total: int
    used: int

    @property
    def frac(self) -> float:
        return self.used / self.total if self.total else 0.0


@dataclass
class NetStat:
    name: str
    kind: str = "virtual"  # ethernet | wifi | loopback | virtual
    up: bool = False
    speed_mbps: int | None = None
    rx_bps: float | None = None
    tx_bps: float | None = None
    rx_total: int = 0
    tx_total: int = 0
    addr: str = ""
    mac: str = ""
    signal_dbm: float | None = None
    temp: float | None = None
    driver: str = ""
    pci: list[str] = field(default_factory=list)
    errors: int = 0


@dataclass
class GpuStat:
    index: int
    name: str
    vendor: str = "nvidia"  # nvidia | amd
    util: float | None = None
    mem_util: float | None = None
    power_w: float | None = None
    power_limit_w: float | None = None
    temp: float | None = None
    vram_used: int | None = None
    vram_total: int | None = None
    fan_pct: float | None = None
    fan_rpm: int | None = None
    clk_mhz: float | None = None
    clk_mem_mhz: float | None = None
    pci: str = ""
    pcie_gen: int | None = None
    pcie_width: int | None = None
    pcie_max_gen: int | None = None
    pcie_max_width: int | None = None
    driver: str = ""
    volt: float | None = None
    pcie_rx_bps: float | None = None  # host → GPU
    pcie_tx_bps: float | None = None  # GPU → host
    processes: list[GpuProc] = field(default_factory=list)
    vf_gpc: int | None = None
    vf_mem: int | None = None
    vf_gpc_range: tuple[int, int] | None = None
    vf_mem_range: tuple[int, int] | None = None
    pcie_l0_recovery: int | None = None
    pcie_corr: int | None = None
    pcie_nak: int | None = None

    @property
    def vram_frac(self) -> float | None:
        return self.vram_used / self.vram_total if self.vram_used is not None and self.vram_total else None


@dataclass
class GpuProc:
    pid: int
    name: str = ""
    kind: str = ""  # C | G | C+G
    vram_bytes: int | None = None


@dataclass
class PsiLine:
    some10: float = 0.0
    some60: float = 0.0
    some300: float = 0.0
    full10: float = 0.0
    full60: float = 0.0
    full300: float = 0.0


@dataclass
class Psi:
    cpu: PsiLine = field(default_factory=PsiLine)
    memory: PsiLine = field(default_factory=PsiLine)
    io: PsiLine = field(default_factory=PsiLine)


@dataclass
class ProcStat:
    pid: int
    name: str
    user: str = ""
    cpu: float = 0.0  # % of one thread
    rss: int = 0
    mem_pct: float = 0.0
    threads: int = 0
    status: str = ""
    io_bps: float | None = None  # read + write
    cmd: str = ""
    last_cpu: int | None = None
    gpu: bool = False
    gpu_vram: int | None = None
    gpu_kind: str = ""
    disks: list[str] = field(default_factory=list)
    io_read_bps: float | None = None
    io_write_bps: float | None = None
    started: float | None = None  # epoch seconds


@dataclass
class IpcNode:
    """One talker on the IPC map. ``in_bps`` (▲) is what a process receives. For a container, VM,
    peer or the Internet, it is what that node sends towards this host. ``out_bps`` (▼) is the
    reverse, so a container's ▲ is what the container transmits."""

    id: str  # host | proc:<pid> | hidden | ctr:<id12> | vm:<name> | peer:<ip> | wan
    kind: str  # host | process | container | vm | peer | wan
    name: str
    sub: str = ""  # image, user, MAC…
    runtime: str = ""  # podman | docker | distrobox | toolbox | lxc | qemu | nspawn
    state: str = ""
    addrs: list[str] = field(default_factory=list)
    pids: list[int] = field(default_factory=list)
    netns: int | None = None
    ports: list[str] = field(default_factory=list)  # published, "0.0.0.0:8080→80/tcp"
    in_bps: float | None = None
    out_bps: float | None = None
    prov: str = "live"  # live | derived
    cmd: str = ""
    dns: str = ""  # reverse-DNS name (peers), derived
    router: bool = False
    iface: str = ""  # tap of a VM, NIC of a peer
    # resources: processes from the process table, containers and VMs from their cgroup
    cpu: float | None = None  # % of one thread
    rss: int | None = None  # bytes; a cgroup's memory.current
    threads: int | None = None
    fds: int | None = None
    started: float | None = None  # epoch seconds
    unit: str = ""  # systemd unit or scope, from the cgroup path
    mem_limit: int | None = None  # cgroup memory.max
    cpu_limit: float | None = None  # cgroup cpu.max, in cores
    pids_n: int | None = None  # cgroup pids.current
    res_prov: str = "live"  # derived when a guest's figures are summed from its PIDs


@dataclass
class IpcLink:
    """A channel between two nodes, ``a`` → ``b`` (client → server, writer → reader). ``in_bps``
    flows towards ``a``. Pipes and socketpairs between the same two processes fold into one
    link with ``count``."""

    a: str
    b: str  # "" for kind == "listen"
    kind: str  # pipe | unix | tcp | udp | listen
    a_end: str = ""  # "ip:port", or the Unix path on the b side
    b_end: str = ""
    pid_a: int | None = None
    pid_b: int | None = None
    state: str = ""
    in_bps: float | None = None
    out_bps: float | None = None
    count: int = 1
    prov: str = "live"
    ino: int = 0  # socket inode (tcp/udp), or the pipe inode

    @property
    def bps(self) -> float:
        return (self.in_bps or 0.0) + (self.out_bps or 0.0)


@dataclass
class IpcStat:
    nodes: list[IpcNode] = field(default_factory=list)
    links: list[IpcLink] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)  # per link kind
    note: str = ""  # what is hidden and why
    age_s: float = 0.0  # since the last full process walk
    kernel: dict[str, float | None] = field(default_factory=dict)  # netstat counters per second, conntrack
    tcp_states: dict[str, int] = field(default_factory=dict)  # this netns's TCP sockets by state

    def node(self, nid: str) -> IpcNode | None:
        return next((n for n in self.nodes if n.id == nid), None)

    def links_of(self, nid: str) -> list[IpcLink]:
        return [k for k in self.links if nid in (k.a, k.b)]


@dataclass
class BoardInfo:
    vendor: str = ""
    name: str = ""
    product: str = ""
    bios: str = ""
    bios_date: str = ""


@dataclass
class SystemSnapshot:
    t: float
    source: str = "linux"  # linux | demo
    hostname: str = ""
    kernel: str = ""
    uptime_s: float = 0.0
    board: BoardInfo = field(default_factory=BoardInfo)
    cpu: CpuStat = field(default_factory=CpuStat)
    mem: MemStat = field(default_factory=MemStat)
    disks: list[DiskStat] = field(default_factory=list)
    parts: list[PartStat] = field(default_factory=list)
    nets: list[NetStat] = field(default_factory=list)
    gpus: list[GpuStat] = field(default_factory=list)
    sensors: list[Reading] = field(default_factory=list)
    procs: list[ProcStat] = field(default_factory=list)
    psi: Psi | None = None
    ipc: IpcStat | None = None  # only while something shows it: the process walk is not free

    # ------------------------------------------------------------------ lookups
    def reading(self, chip: str, label: str) -> Reading | None:
        for r in self.sensors:
            if r.chip == chip and (r.label == label or r.key == label):
                return r
        return None

    def value(self, chip: str, label: str) -> float | None:
        r = self.reading(chip, label)
        return None if r is None else r.value

    def fans(self) -> list[Reading]:
        return [r for r in self.sensors if r.kind == "fan"]

    def disk_on_port(self, port: str) -> DiskStat | None:
        return next((d for d in self.disks if d.root_port == port), None)

    def disk_on_sata(self, port: int) -> DiskStat | None:
        return next((d for d in self.disks if d.sata_port == port), None)

    def disk(self, name: str) -> DiskStat | None:
        return next((d for d in self.disks if d.name == name), None)

    def net(self, name: str) -> NetStat | None:
        return next((n for n in self.nets if n.name == name), None)

    def net_of_kind(self, kind: str) -> NetStat | None:
        cands = [n for n in self.nets if n.kind == kind]
        cands.sort(key=lambda n: (not n.up, n.name))
        return cands[0] if cands else None

    def gpu(self, vendor: str = "nvidia") -> GpuStat | None:
        return next((g for g in self.gpus if g.vendor == vendor), None)

    def physical_disks(self) -> list[DiskStat]:
        return [d for d in self.disks if d.kind in ("nvme", "ssd", "hdd")]

    def physical_nets(self) -> list[NetStat]:
        return [n for n in self.nets if n.kind in ("ethernet", "wifi")]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["procs"] = d["procs"][:25]
        if self.ipc is not None:
            d["ipc"] = ipc_dict(self.ipc)
        return d


def ipc_dict(ipc: IpcStat, keep: int = 100) -> dict:
    """``asdict`` of the busiest ``keep`` links and the nodes they touch. A desktop has hundreds of
    socketpairs, which would swamp ``--json`` and recordings."""
    links = sorted(ipc.links, key=lambda k: (k.bps, k.count), reverse=True)[:keep]
    used = {k.a for k in links} | {k.b for k in links}
    nodes = [n for n in ipc.nodes if n.kind != "process" or n.id in used]
    d = asdict(ipc)
    d["nodes"] = [asdict(n) for n in nodes]
    d["links"] = [asdict(k) for k in links]
    return d


# ------------------------------------------------------------------------- source
class LinuxSource:
    """Live data from this machine."""

    def __init__(self, root: str | Path = "/", procs: bool = True, gpu: bool = True, dimm_power: bool = False) -> None:
        from . import gpu as gpu_mod
        from . import perf
        from . import pmic
        from . import processes

        from .ipc import IpcTracker
        from .sensors import HwmonReader

        self.fs = SysFS(root)
        self.rates = Rates()
        self.hwmon = HwmonReader(self.fs)
        self.want_procs = procs
        self.procs = processes.ProcTracker() if procs else None
        self.nvml = gpu_mod.Nvml() if gpu else None
        self.umc = perf.UmcBandwidth(self.fs)
        self.pmic = pmic.Meter(self.fs) if dimm_power else None  # writes the PMIC's ADC enable: opt-in
        self.ipc = IpcTracker(self.fs)
        self.want_ipc = False  # the app turns this on while the IPC tab is showing
        self.t0 = time.monotonic()

    def collect(self, now: float | None = None) -> SystemSnapshot:
        from . import board, cpu, gpu, memory, network, sensors, storage

        now = time.monotonic() if now is None else now
        fs, rates = self.fs, self.rates
        readings, chips = self.hwmon.read_all()
        snap = SystemSnapshot(t=now, source="linux")
        snap.hostname = socket.gethostname()
        snap.kernel = os.uname().release
        snap.uptime_s = _uptime(fs)
        snap.board = board.read(fs)
        snap.sensors = readings
        snap.cpu = cpu.collect(fs, rates, chips, now)
        snap.mem = memory.collect(fs, rates, chips, now)
        (snap.mem.read_bps, snap.mem.write_bps, snap.mem.peak_bps, snap.mem.channel_bps,
         snap.mem.channel_busy) = self.umc.sample(rates, now)
        snap.mem.bw_note = self.umc.note
        if self.pmic is not None:
            self.pmic.fill(snap.mem.dimms)
            snap.mem.dimm_power_note = self.pmic.note
        memory.fill_clock(fs, snap.mem)
        snap.disks, snap.parts = storage.collect(fs, rates, chips, now)
        snap.nets = network.collect(fs, rates, chips, now)
        snap.gpus = (self.nvml.collect() if self.nvml else []) + gpu.amd(fs, chips)
        snap.psi = cpu.read_psi(fs)
        if self.procs is not None:
            snap.procs = self.procs.collect(now)
            _annotate_procs(snap)
        if self.want_ipc:
            snap.ipc = self.ipc.collect(now)
            if self.procs is not None:
                annotate_ipc(snap.ipc, self.procs.by_pid)
        return snap

    def close(self) -> None:
        if self.nvml is not None:
            close = getattr(self.nvml, "close", None)
            if close is not None:
                close()
        self.umc.close()
        if self.pmic is not None:
            self.pmic.close()
        self.ipc.close()


def _annotate_procs(snap: SystemSnapshot) -> None:
    """Join GPU PIDs onto ProcStat after both collectors have run."""
    by_pid: dict[int, GpuProc] = {}
    for g in snap.gpus:
        for p in g.processes:
            cur = by_pid.get(p.pid)
            if cur is None:
                by_pid[p.pid] = p
            elif p.kind and cur.kind and p.kind != cur.kind:
                cur.kind = "C+G"
    for proc in snap.procs:
        gp = by_pid.get(proc.pid)
        if gp is None:
            continue
        proc.gpu = True
        proc.gpu_vram = gp.vram_bytes
        proc.gpu_kind = gp.kind


def annotate_ipc(ipc: IpcStat, by_pid: dict[int, ProcStat]) -> None:
    """CPU, memory, threads and start time from the process table onto the IPC nodes. A guest
    whose cgroup was unreadable gets the sum over its PIDs instead, tagged derived."""
    for n in ipc.nodes:
        if n.kind not in ("process", "container", "vm"):
            continue
        ps = [by_pid[p] for p in n.pids if p in by_pid]
        if not ps:
            continue
        n.threads = sum(p.threads for p in ps)
        starts = [p.started for p in ps if p.started]
        n.started = n.started or (min(starts) if starts else None)
        if n.kind == "process":
            n.cpu, n.rss = sum(p.cpu for p in ps), sum(p.rss for p in ps)
            continue
        if n.cpu is None:
            n.cpu, n.res_prov = sum(p.cpu for p in ps), "derived"
        if n.rss is None:
            n.rss, n.res_prov = sum(p.rss for p in ps), "derived"


def _uptime(fs: SysFS) -> float:
    s = fs.read("proc/uptime")
    try:
        return float(s.split()[0]) if s else 0.0
    except ValueError:
        return 0.0
