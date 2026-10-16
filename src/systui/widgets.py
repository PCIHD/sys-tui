"""Textual widgets: the animated board view, pixel chart panels, the IPC map, part detail, GPU gauges."""

from __future__ import annotations

import time

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Input, Static

from . import bindings as B
from .boards import Match
from .components import DERIVED, LIVE, SPEC, rows_for
from .history import History
from pixboard.canvas import transition_frame

from .render.board import BoardScene
from .render.panels import Panel
from .telemetry import SystemSnapshot

PROV_STYLE = {LIVE: ("●", "#3dff7a"), DERIVED: ("◐", "#f2c14e"), SPEC: ("○", "#8a9aad")}


class BoardView(Widget, can_focus=True):
    """Renders the board scene at ~15 fps and handles hover / click / keyboard selection."""

    DEFAULT_CSS = "BoardView { width: 1fr; height: 1fr; }"
    BINDINGS = [
        Binding("tab", "cycle(1)", "Next part", priority=True),
        Binding("shift+tab", "cycle(-1)", "Prev part", show=False, priority=True),
        Binding("left", "move(-1,0)", show=False),
        Binding("right", "move(1,0)", show=False),
        Binding("up", "move(0,-1)", show=False),
        Binding("down", "move(0,1)", show=False),
    ]

    TRANSITION_S = 0.55

    class Selected(Message):
        def __init__(self, cid: str | None) -> None:
            self.cid = cid
            super().__init__()

    class Transitioned(Message):
        def __init__(self, layer: str) -> None:
            self.layer = layer
            super().__init__()

    def __init__(self, match: Match, **kw) -> None:
        super().__init__(**kw)
        self.match = match
        self.mode = "live"
        self.labels = True
        self.card = True
        self.snapshot: SystemSnapshot | None = None
        self.selected: str | None = None
        self.hover: str | None = None
        self.highlight: set[str] = set()
        self.scene_layer: str = "board"  # board | gpu-card | gpu-front | gpu-back | gpu-die | gpu-cores | cpu-die
        self.gpu_match = None
        self.gpu_snap = None
        self.gpu_history = None
        self.scene = None
        self._frame = None
        self._t0 = time.monotonic()
        self._built: tuple = ()
        self.paused_anim = False
        self._xfade: dict | None = None

    def on_mount(self) -> None:
        self.set_interval(1 / 15, self._tick)

    @property
    def transitioning(self) -> bool:
        return self._xfade is not None

    def begin_transition(self, target_layer: str, focus_id: str | None = None, inbound: bool = True,
                         duration: float | None = None) -> None:
        """Fly from the current scene into ``target_layer``. No-op if a transition is already running."""
        if self._xfade is not None or self.scene is None or self._frame is None:
            if self._xfade is None:
                self.scene_layer = target_layer
                self.selected = None
                self.rebuild()
            return
        w, h = self.content_size.width, self.content_size.height
        if w < 4 or h < 2:
            self.scene_layer = target_layer
            self.rebuild()
            return
        outgoing = self._frame.copy()
        from_scene = self.scene
        old_layer = self.scene_layer
        self.scene_layer = target_layer
        try:
            to_scene = self._make_scene(w, h)
            snap = self.gpu_snap if target_layer.startswith("gpu-") else self.snapshot
            incoming = to_scene.frame(0.0, snap, None, None, self.mode)
        except Exception:
            self.scene_layer = target_layer
            self.selected = None
            self.rebuild()
            return
        self.scene_layer = old_layer
        focus = None
        src = from_scene if inbound else to_scene
        if focus_id and src and focus_id in src.boxes:
            focus = src.boxes[focus_id]
        elif inbound and from_scene is not None:
            for cid in ("GPU", "CPU"):
                if cid in from_scene.boxes:
                    focus = from_scene.boxes[cid]
                    break
        elif to_scene is not None:
            for cid in ("GPU", "CPU"):
                if cid in to_scene.boxes:
                    focus = to_scene.boxes[cid]
                    break
        self._xfade = {
            "from": outgoing, "to": incoming, "focus": focus, "inbound": inbound,
            "t0": time.monotonic(), "dur": duration if duration is not None else self.TRANSITION_S,
            "layer": target_layer, "scene": to_scene, "size": (w, h),
        }

    def rebuild(self) -> None:
        if self._xfade is not None:
            return
        w, h = self.content_size.width, self.content_size.height
        if w < 4 or h < 2:
            self.scene = None
            return
        self.scene = self._make_scene(w, h)
        self._built = (w, h, self.scene_layer)
        if self.scene is None:
            return
        if self.selected not in self.scene.comps:
            pref = "GPU" if self.scene_layer.startswith("gpu-") else "CORE0" if self.scene_layer == "cpu-die" else "CPU"
            self.selected = pref if pref in self.scene.comps else next(iter(self.scene.order), None)
            self.post_message(self.Selected(self.selected))
        title = self.scene.title
        if self.scene_layer.startswith("gpu-") and self.gpu_match is not None:
            title = f"{self.match.profile.short} › {title}"
        self.border_title = title
        self._render_frame()

    def _make_scene(self, w: int, h: int):
        if self.scene_layer.startswith("gpu-") and self.gpu_match is not None:
            from gputui.render.card import CardScene
            from gputui.render.cores import CoresScene
            from gputui.render.die import DieScene
            from gputui.render.pcb import PcbScene

            m = self.gpu_match
            view = self.scene_layer[4:]  # card|front|back|die|cores
            if view == "card":
                return CardScene(m.profile, m.sku, w, h, labels=self.labels)
            if view == "die":
                return DieScene(m.profile, m.sku, w, h, labels=self.labels)
            if view == "cores":
                return CoresScene(m.profile, m.sku, w, h, labels=self.labels, history=self.gpu_history)
            return PcbScene(m.profile, m.sku, w, h, side=view if view in ("front", "back") else "front",
                            labels=self.labels)
        if self.scene_layer == "cpu-die":
            from .render.cpudie import CpuDieScene
            return CpuDieScene(w, h, self.snapshot, labels=self.labels)
        return BoardScene(self.match.profile, w, h, labels=self.labels, hint=self.snapshot, card=self.card)

    def set_match(self, match: Match) -> None:
        self.match = match
        self.selected = None
        self.rebuild()

    def select(self, cid: str | None) -> None:
        self.selected = cid
        self.post_message(self.Selected(cid))

    def action_cycle(self, step: int) -> None:
        if self._xfade is not None or not self.scene or not self.scene.order:
            return
        order = self.scene.order
        i = order.index(self.selected) if self.selected in order else -1
        self.select(order[(i + step) % len(order)])

    def action_move(self, dx: int, dy: int) -> None:
        if self.scene and self._xfade is None:
            self.select(self.scene.neighbour(self.selected, dx, dy))

    def on_resize(self, event: events.Resize) -> None:
        self.rebuild()

    def _tick(self) -> None:
        if not self.display or not self.is_on_screen:
            return
        if self._xfade is None and (self.content_size.width, self.content_size.height, self.scene_layer) != self._built:
            self.rebuild()
        self._render_frame()
        self.refresh()

    def _render_frame(self) -> None:
        xf = self._xfade
        if xf is not None:
            p = (time.monotonic() - xf["t0"]) / max(0.05, xf["dur"])
            self._frame = transition_frame(xf["from"], xf["to"], xf["focus"], min(1.0, p), xf["inbound"])
            if p >= 1.0:
                self.scene_layer = xf["layer"]
                self.scene = xf["scene"]
                self._built = (*xf["size"], xf["layer"])
                self._xfade = None
                pref = "GPU" if self.scene_layer.startswith("gpu-") else "CORE0" if self.scene_layer == "cpu-die" else "CPU"
                self.selected = pref if self.scene and pref in self.scene.comps else (
                    next(iter(self.scene.order), None) if self.scene else None)
                title = self.scene.title if self.scene else ""
                if self.scene_layer.startswith("gpu-") and self.gpu_match is not None:
                    title = f"{self.match.profile.short} › {title}"
                self.border_title = title
                self.post_message(self.Selected(self.selected))
                self.post_message(self.Transitioned(self.scene_layer))
            return
        if self.scene is None:
            self._frame = None
            return
        t = time.monotonic() - self._t0
        snap = self.gpu_snap if self.scene_layer.startswith("gpu-") else self.snapshot
        self._frame = self.scene.frame(t, snap, self.selected, self.hover, self.mode,
                                       highlight=self.highlight if self.scene_layer == "board" else None)

    def render_line(self, y: int) -> Strip:
        width = self.content_size.width
        cv = self._frame
        if cv is None or y >= cv.h:
            return Strip.blank(width)
        return Strip(cv.segments(y), cv.w).crop(0, width)

    def _cid_at(self, event: events.MouseEvent) -> str | None:
        off = event.get_content_offset(self)
        if off is None or self.scene is None or self._xfade is not None:
            return None
        return self.scene.hit(off.x, off.y)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        self.hover = self._cid_at(event)
        if self.hover and self.scene:
            c = self.scene.comps[self.hover]
            self.tooltip = f"{c.id} — {c.kind_name}" + (f"\n{c.part}" if c.part else "")
        else:
            self.tooltip = None

    def on_leave(self, event: events.Leave) -> None:
        self.hover = None

    def on_click(self, event: events.Click) -> None:
        if self._xfade is not None:
            return
        cid = self._cid_at(event)
        if cid:
            self.select(cid)


class PanelView(Widget):
    """Hosts one pixel ``Panel``; redraws on new data, on resize, and (if animated) at 12 fps."""

    DEFAULT_CSS = "PanelView { width: 1fr; height: 1fr; }"

    def __init__(self, panel: Panel, history: History, **kw) -> None:
        super().__init__(**kw)
        self.panel = panel
        self.history = history
        self.snapshot: SystemSnapshot | None = None
        self._frame = None
        self._t0 = time.monotonic()
        self.border_title = panel.title

    def on_mount(self) -> None:
        if self.panel.animated:
            self.set_interval(1 / 12, self._tick)

    def _tick(self) -> None:
        if self.display and self.is_on_screen:
            self.redraw()

    def update_data(self, snap: SystemSnapshot | None) -> None:
        self.snapshot = snap
        if self.is_on_screen:
            self.redraw()

    def redraw(self) -> None:
        w, h = self.content_size.width, self.content_size.height
        if w < 2 or h < 1:
            self._frame = None
            return
        cv = self.panel.canvas(w, h)
        try:
            self.panel.draw(cv, self.snapshot, self.history, time.monotonic() - self._t0)
        except Exception as e:  # a bad sample must not take the UI down
            cv.put_text(0, 0, f"render error: {e}"[:w], 0xE5383B)
        self._frame = cv
        self.refresh()

    def on_resize(self, event: events.Resize) -> None:
        self.redraw()

    def on_show(self) -> None:
        self.redraw()

    def render_line(self, y: int) -> Strip:
        width = self.content_size.width
        cv = self._frame
        if cv is None or y >= cv.h:
            return Strip.blank(width)
        return Strip(cv.segments(y), cv.w).crop(0, width)


class IpcView(PanelView, can_focus=True):
    """The IPC map. Click a node, or Tab through them, to select it; click empty space to clear."""

    BINDINGS = [
        Binding("tab", "cycle(1)", "Next node", show=False),
        Binding("shift+tab", "cycle(-1)", show=False),
        Binding("enter", "app.ipc_jump", "To process", show=False),
    ]

    class Selected(Message):
        def __init__(self, nid: str | None) -> None:
            self.nid = nid
            super().__init__()

    def node_at(self, x: int, y: int) -> str | None:
        for nid, (x0, y0, x1, y1) in getattr(self.panel, "boxes", {}).items():
            if x0 <= x < x1 and y0 <= y < y1:
                return nid
        return None

    def select(self, nid: str | None) -> None:
        self.panel.selected = nid
        self.panel.highlight = set()
        self.redraw()
        self.post_message(self.Selected(nid))

    def action_cycle(self, step: int) -> None:
        order = list(getattr(self.panel, "boxes", {}))
        if not order:
            return
        i = order.index(self.panel.selected) if self.panel.selected in order else -1
        self.select(order[(i + step) % len(order)])

    def on_click(self, event: events.Click) -> None:
        off = event.get_content_offset(self)
        if off is not None:
            self.select(self.node_at(off.x, off.y))

    def on_mouse_move(self, event: events.MouseMove) -> None:
        off = event.get_content_offset(self)
        nid = self.node_at(off.x, off.y) if off is not None else None
        ipc = self.snapshot.ipc if self.snapshot is not None else None
        n = None
        if ipc is not None and nid:
            from .tables import ipc_node

            n = ipc_node(ipc, nid, self.panel.members(nid))
        self.tooltip = f"{n.name} — {n.runtime or n.kind}" if n else None


class IpcFilter(Input):
    """Text filter over the IPC links table. Escape clears it and gives focus back."""

    BINDINGS = [Binding("escape", "close", show=False)]

    def action_close(self) -> None:
        self.value = ""
        self.display = False
        self.app.query_one("#ipc-links").focus()


class IpcDetail(Static):
    """Everything known about the node selected on the IPC map, tagged live / derived."""

    current: str | None = None

    def show(self, ipc, nid: str | None, history: History | None, members: set[str] | None = None) -> None:
        from .tables import ipc_node, ipc_rows

        if ipc is None:
            self.current = None
            self.update(Text("Collecting connections…", style="dim"))
            return
        n = ipc_node(ipc, nid or "host", members)
        self.current = n.id if n else None
        if n is None:
            self.update(Text("Click a node on the map (or Tab) to inspect it.", style="dim"))
            return
        title = Text()
        title.append(f" {n.name} ", style="bold black on #39c5ff")
        title.append(f"  {n.runtime or n.kind}", style="bold")
        if not nid:
            title.append("  · click a node to narrow the table", style="#8a9aad")
        t = Table.grid(padding=(0, 1), expand=True)
        t.add_column(width=1)
        t.add_column(style="bold #8fb8de", ratio=2)
        t.add_column(ratio=5)
        for r in ipc_rows(ipc, n.id, history, members):
            sym, color = PROV_STYLE[r.kind]
            t.add_row(Text(sym, style=color), r.key, r.value)
        legend = Text.assemble(("● live ", "#3dff7a"), ("◐ derived", "#f2c14e"))
        self.update(Group(title, Text(""), t, Text(""), legend))


class DetailPanel(Static):
    """Everything known about the selected part, tagged live / derived / spec."""

    current: str | None = None

    def show(self, match: Match, scene, cid: str | None, snap, gpu_match=None) -> None:
        self.current = cid if scene is not None and cid in scene.comps else None
        if self.current is None:
            self.update(Text("Click a part (or Tab / arrows) to inspect it.", style="dim"))
            return
        c = scene.comps[cid]
        title = Text()
        title.append(f" {c.id} ", style="bold black on #39c5ff")
        title.append(f"  {c.kind_name}", style="bold")
        if c.label and c.label != c.id:
            title.append(f"  · {c.label}", style="#8a9aad")
        t = Table.grid(padding=(0, 1), expand=True)
        t.add_column(width=1)
        t.add_column(style="bold #8fb8de", ratio=2)
        t.add_column(ratio=3)
        if gpu_match is not None:
            from gputui.components import rows_for as gpu_rows
            rows = gpu_rows(c, gpu_match.profile, gpu_match.sku, snap)
        else:
            rows = rows_for(c, match.profile, snap)
        for r in rows:
            sym, color = PROV_STYLE[r.kind]
            t.add_row(Text(sym, style=color), r.key, r.value)
        legend = Text.assemble(("● live ", "#3dff7a"), ("◐ derived ", "#f2c14e"), ("○ spec", "#8a9aad"))
        self.update(Group(title, Text(""), t, Text(""), legend))


def bar(frac: float | None, width: int) -> Text:
    if frac is None:
        return Text("N/A".ljust(width), style="dim")
    frac = max(0.0, min(1.0, frac))
    full = frac * width
    n = int(full)
    color = "#3fae7a" if frac < 0.6 else "#f2c14e" if frac < 0.85 else "#e5383b"
    t = Text("█" * n, style=color)
    if n < width:
        t.append(" ▏▎▍▌▋▊▉"[int((full - n) * 8)], style=color)
        t.append("·" * (width - n - 1), style="#2e3440")
    return t


class GpuPanel(Static):
    def show(self, s: SystemSnapshot | None, width: int) -> None:
        g = B.gpu(s) if s else None
        if g is None:
            self.update(Text("No NVIDIA GPU via NVML.", style="dim"))
            return
        bw = max(6, width - 20)
        tb = Table.grid(padding=(0, 1))
        tb.add_column(style="bold #8fb8de", width=4)
        tb.add_column(width=bw)
        tb.add_column(justify="right", width=12)
        rows = [
            ("GPU", None if g.util is None else g.util / 100, f"{g.util or 0:.0f} %"),
            ("VRAM", g.vram_frac, f"{(g.vram_used or 0) / 2**30:.1f}/{(g.vram_total or 0) / 2**30:.0f}G"),
            ("PWR", None if not g.power_w or not g.power_limit_w else g.power_w / g.power_limit_w,
             f"{g.power_w or 0:.0f}/{g.power_limit_w or 0:.0f} W"),
            ("TEMP", None if g.temp is None else g.temp / 90, f"{g.temp or 0:.0f} °C"),
            ("FAN", None if g.fan_pct is None else g.fan_pct / 100, f"{g.fan_pct or 0:.0f} %"),
        ]
        for name, frac, val in rows:
            tb.add_row(name, bar(frac, bw), val)
        head = Text.assemble((g.name, "bold"), ("   G flies into the PCB", "#8a9aad"))
        self.update(Group(head, tb))

    def show_gpu(self, s, width: int) -> None:
        from gputui.widgets import GaugePanel
        GaugePanel.show(self, s, width)
