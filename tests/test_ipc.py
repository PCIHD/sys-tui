"""IPC collector against fake /proc trees, with sock_diag and the container API injected."""

import ipaddress
import os
import socket
import struct

import pytest

from systui.history import History
from systui.telemetry import IpcLink, IpcNode, IpcStat, SysFS, ipc_dict
from systui.telemetry import ipc as I
from systui.telemetry import sockdiag as D
from systui.telemetry.demo import DemoSource
from systui.telemetry.sockdiag import InetSock, UnixSock

CTR_A = "df517a10478819db8fbf49125f41b5e49e2bced7d7af778f3ed06f54f49a9062"
CTR_B = "44254bc720fd1111111111111111111111111111111111111111111111111111"
LAN = [ipaddress.ip_network("192.168.1.0/24")]


def _w(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _proc(root, pid, comm, fds=None, flags=None, cgroup="0::/user.slice/app.scope", ns=1, cmdline=""):
    base = f"proc/{pid}"
    _w(root, f"{base}/comm", comm + "\n")
    _w(root, f"{base}/cgroup", cgroup + "\n")
    _w(root, f"{base}/cmdline", cmdline)
    _w(root, f"{base}/stat", f"{pid} ({comm}) S 1 {pid}\n")
    (root / base / "fd").mkdir(parents=True, exist_ok=True)
    for fd, target in (fds or {}).items():
        os.symlink(target, root / base / "fd" / str(fd))
    for fd, text in (flags or {}).items():
        _w(root, f"{base}/fdinfo/{fd}", text if "\n" in text else f"pos:\t0\nflags:\t{text}\n")
    (root / base / "ns").mkdir(exist_ok=True)
    os.symlink(f"net:[{ns}]", root / base / "ns" / "net")


def _hex4(ip, port):
    return f"{socket.inet_aton(ip)[::-1].hex().upper()}:{port:04X}"


def _tcp_table(rows):
    head = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    body = "".join(f"   {i}: {_hex4(s, sp)} {_hex4(d, dp)} {st:02X} 00000000:00000000 00:00000000 00000000"
                   f"   999        0 {ino} 1 0000000000000000 100 0 0 10 0\n"
                   for i, (s, sp, d, dp, st, ino) in enumerate(rows))
    return head + body


class FakeDiag:
    def __init__(self, unix=(), inet=()):
        self.unix_socks, self.inet_socks = list(unix), list(inet)

    def unix(self):
        return list(self.unix_socks)

    def all_inet(self):
        return list(self.inet_socks)


@pytest.fixture
def root(tmp_path):
    (tmp_path / "proc/self/ns").mkdir(parents=True)
    os.symlink("net:[1]", tmp_path / "proc/self/ns/net")
    return tmp_path


def tracker(root, diag, api=None, own=("192.168.1.10",)):
    t = I.IpcTracker(SysFS(root), diag=diag, api=api or (lambda path: []),
                     addrs=lambda: (set(own), list(LAN)), resolve=False)
    t.uid = 1000
    return t


def link(stat, kind, a=None, b=None):
    return [k for k in stat.links if k.kind == kind and (a is None or k.a == a) and (b is None or k.b == b)]


# ------------------------------------------------------------------------- pipes / unix
def test_pipes_run_writer_to_reader_and_drop_self_pipes(root):
    _proc(root, 100, "bash", {1: "pipe:[500]"}, {1: "01"})
    _proc(root, 101, "grep", {0: "pipe:[500]"}, {0: "00"})
    _proc(root, 102, "loop", {3: "pipe:[600]", 4: "pipe:[600]"}, {3: "00", 4: "01"})  # its own wake-up pipe
    # an inherited pipe where both processes hold both ends: no direction to draw
    _proc(root, 103, "parent", {3: "pipe:[700]", 4: "pipe:[700]"}, {3: "00", 4: "01"})
    _proc(root, 104, "child", {3: "pipe:[700]", 4: "pipe:[700]"}, {3: "00", 4: "01"})
    s = tracker(root, FakeDiag()).collect(0.0)
    pipes = link(s, "pipe")
    assert [(k.a, k.b) for k in pipes] == [("proc:100", "proc:101")]
    assert s.node("proc:100").name == "bash"


def test_unix_server_end_carries_the_path_and_pairs_fold(root):
    _proc(root, 200, "client", {5: "socket:[700]", 6: "socket:[710]", 7: "socket:[712]", 8: "socket:[720]"})
    _proc(root, 201, "dbus-broker", {6: "socket:[701]"})
    _proc(root, 202, "worker", {3: "socket:[711]", 4: "socket:[713]"})
    diag = FakeDiag(unix=[
        UnixSock(700, 701, "stream", "ESTAB", ""), UnixSock(701, 700, "stream", "ESTAB", "/run/user/1000/bus"),
        UnixSock(710, 711, "stream", "ESTAB", ""), UnixSock(711, 710, "stream", "ESTAB", ""),  # socketpairs
        UnixSock(712, 713, "stream", "ESTAB", ""), UnixSock(713, 712, "stream", "ESTAB", ""),
        UnixSock(720, 721, "stream", "ESTAB", ""), UnixSock(721, 720, "stream", "ESTAB", "/run/systemd/journal/stdout"),
    ])
    s = tracker(root, diag).collect(0.0)
    bus = link(s, "unix", b="proc:201")
    assert len(bus) == 1 and bus[0].a == "proc:200" and bus[0].b_end == "/run/user/1000/bus"
    pairs = [k for k in link(s, "unix") if {k.a, k.b} == {"proc:200", "proc:202"}]
    assert len(pairs) == 1 and pairs[0].count == 2
    journal = link(s, "unix", b="hidden")  # socket 721's owner is unreadable
    assert journal and journal[0].b_end == "/run/systemd/journal/stdout"
    assert s.node("hidden").name == "unreadable"


def test_unreadable_processes_are_counted_in_the_note(root):
    _proc(root, 300, "kwin_wayland")
    os.chmod(root / "proc/300/fd", 0)
    s = tracker(root, FakeDiag()).collect(0.0)
    os.chmod(root / "proc/300/fd", 0o755)
    if os.geteuid() != 0:  # root reads it anyway
        assert "fds of 1 processes unreadable" in s.note


# ------------------------------------------------------------------------- tcp
def test_tcp_to_lan_and_internet_gets_rates_peers_and_router(root):
    _proc(root, 500, "ssh", {3: "socket:[900]"})
    _proc(root, 501, "firefox", {3: "socket:[901]"})
    _w(root, "proc/net/arp", "IP address       HW type     Flags       HW address            Mask     Device\n"
                             "192.168.1.20     0x1         0x2         68:b9:c2:7e:fd:2e     *        enp6s0\n"
                             "192.168.1.30     0x1         0x0         00:00:00:00:00:00     *        enp6s0\n"
                             "192.168.1.1      0x1         0x2         c0:68:cc:2b:d7:41     *        enp6s0\n"
                             "10.89.0.3        0x1         0x2         aa:bb:cc:dd:ee:ff     *        podman1\n")
    _w(root, "proc/net/route", "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
                               "enp6s0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\n")
    diag = FakeDiag(inet=[InetSock(900, "tcp", "192.168.1.10", 40000, "192.168.1.20", 22, "ESTAB", 1000, 1000, 2000),
                          InetSock(901, "tcp", "192.168.1.10", 40001, "1.1.1.1", 443, "ESTAB", 1000, 0, 0),
                          InetSock(0, "tcp", "192.168.1.10", 40002, "1.1.1.1", 443, "TIME-WAIT", 0)])
    t = tracker(root, diag)
    t.collect(0.0)
    diag.inet_socks[0] = InetSock(900, "tcp", "192.168.1.10", 40000, "192.168.1.20", 22, "ESTAB", 1000, 3000, 12000)
    diag.inet_socks[1] = InetSock(901, "tcp", "192.168.1.10", 40001, "1.1.1.1", 443, "ESTAB", 1000, 500, 4000)
    s = t.collect(1.0)
    ssh = link(s, "tcp", "proc:500", "peer:192.168.1.20")[0]
    assert (ssh.in_bps, ssh.out_bps) == (10000.0, 2000.0)
    web = link(s, "tcp", "proc:501", "wan")[0]
    assert web.in_bps == 4000.0
    peer = s.node("peer:192.168.1.20")
    assert peer.sub == "68:b9:c2:7e:fd:2e" and (peer.in_bps, peer.out_bps) == (10000.0, 2000.0)
    assert s.node("peer:192.168.1.30") is None  # incomplete ARP entry
    assert s.node("peer:10.89.0.3") is None  # a container bridge, not the LAN
    assert s.node("peer:192.168.1.1").router
    assert s.node("proc:500").in_bps == 10000.0  # a process's ▲ is what it receives
    assert len(link(s, "tcp")) == 2  # TIME-WAIT has no owner and is skipped


def test_loopback_pairs_once_from_the_client_side(root):
    _proc(root, 600, "client", {3: "socket:[950]"})
    _proc(root, 601, "server", {3: "socket:[951]", 4: "socket:[952]"})
    diag = FakeDiag(inet=[InetSock(950, "tcp", "127.0.0.1", 50000, "127.0.0.1", 8000, "ESTAB", 1000, 10, 20),
                          InetSock(951, "tcp", "127.0.0.1", 8000, "127.0.0.1", 50000, "ESTAB", 1000, 20, 10),
                          InetSock(952, "tcp", "0.0.0.0", 8000, "0.0.0.0", 0, "LISTEN", 1000)])
    s = tracker(root, diag).collect(0.0)
    tcp = link(s, "tcp")
    assert [(k.a, k.b, k.a_end, k.b_end) for k in tcp] == [("proc:600", "proc:601", "127.0.0.1:50000", "127.0.0.1:8000")]
    assert [k.a_end for k in link(s, "listen")] == ["0.0.0.0:8000"]


# ------------------------------------------------------------------------- containers
def _api(cid, name, ip, ports=(), labels=None, image="docker.io/library/postgres:16"):
    return {"Id": cid, "Names": [f"/{name}"], "Image": image, "State": "running", "Status": "Up 2 hours",
            "Labels": labels or {}, "Ports": [{"IP": h, "PrivatePort": cp, "PublicPort": hp, "Type": "tcp"}
                                             for h, hp, cp in ports],
            "NetworkSettings": {"Networks": {"llm_default": {"IPAddress": ip, "Gateway": "10.89.0.1"}}}}


def test_containers_from_cgroups_api_and_their_netns(root):
    for pid, cid, ns in ((700, CTR_A, 2), (710, CTR_B, 3)):
        _proc(root, pid, "postgres" if cid == CTR_A else "litellm",
              cgroup=f"0::/user.slice/user@1000.service/user.slice/libpod-{cid}.scope/container", ns=ns)
    _w(root, "proc/700/net/dev", "Inter-|   Receive\n face |bytes packets\n"
                                 "    lo: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0\n"
                                 "  eth0: 1000 10 0 0 0 0 0 0 5000 20 0 0 0 0 0 0\n")
    _w(root, "proc/700/net/tcp", _tcp_table([("0.0.0.0", 5432, "0.0.0.0", 0, 0x0A, 1),
                                             ("10.89.0.3", 5432, "10.89.0.4", 47660, 0x01, 2)]))
    _w(root, "proc/710/net/tcp", _tcp_table([("10.89.0.4", 47660, "10.89.0.3", 5432, 0x01, 3),
                                             ("10.89.0.4", 40000, "10.89.0.1", 4000, 0x01, 4),
                                             ("10.89.0.4", 40001, "93.184.215.14", 443, 0x01, 5)]))
    _w(root, "run/user/1000/podman/podman.sock", "")
    calls = []

    def api(path):
        calls.append(path)
        return [_api(CTR_A, "llm-litellm-db-1", "10.89.0.3"), _api(CTR_B, "llm-litellm-1", "10.89.0.4")]

    t = tracker(root, FakeDiag(), api)
    t.collect(0.0)
    _w(root, "proc/700/net/dev", "Inter-|   Receive\n face |bytes packets\n"
                                 "  eth0: 3000 10 0 0 0 0 0 0 9000 20 0 0 0 0 0 0\n")
    s = t.collect(1.0)
    a, b = s.node(f"ctr:{CTR_A[:12]}"), s.node(f"ctr:{CTR_B[:12]}")
    assert (a.name, b.name, a.sub, a.runtime) == ("llm-litellm-db-1", "llm-litellm-1", "docker.io/library/postgres:16", "podman")
    assert (a.in_bps, a.out_bps) == (4000.0, 2000.0)  # ▲ is what the container transmits
    assert a.netns == 2 and a.addrs == ["10.89.0.3"]
    ctr = link(s, "tcp", b.id, a.id)
    assert len(ctr) == 1 and ctr[0].b_end == "10.89.0.3:5432"  # client → server, once
    assert link(s, "tcp", b.id, "host")[0].prov == "derived"  # the bridge gateway
    assert link(s, "tcp", b.id, "wan")
    assert [k.a_end for k in link(s, "listen", a.id)] == ["0.0.0.0:5432"]
    assert len(calls) == 1  # the API is asked again only when the set of containers changes


def test_rootless_published_port_maps_to_its_container(root):
    _proc(root, 800, "curl", {3: "socket:[800]"})
    _proc(root, 801, "rootlessport", {3: "socket:[801]", 4: "socket:[802]"})
    _proc(root, 802, "nginx", cgroup=f"0::/user.slice/libpod-{CTR_A}.scope/container", ns=2)
    _w(root, "run/user/1000/podman/podman.sock", "")
    api = lambda path: [_api(CTR_A, "web", "10.89.0.9", [("127.0.0.1", 8080, 80)], image="nginx")]  # noqa: E731
    diag = FakeDiag(inet=[InetSock(800, "tcp", "127.0.0.1", 50000, "127.0.0.1", 8080, "ESTAB", 1000, 100, 5000),
                          InetSock(801, "tcp", "127.0.0.1", 8080, "127.0.0.1", 50000, "ESTAB", 1000, 5000, 100),
                          InetSock(802, "tcp", "127.0.0.1", 8080, "0.0.0.0", 0, "LISTEN", 1000)])
    s = tracker(root, diag, api).collect(0.0)
    web = f"ctr:{CTR_A[:12]}"
    k = link(s, "tcp", "proc:800")
    assert len(k) == 1 and k[0].b == web and k[0].prov == "derived"
    lis = link(s, "listen")
    assert lis[0].a == web and lis[0].prov == "derived"
    assert s.node(web).ports == ["127.0.0.1:8080→80/tcp"]


def test_pasta_connections_are_charged_to_the_container_that_made_them(root):
    _proc(root, 900, "pasta.avx2", {3: "socket:[990]"})
    _proc(root, 901, "searxng", cgroup=f"0::/user.slice/libpod-{CTR_A}.scope/container", ns=2)
    _w(root, "proc/901/net/tcp", _tcp_table([("10.89.1.2", 36694, "20.204.244.192", 443, 0x01, 7)]))
    _w(root, "run/user/1000/podman/podman.sock", "")
    api = lambda path: [_api(CTR_A, "search-searxng-1", "10.89.1.2")]  # noqa: E731
    diag = FakeDiag(inet=[InetSock(990, "tcp", "192.168.1.10", 56296, "20.204.244.192", 443, "ESTAB", 1000, 1, 1)])
    s = tracker(root, diag, api).collect(0.0)
    out = link(s, "tcp", b="wan")
    assert len(out) == 1 and out[0].a == f"ctr:{CTR_A[:12]}" and out[0].prov == "derived"  # once, with bytes


def test_parse_containers_runtime_labels_and_storage_fallback():
    items = [_api(CTR_A, "fedora-dev", "", labels={"manager": "distrobox"}),
             _api(CTR_B, "tb", "", labels={"com.github.containers.toolbox": "true"})]
    got = I.parse_containers(items, "podman")
    assert got[CTR_A].runtime == "distrobox" and got[CTR_B].runtime == "toolbox"
    assert got[CTR_A].name == "fedora-dev" and got[CTR_A].ips == []
    store = I.parse_storage('[{"id": "%s", "names": ["llm-1"], "metadata": "{\\"image-name\\": \\"img:1\\"}"}]' % CTR_A)
    assert (store[CTR_A].name, store[CTR_A].image) == ("llm-1", "img:1")
    assert I.parse_containers({"message": "nope"}, "docker") == {}


# ------------------------------------------------------------------------- VMs and machines
def test_machined_vm_with_its_tap_and_a_bare_qemu(root):
    scope = r"machine-qemu\x2d1\x2dwin11.scope"
    _w(root, "run/systemd/machines/win11", f"NAME=win11\nCLASS=vm\nLEADER=1200\nSCOPE={scope}\nSERVICE=libvirt-qemu\n")
    _w(root, "run/systemd/machines/unit:" + scope, "")
    _proc(root, 1200, "qemu-system-x86", {10: "/dev/net/tun"}, {10: "pos:\t0\nflags:\t02\niff:\tvnet0\n"},
          cgroup=f"0::/machine.slice/{scope}/libvirt/emulator")
    _proc(root, 1210, "qemu-system-x86", cmdline="qemu-system-x86_64\0-name\0guest=dev,debug-threads=on\0")
    _w(root, "sys/class/net/vnet0/statistics/rx_bytes", "1000\n")
    _w(root, "sys/class/net/vnet0/statistics/tx_bytes", "1000\n")
    t = tracker(root, FakeDiag())
    t.collect(0.0)
    _w(root, "sys/class/net/vnet0/statistics/rx_bytes", "4000\n")
    _w(root, "sys/class/net/vnet0/statistics/tx_bytes", "2000\n")
    s = t.collect(1.0)
    vm = s.node("vm:win11")
    assert (vm.kind, vm.runtime, vm.iface, vm.sub, vm.pids) == ("vm", "qemu", "vnet0", "libvirt-qemu", [1200])
    assert (vm.in_bps, vm.out_bps) == (3000.0, 1000.0)  # the tap's rx is what the guest sent
    assert s.node("vm:dev").runtime == "qemu"
    assert s.node("vm:unit:" + scope) is None


# ------------------------------------------------------------------------- walk cadence
def test_walk_is_reused_between_full_polls_and_redone_after_a_pause(root):
    _proc(root, 100, "bash", {1: "pipe:[500]"}, {1: "01"})
    _proc(root, 101, "grep", {0: "pipe:[500]"}, {0: "00"})
    t = tracker(root, FakeDiag())
    assert t.collect(0.0).counts.get("pipe") == 1
    _proc(root, 102, "cat", {0: "pipe:[501]"}, {0: "00"})
    os.symlink("pipe:[501]", root / "proc/100/fd/2")
    _w(root, "proc/100/fdinfo/2", "flags:\t01\n")
    assert t.collect(1.0).counts.get("pipe") == 1  # cached walk
    assert t.collect(2.0).counts.get("pipe") == 1
    assert t.collect(3.0).counts.get("pipe") == 2  # every 3rd poll walks again
    t.collect(4.0)
    os.remove(root / "proc/100/fd/2")
    assert t.collect(20.0).counts.get("pipe") == 1  # the tab was hidden: walk before showing


# ------------------------------------------------------------------------- parsers
def test_classify_remote_addresses():
    own, ctr = {"192.168.1.10"}, {"10.89.0.3": "ctr:a"}
    assert I.classify("127.0.0.1", own, LAN, ctr) == ("local", "")
    assert I.classify("::1", own, LAN, ctr) == ("local", "")
    assert I.classify("192.168.1.10", own, LAN, ctr) == ("local", "")
    assert I.classify("10.89.0.3", own, LAN, ctr) == ("container", "ctr:a")
    assert I.classify("192.168.1.34", own, LAN, ctr) == ("peer", "")
    assert I.classify("fe80::1", own, LAN, ctr) == ("peer", "")
    assert I.classify("8.8.8.8", own, LAN, ctr) == ("wan", "")
    assert I.classify("not-an-ip", own, LAN, ctr) == ("wan", "")


def test_small_parsers():
    assert I.default_gateways("Iface\tDestination\tGateway\tFlags\n"
                              "wlp8s0\t00000000\t0101A8C0\t0003\n"
                              "wlp8s0\t0001A8C0\t00000000\t0001\n") == {"192.168.1.1"}
    assert I.qemu_name(["qemu", "-name", "win11,debug-threads=on"]) == "win11"
    assert I.qemu_name(["qemu", "-m", "4G"]) == ""
    assert I.netdev_bytes("h\nh\n  lo: 5 0 0 0 0 0 0 0 5\n") is None


def _rta(typ, val):
    ln = 4 + len(val)
    return struct.pack("=HH", ln, typ) + val + b"\0" * ((4 - ln % 4) % 4)


def test_sock_diag_unix_parser():
    msg = D.UNIX_MSG.pack(socket.AF_UNIX, 1, 1, 0, 700, 0, 0)
    u = D.parse_unix(msg + _rta(D.UNIX_DIAG_NAME, b"/run/user/1000/bus") + _rta(D.UNIX_DIAG_PEER, struct.pack("=I", 701)))
    assert (u.ino, u.peer, u.type, u.state, u.path) == (700, 701, "stream", "ESTAB", "/run/user/1000/bus")
    u = D.parse_unix(D.UNIX_MSG.pack(socket.AF_UNIX, 2, 7, 0, 9, 0, 0) + _rta(D.UNIX_DIAG_NAME, b"\0xdg-portal"))
    assert (u.peer, u.type, u.path) == (0, "dgram", "@xdg-portal")
    assert D.parse_unix(b"\0" * 4) is None


def test_sock_diag_inet_parser_reads_tcp_info():
    info = bytearray(232)
    struct.pack_into("=I", info, D.TCPI_RTT, 28711)
    struct.pack_into("=QQ", info, D.TCPI_BYTES_ACKED, 2276, 7723)
    sockid = D.SOCKID.pack(59630, 443, socket.inet_aton("192.168.1.10") + bytes(12),
                           socket.inet_aton("35.190.46.17") + bytes(12), 0, 0, 0)
    msg = D.INET_MSG.pack(socket.AF_INET, 1, 0, 0) + sockid + D.INET_TAIL.pack(0, 0, 0, 1000, 13345037)
    s = D.parse_inet(msg + _rta(D.INET_DIAG_INFO, bytes(info)), "tcp")
    assert (s.src, s.sport, s.dst, s.dport, s.state, s.uid, s.ino) == (
        "192.168.1.10", 59630, "35.190.46.17", 443, "ESTAB", 1000, 13345037)
    assert (s.acked, s.received, s.rtt_us) == (2276, 7723, 28711)
    mapped = socket.inet_pton(socket.AF_INET6, "::ffff:10.0.0.2")
    sockid6 = D.SOCKID.pack(53, 0, mapped, bytes(16), 0, 0, 0)
    u = D.parse_inet(D.INET_MSG.pack(socket.AF_INET6, 7, 0, 0) + sockid6 + D.INET_TAIL.pack(0, 0, 0, 0, 5), "udp")
    assert (u.src, u.state, u.acked) == ("10.0.0.2", "UNCONN", None)


def test_proc_net_fallbacks():
    tcp = D.proc_inet(_tcp_table([("10.89.0.3", 5432, "10.89.0.4", 47660, 0x01, 42)]), "tcp")
    assert [(s.src, s.sport, s.dst, s.dport, s.state, s.ino) for s in tcp] == [
        ("10.89.0.3", 5432, "10.89.0.4", 47660, "ESTAB", 42)]
    six = D.proc_inet("hdr\n   0: 00000000000000000000000001000000:0016 00000000000000000000000000000000:0000 0A "
                      "00000000:00000000 00:00000000 00000000  0  0 77 1\n", "tcp")
    assert (six[0].src, six[0].sport, six[0].state) == ("::1", 22, "LISTEN")
    unix = D.proc_unix("Num RefCount Protocol Flags Type St Inode Path\n"
                       "0000000000000000: 00000002 00000000 00010000 0001 01 190125 /run/user/1000/bus\n")
    assert (unix[0].ino, unix[0].state, unix[0].path) == (190125, "LISTEN", "/run/user/1000/bus")


# ------------------------------------------------------------------------- snapshot plumbing
def test_ipc_dict_keeps_the_busiest_links_and_their_processes():
    nodes = [IpcNode(f"proc:{i}", "process", f"p{i}") for i in range(150)] + [IpcNode("wan", "wan", "Internet")]
    links = [IpcLink(f"proc:{i}", "wan", "tcp", in_bps=float(i)) for i in range(150)]
    d = ipc_dict(IpcStat(nodes, links, {"tcp": 150}))
    assert len(d["links"]) == 100 and d["links"][0]["in_bps"] == 149.0
    assert {n["id"] for n in d["nodes"]} == {f"proc:{i}" for i in range(50, 150)} | {"wan"}


def test_history_keeps_guest_and_peer_rates_but_not_processes():
    h = History()
    snap = DemoSource().collect(5.0)
    h.ingest(snap)
    assert "ipc:ctr:44254bc720fd:in" in h.data and "ipc:peer:192.168.1.20:out" in h.data
    assert not any(k.startswith("ipc:proc:") for k in h.data)


def test_live_tracker_runs_on_this_machine():
    """Smoke test against the real /proc and sock_diag: no exceptions, a host node, sane counts."""
    t = I.IpcTracker(SysFS("/"), api=lambda path: [], resolve=False)
    try:
        s = t.collect()
        s = t.collect()
    finally:
        t.close()
    assert s.node("host") is not None
    assert all(k.a for k in s.links)


# ------------------------------------------------------------------------- sys info
SNMP = """Ip: Forwarding DefaultTTL
Ip: 1 64
Tcp: RtoAlgorithm RtoMin RetransSegs OutRsts
Tcp: 1 200 4711 30
Udp: InDatagrams InErrors
Udp: 100 2
"""
NETSTAT = """TcpExt: SyncookiesSent ListenOverflows ListenDrops
TcpExt: 0 5 7
"""


def test_parse_snmp_pairs_header_and_value_lines():
    from systui.telemetry.netstat import parse_snmp

    d = parse_snmp(SNMP) | parse_snmp(NETSTAT)
    assert d["Tcp.RetransSegs"] == 4711 and d["Tcp.OutRsts"] == 30 and d["Udp.InErrors"] == 2
    assert d["TcpExt.ListenDrops"] == 7 and d["Ip.DefaultTTL"] == 64
    assert parse_snmp("") == {} and parse_snmp("garbage\n") == {}


def test_kernel_counters_become_rates_and_tcp_states_are_counted(root):
    _w(root, "proc/net/snmp", SNMP)
    _w(root, "proc/net/netstat", NETSTAT)
    _w(root, "proc/sys/net/netfilter/nf_conntrack_count", "812\n")
    _w(root, "proc/sys/net/netfilter/nf_conntrack_max", "262144\n")
    socks = [InetSock(0, "tcp", "192.168.1.10", 40000 + i, "1.1.1.1", 443, st, 1000)
             for i, st in enumerate(["ESTAB", "ESTAB", "TIME-WAIT", "CLOSE-WAIT"])]
    t = tracker(root, FakeDiag(inet=socks))
    s = t.collect(0.0)
    assert s.kernel["Tcp.RetransSegs"] is None and s.kernel["conntrack"] == 812  # no rate before a 2nd sample
    assert s.tcp_states == {"ESTAB": 2, "TIME-WAIT": 1, "CLOSE-WAIT": 1}
    _w(root, "proc/net/snmp", SNMP.replace("4711", "4721"))
    s = t.collect(2.0)
    assert s.kernel["Tcp.RetransSegs"] == 5.0 and s.kernel["TcpExt.ListenDrops"] == 0.0
    assert s.kernel["conntrack_max"] == 262144


def test_container_cpu_and_memory_come_from_its_scope_not_the_leaf(root):
    scope = f"user.slice/user@1000.service/user.slice/libpod-{CTR_A}.scope"
    _proc(root, 800, "postgres", cgroup=f"0::/{scope}/container", ns=2)
    base = f"sys/fs/cgroup/{scope}"
    _w(root, f"{base}/cpu.stat", "usage_usec 1000000\nuser_usec 900000\n")
    _w(root, f"{base}/memory.current", "73400320\n")
    _w(root, f"{base}/memory.max", "max\n")
    _w(root, f"{base}/cpu.max", "200000 100000\n")
    _w(root, f"{base}/pids.current", "10\n")
    _w(root, f"{base}/container/memory.current", "1\n")  # the nested leaf: not what we want
    t = tracker(root, FakeDiag())
    t.collect(0.0)
    _w(root, f"{base}/cpu.stat", "usage_usec 1500000\n")
    n = t.collect(1.0).node(f"ctr:{CTR_A[:12]}")
    assert n.cpu == pytest.approx(50.0) and n.rss == 73400320 and n.mem_limit is None
    assert n.cpu_limit == 2.0 and n.pids_n == 10 and n.unit == f"libpod-{CTR_A}.scope"


def test_process_nodes_get_fds_and_unit(root):
    _proc(root, 100, "bash", {1: "pipe:[500]", 2: "/dev/null"}, {1: "01"},
          cgroup="0::/user.slice/user@1000.service/app.slice/app-org.kde.konsole-123.scope")
    _proc(root, 101, "grep", {0: "pipe:[500]"}, {0: "00"})
    n = tracker(root, FakeDiag()).collect(0.0).node("proc:100")
    assert n.fds == 2 and n.unit == "app-org.kde.konsole-123.scope"


def test_annotate_ipc_joins_the_process_table_and_sums_guests():
    from systui.telemetry import ProcStat, annotate_ipc

    nodes = [IpcNode("proc:1", "process", "a", pids=[1]),
             IpcNode("ctr:x", "container", "db", pids=[2, 3]),
             IpcNode("ctr:y", "container", "web", pids=[4], cpu=12.0, rss=100)]
    by_pid = {1: ProcStat(1, "a", cpu=5.0, rss=10, threads=2, started=50.0),
              2: ProcStat(2, "p", cpu=1.0, rss=20, threads=1, started=40.0),
              3: ProcStat(3, "q", cpu=2.0, rss=30, threads=3, started=45.0),
              4: ProcStat(4, "w", cpu=99.0, rss=999, threads=1)}
    ipc = IpcStat(nodes, [])
    annotate_ipc(ipc, by_pid)
    a, x, y = ipc.nodes
    assert (a.cpu, a.rss, a.threads, a.started, a.res_prov) == (5.0, 10, 2, 50.0, "live")
    assert (x.cpu, x.rss, x.threads, x.started, x.res_prov) == (3.0, 50, 4, 40.0, "derived")
    assert (y.cpu, y.rss, y.res_prov) == (12.0, 100, "live")  # its cgroup already answered


def test_detail_rows_show_resources_and_host_health():
    from systui.tables import ipc_rows

    ipc = DemoSource().collect().ipc
    rows = {r.key: r.value for r in ipc_rows(ipc, "ctr:df517a104788")}
    assert "% of a thread" in rows["CPU"] and " of " in rows["Memory"] and rows["Unit"].startswith("libpod-")
    host = {r.key: r.value for r in ipc_rows(ipc, "host")}
    assert "ESTAB" in host["TCP states"] and "retrans" in host["Net errors"] and "of" in host["Conntrack"]
    proc = {r.key: r.value for r in ipc_rows(ipc, "proc:1000")}
    assert "Open fds" in proc and proc["Started"].endswith("ago")


# ------------------------------------------------------------------------- events
def test_events_log_opens_and_closes_once_and_not_on_the_first_poll(root):
    _proc(root, 100, "curl", {3: "socket:[900]"})
    a = InetSock(900, "tcp", "192.168.1.10", 51900, "93.184.215.14", 443, "ESTAB", 1000)
    lst = InetSock(901, "tcp", "0.0.0.0", 8080, "0.0.0.0", 0, "LISTEN", 1000)
    diag = FakeDiag(inet=[lst])
    t = tracker(root, diag)
    t.clock = lambda: 1000.0
    assert t.collect(0.0).events == []  # everything is new on the first poll: nothing to report
    diag.inet_socks = [lst, a]
    _proc(root, 101, "server", {4: "socket:[901]"})
    for now in (1.0, 2.0, 3.0):  # the 3rd is a full walk: the socket's owner becomes known
        s = t.collect(now)
    opened = [e for e in s.events if e.verb == "opened"]
    assert len(opened) == 1 and opened[0].end == "93.184.215.14:443" and opened[0].kind == "tcp"
    assert [e.verb for e in s.events if e.kind == "listen"] == []  # it was there from the start
    diag.inet_socks = [lst]
    s = t.collect(4.0)
    closed = [e for e in s.events if e.verb == "closed"]
    assert len(closed) == 1 and closed[0].name_b == "Internet"
    s = t.collect(5.0)
    assert len(s.events) == 2  # nothing changed, and the listener did not flap when its owner became known


def test_events_log_guests_listeners_and_new_peers(root):
    diag = FakeDiag()
    t = tracker(root, diag)
    t.collect(0.0)
    _proc(root, 800, "postgres", cgroup=f"0::/user.slice/libpod-{CTR_A}.scope/container", ns=2)
    _w(root, "proc/net/arp", "IP address HW type Flags HW address Mask Device\n"
                             "192.168.1.20 0x1 0x2 68:b9:c2:7e:fd:2e * enp6s0\n")
    for now in (1.0, 2.0, 3.0):  # the 3rd is a full walk
        s = t.collect(now)
    verbs = {(e.verb, e.kind) for e in s.events}
    assert ("started", "container") in verbs and ("new peer", "peer") in verbs
    import shutil

    shutil.rmtree(root / "proc/800")
    for now in (4.0, 5.0, 6.0):
        s = t.collect(now)
    assert ("stopped", "container") in {(e.verb, e.kind) for e in s.events}


def test_no_events_after_a_pause(root):
    _proc(root, 100, "curl", {3: "socket:[900]"})
    diag = FakeDiag()
    t = tracker(root, diag)
    t.collect(0.0)
    diag.inet_socks = [InetSock(900, "tcp", "192.168.1.10", 51900, "93.184.215.14", 443, "ESTAB", 1000)]
    assert t.collect(60.0).events == []  # the tab was hidden: this is a fresh start, not news


def test_demo_has_events_and_ipc_dict_caps_them():
    ipc = DemoSource().collect().ipc
    assert any(e.verb == "opened" for e in ipc.events)
    ipc.events = ipc.events * 40
    assert len(ipc_dict(ipc)["events"]) == 50
