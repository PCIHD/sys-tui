"""IPC map: this machine's processes and guests inside its frame, and the LAN beyond its NIC.

Layout, left to right:
- The machine frame. Inside it:
  - an arc gutter for Unix sockets and pipes between processes
  - the busiest processes, one row each
  - a channel with one vertical lane per destination
  - the containers and VMs column
- The NIC on the frame's right edge. A trunk runs from it to the LAN bus and the peers.
- The router, which links on to the Internet.

TCP routes carry two lanes, using the same convention as the board traces:
- ▲ cyan particles run towards the machine-side end.
- ▼ orange particles run away from it.

Links without byte counters are drawn as steady traces. Positions are sorted by name, not
traffic, so tiles stay put between polls.

A desktop runs dozens of daemons whose only links go to a hub: a process serving Unix sockets to
many others, like dbus-broker or pipewire. ``level`` picks which rows show: 0 only processes
that move bytes or reach outside the list (and hubs), 1 also those with direct Unix/pipe peers,
2 everything. Rows a selected node talks to always show. A hub's own arcs are drawn only while something is
selected; its clients carry a ◆ instead.
"""

from __future__ import annotations

import math
import time

from .. import bindings as B
from ..canvas import Canvas, mix, polyline_points, ramp, shade
from ..history import History
from ..telemetry import IpcLink, IpcNode, SystemSnapshot
from . import charts as ch
from .board import lanes
from .panels import TILE, Panel, _waiting

FRAME = 0x2E4560
TRACE = 0x1F2B3A
TRACE_HI = 0x5E7FA8
TEXT = 0xE6EDF3
SUB = 0x8A9AAD
ACCENT = {"process": 0x8FB8DE, "container": 0x5B9BD5, "vm": 0xC58CFF, "peer": 0x3DDC84, "wan": 0xF2C14E,
          "host": 0x39C5FF}
BADGE = {"podman": 0x8F6BD6, "docker": 0x2496ED, "distrobox": 0xE87A3C, "toolbox": 0xD6A33C, "lxc": 0x6BB86B,
         "qemu": 0xFF6B5A, "vm": 0xFF6B5A, "nspawn": 0x6BB8B0}
ARC = {"unix": 0x7A6BB8, "pipe": 0x4E9A6A, "shm": 0x3FB8AF, "tcp": 0x4F7FA8, "udp": 0x4F7FA8}
SELECT = 0x39C5FF
HUB_MIN = 10  # Unix clients that make a process a hub
STRIP_MIN = 8  # free rows under the peers before the host summary is drawn
STRIP_ROWS = 9  # its full height, NIC chart included
PULSE_S = 3.0  # a node with an event this recent flashes its accent: green for new, red for gone
GOOD = ("opened", "started", "listening", "new peer")
LEVELS = ("talkers", "talkers + peer IPC", "all processes")


def short(v: float | None) -> str:
    return ch.human_bytes(v, digits=0) if v else "0"


def rates(i: float | None, o: float | None) -> str:
    return f"▲{short(i)} ▼{short(o)}"


class IpcMap(Panel):
    title = "IPC — who talks to whom"
    animated = True

    def __init__(self) -> None:
        self.boxes: dict[str, tuple[int, int, int, int]] = {}  # node id → cell box, for clicks
        self.groups: dict[str, list[str]] = {}  # "group:<name>" row → the process node ids it folds
        self.selected: str | None = None
        self.highlight: set[str] = set()
        self._shown: set[str] = set()  # process rows last time: hysteresis keeps them
        self._phase: dict[str, float] = {}
        self._t: float | None = None
        self.level = 0  # LEVELS: which process rows show
        self.status = ""  # the app's filter state, shown in the footer

    def members(self, nid: str | None) -> set[str]:
        """Node ids behind a map id: a folded process row stands for all its processes."""
        return set(self.groups.get(nid, [nid])) if nid else set()

    # --------------------------------------------------------------------- layout
    def _rows(self, procs: list[IpcNode]) -> tuple[list[IpcNode], dict[str, str]]:
        """One row per process name: same-name processes (workers, tabs, helpers) fold into
        ``name ×n``. Returns the rows and node id → row id."""
        by_name: dict[str, list[IpcNode]] = {}
        for p in procs:
            by_name.setdefault(p.id if p.id == "hidden" else p.name, []).append(p)
        self.groups = {}
        rows, row_of = [], {}
        for name, ms in by_name.items():
            if len(ms) == 1:
                rows.append(ms[0])
                row_of[ms[0].id] = ms[0].id
                continue
            gid = f"group:{name}"
            self.groups[gid] = [m.id for m in ms]
            measured = any(m.in_bps is not None or m.out_bps is not None for m in ms)
            cpus = [m.cpu for m in ms if m.cpu is not None]
            rows.append(IpcNode(gid, "process", f"{name} ×{len(ms)}", pids=[q for m in ms for q in m.pids],
                                in_bps=sum(m.in_bps or 0 for m in ms) if measured else None,
                                out_bps=sum(m.out_bps or 0 for m in ms) if measured else None,
                                cpu=sum(cpus) if cpus else None))
            for m in ms:
                row_of[m.id] = gid
        return rows, row_of

    def _pick_rows(self, rows: list[IpcNode], row_of: dict[str, str], links: list[IpcLink], n: int,
                   pinned: set[str] = frozenset()) -> list[IpcNode]:
        score: dict[str, float] = {r.id: 100.0 if r.id in pinned else 0.0 for r in rows}
        for k in links:
            for end, other in ((k.a, k.b), (k.b, k.a)):
                rid = row_of.get(end)
                if rid in score:
                    score[rid] += 0.3 * k.count + math.log10(1 + k.bps) + (2.0 if other not in row_of else 0.0)
        for rid in self._shown:
            if rid in score:
                score[rid] *= 1.5
        if len(rows) > n:
            keep = sorted(rows, key=lambda r: score[r.id], reverse=True)[:max(0, n - 1)]
        else:
            keep = rows
        self._shown = {r.id for r in keep}
        return sorted(keep, key=lambda r: (r.name.lower(), r.id))

    def draw(self, cv: Canvas, s: SystemSnapshot | None, h: History, t: float) -> None:
        self.boxes = {}
        ipc = s.ipc if s is not None else None
        if ipc is None:
            return _waiting(cv)
        dt = 0.0 if self._t is None else max(0.0, min(0.5, t - self._t))
        self._t = t
        W, Hc = cv.w, cv.h
        if W < 40 or Hc < 10:
            cv.put_text(0, 0, "IPC map needs a bigger panel"[:W], ch.DIM)
            return
        nodes = {n.id: n for n in ipc.nodes}
        procs = [n for n in ipc.nodes if n.kind == "process"]
        guests = sorted((n for n in ipc.nodes if n.kind in ("container", "vm")),
                        key=lambda n: (n.kind != "container", n.name.lower()))
        peers = sorted((n for n in ipc.nodes if n.kind == "peer"), key=lambda n: (not n.router, _ipkey(n)))
        wan = nodes.get("wan")
        links = [k for k in ipc.links if k.kind != "listen" and k.b]
        all_rows, row_of = self._rows(procs)
        focus = self.members(self.selected) | {m for nid in self.highlight for m in self.members(nid)}

        # ---- hubs, quiet rows, and what the selection talks to
        partners: dict[str, set[str]] = {}  # row → rows it shares a Unix socket or pipe with
        clients: dict[str, set[str]] = {}  # row → rows connected to its Unix sockets
        netted: set[str] = set()  # rows with a route out of the process list
        for k in links:
            ra, rb = row_of.get(k.a), row_of.get(k.b)
            if ra is not None and rb is not None:
                if ra != rb:
                    partners.setdefault(ra, set()).add(rb)
                    partners.setdefault(rb, set()).add(ra)
                    if k.kind in ("unix", "shm"):  # b is the server, or the segment's anchor
                        clients.setdefault(rb, set()).add(ra)
            elif (ra or rb) and "host" not in (k.a, k.b):
                netted.add(ra or rb)
        hubs = {r for r, c in clients.items() if len(c) >= HUB_MIN}
        sel_rows = {row_of[m] for m in focus if m in row_of}
        near = set().union(*(partners.get(r, set()) for r in sel_rows)) - sel_rows
        pinned = sel_rows | near

        def quiet(r: IpcNode) -> bool:
            if self.level >= 2 or r.in_bps or r.out_bps or r.id in netted | hubs:
                return False
            return self.level == 0 or partners.get(r.id, set()) <= hubs

        hidden = {r.id for r in all_rows if r.id not in pinned and quiet(r)}
        cand = [r for r in all_rows if r.id not in hidden]

        # ---- columns, in cells
        outside = W >= 100 and (peers or wan)
        wan_w = 18 if outside and wan else 0
        peer_w = min(24, max(16, W // 8)) if outside else 0
        fx1 = W - (peer_w + wan_w + (6 if outside else 0)) if outside else W
        gutter = 8 if fx1 >= 80 else 4
        list_w = min(30, max(18, (fx1 - gutter) // 3))
        guest_w = min(32, max(18, (fx1 - gutter - list_w) // 2)) if guests else 0
        lx0, lx1 = 1 + gutter, 1 + gutter + list_w
        gx1 = fx1 - 3
        gx0 = gx1 - guest_w
        top, bottom = 3, Hc - 3  # first and last content row inside the frame

        # ---- process rows
        avail = bottom - top + 1
        rows = self._pick_rows(cand, row_of, links, avail, pinned)
        more = [p for p in procs if row_of[p.id] not in self._shown]
        n_quiet = sum(row_of[p.id] in hidden for p in more)
        step = 2 if (len(rows) + bool(more)) * 2 <= avail else 1  # a blank row between, when there is room
        slot: dict[str, str] = {}  # node id → its slot on the map
        row_y: dict[str, int] = {}
        for i, r in enumerate(rows):
            row_y[r.id] = top + i * step
            self.boxes[r.id] = (lx0, top + i * step, lx1, top + i * step + 1)
        if more:
            row_y["more"] = top + len(rows) * step
        for p in procs:
            slot[p.id] = row_of[p.id] if row_of[p.id] in row_y else "more"
        # ---- guest tiles: 2 rows + gap
        g_y: dict[str, int] = {}
        y = top + 1
        for g in guests:
            if y + 1 > bottom:
                break
            g_y[g.id] = y
            slot[g.id] = g.id
            self.boxes[g.id] = (gx0, y, gx1, y + 2)
            y += 3
        nic_y = 2  # the NIC street runs along this cell row
        for n in peers + ([wan] if wan else []):
            slot[n.id] = "nic"
        slot["host"] = "host"

        # ---- aggregate links onto map routes
        route: dict[tuple[str, str], list] = {}  # (inner slot, outer slot) → [in, out, measured, kinds]
        arcs: dict[tuple[str, str], list] = {}
        for k in links:
            sa, sb = slot.get(k.a), slot.get(k.b)
            if sa is None or sb is None or sa == sb:
                continue
            ka, kb = _rank(sa, g_y), _rank(sb, g_y)
            if ka == kb == 0 or ka == kb == 1:  # process↔process, guest↔guest
                pair = tuple(sorted((sa, sb)))
                a = arcs.setdefault(pair, [0, set(), {k.a, k.b}])
                a[0] += k.count
                a[1].add(k.kind)
                a[2] |= {k.a, k.b}
                continue
            if ka > kb:  # the inner (machine-side) end first; flip the rates with it
                sa, sb, i, o = sb, sa, k.out_bps, k.in_bps
            else:
                i, o = k.in_bps, k.out_bps
            if sb == "host":
                continue  # a guest's gateway or a host-wide listener: the table has them
            r = route.setdefault((sa, sb), [0.0, 0.0, False, set(), set()])
            r[0] += i or 0.0
            r[1] += o or 0.0
            r[2] = r[2] or i is not None or o is not None
            r[3].add(k.kind)
            r[4] |= {k.a, k.b}

        # ---- lanes in the channel: one per destination, nearest the guest column first
        dests = [d for d in [*(g.id for g in guests if g.id in g_y), "nic"]
                 if any(key[1] == d for key in route)]
        ch_x0, ch_x1 = lx1 + 2, gx0 - 5 if guests else fx1 - 4
        lane_x = {}
        for j, d in enumerate(dests):
            lane_x[d] = max(ch_x0, ch_x1 - 2 * j)
        nic_x = fx1 - 1  # the frame's right edge, in pixels (cells = pixel columns)
        nic_py = nic_y * 2 + 1

        paths: list[tuple[str, list, float | None, float | None, bool, set[str]]] = []
        for (sa, sb), (i, o, measured, _kinds, ids) in route.items():
            if sa not in row_y and sa not in g_y:
                continue
            if sa in row_y:
                y0 = row_y[sa] * 2 + 1
                x0 = lx1
                lx = lane_x.get(sb)
                if lx is None:
                    continue
                if sb == "nic":
                    pts = [(x0, y0), (lx, y0), (lx, nic_py), (nic_x, nic_py)]
                else:
                    gy = g_y[sb] * 2 + 1
                    pts = [(x0, y0), (lx, y0), (lx, gy), (gx0 - 1, gy)]
            else:  # guest → NIC, up the bus inside the frame edge
                gy = g_y[sa] * 2 + 1
                bus = fx1 - 2
                pts = [(gx1, gy), (bus, gy), (bus, nic_py), (nic_x, nic_py)]
            paths.append((f"{sa}>{sb}", pts, i if measured else None, o if measured else None, measured, ids))

        # ---- outside: the LAN bus, peers and the Internet
        p_box: dict[str, tuple[int, int, int, int]] = {}
        strip = None  # the host summary under the peers, when there is room
        if outside:
            lan_x = fx1 + 3
            px0, px1 = lan_x + 2, lan_x + 2 + peer_w
            y = nic_y
            for n in peers:
                if y + 1 > Hc - 2:
                    break
                p_box[n.id] = (px0, y, px1, y + 2)
                y += 3
            if wan is not None:
                router = next((n for n in peers if n.router and n.id in p_box), None)
                if router is not None:
                    wx0 = px1 + 3
                    p_box["wan"] = (wx0, p_box[router.id][1], min(W - 1, wx0 + wan_w - 2), p_box[router.id][1] + 2)
                elif y + 1 <= Hc - 2:
                    p_box["wan"] = (px0, y, px1, y + 2)
            strip_y = max((b[3] for b in p_box.values()), default=nic_y) + 1
            if Hc - 2 - strip_y >= STRIP_MIN:
                strip = (px0, strip_y, W - 1, min(Hc - 2, strip_y + STRIP_ROWS))
            ext_in = sum(n.in_bps or 0 for n in peers + ([wan] if wan else []))
            ext_out = sum(n.out_bps or 0 for n in peers + ([wan] if wan else []))
            last_y = max((b[1] for nid, b in p_box.items() if not (nid == "wan" and b[0] > px0)), default=nic_y) * 2 + 1
            paths.append(("trunk", [(nic_x, nic_py), (lan_x, nic_py), (lan_x, last_y)], ext_in, ext_out, True, set()))
            for nid, (bx0, by0, bx1, by1) in p_box.items():
                n = nodes[nid]
                py = by0 * 2 + 1
                if nid == "wan" and bx0 > px0:  # beside the router
                    r = next(b for k2, b in p_box.items() if nodes[k2].router)
                    pts = [(r[2], py), (bx0 - 1, py)]
                else:
                    pts = [(lan_x, py), (bx0 - 1, py)]
                paths.append((nid, pts, n.in_bps, n.out_bps, True, {nid}))
                self.boxes[nid] = (bx0, by0, bx1, by1)

        # ================================================================= draw
        cv.fill(1, 4, fx1 - 1, 2 * (Hc - 2), mix(0x0D1117, FRAME, 0.05))
        cv.frame(0, 3, fx1, 2 * (Hc - 2) + 1, FRAME)
        hostn = nodes.get("host")
        title = f" ▣ this machine · {hostn.name if hostn else 'host'} "
        cv.put_text(2, 1, title[: fx1 - 4], ACCENT["host"], None, True)
        self.boxes["host"] = (2, 1, 2 + min(len(title), fx1 - 4), 2)
        # NIC port
        cv.fill(nic_x - 1, nic_py - 2, nic_x + 1, nic_py + 3, 0x6E7A8A)
        if outside:
            cv.put_text(fx1 - 4, nic_y - 1, "NIC", SUB)

        for key, pts, i, o, measured, ids in paths:
            hi = bool(focus & ids)
            inner, outer = lanes(pts)
            col = TRACE_HI if hi else TRACE
            for x, yy in inner:
                cv.set(x, yy, col)
            if measured:
                for x, yy in outer:
                    cv.set(x, yy, col)
            if measured:
                li, lo = B.level(i), B.level(o)
                self._flow(cv, inner, self._advance(key + ":i", dt, li), li, ch.IN, reverse=True)
                self._flow(cv, outer, self._advance(key + ":o", dt, lo), lo, ch.OUT, reverse=False)

        # arcs: process↔process in the left gutter, guest↔guest left of the guest column. A desktop
        # has hundreds of socketpairs: unless something is selected, only the busiest few are drawn.
        proc_arcs = {p: q for p, q in arcs.items() if p[0] in row_y and p[1] in row_y}
        if focus:
            proc_arcs = {p: q for p, q in proc_arcs.items() if focus & q[2]}
        else:
            proc_arcs = {p: q for p, q in proc_arcs.items() if not (hubs | {"more"}) & set(p)}
            proc_arcs = dict(sorted(proc_arcs.items(), key=lambda kv: -kv[1][0])[:2 * gutter])
        self._arcs(cv, proc_arcs, row_y, lx0, gutter, focus)
        self._arcs(cv, {p: q for p, q in arcs.items() if p[0] in g_y and p[1] in g_y}, g_y, gx0, 3, focus)

        # event pulses, keyed by map id: a process's row, or the guest / peer itself
        pulse: dict[str, tuple[int, float]] = {}
        wall = time.time()
        for e in reversed(ipc.events):
            age = wall - e.t
            if age > PULSE_S:
                break
            if age < 0:
                continue
            col = ch.GREEN if e.verb in GOOD else ch.RED
            amt = (1 - age / PULSE_S) * (0.6 + 0.4 * math.cos(age * 7))
            for nid in (e.a, e.b):
                if nid:
                    pulse.setdefault(row_of.get(nid, nid), (col, amt))

        # tiles
        nlinks: dict[str, int] = {}
        for k in links:
            for end in {slot.get(k.a), slot.get(k.b)} - {None}:
                nlinks[end] = nlinks.get(end, 0) + k.count
        rel: dict[str, dict[str, int]] = {}  # visible partner row → {kind: channels} with the selection
        for k in links:
            ra, rb = row_of.get(k.a), row_of.get(k.b)
            for mine, other in ((ra, rb), (rb, ra)):
                if mine in sel_rows and other in near:
                    rel.setdefault(other, {})
                    rel[other][k.kind] = rel[other].get(k.kind, 0) + k.count
        for r in rows:
            tag = None
            if r.id in rel:
                tag = " ".join(f"{kd}×{c}" if c > 1 else kd for kd, c in sorted(rel[r.id].items()))
            elif r.id in hubs:
                tag = f"hub · {len(clients[r.id])} clients"
            self._row(cv, r, row_y[r.id], lx0, lx1, bool(self.members(r.id) & focus), nlinks.get(r.id, 0),
                      tag, r.id not in hubs and bool(partners.get(r.id, set()) & hubs), pulse.get(r.id))
        if more:
            yy = row_y["more"]
            n_over = len(more) - n_quiet
            if n_over and n_quiet:
                chip = f"+{n_over} more · +{n_quiet} quiet (i)"
            else:
                chip = f"+{n_quiet} quiet processes (i)" if n_quiet else f"+{n_over} more processes"
            cv.put_text(lx0 + 1, yy, chip[: list_w - 1], ch.DIM)
        for g in guests:
            if g.id in g_y:
                self._guest(cv, g, g_y[g.id], gx0, gx1, g.id in focus, pulse.get(g.id))
        if len(g_y) < len(guests):
            cv.put_text(gx0, bottom, f"+{len(guests) - len(g_y)} more guests", ch.DIM)
        for nid, b in p_box.items():
            self._peer(cv, nodes[nid], b, nid in focus, pulse.get(nid))
        if strip is not None and s is not None:
            self._strip(cv, s, h, strip, "host" in focus)
            self.boxes["host#strip"] = strip
        if not outside and (peers or wan):
            cv.put_text(max(0, fx1 - 30), nic_y, f"LAN {len(peers)} peers{' · Internet' if wan else ''}"[:28], SUB)

        # header and footer
        c = ipc.counts
        head = (f"{c.get('tcp', 0)} tcp · {c.get('udp', 0)} udp · {c.get('unix', 0)} unix · {c.get('pipe', 0)} pipe"
                f" · {c.get('shm', 0)} shm · {c.get('listen', 0)} listening · {len(guests)} guests"
                f" · {len(peers)} LAN peers")
        cv.put_text(1, 0, head[: W - 2], 0xC9D1D9, None, True)
        legend = "▲ towards this machine ▼ away (B/s)  ━ unix ━ pipe ━ shm"
        if len(head) + len(legend) + 4 <= W:
            ch.text_right(cv, W - 1, 0, legend, ch.DIM)
            x_leg = W - 1 - len(legend)
            for kind in ("unix", "pipe", "shm"):
                cv.put_text(x_leg + legend.index(f"━ {kind}"), 0, "━", shade(ARC[kind], 1.4))
        foot = "  ·  ".join(x for x in (f"i: {LEVELS[self.level]}", self.status,
                                         ipc.note or "click a node · Tab cycles") if x)
        cv.put_text(1, Hc - 1, foot[: W - 2], ch.DIM)

    # --------------------------------------------------------------------- pieces
    def _row(self, cv: Canvas, p: IpcNode, y: int, x0: int, x1: int, sel: bool, nlinks: int,
             tag: str | None = None, hub_client: bool = False, pulse: tuple[int, float] | None = None) -> None:
        """One process row. ``tag`` replaces the right-hand text: how the row relates to the
        selection, or a hub's client count. ``hub_client`` adds a ◆ after the name."""
        acc = ACCENT["process"] if p.id != "hidden" else ch.DIM
        bg = mix(TILE, acc, 0.22 if sel else 0.06 + 0.04 * (y % 2))
        cv.fill(x0, y * 2, x1, y * 2 + 2, bg)
        if p.cpu is not None and p.cpu >= 1:  # CPU as a tint behind the row, so text stays legible on it
            frac = min(1.0, p.cpu / 100)
            cv.fill(x0 + 1, y * 2, x0 + 1 + max(1, round(frac * (x1 - x0 - 1))), y * 2 + 2,
                    mix(bg, ramp(frac, ch.LOAD_STOPS), 0.3))
        cv.fill(x0, y * 2, x0 + 1, y * 2 + 2, SELECT if sel else _pulsed(acc, pulse))
        measured = bool(p.in_bps or p.out_bps) and tag is None
        busy = p.cpu is not None and p.cpu >= 1
        links_txt = f"{nlinks} link{'s' * (nlinks != 1)}"
        r = rates(p.in_bps, p.out_bps) if measured else tag or (f"{p.cpu:.0f}% · {links_txt}" if busy else links_txt)
        room = x1 - x0 - 2 - len(r) - 1 - 2 * hub_client
        name = p.name[:max(1, room)]
        cv.put_text(x0 + 1, y, name, TEXT if p.id != "hidden" else SUB, None, sel)
        if hub_client:
            cv.put_text(x0 + 2 + len(name), y, "◆", ARC["unix"])
        if measured:
            k = r.index("▼")
            ch.text_right(cv, x1 - 1, y * 2, r, ch.IN)
            ch.text_right(cv, x1 - 1, y * 2, r[k:], ch.OUT)
        elif room > 0:
            ch.text_right(cv, x1 - 1, y * 2, r, shade(ARC["unix"], 1.6) if tag and not tag.startswith("hub") else ch.DIM)

    def _tile(self, cv: Canvas, box, acc: int, sel: bool, pulse: tuple[int, float] | None = None) -> None:
        x0, y0, x1, y1 = box
        cv.fill(x0, y0 * 2, x1, y1 * 2, mix(TILE, acc, 0.24 if sel else 0.12))
        cv.fill(x0, y0 * 2, x0 + 1, y1 * 2, _pulsed(acc, pulse))
        if sel:
            cv.frame(x0, y0 * 2, x1, y1 * 2, SELECT)

    def _guest(self, cv: Canvas, g: IpcNode, y: int, x0: int, x1: int, sel: bool,
               pulse: tuple[int, float] | None = None) -> None:
        acc = ACCENT[g.kind]
        self._tile(cv, (x0, y, x1, y + 2), acc, sel, pulse)
        badge = g.runtime or g.kind
        cv.put_text(x0 + 1, y, badge, TEXT, BADGE.get(badge, 0x3A4658), True)
        res = ""
        if g.cpu is not None or g.rss is not None:
            res = " ".join(x for x in (f"{g.cpu:.0f}%" if g.cpu is not None else "",
                                       ch.human_bytes(g.rss, digits=0) if g.rss else "") if x)
        room = x1 - x0 - len(badge) - 3
        if res and room - len(res) - 1 >= 8:
            ch.text_right(cv, x1 - 1, y * 2, res, ramp(min(1.0, (g.cpu or 0) / 100), ch.LOAD_STOPS) if g.cpu else SUB)
            room -= len(res) + 1
        cv.put_text(x0 + 2 + len(badge), y, g.name[:room], TEXT, None, True)
        r = rates(g.in_bps, g.out_bps)
        cv.put_text(x0 + 1, y + 1, r[: x1 - x0 - 2], ch.IN)
        cv.put_text(x0 + 1 + r.index("▼"), y + 1, r[r.index("▼"):][: max(0, x1 - x0 - 2 - r.index("▼"))], ch.OUT)
        tail = g.addrs[0] if g.addrs else ("host net" if g.kind == "container" and g.netns is None else g.iface)
        if tail and len(r) + len(tail) + 3 <= x1 - x0:
            ch.text_right(cv, x1 - 1, (y + 1) * 2, tail, SUB)

    def _peer(self, cv: Canvas, n: IpcNode, box, sel: bool, pulse: tuple[int, float] | None = None) -> None:
        x0, y0, x1, _ = box
        acc = ACCENT[n.kind]
        self._tile(cv, box, acc, sel, pulse)
        name = ("⌂ " if n.router else "") + n.name
        cv.put_text(x0 + 1, y0, name[: x1 - x0 - 2], TEXT, None, True)
        r = rates(n.in_bps, n.out_bps)
        cv.put_text(x0 + 1, y0 + 1, r[: x1 - x0 - 2], ch.IN)
        k = r.index("▼")
        cv.put_text(x0 + 1 + k, y0 + 1, r[k:][: max(0, x1 - x0 - 2 - k)], ch.OUT)

    def _strip(self, cv: Canvas, s: SystemSnapshot, h: History, box, sel: bool) -> None:
        """This machine at a glance: CPU, memory, NIC traffic, TCP states, kernel error counters."""
        x0, y0, x1, y1 = box
        ipc = s.ipc
        self._tile(cv, box, ACCENT["host"], sel)
        w = x1 - x0 - 2
        y = y0
        cv.put_text(x0 + 1, y, "this machine"[:w], ACCENT["host"], None, True)
        y += 1

        def gauge(label: str, frac: float, tail: str) -> None:
            nonlocal y
            cv.put_text(x0 + 1, y, label, SUB)
            bx0, bx1 = x0 + 5, x1 - 2 - len(tail)
            if bx1 - bx0 >= 4:
                ch.hbar(cv, (bx0, y * 2, bx1, y * 2 + 2), max(0.0, min(1.0, frac)))
            ch.text_right(cv, x1 - 1, y * 2, tail, TEXT)
            y += 1

        gauge("CPU", s.cpu.util / 100, f"{s.cpu.util:3.0f}%")
        if s.mem.total:
            used = s.mem.total - s.mem.available
            gauge("MEM", used / s.mem.total, f"{ch.human_bytes(used, digits=0)}/{ch.human_bytes(s.mem.total, digits=0)}")
        rx, tx = list(h.get("net:all:rx")), list(h.get("net:all:tx"))
        if y1 - y >= 6:
            cv.put_text(x0 + 1, y, "NIC", SUB)
            r = rates(rx[-1] if rx else None, tx[-1] if tx else None)
            cv.put_text(x0 + 5, y, r[: w - 4], ch.IN)
            k = r.index("▼")
            cv.put_text(x0 + 5 + k, y, r[k:][: max(0, w - 4 - k)], ch.OUT)
            n = x1 - x0 - 2
            vmax = ch.nice_max(max(rx[-n:] + tx[-n:], default=0.0), 1e3)
            ch.mirrored(cv, (x0 + 1, (y + 1) * 2, x1 - 1, (y + 3) * 2), ch.fit(rx, n), ch.fit(tx, n), vmax)
            y += 3
        if ipc is None:
            return
        st = ipc.tcp_states
        if y <= y1 - 1 and st:
            parts = [(f"{st.get('ESTAB', 0)} estab", TEXT)]
            if st.get("CLOSE-WAIT"):  # an app that never closes its end: worth the room before the rest
                parts.append((f"{st['CLOSE-WAIT']} close-wait", ch.ORANGE))
            parts += [(f"{st.get('TIME-WAIT', 0)} time-wait", SUB), (f"{st.get('LISTEN', 0)} listen", SUB)]
            x = x0 + 1
            cv.put_text(x, y, "TCP", SUB)
            x += 4
            for txt, col in parts:
                if x + len(txt) > x1 - 1:
                    break
                cv.put_text(x, y, txt, col)
                x += len(txt) + 1
            y += 1
        k = ipc.kernel
        if y <= y1 - 1 and any(v is not None for v in k.values()):
            items = [("retrans", k.get("Tcp.RetransSegs")), ("rst", k.get("Tcp.OutRsts")),
                     ("drops", k.get("TcpExt.ListenDrops"))]
            x = x0 + 1
            for label, v in items:
                txt = f"{label} {v:.1f}/s" if v else f"{label} 0"
                if x + len(txt) > x1 - 1:
                    break
                cv.put_text(x, y, txt, ch.RED if v and label != "rst" else ch.ORANGE if v else SUB)
                x += len(txt) + 2
            y += 1
        ct, mx = k.get("conntrack"), k.get("conntrack_max")
        if y <= y1 - 1 and ct is not None:
            txt = f"conntrack {ct:.0f}" + (f" of {mx / 1e3:.0f}k" if mx and mx >= 1e4 else f" of {mx:.0f}" if mx else "")
            cv.put_text(x0 + 1, y, txt[:w], ch.RED if mx and ct > 0.8 * mx else SUB)

    def _arcs(self, cv: Canvas, arcs: dict, ys: dict[str, int], x_edge: int, width: int, focus: set[str]) -> None:
        """Nested arcs left of a column: shorter spans inside, one 2-px level each."""
        levels: list[list[tuple[int, int]]] = []
        for (a, b), (count, kinds, ids) in sorted(arcs.items(), key=lambda kv: abs(ys[kv[0][0]] - ys[kv[0][1]])):
            y0, y1 = sorted((ys[a] * 2 + 1, ys[b] * 2 + 1))
            lv = next((i for i, used in enumerate(levels) if all(y1 < u0 - 1 or y0 > u1 + 1 for u0, u1 in used)), None)
            if lv is None:
                lv = len(levels)
                levels.append([])
            levels[lv].append((y0, y1))
            x = x_edge - 2 - 2 * min(lv, max(0, width // 2 - 1))
            kind = "pipe" if kinds == {"pipe"} else next(iter(kinds - {"pipe"}), "unix")
            col = ARC.get(kind, ARC["unix"])
            col = shade(col, 1.7) if focus & ids else mix(0x0D1117, col, 0.7)
            pts = polyline_points([(x_edge - 1, y0), (x, y0), (x, y1), (x_edge - 1, y1)])
            for px, py in pts:
                cv.set(px, py, col)

    def _advance(self, key: str, dt: float, lv: float) -> float:
        ph = self._phase.get(key, 0.0) + dt * (4 + 50 * lv)
        self._phase[key] = ph
        return ph

    @staticmethod
    def _flow(cv: Canvas, path, phase: float, lv: float, color: int, reverse: bool) -> None:
        """Particles along ``path``; ``reverse`` runs them from its end back to its start."""
        n = len(path)
        if n < 2 or lv <= 0.003:
            return
        count = max(1, int(n / 7 * (0.15 + lv)))
        spacing = n / count
        for k in range(count):
            i = int((phase + k * spacing) % n)
            for tail in range(3):
                j = (n - 1 - i + tail) if reverse else (i - tail)
                if 0 <= j < n:
                    x, y = path[j]
                    cv.blend(x, y, color, 1.0 - tail * 0.35)


def _pulsed(acc: int, pulse: tuple[int, float] | None) -> int:
    return mix(acc, pulse[0], max(0.0, min(1.0, pulse[1]))) if pulse else acc


def _rank(slot: str, g_y: dict) -> int:
    """0 process row, 1 guest, 2 outside (NIC), 3 the host itself."""
    if slot == "nic":
        return 2
    if slot == "host":
        return 3
    return 1 if slot in g_y else 0


def _ipkey(n: IpcNode):
    import ipaddress

    try:
        a = ipaddress.ip_address(n.addrs[0] if n.addrs else n.name)
        return (a.version, int(a))
    except ValueError:
        return (9, 0)
