"""gputui — Textual app."""

from __future__ import annotations

import json
import time
from datetime import datetime

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, OptionList, Sparkline, Static, TabbedContent, TabPane
from textual.widgets.option_list import Option

from . import tables
from .boards import load_profiles
from .identify import Match, identify
from .telemetry import DemoSource
from .widgets import VIEWS, BoardView, DetailPanel, GaugePanel, History

MODES = ["live", "thermal", "off"]


class BoardBrowser(ModalScreen[str | None]):
    """Pick any RTX 50 board layout to view."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Close")]
    DEFAULT_CSS = """
    BoardBrowser { align: center middle; }
    BoardBrowser > Vertical { width: 90; height: 80%; border: round $accent; background: $surface; }
    BoardBrowser OptionList { height: 1fr; }
    """

    def __init__(self, current: str, detected_sku: str | None) -> None:
        super().__init__()
        self.current = current
        self.detected_sku = detected_sku

    def compose(self) -> ComposeResult:
        from .chips import SKU_BY_KEY

        opts = [Option(Text("⟲  Detected card (auto)", style="bold #3dff7a"), id="__auto__")]
        for p in sorted(load_profiles().values(), key=lambda p: (SKU_BY_KEY[p.skus[0]].tgp_w * -1, p.kind)):
            sku = SKU_BY_KEY[p.skus[0]]
            tag = {"fe": "Founders Edition", "reference": "reference", "aib": "partner"}[p.kind]
            demo = "" if p.skus[0] == self.detected_sku else "  · demo data"
            label = Text.assemble((f"{sku.name:<22}", "bold"), (f"{p.short:<34}", ""), (tag, "#8a9aad"), (demo, "#f2c14e"))
            opts.append(Option(label, id=p.id))
        with Vertical():
            yield Static(Text(" RTX 50-series board layouts — Enter to view, Esc to close", style="bold"))
            yield OptionList(*opts, id="boards")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


class GpuTuiApp(App):
    TITLE = "gputui"
    CSS = """
    Screen { layout: vertical; }
    #hdr { height: 1; background: $boost; padding: 0 1; }
    #top { height: 1fr; }
    BoardView { border: round #3a4658; border-title-color: #8fb8de; border-title-style: bold; }
    BoardView:focus { border: round #39c5ff; }
    #side { width: 58; }
    #detail-scroll { height: 1fr; border: round #3a4658; border-title-color: #8fb8de; padding: 0 1; }
    #gauges { height: auto; border: round #3a4658; border-title-color: #8fb8de; padding: 0 1; }
    #hist { height: auto; border: round #3a4658; border-title-color: #8fb8de; padding: 0 1; }
    .sl-row { height: 1; }
    .sl-label { width: 16; color: #8fb8de; }
    Sparkline { width: 1fr; height: 1; }
    #tables { height: 16; }
    #tables.hidden { display: none; }
    #tables.full { height: 1fr; }
    #top.hidden { display: none; }
    TabPane { padding: 0 1; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("1", "view('card')", "Card"),
        Binding("2", "view('front')", "PCB"),
        Binding("3", "view('back')", "Back"),
        Binding("4", "view('die')", "Die"),
        Binding("5", "view('cores')", "Cores"),
        Binding("tab", "cycle(1)", "Next part", priority=True),
        Binding("shift+tab", "cycle(-1)", "Prev part", show=False, priority=True),
        Binding("left", "move(-1,0)", show=False),
        Binding("right", "move(1,0)", show=False),
        Binding("up", "move(0,-1)", show=False),
        Binding("down", "move(0,1)", show=False),
        Binding("h", "mode", "Overlay"),
        Binding("l", "labels", "Labels"),
        Binding("b", "browse", "Boards"),
        Binding("t", "tables", "Tables"),
        Binding("0", "tables_full", "Tables full", show=False),
        Binding("[", "tab(-1)", show=False),
        Binding("]", "tab(1)", show=False),
        Binding("p", "pause", "Pause"),
        Binding("plus,equals_sign", "interval(-0.25)", "Faster", show=False),
        Binding("minus", "interval(0.25)", "Slower", show=False),
        Binding("g", "next_gpu", "Next GPU", show=False),
        Binding("s", "save", "Save JSON"),
    ]

    def __init__(self, source, interval: float = 1.0, view: str = "front", force_board: str | None = None,
                 make_source=None) -> None:
        super().__init__()
        self.source = source
        self.real_source = source
        self.make_source = make_source
        self.interval = interval
        self.paused = False
        self.history = History()
        self.snapshot = source.collect()
        self.detected = identify(self.snapshot)
        self.match: Match = identify(self.snapshot, force_board) or self._fallback_match()
        self.demo_override = False
        self.initial_view = view
        self._timer = None
        self._polling = False

    def _fallback_match(self) -> Match:
        from .chips import SKU_BY_KEY

        sku = SKU_BY_KEY["5080"]
        return Match(sku, load_profiles()["zotac-5080"], "forced", None)

    # --------------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield Static(id="hdr")
        with Horizontal(id="top"):
            board = BoardView(self.match, id="board")
            board.history = self.history.data
            yield board
            with Vertical(id="side"):
                with VerticalScroll(id="detail-scroll"):
                    yield DetailPanel(id="detail")
                yield GaugePanel(id="gauges")
                with Vertical(id="hist"):
                    for key, label in (("util", "GPU util %"), ("power", "Power W"), ("temp", "Temp °C"), ("vram", "VRAM GiB")):
                        with Horizontal(classes="sl-row"):
                            yield Static(label, classes="sl-label", id=f"sl-{key}-label")
                            yield Sparkline(list(self.history.data[key]), id=f"sl-{key}", summary_function=max)
        with TabbedContent(id="tables"):
            for i, name in enumerate(tables.TABS):
                with TabPane(name, id=f"tab{i}"):
                    with VerticalScroll():
                        yield Static(id=f"tabc{i}")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#detail-scroll").border_title = "Component"
        self.query_one("#gauges").border_title = "Live"
        self.query_one("#hist").border_title = "History (2 min)"
        board = self.query_one(BoardView)
        board.view = self.initial_view
        board.snapshot = self.snapshot
        board.focus()
        self._apply(self.snapshot)
        self._timer = self.set_interval(self.interval, self.poll)

    # --------------------------------------------------------------------- polling
    def poll(self) -> None:
        if self.paused or self._polling:
            return
        self._polling = True
        self._collect()

    @work(thread=True, exclusive=True, group="poll")
    def _collect(self) -> None:
        try:
            snap = self.source.collect()
        except Exception as e:  # keep the UI alive if the driver hiccups
            self.call_from_thread(self.notify, f"poll failed: {e}", severity="error")
            snap = None
        self.call_from_thread(self._apply, snap)

    def _apply(self, snap) -> None:
        self._polling = False
        if snap is None:
            return
        self.snapshot = snap
        self.history.push(snap)
        board = self.query_one(BoardView)
        board.snapshot = snap
        self._header()
        self.query_one(GaugePanel).show(snap, self.query_one("#side").size.width - 4)
        for key in History.KEYS:
            self.query_one(f"#sl-{key}", Sparkline).data = list(self.history.data[key])
            last = self.history.data[key][-1]
            label = {"util": "GPU util", "power": "Power", "temp": "Temp", "vram": "VRAM"}[key]
            unit = {"util": "%", "power": "W", "temp": "°C", "vram": "G"}[key]
            self.query_one(f"#sl-{key}-label", Static).update(f"{label} {last:5.1f}{unit}")
        self._refresh_detail()
        self._refresh_tables()

    def _header(self) -> None:
        s, m = self.snapshot, self.match
        t = Text()
        t.append(" ▣ ", style="bold #39c5ff")
        t.append(f"{s.name or m.sku.name}", style="bold")
        t.append(f"  ·  {m.profile.short}", style="#c9d1d9")
        badge = {"exact": ("EXACT", "#3dff7a"), "vendor": ("VENDOR MATCH", "#f2c14e"), "reference": ("REFERENCE", "#8fb8de"),
                 "similar": ("SIMILAR BOARD", "#f2c14e"), "generic": ("GENERIC", "#e5383b"), "forced": ("MANUAL", "#c58cff")}[m.level]
        t.append(f"  [{badge[0]}]", style=f"bold {badge[1]}")
        if self.demo_override or s.source == "demo":
            t.append("  [DEMO DATA]", style="bold #e5383b")
        t.append(f"   driver {s.driver or '?'} · CUDA {s.cuda_driver or '?'}", style="#8a9aad")
        board = self.query_one(BoardView)
        t.append(f"   view: {VIEWS[board.view]} · overlay: {board.mode}", style="#8a9aad")
        if self.paused:
            t.append("  ⏸ PAUSED", style="bold #f2c14e")
        self.query_one("#hdr", Static).update(t)

    def _refresh_detail(self) -> None:
        board = self.query_one(BoardView)
        self.query_one(DetailPanel).show(self.match, board.scene, board.selected, self.snapshot)

    def _refresh_tables(self, force_components: bool = False) -> None:
        tc = self.query_one(TabbedContent)
        active = tc.active or "tab0"
        i = int(active[3:])
        name = tables.TABS[i]
        target = self.query_one(f"#tabc{i}", Static)
        if name == "Components":
            board = self.query_one(BoardView)
            extra = [c for cid, c in (board.scene.comps.items() if board.scene else [])
                     if self.match.profile.by_id(cid) is None]
            target.update(tables.components_table(self.snapshot, self.match, board.selected, extra))
        else:
            target.update(tables.BUILDERS[name](self.snapshot, self.match))

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self._refresh_tables()

    def on_board_view_selected(self, event: BoardView.Selected) -> None:
        self._refresh_detail()
        if (self.query_one(TabbedContent).active or "") == f"tab{tables.TABS.index('Components')}":
            self._refresh_tables()

    # --------------------------------------------------------------------- actions
    def action_view(self, view: str) -> None:
        self.query_one(BoardView).set_view(view)
        self._header()
        self._refresh_detail()

    def action_cycle(self, step: int) -> None:
        self.query_one(BoardView).cycle(step)

    def action_move(self, dx: int, dy: int) -> None:
        self.query_one(BoardView).move(dx, dy)

    def action_mode(self) -> None:
        b = self.query_one(BoardView)
        b.mode = MODES[(MODES.index(b.mode) + 1) % len(MODES)]
        self._header()

    def action_labels(self) -> None:
        b = self.query_one(BoardView)
        b.labels = not b.labels
        b.rebuild()

    def action_tables(self) -> None:
        tc = self.query_one("#tables")
        top = self.query_one("#top")
        if tc.has_class("full"):
            tc.remove_class("full")
            top.remove_class("hidden")
        else:
            tc.toggle_class("hidden")

    def action_tables_full(self) -> None:
        tc = self.query_one("#tables")
        tc.remove_class("hidden")
        tc.toggle_class("full")
        self.query_one("#top").set_class(tc.has_class("full"), "hidden")

    def action_tab(self, step: int) -> None:
        tc = self.query_one(TabbedContent)
        i = int((tc.active or "tab0")[3:])
        tc.active = f"tab{(i + step) % len(tables.TABS)}"

    def action_pause(self) -> None:
        self.paused = not self.paused
        self._header()

    def action_interval(self, delta: float) -> None:
        self.interval = max(0.25, min(10.0, self.interval + delta))
        if self._timer:
            self._timer.stop()
        self._timer = self.set_interval(self.interval, self.poll)
        self.notify(f"poll interval {self.interval:.2f} s")

    def action_next_gpu(self) -> None:
        count = getattr(self.real_source, "count", 1)
        if count <= 1 or self.make_source is None:
            self.notify("only one GPU")
            return
        nxt = (self.real_source.index + 1) % count
        self.real_source.close()
        self.real_source = self.source = self.make_source(nxt)
        self.demo_override = False
        snap = self.source.collect()
        self.detected = identify(snap)
        self.match = self.detected or self._fallback_match()
        self.query_one(BoardView).set_match(self.match)
        self._apply(snap)

    def action_save(self) -> None:
        path = f"gputui-{datetime.now():%Y%m%d-%H%M%S}.json"
        data = self.snapshot.to_dict()
        data["board_profile"] = self.match.profile.id
        data["match_level"] = self.match.level
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        self.notify(f"saved {path}")

    def action_browse(self) -> None:
        def done(pid: str | None) -> None:
            if pid is None:
                return
            if pid == "__auto__":
                self.source = self.real_source
                self.demo_override = False
                self.match = self.detected or self._fallback_match()
            else:
                prof = load_profiles()[pid]
                from .chips import SKU_BY_KEY

                sku = SKU_BY_KEY[prof.skus[0]]
                if self.detected and sku.key == self.detected.sku.key:
                    self.source = self.real_source
                    self.demo_override = False
                else:
                    gb = round(len(prof.of_kind("mem")) * prof.mem_gb_each) or None
                    self.source = DemoSource(sku.key, prof.vendor or 0x10DE, (prof.subsystems or [0])[0], mem_gb=gb)
                    self.demo_override = True
                self.match = Match(sku, prof, "forced", None)
            self.query_one(BoardView).set_match(self.match)
            self._apply(self.source.collect())

        self.push_screen(BoardBrowser(self.match.profile.id, self.detected.sku.key if self.detected else None), done)
