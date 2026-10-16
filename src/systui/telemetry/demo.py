"""Synthetic telemetry shaped like a 6-core AM5 desktop, for ``--demo`` and tests."""

from __future__ import annotations

import math
import random
import time

from . import (BoardInfo, CpuStat, Dimm, DiskStat, GpuProc, GpuStat, IpcEvent, IpcLink, IpcNode, IpcStat, MemStat,
               NetStat, PartStat, ProcStat, Psi, PsiLine, Reading, SystemSnapshot, ZramStat)

GiB = 2**30


def _wave(t: float, period: float, phase: float = 0.0) -> float:
    return 0.5 + 0.5 * math.sin(2 * math.pi * t / period + phase)


class DemoSource:
    def __init__(self, board: str = "B650M GAMING PLUS WIFI (MS-7E24)", seed: int = 7) -> None:
        self.board = board
        self.rng = random.Random(seed)
        self.t0 = time.monotonic()

    def collect(self, now: float | None = None) -> SystemSnapshot:
        now = time.monotonic() if now is None else now
        t = now - self.t0
        r = self.rng
        burst = _wave(t, 37) ** 2
        s = SystemSnapshot(t=now, source="demo", hostname="demo-box", kernel="6.17.0-demo", uptime_s=86400 + t)
        s.board = BoardInfo("Micro-Star International Co., Ltd.", self.board, "MS-7E24", "1.E2", "07/02/2026")

        threads, cores = 12, 6
        per = []
        for i in range(threads):
            base = 8 + 80 * burst * _wave(t, 5 + i, i) + (40 if i == 0 else 0) * _wave(t, 9)
            per.append(max(0.0, min(100.0, base + r.uniform(-4, 4))))
        c = CpuStat(model="AMD Ryzen 5 9600X 6-Core Processor", threads=threads, cores=cores, per_thread=per,
                    util=sum(per) / threads, core_of_thread=[i % cores for i in range(threads)])
        c.mhz = [3000 + 2400 * min(1.0, p / 60) + r.uniform(-40, 40) for p in per]
        c.prefcore = [200 - i * 8 for i in range(threads)]
        c.governor, c.epp, c.boost = "powersave", "balance_performance", True
        c.cstates = [("POLL", 0.01), ("C1", 0.08), ("C2", 0.22), ("C3", 0.69)]
        c.max_mhz = 5486.0
        c.core_w = [1.5 + 14 * c.core_util(k) / 100 + r.uniform(-0.3, 0.3) for k in range(cores)]
        c.package_w = sum(w for w in c.core_w if w) + 22
        c.tctl = 42 + 0.45 * c.package_w + r.uniform(-0.5, 0.5)
        c.tccd = [c.tctl - 6]
        c.load = (c.util / 100 * 12, c.util / 100 * 10, 3.2)
        c.ctx_rate, c.irq_rate, c.procs, c.power_source = 60000 + 400 * c.util, 30000 + 200 * c.util, 512, "zenergy"
        s.cpu = c

        total = 64 * GiB
        used = int(total * (0.42 + 0.25 * _wave(t, 90)))
        s.mem = MemStat(total=total, available=total - used, used=used, free=int(total * 0.05), buffers=2 * GiB,
                        cached=int(total - used - total * 0.05 - 2 * GiB), shared=GiB, swap_total=16 * GiB,
                        swap_used=3 * GiB, swap_in_rate=0.0, swap_out_rate=r.choice([0.0, 0.0, 4096.0]))
        parts = ["CP16G56C46U5.M8G1", "CP16G56C46U5.M8G1", "CP16G56C46U5.M8D1", "CP16G56C46U5.M8D1"]
        serials = ["DEMO0000001", "DEMO0000002", "DEMO0000003", "DEMO0000004"]
        s.mem.dimms = [Dimm(0x50 + k, 40 + 6 * burst + k * 0.4, part=parts[k], serial=serials[k],
                            speed="5600 CL46", expo=True, maker="Micron", dram_maker="Micron", ranks=1, width=8,
                            die_gbit=16, timings="46-45-45-90", made="2025 wk10" if k < 2 else "2025 wk35",
                            temp_max=55.0, temp_crit=85.0, vdd=1.1, vddq=1.1, vpp=1.8,
                            rail_w=(1.1 * (0.75 + 2.5 * burst), 1.1 * (0.25 + 0.75 * burst), 1.8 * 0.125))
                       for k in range(4)]
        s.mem.read_bps, s.mem.write_bps = 3e9 + 4.5e10 * burst, 1.5e9 + 2e10 * burst
        s.mem.channel_bps = [(s.mem.read_bps / 2, s.mem.write_bps / 2),
                             (s.mem.read_bps / 2, s.mem.write_bps / 2)]
        s.mem.peak_bps = 96e9  # DDR5-6000, two channels
        s.mem.channel_busy = [min(1.0, (rd + wr) / 48e9 * 1.3) for rd, wr in s.mem.channel_bps]
        s.mem.memclk_mhz = 3000
        s.mem.zram = [ZramStat("zram0", 16 * GiB, 3 * GiB, 900 * 2**20, 950 * 2**20, "zstd")]

        def disk(name, model, kind, size, rd, wr, temp, pci, link):
            return DiskStat(name, model, kind, size, rd, wr, rd / 65536, wr / 65536, min(100.0, (rd + wr) / 2e7),
                            temp, {"Composite": temp} if temp else {}, pci, link=link)

        io = _wave(t, 23) ** 3
        s.disks = [
            disk("nvme0n1", "CT1000P3PSSD8", "nvme", 1000 * 10**9, 9e8 * io, 3e8 * _wave(t, 11) ** 4,
                 44 + 10 * io, ["0000:00:01.2", "0000:02:00.0"], "PCIe 4.0 x4"),
            disk("nvme1n1", "KIOXIA-EXCERIA G3 SSD", "nvme", 2000 * 10**9, 2e8 * _wave(t, 17) ** 5, 6e8 * burst,
                 49 + 6 * burst, ["0000:00:02.2", "0000:13:00.0"], "PCIe 4.0 x4"),
            disk("sda", "ST1000LM035-1RK1", "hdd", 1000 * 10**9, 4e7 * _wave(t, 41) ** 8, 0.0, None,
                 ["0000:00:02.1", "0000:03:00.0", "0000:04:0d.0", "0000:12:00.0"], "SATA"),
            disk("zram0", "zram (compressed RAM)", "zram", 16 * GiB, 0.0, 1e5, None, [], ""),
        ]
        s.disks[0].serial, s.disks[1].serial = "DEMO-NVME-0001", "DEMO-NVME-0002"
        s.parts = [PartStat("/dev/nvme0n1p7", "/", "btrfs", 733 * 10**9, 158 * 10**9),
                   PartStat("/dev/nvme0n1p6", "/boot", "ext4", 10**9, 5 * 10**8),
                   PartStat("/dev/nvme1n1p1", "/var/mnt/data", "btrfs", 1488 * 10**9, 1200 * 10**9)]

        net = _wave(t, 13) ** 2
        s.nets = [
            NetStat("enp6s0", "ethernet", True, 2500, 2.6e7 * net, 1.2e6 * net, 0, 0, "192.168.1.10", temp=44.5,
                    driver="r8169", pci=["0000:00:02.1", "0000:03:00.0", "0000:04:01.0", "0000:06:00.0"]),
            NetStat("wlp8s0", "wifi", True, None, 4e5 * _wave(t, 7), 9e4 * _wave(t, 5), 0, 0, "192.168.1.20",
                    signal_dbm=-55.0, temp=56.0, driver="mt7921e",
                    pci=["0000:00:02.1", "0000:03:00.0", "0000:04:03.0", "0000:08:00.0"]),
            NetStat("lo", "loopback", True, None, 4e4, 4e4, 0, 0, "127.0.0.1"),
        ]

        gu = 100 * _wave(t, 29) ** 2
        s.gpus = [
            GpuStat(0, "NVIDIA GeForce RTX 5080", "nvidia", gu, gu * 0.4, 30 + 3.0 * gu, 360.0, 38 + 0.3 * gu,
                    int(4 * GiB + 10 * GiB * gu / 100), 16 * GiB, 30 + 0.4 * gu, int(800 + 12 * gu), 600 + 23 * gu,
                    14801, "0000:01:00.0", 4, 16, 4, 16, "demo", pcie_rx_bps=2e5 + 3e10 * (gu / 100) ** 3,
                    pcie_tx_bps=1e5 + 6e9 * (gu / 100) ** 3, vf_gpc=0, vf_mem=0, vf_gpc_range=(-1000, 1000),
                    vf_mem_range=(-2000, 6000), pcie_l0_recovery=372, pcie_corr=0, pcie_nak=0,
                    processes=[GpuProc(1001, "python3", "C", int(8 * GiB * gu / 100))]),
            GpuStat(100, "AMD Radeon (iGPU)", "amd", 4.0, None, 0.1, None, 54.0, GiB, 2 * GiB, clk_mhz=2200,
                    pci="0000:14:00.0", driver="amdgpu"),
        ]

        def rd(chip, key, label, kind, v, unit, hw):
            return Reading(chip, key, label, kind, v, unit, hw, "")

        fans = [("CPU Fan", 700 + 9 * c.util), ("Pump Fan", 2100 + r.uniform(-20, 20)), ("System Fan #1", 0),
                ("System Fan #2", 0), ("System Fan #3", 650 + 4 * c.util), ("System Fan #4", 0),
                ("System Fan #5", 0), ("System Fan #6", 0)]
        sens = [rd("nct6687", f"fan{i + 1}", lab, "fan", round(v), "rpm", "hwmon2") for i, (lab, v) in enumerate(fans)]
        volts = [("+12V", 12.1), ("+5V", 5.01), ("CPU Soc", 1.206), ("CPU Vcore", 1.0 + 0.003 * c.util),
                 ("DRAM", 1.1), ("Chipset", 0.352), ("CPU SA", 0.644)]
        sens += [rd("nct6687", f"in{i}", lab, "in", v, "V", "hwmon2") for i, (lab, v) in enumerate(volts)]
        temps = [("CPU", c.tctl - 2), ("System", 48 + 3 * burst), ("VRM MOS", 40 + 0.15 * c.package_w),
                 ("PCH", 46 + 2 * io), ("CPU Socket", 42 + 2 * burst), ("PCIe x1", 60.0)]
        sens += [rd("nct6687", f"temp{i + 1}", lab, "temp", v, "°C", "hwmon2") for i, (lab, v) in enumerate(temps)]
        sens += [rd("k10temp", "temp1", "Tctl", "temp", c.tctl, "°C", "hwmon5"),
                 rd("k10temp", "temp3", "Tccd1", "temp", c.tccd[0], "°C", "hwmon5")]
        sens += [rd("spd5118", "temp1", "temp1", "temp", d.temp, "°C", f"hwmon{6 + k}") for k, d in enumerate(s.mem.dimms)]
        sens += [rd("nvme", "temp1", "Composite", "temp", d.temp, "°C", f"hwmon{k}") for k, d in enumerate(s.disks[:2])]
        s.sensors = sens

        names = ["firefox", "python3", "gnome-shell", "Xwayland", "pipewire", "code", "kworker/u32:1", "systemd"]
        s.procs = [ProcStat(1000 + k, n, "demo", max(0.0, 120 * burst / (k + 1) + r.uniform(0, 3)),
                            int(GiB * 2 / (k + 1)), 3.0 / (k + 1), 10 + k, "running" if k < 2 else "sleeping",
                            1e6 / (k + 1), cmd=n, last_cpu=k % threads, io_read_bps=0.7e6 / (k + 1),
                            io_write_bps=0.3e6 / (k + 1)) for k, n in enumerate(names)]
        s.procs[1].gpu, s.procs[1].gpu_kind, s.procs[1].gpu_vram = True, "C", s.gpus[0].processes[0].vram_bytes
        s.procs[1].disks = ["nvme0n1"]
        s.psi = Psi(cpu=PsiLine(0.0, 0.16, 2.22), memory=PsiLine(0.0, 0.51, 1.29, 0.0, 0.51, 1.28),
                    io=PsiLine(0.87, 1.36, 2.01, 0.70, 1.04, 1.47))
        s.ipc = demo_ipc(t, s.hostname)
        return s

    def close(self) -> None:
        pass


def demo_ipc(t: float, hostname: str) -> IpcStat:
    """A desktop with two podman containers, a distrobox, a QEMU guest and three LAN peers."""
    llm = _wave(t, 19) ** 3  # token streaming from the llama.cpp container
    web = _wave(t, 13) ** 2
    ssh = _wave(t, 31, 1.0) ** 4
    procs = [(1000, "firefox"), (1001, "python3"), (1002, "gnome-shell"), (1003, "Xwayland"), (1004, "pipewire"),
             (1005, "code"), (1100, "dbus-broker"), (1200, "sshd")]
    nodes = [IpcNode("host", "host", hostname, addrs=["192.168.1.10", "192.168.1.20"])]
    nodes += [IpcNode(f"proc:{pid}", "process", name, "root" if name == "sshd" else "demo", pids=[pid], cmd=name)
              for pid, name in procs]
    nodes += [
        IpcNode("ctr:df517a104788", "container", "llm-litellm-db-1", "docker.io/library/postgres:16", "podman",
                "Up 25 hours (healthy)", ["10.89.0.3"], [30278, 30301], 4026533725, ["127.0.0.1:5432→5432/tcp"],
                in_bps=2e4 + 4e5 * llm, out_bps=1e4 + 1.5e5 * llm),
        IpcNode("ctr:44254bc720fd", "container", "llm-llamacpp-1", "ghcr.io/ggml-org/llama.cpp:server-cuda", "podman",
                "Up 3 hours", ["10.89.0.5"], [31002], 4026533802, ["127.0.0.1:8080→8080/tcp"],
                in_bps=3e3 + 2.4e6 * llm, out_bps=2e3 + 3e5 * llm),
        IpcNode("ctr:9a1fd0c2be11", "container", "fedora-dev", "registry.fedoraproject.org/fedora-toolbox:44",
                "distrobox", "Up 2 days", [], [4100, 4101], None, in_bps=8e5 * web ** 3, out_bps=2e4 * web ** 3),
        IpcNode("vm:win11", "vm", "win11", "libvirt-qemu", "qemu", "running", ["192.168.122.50"], [5200],
                in_bps=1e4 + 9e5 * _wave(t, 23, 2.0) ** 4, out_bps=4e4 + 6e6 * _wave(t, 17, 0.5) ** 6, iface="vnet0"),
        IpcNode("peer:192.168.1.1", "peer", "router", "c0:68:cc:2b:d7:41", addrs=["192.168.1.1"], router=True,
                iface="enp6s0", dns="router.lan", prov="derived"),
        IpcNode("peer:192.168.1.20", "peer", "nas", "68:b9:c2:7e:fd:2e", addrs=["192.168.1.20"], iface="enp6s0",
                dns="nas.lan", prov="derived"),
        IpcNode("peer:192.168.1.34", "peer", "laptop", "f0:a3:b2:0b:d5:d5", addrs=["192.168.1.34"], iface="wlp8s0",
                dns="laptop.lan", prov="derived"),
        IpcNode("wan", "wan", "Internet"),
    ]

    def tcp(a, b, src, dst, pid, i, o, prov="live", state="ESTAB", proto="tcp"):
        return IpcLink(a, b, proto, src, dst, pid, None, state, i, o, prov=prov)

    links = [
        IpcLink("proc:1000", "proc:1004", "unix", "", "/run/user/1000/pipewire-0", 1000, 1004, "ESTAB", count=3),
        IpcLink("proc:1000", "proc:1002", "unix", "", "/run/user/1000/wayland-0", 1000, 1002, "ESTAB"),
        IpcLink("proc:1003", "proc:1002", "unix", "", "/run/user/1000/wayland-0", 1003, 1002, "ESTAB", count=2),
        IpcLink("proc:1005", "proc:1002", "unix", "", "/run/user/1000/wayland-0", 1005, 1002, "ESTAB"),
        *[IpcLink(f"proc:{pid}", "proc:1100", "unix", "", "/run/user/1000/bus", pid, 1100, "ESTAB")
          for pid in (1000, 1002, 1004, 1005)],
        IpcLink("proc:1000", "proc:1004", "shm", "", "pipewire-memfd", 1000, 1004, count=4),
        IpcLink("proc:1003", "proc:1002", "shm", "", "SysV #6", 1003, 1002),
        IpcLink("proc:1005", "proc:1001", "pipe", "", "", 1005, 1001, count=2),
        IpcLink("proc:1001", "proc:1005", "pipe", "", "", 1001, 1005),
        tcp("proc:1001", "ctr:44254bc720fd", "127.0.0.1:41234", "127.0.0.1:8080", 1001, 2e3 + 2.4e6 * llm,
            1e3 + 2e5 * llm, "derived"),
        tcp("proc:1001", "ctr:df517a104788", "127.0.0.1:52210", "127.0.0.1:5432", 1001, 1e4 + 4e5 * llm,
            5e3 + 1.5e5 * llm, "derived"),
        tcp("proc:1000", "wan", "192.168.1.10:46096", "34.54.185.247:443", 1000, 3e6 * web, 8e4 * web),
        tcp("proc:1000", "wan", "192.168.1.10:51022", "142.250.183.14:443", 1000, 9e5 * web ** 2, 2e4 * web,
            proto="udp"),
        tcp("proc:1005", "peer:192.168.1.20", "192.168.1.10:40686", "192.168.1.20:445", 1005, 6e5 * ssh, 4e6 * ssh),
        tcp("proc:1200", "peer:192.168.1.34", "192.168.1.10:22", "192.168.1.34:51514", 1200, 2e3 + 3e4 * ssh,
            5e3 + 2e5 * ssh),
        tcp("proc:1002", "peer:192.168.1.1", "192.168.1.10:5353", "224.0.0.251:5353", 1002, None, None,
            proto="udp"),
        tcp("ctr:9a1fd0c2be11", "wan", "192.168.1.10:38810", "151.101.2.132:443", 4100, 8e5 * web ** 3,
            2e4 * web ** 3),
        tcp("ctr:44254bc720fd", "wan", "10.89.0.5:44312", "185.199.108.133:443", None, None, None),
        IpcLink("proc:1200", "", "listen", "0.0.0.0:22", state="LISTEN"),
        IpcLink("ctr:df517a104788", "", "listen", "0.0.0.0:5432", state="LISTEN"),
        IpcLink("ctr:44254bc720fd", "", "listen", "0.0.0.0:8080", state="LISTEN"),
    ]
    # `code` fetches something every other 6 s: a link that opens and closes, for the events log
    period, wall = 6.0, time.time()
    fetch = ("192.168.1.10:51900", "93.184.215.14:443")
    if int(t // period) % 2 == 0:
        links.append(tcp("proc:1005", "wan", *fetch, 1005, 4e4, 2e3))
    events = [IpcEvent(wall - t - 40, "new peer", "peer", "peer:192.168.1.34", "", "192.168.1.34", "laptop"),
              IpcEvent(wall - t - 30, "started", "container", "ctr:44254bc720fd", "", "", "llm-llamacpp-1"),
              IpcEvent(wall - t - 29, "listening", "listen", "ctr:44254bc720fd", "", "0.0.0.0:8080", "llm-llamacpp-1")]
    events += [IpcEvent(wall - (t - j * period), "closed" if j % 2 else "opened", "tcp", "proc:1005", "wan",
                        fetch[1], "code", "Internet") for j in range(max(0, int(t // period) - 9), int(t // period) + 1)]
    from .ipc import rollup

    rollup({n.id: n for n in nodes}, links)
    # resources: processes as the process table would give them, guests from their cgroup
    wall = time.time()
    res = {"firefox": (38 * web + 4, 2.1e9, 112, 160), "python3": (90 * llm + 2, 6.4e8, 14, 23),
           "gnome-shell": (6.0, 3.1e8, 31, 88), "Xwayland": (2.0, 1.2e8, 9, 40), "pipewire": (1.5, 3.0e7, 4, 61),
           "code": (12 * ssh + 3, 1.4e9, 46, 210), "dbus-broker": (0.2, 6e6, 1, 70), "sshd": (0.4, 9e6, 1, 12)}
    shm = {"firefox": (4, 2.0e5), "pipewire": (4, 2.0e5), "Xwayland": (1, 1.08e7), "gnome-shell": (1, 1.08e7)}
    for k, n in enumerate(x for x in nodes if x.kind == "process"):
        n.cpu, n.rss, n.threads, n.fds = res[n.name]
        n.shm_n, n.shm_bytes = shm.get(n.name, (None, None))
        n.started, n.unit = wall - 3600 * (k + 1) - 427, f"app-{n.name}-{n.pids[0]}.scope"
    guest_res = {"ctr:df517a104788": (3 + 40 * llm, 7.3e7, 2 * 2**30, None, 10, 25 * 3600),
                 "ctr:44254bc720fd": (100 + 700 * llm, 9.6e9, None, 8.0, 46, 3 * 3600),
                 "ctr:9a1fd0c2be11": (1 + 20 * web ** 3, 4.1e8, None, None, 31, 50 * 3600),
                 "vm:win11": (60 + 140 * _wave(t, 23, 2.0) ** 4, 8.6e9, 8 * 2**30, 4.0, 3, 5 * 3600)}
    for n in nodes:
        if n.id in guest_res:
            n.cpu, n.rss, n.mem_limit, n.cpu_limit, n.pids_n, up = guest_res[n.id]
            n.started = wall - up
            n.unit = (f"libpod-{n.id[4:]}0b9e5f1c2d7a.scope" if n.kind == "container"
                      else "machine-qemu\\x2d1\\x2dwin11.scope")
    counts: dict[str, int] = {}
    for k in links:
        counts[k.kind] = counts.get(k.kind, 0) + k.count
    kernel = {"Tcp.RetransSegs": 0.4 + 6 * ssh, "Tcp.OutRsts": 0.2, "Tcp.EstabResets": 0.0, "Tcp.AttemptFails": 0.0,
              "Tcp.InErrs": 0.0, "TcpExt.ListenOverflows": 0.0, "TcpExt.ListenDrops": 0.0,
              "TcpExt.TCPTimeouts": 0.1 * ssh, "Udp.InErrors": 0.0, "Udp.RcvbufErrors": 0.0,
              "conntrack": 812.0, "conntrack_max": 262144.0}
    states = {"ESTAB": 23, "LISTEN": 9, "TIME-WAIT": 41, "CLOSE-WAIT": 1}
    return IpcStat(nodes, links, counts, "demo data", 0.0, kernel, states, events)
