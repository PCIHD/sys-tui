"""Host-wide network health counters: ``/proc/net/snmp``, ``/proc/net/netstat`` and conntrack.

Both files pair a header line with a value line per protocol::

    Tcp: RtoAlgorithm RtoMin ... RetransSegs ...
    Tcp: 1 200 ... 4711 ...
"""

from __future__ import annotations

# counter → short label, in the order the IPC strip and detail panel list them
COUNTERS = {
    "Tcp.RetransSegs": "retrans",
    "Tcp.OutRsts": "resets sent",
    "Tcp.EstabResets": "estab resets",
    "Tcp.AttemptFails": "connect fails",
    "Tcp.InErrs": "TCP in errors",
    "TcpExt.ListenOverflows": "listen overflows",
    "TcpExt.ListenDrops": "listen drops",
    "TcpExt.TCPTimeouts": "timeouts",
    "Udp.InErrors": "UDP in errors",
    "Udp.RcvbufErrors": "UDP rcvbuf errors",
}


def parse_snmp(text: str) -> dict[str, int]:
    """``{"Tcp.RetransSegs": 4711, ...}`` from snmp/netstat-style paired lines."""
    out: dict[str, int] = {}
    head: dict[str, list[str]] = {}
    for line in text.splitlines():
        proto, _, rest = line.partition(":")
        fields = rest.split()
        if not fields:
            continue
        if proto not in head or not all(f.lstrip("-").isdigit() for f in fields):
            head[proto] = fields
            continue
        for name, v in zip(head.pop(proto), fields):
            out[f"{proto}.{name}"] = int(v)
    return out
