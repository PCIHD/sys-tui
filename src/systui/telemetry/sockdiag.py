"""Socket tables over ``NETLINK_SOCK_DIAG``: Unix sockets with their peers, TCP/UDP with ``tcp_info``.

This is the interface ``ss`` uses, and it works unprivileged. It reports the sockets of the
netns systui runs in. ``/proc/net/unix`` has no peers and ``/proc/net/tcp`` has no byte counts,
so those are only the fallback (and the only view into another netns, see ``proc_inet``).

Each dump is split into ``_request`` (bytes out), ``_dump`` (socket I/O) and a ``parse_*``
function (bytes in), so tests feed the parsers canned messages.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
from dataclasses import dataclass

NETLINK_SOCK_DIAG = 4
SOCK_DIAG_BY_FAMILY = 20
NLM_F_REQUEST, NLM_F_DUMP = 0x1, 0x300
NLMSG_ERROR, NLMSG_DONE = 2, 3
NLMSG = struct.Struct("=IHHII")  # len, type, flags, seq, pid
RTA = struct.Struct("=HH")  # len, type

UDIAG_SHOW_NAME, UDIAG_SHOW_PEER = 0x1, 0x4
UNIX_DIAG_NAME, UNIX_DIAG_PEER = 0, 2
UNIX_REQ = struct.Struct("=BBHIIIII")  # family, protocol, pad, states, ino, show, cookie[2]
UNIX_MSG = struct.Struct("=BBBBIII")  # family, type, state, pad, ino, cookie[2]

INET_DIAG_INFO = 2
SOCKID = struct.Struct(">HH16s16sIII")  # sport, dport, src, dst (network order); if, cookie in host order
INET_REQ = struct.Struct("=BBBBI")  # family, protocol, ext, pad, states  (+ SOCKID)
INET_MSG = struct.Struct("=BBBB")  # family, state, timer, retrans  (+ SOCKID + 5 × u32)
INET_TAIL = struct.Struct("=IIIII")  # expires, rqueue, wqueue, uid, inode
TCPI_RTT, TCPI_BYTES_ACKED, TCPI_BYTES_RECEIVED = 68, 120, 128  # offsets in struct tcp_info (linux/tcp.h)

TCP_STATES = {1: "ESTAB", 2: "SYN-SENT", 3: "SYN-RECV", 4: "FIN-WAIT-1", 5: "FIN-WAIT-2", 6: "TIME-WAIT",
              7: "CLOSE", 8: "CLOSE-WAIT", 9: "LAST-ACK", 10: "LISTEN", 11: "CLOSING"}
UNIX_TYPES = {1: "stream", 2: "dgram", 5: "seqpacket"}


@dataclass
class UnixSock:
    ino: int
    peer: int  # 0 if unconnected
    type: str  # stream | dgram | seqpacket
    state: str  # LISTEN | ESTAB | ...
    path: str  # "" for unnamed, "@name" for abstract


@dataclass
class InetSock:
    ino: int  # 0 for TIME-WAIT and other orphans
    proto: str  # tcp | udp
    src: str
    sport: int
    dst: str
    dport: int
    state: str
    uid: int
    acked: int | None = None  # tcp_info bytes_acked: sent and acknowledged
    received: int | None = None
    rtt_us: int | None = None


class DiagError(OSError):
    pass


# ------------------------------------------------------------------------- netlink I/O
def _attrs(buf: bytes, off: int, end: int):
    while off + RTA.size <= end:
        ln, typ = RTA.unpack_from(buf, off)
        if ln < RTA.size:
            return
        yield typ, buf[off + RTA.size:off + ln]
        off += (ln + 3) & ~3


def _messages(buf: bytes):
    """(type, payload) of each netlink message in ``buf``."""
    off = 0
    while off + NLMSG.size <= len(buf):
        ln, typ, _flags, _seq, _pid = NLMSG.unpack_from(buf, off)
        if ln < NLMSG.size:
            return
        yield typ, buf[off + NLMSG.size:off + ln]
        off += (ln + 3) & ~3


def _dump(req: bytes, timeout: float = 1.0) -> list[bytes]:
    """Send one dump request and return each reply message's payload. Raises DiagError."""
    try:
        s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_SOCK_DIAG)
    except (OSError, AttributeError) as e:
        raise DiagError(getattr(e, "errno", None) or 0, f"sock_diag unavailable: {e}") from e
    out: list[bytes] = []
    try:
        s.settimeout(timeout)
        s.send(NLMSG.pack(NLMSG.size + len(req), SOCK_DIAG_BY_FAMILY, NLM_F_REQUEST | NLM_F_DUMP, 1, 0) + req)
        while True:
            buf = s.recv(1 << 17)
            if not buf:
                return out
            for typ, payload in _messages(buf):
                if typ == NLMSG_DONE:
                    return out
                if typ == NLMSG_ERROR:
                    err = -struct.unpack_from("=i", payload)[0] if len(payload) >= 4 else 0
                    if err == 0:
                        continue
                    raise DiagError(err, f"sock_diag error {err}")
                out.append(payload)
    except OSError as e:
        if isinstance(e, DiagError):
            raise
        raise DiagError(e.errno or 0, f"sock_diag: {e}") from e
    finally:
        s.close()


# ------------------------------------------------------------------------- unix
def unix_request() -> bytes:
    return UNIX_REQ.pack(socket.AF_UNIX, 0, 0, 0xFFFFFFFF, 0, UDIAG_SHOW_NAME | UDIAG_SHOW_PEER, 0, 0)


def parse_unix(payload: bytes) -> UnixSock | None:
    if len(payload) < UNIX_MSG.size:
        return None
    _fam, typ, state, _pad, ino, _c0, _c1 = UNIX_MSG.unpack_from(payload)
    peer, path = 0, ""
    for at, val in _attrs(payload, UNIX_MSG.size, len(payload)):
        if at == UNIX_DIAG_PEER and len(val) >= 4:
            peer = struct.unpack_from("=I", val)[0]
        elif at == UNIX_DIAG_NAME and val:
            path = "@" + val[1:].rstrip(b"\0").decode(errors="replace") if val[:1] == b"\0" \
                else val.rstrip(b"\0").decode(errors="replace")
    return UnixSock(ino, peer, UNIX_TYPES.get(typ, str(typ)), TCP_STATES.get(state, str(state)), path)


def unix() -> list[UnixSock]:
    return [u for u in map(parse_unix, _dump(unix_request())) if u is not None]


# ------------------------------------------------------------------------- tcp / udp
def inet_request(family: int, proto: int) -> bytes:
    ext = 1 << (INET_DIAG_INFO - 1) if proto == socket.IPPROTO_TCP else 0
    return INET_REQ.pack(family, proto, ext, 0, 0xFFFFFFFF) + SOCKID.pack(0, 0, bytes(16), bytes(16), 0, 0, 0)


def _addr(family: int, raw: bytes) -> str:
    if family == socket.AF_INET:
        return socket.inet_ntop(socket.AF_INET, raw[:4])
    a = ipaddress.IPv6Address(raw)
    return str(a.ipv4_mapped) if a.ipv4_mapped else str(a)  # ::ffff:a.b.c.d → a.b.c.d


def parse_inet(payload: bytes, proto: str) -> InetSock | None:
    need = INET_MSG.size + SOCKID.size + INET_TAIL.size
    if len(payload) < need:
        return None
    family, state, _timer, _retrans = INET_MSG.unpack_from(payload)
    sport, dport, src, dst, _if, _c0, _c1 = SOCKID.unpack_from(payload, INET_MSG.size)
    _exp, _rq, _wq, uid, ino = INET_TAIL.unpack_from(payload, INET_MSG.size + SOCKID.size)
    s = InetSock(ino, proto, _addr(family, src), sport, _addr(family, dst), dport,
                 TCP_STATES.get(state, str(state)) if proto == "tcp" else ("ESTAB" if dport else "UNCONN"), uid)
    for at, val in _attrs(payload, need, len(payload)):
        if at == INET_DIAG_INFO and len(val) >= TCPI_BYTES_RECEIVED + 8:
            s.rtt_us = struct.unpack_from("=I", val, TCPI_RTT)[0]
            s.acked, s.received = struct.unpack_from("=QQ", val, TCPI_BYTES_ACKED)
    return s


def inet(family: int, proto: int) -> list[InetSock]:
    name = "tcp" if proto == socket.IPPROTO_TCP else "udp"
    return [s for s in (parse_inet(p, name) for p in _dump(inet_request(family, proto))) if s is not None]


def all_inet() -> list[InetSock]:
    out: list[InetSock] = []
    for proto in (socket.IPPROTO_TCP, socket.IPPROTO_UDP):
        for fam in (socket.AF_INET, socket.AF_INET6):
            out += inet(fam, proto)
    return out


# ------------------------------------------------------------------------- /proc/net fallbacks
def _hex_addr(h: str) -> tuple[str, int]:
    """``0100007F:1538`` → ("127.0.0.1", 5432); IPv6 words are host-order u32s."""
    a, p = h.split(":")
    raw = bytes.fromhex(a)
    if len(raw) == 4:
        return socket.inet_ntop(socket.AF_INET, raw[::-1]), int(p, 16)
    b = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
    ip = ipaddress.IPv6Address(b)
    return (str(ip.ipv4_mapped) if ip.ipv4_mapped else str(ip)), int(p, 16)


def proc_inet(text: str, proto: str) -> list[InetSock]:
    """Parse ``/proc/<pid>/net/{tcp,tcp6,udp,udp6}``: the sockets of that pid's netns, no byte counts."""
    out = []
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 10:
            continue
        try:
            src, sport = _hex_addr(f[1])
            dst, dport = _hex_addr(f[2])
            st, uid, ino = int(f[3], 16), int(f[7]), int(f[9])
        except ValueError:
            continue
        state = TCP_STATES.get(st, str(st)) if proto == "tcp" else ("ESTAB" if dport else "UNCONN")
        out.append(InetSock(ino, proto, src, sport, dst, dport, state, uid))
    return out


def proc_unix(text: str) -> list[UnixSock]:
    """Parse ``/proc/net/unix``: paths and states, but no peers."""
    out = []
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 7:
            continue
        try:
            typ, st, ino = int(f[4], 16), int(f[5], 16), int(f[6])
        except ValueError:
            continue
        state = {1: "UNCONN", 2: "CONNECTING", 3: "ESTAB", 4: "DISCONNECTING"}.get(st, str(st))
        if int(f[3], 16) & 0x10000:  # __SO_ACCEPTCON
            state = "LISTEN"
        out.append(UnixSock(ino, 0, UNIX_TYPES.get(typ, str(typ)), state, f[7] if len(f) > 7 else ""))
    return out
