"""Text tables: processes and every hwmon sensor (with min/max since start)."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text

from pixboard.rows import DERIVED, LIVE, Row

from .history import History
from .render.charts import IN, OUT, human_bytes
from .telemetry import IpcLink, IpcNode, IpcStat, ProcStat, SystemSnapshot

SORTS = {
    "cpu": ("CPU", lambda p: p.cpu),
    "mem": ("memory", lambda p: p.rss),
    "io": ("I/O", lambda p: p.io_bps or 0.0),
}
PROC_COLS = ("PID", "Name", "User", "CPU %", "RSS", "Mem %", "I/O", "Core", "GPU", "State")


def minibar(frac: float, width: int = 8) -> Text:
    frac = max(0.0, min(1.0, frac))
    full = frac * width
    n = int(full)
    color = "#3fae7a" if frac < 0.5 else "#f2c14e" if frac < 0.8 else "#e5383b"
    t = Text("█" * n, style=color)
    if n < width:
        t.append(" ▏▎▍▌▋▊▉"[int((full - n) * 8)], style=color)
        t.append(" " * (width - n - 1))
    return t


def proc_row(p: ProcStat, threads: int) -> tuple:
    cpu = Text.assemble(minibar(p.cpu / 100, 6),
                        (f" {p.cpu:6.1f}", "bold" if p.cpu >= 50 else ""))
    mem = Text.assemble(minibar(p.mem_pct / 100 * 4, 6), f" {p.mem_pct:5.1f}")
    io = io_cell(p)
    state = {"running": Text("run", style="#3dff7a"), "sleeping": Text("sleep", style="dim"),
             "disk-sleep": Text("disk", style="#f2c14e"), "zombie": Text("zombie", style="#e5383b")}.get(
        p.status, Text(p.status or "?", style="dim"))
    core = "—" if p.last_cpu is None else str(p.last_cpu)
    gpu = (p.gpu_kind or "GPU") if p.gpu else "—"
    return (str(p.pid), Text(p.name, style="bold" if p.cpu >= 10 or p.gpu else ""), p.user[:12], cpu,
            human_bytes(p.rss), mem, io, core, gpu, state)


def io_cell(p: ProcStat) -> Text | str:
    """``▲read ▼write``; the arrows and colours match the board traces and the I/O charts."""
    if p.io_read_bps is None and p.io_write_bps is None:
        return "—" if p.io_bps is None else human_bytes(p.io_bps, True)
    t = Text()
    if p.io_read_bps:
        t.append("▲" + human_bytes(p.io_read_bps, True), style=f"#{IN:06x}")
    if p.io_write_bps:
        t.append((" " if t else "") + "▼" + human_bytes(p.io_write_bps, True), style=f"#{OUT:06x}")
    return t if t else Text("0", style="dim")


def sorted_procs(s: SystemSnapshot, key: str) -> list[ProcStat]:
    return sorted(s.procs, key=SORTS[key][1], reverse=True)


def sensors_table(s: SystemSnapshot | None, h: History) -> Table:
    t = Table(expand=True, box=None, header_style="bold #8fb8de", padding=(0, 1), show_edge=False)
    for col, kw in (("Chip", {"style": "#8fb8de"}), ("Sensor", {}), ("Value", {"justify": "right"}),
                    ("Min", {"justify": "right", "style": "dim"}), ("Max", {"justify": "right", "style": "dim"}),
                    ("hwmon", {"style": "dim"})):
        t.add_column(col, **kw)
    if s is None:
        return t
    last = None
    for r in s.sensors:
        if r.kind == "energy":
            continue
        lo, hi = h.minmax.get((r.chip, r.hwmon, r.key), (r.value, r.value))
        digits = 0 if r.kind in ("fan", "pwm") else 3 if r.kind == "in" else 1
        fmt = f"{{:.{digits}f}} {r.unit}"
        val = Text(fmt.format(r.value), justify="right")
        if r.kind == "temp":
            val.stylize("#e5383b" if r.value >= 85 else "#f2c14e" if r.value >= 70 else "#3dff7a" if r.value > 0 else "dim")
        elif r.kind == "fan" and r.value == 0:
            val.stylize("dim")
        chip = "" if (r.chip, r.hwmon) == last else r.chip
        if chip and last is not None:
            t.add_row("", "", "", "", "", "")
        last = (r.chip, r.hwmon)
        t.add_row(chip, r.label, val, fmt.format(lo), fmt.format(hi), r.hwmon if chip else "")
    return t


# ------------------------------------------------------------------------- IPC
IPC_COLS = ("Kind", "From", "To", "State", "▲ in", "▼ out", "")
IPC_KIND_STYLE = {"tcp": "#4fd1ff", "udp": "#5b9bd5", "unix": "#a78bfa", "pipe": "#3ddc84", "listen": "#8a9aad"}
KIND_ORDER = {"tcp": 0, "udp": 1, "unix": 2, "pipe": 3, "listen": 4}


def ipc_widths(total: int) -> list[int]:
    """Column widths for ``IPC_COLS`` in a table ``total`` cells wide (each column pads 1 each side)."""
    fixed = [8, 0, 0, 10, 10, 10, 1]
    room = max(30, total - sum(fixed) - 2 * len(fixed) - 2)
    fixed[1], fixed[2] = room * 9 // 20, room - room * 9 // 20
    return fixed


def ipc_sorted(links: list[IpcLink]) -> list[IpcLink]:
    """Busiest first, then by kind, then by how many channels a row folds."""
    return sorted(links, key=lambda k: (-k.bps, KIND_ORDER.get(k.kind, 9), -k.count))


def _end(n: IpcNode | None, nid: str, pid: int | None, end: str) -> Text:
    if n is not None and n.kind == "peer" and end.lstrip("[").startswith(n.name):
        return Text(end, style="bold")  # an unnamed peer: its address is its name
    t = Text(n.name if n else nid, style="bold" if n and n.kind != "process" else "")
    if n is not None and n.kind == "process" and n.pids:
        t.append(f" {n.pids[0]}", style="dim")
    elif pid is not None:
        t.append(f" [{pid}]", style="dim")
    if end:
        t.append(f"  {end}", style="#8a9aad")
    return t


def ipc_row(k: IpcLink, nodes: dict[str, IpcNode]) -> tuple:
    kind = Text(k.kind + (f" ×{k.count}" if k.count > 1 else ""), style=IPC_KIND_STYLE.get(k.kind, ""))
    a = _end(nodes.get(k.a), k.a, k.pid_a, k.a_end)
    b = _end(nodes.get(k.b), k.b, k.pid_b, k.b_end) if k.b else Text("listening", style="dim")
    rin = Text("▲" + human_bytes(k.in_bps, True), style=f"#{IN:06x}") if k.in_bps else Text("—", style="dim")
    rout = Text("▼" + human_bytes(k.out_bps, True), style=f"#{OUT:06x}") if k.out_bps else Text("—", style="dim")
    tag = Text("●", style="#3dff7a") if k.prov == "live" else Text("◐", style="#f2c14e")
    return kind, a, b, Text(k.state, style="dim"), rin, rout, tag


def ipc_node(ipc: IpcStat, nid: str, members: set[str] | None = None) -> IpcNode | None:
    """The node behind a map id. A folded row of same-name processes (``members``) becomes
    one summed process node."""
    ids = members or {nid}
    ms = [n for n in ipc.nodes if n.id in ids]
    if not ms:
        return None
    if len(ms) == 1 and ms[0].id == nid:
        return ms[0]
    measured = any(m.in_bps is not None or m.out_bps is not None for m in ms)
    return IpcNode(nid, "process", f"{ms[0].name} ×{len(ms)}", ms[0].sub, pids=sorted(p for m in ms for p in m.pids),
                   in_bps=sum(m.in_bps or 0 for m in ms) if measured else None,
                   out_bps=sum(m.out_bps or 0 for m in ms) if measured else None, cmd=ms[0].cmd)


def ipc_rows(ipc: IpcStat, nid: str, h: History | None = None, members: set[str] | None = None) -> list[Row]:
    """Everything known about one node of the IPC map, each value tagged live or derived."""
    n = ipc_node(ipc, nid, members)
    if n is None:
        return []
    out: list[Row] = []
    ids = members or {nid}
    links = [k for k in ipc.links if k.a in ids or k.b in ids]
    talk = [k for k in links if k.kind != "listen"]
    if n.kind == "process":
        if n.pids:
            out.append(Row("PID", ", ".join(map(str, n.pids[:8])), LIVE))
        if n.sub:
            out.append(Row("User" if n.id != "hidden" else "Note", n.sub, LIVE if n.id != "hidden" else DERIVED))
        if n.cmd:
            out.append(Row("Command", n.cmd[:160], LIVE))
    elif n.kind in ("container", "vm"):
        out.append(Row("Runtime", n.runtime or n.kind, LIVE))
        if n.sub:
            out.append(Row("Image" if n.kind == "container" else "Service", n.sub, LIVE))
        if n.state:
            out.append(Row("Status", n.state, LIVE))
        if n.addrs:
            out.append(Row("Addresses", ", ".join(n.addrs), LIVE))
        net = f"own netns {n.netns}" if n.netns else f"tap {n.iface}" if n.iface else "host network"
        out.append(Row("Network", net, LIVE))
        if n.ports:
            out.append(Row("Published", ", ".join(n.ports), LIVE))
        if n.pids:
            more = f" (+{len(n.pids) - 8})" if len(n.pids) > 8 else ""
            out.append(Row("PIDs", ", ".join(map(str, n.pids[:8])) + more, LIVE))
    elif n.kind == "peer":
        out.append(Row("Address", ", ".join(n.addrs), LIVE))
        if n.dns:
            out.append(Row("Name", f"{n.dns} (reverse DNS)", DERIVED))
        if n.sub:
            out.append(Row("MAC", n.sub, LIVE))
        if n.iface:
            out.append(Row("Via", n.iface, LIVE))
        if n.router:
            out.append(Row("Role", "default gateway", LIVE))
    elif n.kind == "host":
        if n.addrs:
            out.append(Row("Addresses", ", ".join(n.addrs[:4]), LIVE))
        c = ipc.counts
        out.append(Row("Channels", " · ".join(f"{c[k]} {k}" for k in KIND_ORDER if c.get(k)), LIVE))
        if ipc.note:
            out.append(Row("Hidden", ipc.note, DERIVED))
        return out
    if n.in_bps is not None or n.out_bps is not None:
        counters = n.kind in ("container", "vm") and bool(n.netns or n.iface)
        how = "interface counters" if counters else "sum of TCP links"
        out.append(Row("▲ in · ▼ out", f"▲{human_bytes(n.in_bps, True)} · ▼{human_bytes(n.out_bps, True)}  ({how})",
                       LIVE if counters or n.kind == "process" else DERIVED))
    if h is not None and n.kind in ("container", "vm", "peer", "wan"):
        spark = sparkline(list(h.get(f"ipc:{n.id}:in"))[-40:])
        if spark.strip():
            out.append(Row("▲ history", spark, LIVE))
    by_kind: dict[str, int] = {}
    for k in talk:
        by_kind[k.kind] = by_kind.get(k.kind, 0) + k.count
    if by_kind:
        out.append(Row("Links", " · ".join(f"{by_kind[k]} {k}" for k in KIND_ORDER if k in by_kind), LIVE))
    listens = sorted({k.a_end for k in links if k.kind == "listen"})
    if listens:
        extra = f" (+{len(listens) - 10})" if len(listens) > 10 else ""
        out.append(Row("Listening", ", ".join(listens[:10]) + extra, LIVE))
    who: dict[tuple[str, str], list[float]] = {}  # (other node, kind) → [channels, bytes/s]
    for k in talk:
        other = k.b if k.a in ids else k.a
        w = who.setdefault((other, k.kind), [0, 0.0])
        w[0] += k.count
        w[1] += k.bps
    peers = []
    for (other, kind), (count, bps) in sorted(who.items(), key=lambda kv: (-kv[1][1], -kv[1][0]))[:6]:
        on = ipc.node(other)
        rate = f" {human_bytes(bps, True)}" if bps else ""
        peers.append(f"{on.name if on else '?'} ({count} {kind}{rate})" if count > 1 else
                     f"{on.name if on else '?'} ({kind}{rate})")
    if peers:
        out.append(Row("Talks to", ", ".join(peers), LIVE))
    return out


def sparkline(values: list[float]) -> str:
    vmax = max(values, default=0.0)
    if vmax <= 0:
        return ""
    return "".join(" ▁▂▃▄▅▆▇█"[min(8, int(v / vmax * 8 + 0.5))] for v in values)
