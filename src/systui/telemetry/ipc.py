"""Who talks to whom. Covers pipes, Unix sockets and TCP/UDP between processes, plus podman and
docker containers, VMs and machines on this host, and peers on the LAN.

**Full walk.** A full walk runs every ``every``-th poll, and on the first poll after a pause
(the tab was hidden). It reads every ``/proc/<pid>/fd`` to map socket and pipe inodes to
processes. It also reads the container cgroups, ARP, machined and the container API. The
polls in between reuse the walk. They only re-dump TCP, to get ``tcp_info`` byte rates, and
re-read the container and VM interface counters.

**Unprivileged view.**
- Other users' fds are unreadable, so their end of a link is the ``hidden`` node.
- Rootless containers run as sub-uids, so their fds are hidden too. Their netns
  (``/proc/<pid>/net/{dev,tcp,…}``) is readable, so their links and ▲▼ rates come from there.
- Rootless networking leaves the host through helper processes, so those sockets are
  re-attributed to their container (◐):
  - ``rootlessport`` for published ports
  - ``pasta`` or ``slirp4netns`` for outbound connections
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import pwd
import re
import socket
import struct
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import IpcEvent, IpcLink, IpcNode, IpcStat, Rates, SysFS
from . import netstat, sockdiag
from .sockdiag import InetSock, UnixSock

CTR_CGROUP = re.compile(r"(libpod-|docker-|/docker/)([0-9a-f]{64})")
LXC_CGROUP = re.compile(r"lxc\.payload\.([^/\s]+)")
HELPERS = ("rootlessport", "pasta", "slirp4netns")  # host-side ends of rootless container networking
BRIDGES = ("podman", "cni-", "docker", "br-", "virbr", "vnet", "veth", "tap", "lo")  # not the LAN
EVERY = 3
STALE_S = 5.0  # a gap this long means the tab was hidden: walk again before showing anything
EVENTS_KEEP = 300
API_REFRESH_S = 60.0


# ------------------------------------------------------------------------- parsers
@dataclass
class CtrMeta:
    id: str
    name: str
    image: str = ""
    status: str = ""
    runtime: str = "podman"  # podman | docker | distrobox | toolbox
    ips: list[str] = field(default_factory=list)
    gateways: list[str] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)
    ports: list[tuple[str, int, int, str]] = field(default_factory=list)  # host ip, host port, ctr port, proto
    netmode: str = ""
    created: float | None = None  # epoch seconds


def parse_containers(items, engine: str) -> dict[str, CtrMeta]:
    """Docker-compatible ``GET /containers/json`` (podman serves the same shape) → by full id."""
    out: dict[str, CtrMeta] = {}
    for it in items if isinstance(items, list) else []:
        cid = it.get("Id") or ""
        if not cid:
            continue
        names = it.get("Names") or []
        labels = it.get("Labels") or {}
        runtime = ("distrobox" if labels.get("manager") == "distrobox"
                   else "toolbox" if labels.get("com.github.containers.toolbox") == "true" else engine)
        nets = (it.get("NetworkSettings") or {}).get("Networks") or {}
        m = CtrMeta(cid, names[0].lstrip("/") if names else cid[:12], it.get("Image") or "",
                    it.get("Status") or it.get("State") or "", runtime,
                    [v["IPAddress"] for v in nets.values() if v.get("IPAddress")],
                    [v["Gateway"] for v in nets.values() if v.get("Gateway")], sorted(nets),
                    netmode=(it.get("HostConfig") or {}).get("NetworkMode") or "",
                    created=it.get("Created") if isinstance(it.get("Created"), (int, float)) else None)
        for p in it.get("Ports") or []:
            if p.get("PublicPort"):
                m.ports.append((p.get("IP") or "0.0.0.0", int(p["PublicPort"]), int(p.get("PrivatePort") or 0),
                                p.get("Type") or "tcp"))
        out[cid] = m
    return out


def parse_storage(text: str) -> dict[str, CtrMeta]:
    """Names and images from podman's ``overlay-containers/containers.json``, used when the API is down."""
    try:
        items = json.loads(text)
    except ValueError:
        return {}
    out = {}
    for it in items if isinstance(items, list) else []:
        try:
            meta = json.loads(it.get("metadata") or "{}")
        except ValueError:
            meta = {}
        cid = it.get("id") or ""
        if cid:
            out[cid] = CtrMeta(cid, (it.get("names") or [cid[:12]])[0], meta.get("image-name", ""))
    return out


def parse_arp(text: str) -> list[tuple[str, str, str]]:
    """``/proc/net/arp`` → (ip, mac, device) of complete entries (flag 0x2)."""
    out = []
    for line in text.splitlines()[1:]:
        f = line.split()
        try:
            if len(f) >= 6 and int(f[2], 16) & 0x2 and f[3] != "00:00:00:00:00:00":
                out.append((f[0], f[3], f[5]))
        except ValueError:
            continue
    return out


def default_gateways(text: str) -> set[str]:
    """IPv4 default gateways from ``/proc/net/route`` (little-endian hex)."""
    out = set()
    for line in text.splitlines()[1:]:
        f = line.split()
        try:
            if len(f) >= 4 and f[1] == "00000000" and int(f[3], 16) & 0x2:  # RTF_GATEWAY
                out.add(socket.inet_ntoa(struct.pack("<I", int(f[2], 16))))
        except (ValueError, struct.error):
            continue
    return out


def parse_keyvals(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def netdev_bytes(text: str) -> tuple[int, int] | None:
    """``/proc/<pid>/net/dev`` → (rx, tx) bytes summed over every interface but ``lo``."""
    rx = tx = 0
    seen = False
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        f = rest.split()
        if name.strip() == "lo" or len(f) < 9:
            continue
        try:
            rx += int(f[0])
            tx += int(f[8])
            seen = True
        except ValueError:
            continue
    return (rx, tx) if seen else None


def scope_of(cgroup: str, needle: str) -> str:
    """The cgroup path down to the component containing ``needle``. For a container that is its
    ``libpod-<id>.scope``, not podman 5's nested ``…/container`` leaf, which undercounts."""
    parts = cgroup.split("/")
    for i, c in enumerate(parts):
        if needle and needle in c:
            return "/".join(parts[:i + 1])
    return ""


def unit_of(cgroup: str) -> str:
    """The innermost systemd unit in a cgroup path: ``app-…-1234.scope``, ``foo.service``."""
    for c in reversed(cgroup.split("/")):
        if c.endswith((".scope", ".service")):
            return c
    return cgroup.rsplit("/", 1)[-1]


def parse_cpu_max(text: str | None) -> float | None:
    """cgroup ``cpu.max`` (``"200000 100000"`` or ``"max 100000"``) → cores, None if unlimited."""
    f = (text or "").split()
    try:
        return int(f[0]) / int(f[1]) if len(f) == 2 and f[0] != "max" else None
    except (ValueError, ZeroDivisionError):
        return None


def qemu_name(argv: list[str]) -> str:
    """Guest name from ``-name guest=win11,debug-threads=on`` or ``-name win11``."""
    for i, a in enumerate(argv[:-1]):
        if a == "-name":
            v = argv[i + 1]
            for part in v.split(","):
                if part.startswith("guest="):
                    return part[6:]
            return v.split(",")[0]
    return ""


def classify(ip: str, own: set[str], nets, ctr_ips: dict[str, str]) -> tuple[str, str]:
    """Where a remote address lives: ("local" | "container" | "peer" | "wan", container id)."""
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return "wan", ""
    if a.is_loopback or a.is_unspecified or ip in own:
        return "local", ""
    if ip in ctr_ips:
        return "container", ctr_ips[ip]
    if a.is_link_local or any(a in n for n in nets if n.version == a.version):
        return "peer", ""
    return "wan", ""


def local_addrs() -> tuple[set[str], list]:
    """This host's own addresses, and the networks of its LAN-facing interfaces."""
    import psutil

    own: set[str] = set()
    nets = []
    try:
        addrs = psutil.net_if_addrs()
    except OSError:
        return own, nets
    for name, lst in addrs.items():
        for a in lst:
            if a.family not in (socket.AF_INET, socket.AF_INET6):
                continue
            ip = a.address.split("%")[0]
            own.add(ip)
            if name.startswith(BRIDGES) or not a.netmask:
                continue
            try:
                bits = bin(int(ipaddress.ip_address(a.netmask))).count("1")
                net = ipaddress.ip_network(f"{ip}/{bits}", strict=False)
            except ValueError:
                continue
            if not net.is_link_local:
                nets.append(net)
    return own, nets


# ------------------------------------------------------------------------- container API
class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self.unix_path)
        except OSError:
            s.close()
            raise
        self.sock = s


def api_containers(sock_path: str, timeout: float = 0.5):
    """Running containers from a docker-compatible API socket. Raises OSError/ValueError."""
    c = _UnixHTTP(sock_path, timeout)
    try:
        c.request("GET", "/containers/json")
        r = c.getresponse()
        body = r.read()
        if r.status != 200:
            raise OSError(f"HTTP {r.status}")
        return json.loads(body)
    finally:
        c.close()


class _Rdns:
    """Reverse DNS on two background threads. ``name`` never blocks: "" until the answer is in."""

    def __init__(self) -> None:
        self.pool: ThreadPoolExecutor | None = None
        self.cache: dict[str, str | None] = {}

    def name(self, ip: str) -> str:
        if ip not in self.cache:
            self.cache[ip] = None
            if self.pool is None:
                self.pool = ThreadPoolExecutor(2, thread_name_prefix="systui-rdns")
            try:
                self.pool.submit(self._lookup, ip)
            except RuntimeError:  # shut down
                pass
        return self.cache.get(ip) or ""

    def _lookup(self, ip: str) -> None:
        try:
            self.cache[ip] = socket.gethostbyaddr(ip)[0]
        except (OSError, UnicodeError):
            self.cache[ip] = ""

    def close(self) -> None:
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)


# ------------------------------------------------------------------------- the walk
@dataclass
class _Proc:
    pid: int
    comm: str = ""
    uid: int = -1
    ctr: str = ""  # 64-hex container id, or "lxc:<name>"
    engine: str = ""  # podman | docker | lxc
    cgroup: str = ""
    tun: list[str] = field(default_factory=list)  # tap interfaces behind /dev/net/tun fds
    nfd: int = 0


@dataclass
class _Guest:
    nid: str
    kind: str  # container | vm
    name: str
    runtime: str
    pids: list[int]
    leader: int | None = None
    netns: int | None = None  # set when it has its own netns (not the host's)
    meta: CtrMeta | None = None
    iface: str = ""
    sub: str = ""
    socks: list[InetSock] = field(default_factory=list)  # its own netns' sockets
    cgroup: str = ""  # its scope under /sys/fs/cgroup


@dataclass
class _Walk:
    t: float
    procs: dict[int, _Proc]
    socks: dict[int, list[int]]  # socket inode → pids
    pipes: dict[int, tuple[set[int], set[int]]]  # pipe inode → (writers, readers)
    hidden: int
    unix: list[UnixSock]
    guests: dict[str, _Guest]
    pid_guest: dict[int, str]
    own: set[str]
    nets: list
    arp: list[tuple[str, str, str]]
    gateways: set[str]
    unowned: set[int]  # inet inodes without a readable owner at walk time
    notes: list[str]


def _read(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read().decode(errors="replace")
    except OSError:
        return ""


def _ns_ino(path: str) -> int | None:
    try:
        t = os.readlink(path)  # "net:[4026531833]"
        return int(t[t.index("[") + 1:-1])
    except (OSError, ValueError):
        return None


def _rep(pids) -> int | None:
    return min(pids) if pids else None


class IpcTracker:
    def __init__(self, fs: SysFS, diag=sockdiag, api=api_containers, addrs=local_addrs, resolve: bool = True,
                 every: int = EVERY) -> None:
        self.fs, self.diag, self.api, self.addrs, self.every = fs, diag, api, addrs, every
        self.rdns = _Rdns() if resolve else None
        self.rates = Rates()
        self.n = 0
        self.walk: _Walk | None = None
        self._last = -1e9
        self.ctrs: dict[str, CtrMeta] = {}
        self.api_note = ""
        self._ctr_ids: frozenset[str] = frozenset()
        self._api_t = -1e9
        self.uid = int(os.environ.get("SUDO_UID") or os.getuid())
        self.self_pid = os.getpid()
        self._cmd: dict[int, str] = {}
        self._users: dict[int, str] = {}
        self.events: deque[IpcEvent] = deque(maxlen=EVENTS_KEEP)
        self._seen: dict[tuple, tuple] | None = None  # event key → (verb pair, kind, a, b, end, names)
        self._peers_seen: set[str] = set()
        self.clock = time.time  # event timestamps

    def close(self) -> None:
        if self.rdns is not None:
            self.rdns.close()

    # --------------------------------------------------------------------- polling
    def collect(self, now: float | None = None) -> IpcStat:
        now = time.monotonic() if now is None else now
        fresh = self.walk is None or now - self._last > STALE_S  # nothing to diff events against
        full = fresh or self.n % self.every == 0
        self.n = 1 if full else self.n + 1
        self._last = now
        inet, note = self._inet()
        if full:
            self.walk = self._full(now, inet)
        stat = self._assemble(self.walk, inet, now)
        self._diff(stat, fresh)
        stat.events = list(self.events)
        if note:
            stat.note = "  ·  ".join(x for x in (note, stat.note) if x)
        return stat

    def _inet(self) -> tuple[list[InetSock], str]:
        try:
            return self.diag.all_inet(), ""
        except OSError:
            out: list[InetSock] = []
            for name in ("tcp", "tcp6", "udp", "udp6"):
                out += sockdiag.proc_inet(self.fs.read(f"proc/net/{name}") or "", name[:3])
            return out, "no sock_diag: TCP rates unavailable"

    def _full(self, now: float, inet: list[InetSock]) -> _Walk:
        procs, socks, pipes, hidden = self._walk_procs()
        notes = []
        try:
            unix = self.diag.unix()
        except OSError:
            unix = sockdiag.proc_unix(self.fs.read("proc/net/unix") or "")
            notes.append("no sock_diag: Unix peers unknown")
        if hidden:
            notes.append(f"fds of {hidden} processes unreadable (other users, or protected like the compositor):"
                         " ./systui-root shows them")
        ids = frozenset(p.ctr for p in procs.values() if p.ctr and not p.ctr.startswith("lxc:"))
        if ids and (ids != self._ctr_ids or now - self._api_t > API_REFRESH_S):
            self.ctrs = self._containers()
            self._api_t = now
        self._ctr_ids = ids
        if self.api_note:
            notes.append(self.api_note)
        own, nets = self.addrs()
        guests, pid_guest = self._guests(procs)
        unowned = {s.ino for s in inet if s.ino and s.ino not in socks}
        alive = set(procs)
        for k in [k for k in self._cmd if k not in alive]:
            del self._cmd[k]
        return _Walk(now, procs, socks, pipes, hidden, unix, guests, pid_guest, own, nets,
                     parse_arp(self.fs.read("proc/net/arp") or ""), default_gateways(self.fs.read("proc/net/route") or ""),
                     unowned, notes)

    def _walk_procs(self):
        procs: dict[int, _Proc] = {}
        socks: dict[int, list[int]] = {}
        holders: dict[int, list[tuple[int, str]]] = {}
        hidden = 0
        try:
            it = os.scandir(self.fs.p("proc"))
        except OSError:
            return procs, socks, {}, 0
        with it:
            for e in it:
                if not e.name.isdigit():
                    continue
                pid = int(e.name)
                if pid == self.self_pid:
                    continue
                base = e.path
                p = _Proc(pid, _read(base + "/comm").strip())
                try:
                    p.uid = e.stat(follow_symlinks=False).st_uid
                except OSError:
                    continue
                cg = _read(base + "/cgroup")
                p.cgroup = cg.strip().splitlines()[-1].split(":", 2)[-1] if cg.strip() else ""
                m = CTR_CGROUP.search(cg)
                if m:
                    p.ctr, p.engine = m.group(2), "docker" if "docker" in m.group(1) else "podman"
                elif (m := LXC_CGROUP.search(cg)):
                    p.ctr, p.engine = f"lxc:{m.group(1)}", "lxc"
                procs[pid] = p
                try:
                    fds = os.scandir(base + "/fd")
                except OSError:
                    if not p.ctr and not _kthread(base):
                        hidden += 1
                    continue
                tun = []
                with fds:
                    for f in fds:
                        p.nfd += 1
                        try:
                            t = os.readlink(f.path)
                        except OSError:
                            continue
                        if t.startswith("socket:["):
                            socks.setdefault(int(t[8:-1]), []).append(pid)
                        elif t.startswith("pipe:["):
                            holders.setdefault(int(t[6:-1]), []).append((pid, f.name))
                        elif t == "/dev/net/tun":
                            tun.append(f.name)
                for fd in tun:
                    for line in _read(f"{base}/fdinfo/{fd}").splitlines():
                        if line.startswith("iff:"):
                            p.tun.append(line.split()[-1])
        pipes: dict[int, tuple[set[int], set[int]]] = {}
        for ino, hs in holders.items():
            if len({pid for pid, _ in hs}) < 2:
                continue  # a process's own wake-up pipe
            w, r = set(), set()
            for pid, fd in hs:
                mode = 0
                for line in _read(f"{self.fs.p('proc')}/{pid}/fdinfo/{fd}").splitlines():
                    if line.startswith("flags:"):
                        try:
                            mode = int(line.split()[1], 8) & 3
                        except (IndexError, ValueError):
                            pass
                        break
                if mode in (1, 2):
                    w.add(pid)
                if mode in (0, 2):
                    r.add(pid)
            pipes[ino] = (w, r)
        return procs, socks, pipes, hidden

    def _containers(self) -> dict[str, CtrMeta]:
        out: dict[str, CtrMeta] = {}
        errors = []
        for rel, engine in ((f"run/user/{self.uid}/podman/podman.sock", "podman"), ("run/podman/podman.sock", "podman"),
                            ("var/run/docker.sock", "docker")):
            path = self.fs.p(rel)
            if not path.exists():
                continue
            try:
                out.update(parse_containers(self.api(str(path)), engine))
            except (OSError, ValueError) as e:
                errors.append(f"{rel.split('/')[-1]}: {e}")
        if not out:
            try:
                home = pwd.getpwuid(self.uid).pw_dir
            except KeyError:
                home = ""
            if home:
                out = parse_storage(self.fs.read(f"{home}/.local/share/containers/storage/overlay-containers/containers.json") or "")
        self.api_note = f"container API: {errors[0]}" if errors and not out else ""
        return out

    def _guests(self, procs: dict[int, _Proc]) -> tuple[dict[str, _Guest], dict[int, str]]:
        """Containers (cgroups + API), machined machines and QEMU guests, with their pids and netns."""
        guests: dict[str, _Guest] = {}
        pid_guest: dict[int, str] = {}
        host_ns = _ns_ino(str(self.fs.p("proc/self/ns/net")))
        by_ctr: dict[str, list[int]] = {}
        for p in procs.values():
            if p.ctr:
                by_ctr.setdefault(p.ctr, []).append(p.pid)
        for cid, pids in by_ctr.items():
            meta = self.ctrs.get(cid)
            lxc = cid.startswith("lxc:")
            nid = f"ctr:{cid[4:] if lxc else cid[:12]}"
            runtime = meta.runtime if meta else procs[pids[0]].engine
            g = _Guest(nid, "container", meta.name if meta else (cid[4:] if lxc else cid[:12]), runtime, sorted(pids),
                       min(pids), meta=meta, sub=meta.image if meta else "")
            g.cgroup = scope_of(procs[min(pids)].cgroup, f"lxc.payload.{cid[4:]}" if lxc else cid)
            guests[nid] = g
        for path in self.fs.glob("run/systemd/machines/*"):
            if ":" in path.name:
                continue
            kv = parse_keyvals(self.fs.read(path) or "")
            scope = kv.get("SCOPE", "")
            vm = kv.get("CLASS") == "vm"
            nid = f"vm:{path.name}"
            pids = sorted(p.pid for p in procs.values() if scope and p.cgroup.endswith("/" + scope) or
                          scope and f"/{scope}/" in p.cgroup)
            try:
                leader = int(kv.get("LEADER", ""))
            except ValueError:
                leader = pids[0] if pids else None
            guests[nid] = _Guest(nid, "vm" if vm else "container", path.name, "vm" if vm else "nspawn",
                                 pids or ([leader] if leader else []), leader, sub=kv.get("SERVICE", ""),
                                 cgroup=scope_of(procs[pids[0]].cgroup, scope) if pids else "")
        for p in procs.values():
            if not (p.comm.startswith("qemu-system") or p.comm.startswith("qemu-kvm")):
                continue
            owner = next((g for g in guests.values() if p.pid in g.pids), None)
            if owner is None:
                argv = _read(f"{self.fs.p('proc')}/{p.pid}/cmdline").split("\0")
                name = qemu_name(argv) or f"qemu-{p.pid}"
                owner = guests.setdefault(f"vm:{name}", _Guest(f"vm:{name}", "vm", name, "qemu", [p.pid], p.pid))
            owner.runtime = "qemu"
            owner.iface = owner.iface or (p.tun[0] if p.tun else "")
        for g in guests.values():
            for pid in g.pids:
                pid_guest.setdefault(pid, g.nid)
            if g.kind == "container" and g.leader is not None:
                ns = _ns_ino(f"{self.fs.p('proc')}/{g.leader}/ns/net")
                if ns is not None and ns != host_ns:
                    g.netns = ns
        seen_ns: set[int] = set()
        for g in sorted(guests.values(), key=lambda g: g.name):
            if g.netns is None or g.netns in seen_ns:
                continue  # pods share one netns: read it once
            seen_ns.add(g.netns)
            for name in ("tcp", "tcp6", "udp", "udp6"):
                g.socks += sockdiag.proc_inet(self.fs.read(f"proc/{g.leader}/net/{name}") or "", name[:3])
        return guests, pid_guest

    # --------------------------------------------------------------------- graph
    def _user(self, uid: int) -> str:
        u = self._users.get(uid)
        if u is None:
            try:
                u = pwd.getpwuid(uid).pw_name
            except (KeyError, OverflowError):
                u = str(uid)
            self._users[uid] = u
        return u

    def _cmdline(self, pid: int) -> str:
        c = self._cmd.get(pid)
        if c is None:
            c = self._cmd[pid] = _read(f"{self.fs.p('proc')}/{pid}/cmdline").replace("\0", " ").strip()[:200]
        return c

    def _assemble(self, w: _Walk, inet: list[InetSock], now: float) -> IpcStat:
        nodes: dict[str, IpcNode] = {}
        links: list[IpcLink] = []
        touched: set[str] = set()

        def rate(key: str, value: int | None) -> float | None:
            if value is None:
                return None
            touched.add(key)
            return self.rates.rate(key, value, now)

        nodes["host"] = IpcNode("host", "host", socket.gethostname(),
                                addrs=sorted(a for a in w.own if not ipaddress.ip_address(a).is_loopback
                                             and not a.startswith("fe80")))
        ctr_ips: dict[str, str] = {}  # ip → guest node id
        published: dict[tuple[str, int], str] = {}  # (proto, host port) → guest node id
        for g in w.guests.values():
            m = g.meta
            n = IpcNode(g.nid, g.kind, g.name, g.sub, g.runtime, m.status if m else "", list(m.ips) if m else [],
                        g.pids[:64], g.netns, [f"{h}:{hp}→{cp}/{pr}" for h, hp, cp, pr in m.ports] if m else [],
                        iface=g.iface)
            nodes[g.nid] = n
            if g.cgroup:
                self._cgroup(n, g.cgroup, rate)
            n.started = m.created if m else None
            for ip in n.addrs:
                ctr_ips[ip] = g.nid
            for _h, hp, _cp, pr in (m.ports if m else []):
                published[(pr, hp)] = g.nid
            # interface counters: ▲ is what the guest sends towards this host
            if g.netns is not None and g.leader is not None:
                b = netdev_bytes(self.fs.read(f"proc/{g.leader}/net/dev") or "")
                if b:
                    n.out_bps, n.in_bps = rate(f"{g.nid}:rx", b[0]), rate(f"{g.nid}:tx", b[1])
            elif g.iface:
                st = f"sys/class/net/{g.iface}/statistics"
                n.in_bps = rate(f"{g.nid}:rx", self.fs.read_int(f"{st}/rx_bytes"))
                n.out_bps = rate(f"{g.nid}:tx", self.fs.read_int(f"{st}/tx_bytes"))
        gateways = {gw for g in w.guests.values() if g.meta for gw in g.meta.gateways}
        for ip, mac, dev in w.arp:
            if dev.startswith(BRIDGES) or ip in ctr_ips:
                continue
            nodes[f"peer:{ip}"] = IpcNode(f"peer:{ip}", "peer", ip, mac, addrs=[ip], router=ip in w.gateways, iface=dev)

        def pnode(pid: int | None) -> str:
            if pid is None:
                if "hidden" not in nodes:
                    nodes["hidden"] = IpcNode("hidden", "process", "unreadable", "other users' or protected processes",
                                             prov="derived")
                return "hidden"
            gid = w.pid_guest.get(pid)
            if gid:
                return gid
            nid = f"proc:{pid}"
            if nid not in nodes:
                p = w.procs.get(pid)
                nodes[nid] = IpcNode(nid, "process", p.comm if p else str(pid), self._user(p.uid) if p else "",
                                     pids=[pid], cmd=self._cmdline(pid), fds=p.nfd if p else None,
                                     unit=unit_of(p.cgroup) if p else "")
            return nid

        def remote(kind: str, ip: str, cid: str) -> str:
            if kind == "container" and cid:
                return cid
            if kind == "peer":
                nid = f"peer:{ip}"
                if nid not in nodes:
                    nodes[nid] = IpcNode(nid, "peer", ip, addrs=[ip], router=ip in w.gateways)
                return nid
            if "wan" not in nodes:
                nodes["wan"] = IpcNode("wan", "wan", "Internet")
            return "wan"

        agg: dict[tuple, IpcLink] = {}

        def fold(a: str, b: str, kind: str, pa, pb, b_end: str, state: str, ino: int) -> None:
            key = (a, b, kind, pa, pb, b_end)
            k = agg.get(key)
            if k is None:
                agg[key] = IpcLink(a, b, kind, "", b_end, pa, pb, state, ino=ino)
            else:
                k.count += 1

        # ---- Unix sockets: the end carrying the listener's path is the server
        by_ino = {u.ino: u for u in w.unix}
        for u in w.unix:
            if not u.peer:
                continue
            v = by_ino.get(u.peer)
            if v is not None and u.ino > u.peer:
                continue
            pu, pv = _rep(w.socks.get(u.ino)), _rep(w.socks.get(u.peer))
            if pu == pv:
                continue
            path = u.path or (v.path if v else "")
            (cp, sp) = (pv, pu) if u.path and not (v and v.path) else (pu, pv)
            a, b = pnode(cp), pnode(sp)
            if a != b:
                fold(a, b, "unix", cp, sp, path, u.state, u.ino)
        # ---- pipes: writer → reader
        for ino, (wr, rd) in w.pipes.items():
            pw, pr = max(wr - rd or wr, default=None), max(rd - wr or rd, default=None)
            if pw is None or pr is None or pw == pr:
                continue
            a, b = pnode(pw), pnode(pr)
            if a != b:
                fold(a, b, "pipe", pw, pr, "", "", ino)
        links += agg.values()

        # ---- TCP / UDP in this netns
        listen = {(s.proto, s.src, s.sport) for s in inet if s.state == "LISTEN" or (s.proto == "udp" and not s.dport)}

        def serving(s: InetSock) -> bool:
            return any((s.proto, ip, s.sport) in listen for ip in (s.src, "0.0.0.0", "::"))

        by_tuple = {(s.proto, s.src, s.sport, s.dst, s.dport): s for s in inet}
        guest_remote: dict[tuple[str, str, int], str] = {}  # remote end of a container-netns socket → guest
        for g in w.guests.values():
            for s in g.socks:
                if s.dport:
                    guest_remote[(s.proto, s.dst, s.dport)] = g.nid
        via_helper: set[tuple[str, str, int]] = set()
        for s in inet:
            if not s.ino or s.state == "TIME-WAIT":
                continue
            pids = w.socks.get(s.ino)
            if pids is None and s.ino not in w.unowned:
                continue  # opened since the last walk; it shows up after the next one
            owner = _rep(pids)
            nid, prov = pnode(owner), "live"
            p = w.procs.get(owner) if owner is not None else None
            if p is not None and p.comm.startswith(HELPERS):
                gid = published.get((s.proto, s.sport))
                if gid is None and s.dport and (s.proto, s.dst, s.dport) in guest_remote:
                    gid = guest_remote[(s.proto, s.dst, s.dport)]
                    via_helper.add((s.proto, s.dst, s.dport))
                if gid:
                    nid, prov = gid, "derived"
            end = _hp(s.src, s.sport)
            if s.state == "LISTEN" or (s.proto == "udp" and not s.dport):
                links.append(IpcLink(nid, "", "listen", end, "", owner, None, s.state, prov=prov, ino=s.ino))
                continue
            rx, tx = rate(f"{s.ino}:rx", s.received), rate(f"{s.ino}:tx", s.acked)
            kind, cid = classify(s.dst, w.own, w.nets, ctr_ips)
            peer_pid = None
            if kind == "local":
                twin = by_tuple.get((s.proto, s.dst, s.dport, s.src, s.sport))
                if twin is not None:
                    if serving(s) and not serving(twin):
                        continue  # the client side reports it
                    if serving(s) == serving(twin) and (s.src, s.sport) > (s.dst, s.dport):
                        continue
                    peer_pid = _rep(w.socks.get(twin.ino))
                    b = pnode(peer_pid)
                    tp = w.procs.get(peer_pid) if peer_pid is not None else None
                    if tp is not None and tp.comm.startswith(HELPERS) and (s.proto, s.dport) in published:
                        b, prov = published[(s.proto, s.dport)], "derived"
                elif (s.proto, s.dport) in published:
                    b, prov = published[(s.proto, s.dport)], "derived"
                else:
                    b = pnode(None)
            else:
                b = remote(kind, s.dst, cid)
            if b == nid:
                continue
            links.append(IpcLink(nid, b, s.proto, end, _hp(s.dst, s.dport), owner, peer_pid, s.state, rx, tx,
                                 prov=prov, ino=s.ino))

        # ---- sockets inside container netns (no byte counts there)
        pairs: set[frozenset] = set()
        for g in w.guests.values():
            mine = {(s.proto, s.src, s.sport) for s in g.socks if s.state == "LISTEN" or (s.proto == "udp" and not s.dport)}
            for s in g.socks:
                if s.state == "TIME-WAIT":
                    continue
                end = _hp(s.src, s.sport)
                if (s.proto, s.src, s.sport) in mine:
                    links.append(IpcLink(g.nid, "", "listen", end, "", state=s.state, ino=s.ino))
                    continue
                if (s.proto, s.dst, s.dport) in via_helper:
                    continue  # already drawn, with bytes, through the rootless helper
                other = ctr_ips.get(s.dst)
                if other == g.nid:
                    continue
                if other is not None:
                    key = frozenset({(s.src, s.sport), (s.dst, s.dport)})
                    if key in pairs:
                        continue
                    pairs.add(key)
                    server = any((s.proto, ip, s.sport) in mine for ip in (s.src, "0.0.0.0", "::"))
                    a, b = (other, g.nid) if server else (g.nid, other)
                    links.append(IpcLink(a, b, s.proto, end if a == g.nid else _hp(s.dst, s.dport),
                                         _hp(s.dst, s.dport) if a == g.nid else end, state=s.state, ino=s.ino))
                    continue
                kind, _ = classify(s.dst, set(), w.nets, {})
                if kind == "local":
                    continue  # loopback inside the container
                ip = ipaddress.ip_address(s.dst.split("%")[0])
                if s.dst in gateways or ip.is_link_local or (kind == "wan" and ip.is_private):
                    b, prov = "host", "derived"  # the bridge gateway, pasta's host mapping, or a guest we can't name
                else:
                    b, prov = remote(kind, s.dst, ""), "live"
                links.append(IpcLink(g.nid, b, s.proto, end, _hp(s.dst, s.dport), state=s.state, prov=prov, ino=s.ino))

        # ---- host-wide health: kernel counters and TCP states
        snmp = netstat.parse_snmp(self.fs.read("proc/net/snmp") or "")
        snmp |= netstat.parse_snmp(self.fs.read("proc/net/netstat") or "")
        kernel: dict[str, float | None] = {c: rate(f"k:{c}", snmp.get(c)) for c in netstat.COUNTERS}
        kernel["conntrack"] = self.fs.read_int("proc/sys/net/netfilter/nf_conntrack_count")
        kernel["conntrack_max"] = self.fs.read_int("proc/sys/net/netfilter/nf_conntrack_max")
        states: dict[str, int] = {}
        for s in inet:
            if s.proto == "tcp":
                states[s.state] = states.get(s.state, 0) + 1

        for k in [k for k in self.rates.prev if k not in touched]:
            del self.rates.prev[k]
        self._rollup(nodes, links)
        counts: dict[str, int] = {}
        for k in links:
            counts[k.kind] = counts.get(k.kind, 0) + k.count
        return IpcStat(list(nodes.values()), links, counts, "  ·  ".join(w.notes), now - w.t, kernel, states)

    def _diff(self, stat: IpcStat, fresh: bool) -> None:
        """Log what appeared and went away since the last poll. Sockets are keyed on their
        addresses, not on node ids: a new socket's owner is only known after the next full
        walk, and keying on it would log a fake close and reopen. Unix sockets and pipes are
        left out; a desktop churns through them constantly."""
        names = {n.id: n.name for n in stat.nodes}
        cur: dict[tuple, tuple] = {}
        for k in stat.links:
            if k.kind in ("tcp", "udp"):
                cur[(k.kind, k.a_end, k.b_end)] = (("opened", "closed"), k.kind, k.a, k.b, k.b_end)
            elif k.kind == "listen":
                cur[("listen", k.a_end, k.ino)] = (("listening", "stopped listening"), "listen", k.a, "", k.a_end)
        for n in stat.nodes:
            if n.kind in ("container", "vm"):
                cur[(n.kind, n.id)] = (("started", "stopped"), n.kind, n.id, "", "")
        cur = {key: (*v, names.get(v[2], v[2]), names.get(v[3], v[3])) for key, v in cur.items()}
        t = self.clock()
        peers = {n.id: n for n in stat.nodes if n.kind == "peer"}
        if not fresh and self._seen is not None:
            for key in cur.keys() - self._seen.keys():
                (verb, _), kind, a, b, end, na, nb = cur[key]
                self.events.append(IpcEvent(t, verb, kind, a, b, end, na, nb))
            for key in self._seen.keys() - cur.keys():
                (_, verb), kind, a, b, end, na, nb = self._seen[key]
                self.events.append(IpcEvent(t, verb, kind, a, b, end, na, nb))
            for nid in peers.keys() - self._peers_seen:
                n = peers[nid]
                self.events.append(IpcEvent(t, "new peer", "peer", nid, "", n.addrs[0] if n.addrs else "", n.name))
        self._seen = cur
        self._peers_seen |= peers.keys()

    def _cgroup(self, n: IpcNode, path: str, rate) -> None:
        """A guest's CPU, memory and limits from its cgroup scope, read every poll."""
        base = f"sys/fs/cgroup{path}"
        usec = None
        for line in (self.fs.read(f"{base}/cpu.stat") or "").splitlines():
            if line.startswith("usage_usec "):
                usec = int(line.split()[1])
        per_s = rate(f"cg:{n.id}:cpu", usec)
        n.cpu = per_s / 1e4 if per_s is not None else None  # µs of CPU per second → % of one thread
        n.rss = self.fs.read_int(f"{base}/memory.current")
        n.mem_limit = self.fs.read_int(f"{base}/memory.max")  # "max" → None
        n.cpu_limit = parse_cpu_max(self.fs.read(f"{base}/cpu.max"))
        n.pids_n = self.fs.read_int(f"{base}/pids.current")
        n.unit = unit_of(path)

    def _rollup(self, nodes: dict[str, IpcNode], links: list[IpcLink]) -> None:
        """Node rates from their links; reverse-DNS names for peers."""
        rollup(nodes, links)
        if self.rdns is not None:
            for n in nodes.values():
                if n.kind == "peer":
                    n.dns = self.rdns.name(n.addrs[0]) if n.addrs else ""
                    if n.dns:  # systemd-resolved answers "_gateway" for the default route
                        n.name = "gateway" if n.dns.startswith("_gateway") else n.dns.split(".")[0]
                        n.prov = "derived"


def rollup(nodes: dict[str, IpcNode], links: list[IpcLink]) -> None:
    """Fill ``in_bps``/``out_bps`` from the links of every node an interface counter didn't cover.
    A process's ▲ is what it receives. A guest, peer or the Internet's ▲ is what it sends
    towards this host."""
    sums: dict[str, list[float]] = {}
    for k in links:
        if k.in_bps is None and k.out_bps is None:
            continue
        i, o = k.in_bps or 0.0, k.out_bps or 0.0
        for nid, into, out_of in ((k.a, i, o), (k.b, o, i)):
            n = nodes.get(nid)
            if n is None:
                continue
            acc = sums.setdefault(nid, [0.0, 0.0])
            up, down = (into, out_of) if n.kind == "process" else (out_of, into)
            acc[0] += up
            acc[1] += down
    for nid, (i, o) in sums.items():
        n = nodes.get(nid)
        if n is not None and n.in_bps is None and n.out_bps is None and n.kind != "host":
            n.in_bps, n.out_bps = i, o


def _hp(ip: str, port: int) -> str:
    return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"


def _kthread(base: str) -> bool:
    """Kernel threads (children of kthreadd) have no fds worth hiding."""
    stat = _read(base + "/stat")
    try:
        ppid = int(stat[stat.rindex(")") + 2:].split()[1])
    except (ValueError, IndexError):
        return False
    return ppid in (0, 2) and not _read(base + "/cmdline")
