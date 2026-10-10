"""Textual widgets: the animated board view, component detail, gauges and history."""

from __future__ import annotations

import time
from collections import deque

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import events
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Static

from .components import DERIVED, LIVE, SPEC, rows_for
from .identify import Match
from .render import Scene
from .render.card import CardScene
from .render.cores import CoresScene
from .render.die import DieScene
from .render.pcb import PcbScene
from .telemetry import GpuSnapshot

VIEWS = {"card": "Card", "front": "PCB front", "back": "PCB back", "die": "Die", "cores": "Cores"}
PROV_STYLE = {LIVE: ("●", "#3dff7a"), DERIVED: ("◐", "#f2c14e"), SPEC: ("○", "#8a9aad")}


class BoardView(Widget, can_focus=True):
    """Renders the current scene at ~15 fps and handles hover / click selection."""

    DEFAULT_CSS = """
    BoardView { width: 1fr; height: 1fr; }
    """

    class Selected(Message):
        def __init__(self, cid: str | None) -> None:
            self.cid = cid
            super().__init__()

    def __init__(self, match: Match, **kw) -> None:
        super().__init__(**kw)
        self.match = match
        self.view = "front"
        self.mode = "live"
        self.labels = True
        self.snapshot: GpuSnapshot | None = None
        self.selected: str | None = None
        self.hover: str | None = None
        self.scene: Scene | None = None
        self.history = None  # History.data, for scenes that chart over time
        self._frame = None
        self._t0 = time.monotonic()
        self._built_size = (0, 0)

    def on_mount(self) -> None:
        self.set_interval(1 / 15, self._tick)

    # ------------------------------------------------------------------ scene control
    def rebuild(self) -> None:
        w, h = self.content_size.width, self.content_size.height
        if w < 4 or h < 2:
            self.scene = None
            return
        m = self.match
        if self.view == "card":
            self.scene = CardScene(m.profile, m.sku, w, h, labels=self.labels)
        elif self.view == "die":
            self.scene = DieScene(m.profile, m.sku, w, h, labels=self.labels)
        elif self.view == "cores":
            self.scene = CoresScene(m.profile, m.sku, w, h, labels=self.labels, history=self.history)
        else:
            self.scene = PcbScene(m.profile, m.sku, w, h, side=self.view, labels=self.labels)
        self._built_size = (w, h)
        if self.selected not in self.scene.comps:
            self.selected = self._default_selection()
            self.post_message(self.Selected(self.selected))
        self.border_title = self.scene.title
        self._render_frame()

    def _default_selection(self) -> str | None:
        if not self.scene:
            return None
        for pref in ("GPU", "GPC0", "FAN1"):
            if pref in self.scene.comps:
                return pref
        return next(iter(self.scene.order), None)

    def set_view(self, view: str) -> None:
        self.view = view
        self.rebuild()

    def set_match(self, match: Match) -> None:
        self.match = match
        self.selected = None
        self.rebuild()

    def select(self, cid: str | None) -> None:
        self.selected = cid
        self.post_message(self.Selected(cid))

    def cycle(self, step: int) -> None:
        if not self.scene:
            return
        order = self.scene.order
        if not order:
            return
        i = order.index(self.selected) if self.selected in order else -1
        self.select(order[(i + step) % len(order)])

    def move(self, dx: int, dy: int) -> None:
        if self.scene:
            self.select(self.scene.neighbour(self.selected, dx, dy))

    # --------------------------------------------------------------------- rendering
    def on_resize(self, event: events.Resize) -> None:
        self.rebuild()

    def _tick(self) -> None:
        if (self.content_size.width, self.content_size.height) != self._built_size:
            self.rebuild()
        self._render_frame()
        self.refresh()

    def _render_frame(self) -> None:
        if self.scene is None:
            self._frame = None
            return
        t = time.monotonic() - self._t0
        self._frame = self.scene.frame(t, self.snapshot, self.selected, self.hover, self.mode)

    def render_line(self, y: int) -> Strip:
        width = self.content_size.width
        cv = self._frame
        if cv is None or y >= cv.h:
            return Strip.blank(width)
        return Strip(cv.segments(y), cv.w).crop(0, width)

    # ------------------------------------------------------------------------ mouse
    def _cid_at(self, event: events.MouseEvent) -> str | None:
        off = event.get_content_offset(self)
        if off is None or self.scene is None:
            return None
        return self.scene.hit(off.x, off.y)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        self.hover = self._cid_at(event)
        self.tooltip = None if not self.hover or not self.scene else self._tip(self.hover)

    def _tip(self, cid: str) -> str:
        c = self.scene.comps[cid]
        return f"{c.id} — {c.kind_name}" + (f"\n{c.part}" if c.part else "")

    def on_leave(self, event: events.Leave) -> None:
        self.hover = None

    def on_click(self, event: events.Click) -> None:
        cid = self._cid_at(event)
        if cid:
            self.select(cid)


class DetailPanel(Static):
    """Everything known about the selected component, tagged live / derived / spec."""

    current: str | None = None

    def show(self, match: Match, scene: Scene | None, cid: str | None, snap: GpuSnapshot | None) -> None:
        self.current = cid if scene is not None and cid in scene.comps else None
        if scene is None or cid is None or cid not in scene.comps:
            self.update(Text("Click a component (or Tab / arrows) to inspect it.", style="dim"))
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
        for r in rows_for(c, match.profile, match.sku, snap):
            sym, color = PROV_STYLE[r.kind]
            t.add_row(Text(sym, style=color), r.key, r.value)
        legend = Text.assemble(("● live ", "#3dff7a"), ("◐ derived ", "#f2c14e"), ("○ spec", "#8a9aad"))
        self.update(Group(title, Text(""), t, Text(""), legend))


def _bar(frac: float | None, width: int, neutral: bool = False) -> Text:
    if frac is None:
        return Text("N/A".ljust(width), style="dim")
    frac = max(0.0, min(1.0, frac))
    full = frac * width
    n = int(full)
    color = "#5b9bd5" if neutral else "#3fae7a" if frac < 0.6 else "#f2c14e" if frac < 0.85 else "#e5383b"
    t = Text("█" * n, style=color)
    if n < width:
        part = " ▏▎▍▌▋▊▉"[int((full - n) * 8)]
        t.append(part, style=color)
        t.append("·" * (width - n - 1), style="#2e3440")
    return t


class GaugePanel(Static):
    def show(self, s: GpuSnapshot | None, width: int) -> None:
        if s is None:
            self.update("waiting for data…")
            return
        bw = max(6, width - 22)
        g = Table.grid(padding=(0, 1))
        g.add_column(style="bold #8fb8de", width=5)
        g.add_column(width=bw)
        g.add_column(justify="right", width=13)
        mem = s.mem_used / s.mem_total if s.mem_used is not None and s.mem_total else None
        temp_frac = None if s.temp_gpu is None else s.temp_gpu / (s.temp_slowdown or 90)
        fan = s.fans[0] if s.fans else None
        rows = [
            ("GPU", None if s.util_gpu is None else s.util_gpu / 100, f"{s.util_gpu if s.util_gpu is not None else '—'} %"),
            ("SM", None if "SM_UTIL" not in s.gpm else s.gpm["SM_UTIL"] / 100,
             "N/A" if "SM_UTIL" not in s.gpm else f"active {s.gpm['SM_UTIL']:.0f} %"),
            ("VRAM", mem, "N/A" if mem is None else f"{s.mem_used / 2**30:.1f}/{s.mem_total / 2**30:.0f}G"),
            ("PWR", s.power_frac, "N/A" if s.power_w is None else f"{s.power_w:.0f}/{s.power_limit_w or 0:.0f} W"),
            ("TEMP", temp_frac, "N/A" if s.temp_gpu is None else f"{s.temp_gpu} °C"),
            ("FAN", None if fan is None or fan.speed_pct is None else fan.speed_pct / 100,
             "N/A" if fan is None else f"{fan.rpm if fan.rpm is not None else '—'} rpm"),
            ("CLK", None if not s.clk_graphics or not s.max_clk_graphics else s.clk_graphics / s.max_clk_graphics,
             f"{s.clk_graphics or '—'} MHz"),
            ("MEM", s.dram_activity, "N/A" if s.dram_activity is None else f"bw {s.dram_activity * 100:.0f} %"),
        ]
        for name, frac, val in rows:
            g.add_row(name, _bar(frac, bw, neutral=name in ("FAN", "CLK", "SM", "MEM")), val)
        foot = Text()
        foot.append(f"P{s.pstate if s.pstate is not None else '?'}", style="bold")
        if s.throttle:
            foot.append("  ⚠ " + ", ".join(s.throttle), style="#f2c14e")
        else:
            foot.append("  no clock limiters", style="dim")
        self.update(Group(g, foot))


class History:
    """Rolling 2-minute windows of key metrics for the sparklines."""

    KEYS = ("util", "power", "temp", "vram")  # the sidebar sparklines
    CORE_KEYS = ("sm", "occ", "tensor", "smclk")  # the Cores view chart

    def __init__(self, n: int = 120) -> None:
        self.data = {k: deque([0.0] * n, maxlen=n) for k in self.KEYS + self.CORE_KEYS}

    def push(self, s: GpuSnapshot) -> None:
        if s.sample_util:
            for v in s.sample_util:
                self.data["util"].append(float(v))
        else:
            self.data["util"].append(float(s.util_gpu or 0))
        if s.sample_power:
            for v in s.sample_power:
                self.data["power"].append(float(v))
        else:
            self.data["power"].append(float(s.power_w or 0))
        self.data["temp"].append(float(s.temp_gpu or 0))
        self.data["vram"].append(float((s.mem_used or 0) / 2**30))
        self.data["sm"].append((s.sm_activity or 0.0) * 100)
        self.data["occ"].append(float(s.gpm.get("SM_OCCUPANCY", 0.0)))
        self.data["tensor"].append(float(s.gpm.get("ANY_TENSOR_UTIL", 0.0)))
        self.data["smclk"].append(float(s.clk_sm or 0))
