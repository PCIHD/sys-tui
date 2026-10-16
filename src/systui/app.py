"""systui — Textual app."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from collections import deque
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Input, Sparkline, Static, TabbedContent, TabPane

from . import bindings as B
from . import tables
from .boards import Match, identify
from .history import History
from .render import panels as P
from .render.ipc import IpcMap
from .widgets import BoardView, DetailPanel, GpuPanel, IpcDetail, IpcEvents, IpcFilter, IpcView, PanelView

MODES = ["live", "thermal", "off"]
TABS = [("board", "Board"), ("cpu", "CPU"), ("mem", "Memory"), ("disk", "Storage"), ("net", "Network"),
        ("proc", "Processes"), ("sens", "Sensors"), ("ipc", "IPC")]
SYS_TAB_LABELS = {
    "board": "1 Board", "cpu": "2 CPU", "mem": "3 Memory", "disk": "4 Storage",
    "net": "5 Network", "proc": "6 Processes", "sens": "7 Sensors", "ipc": "8 IPC",
}
GPU_TAB_LABELS = {
    "board": "1 Card", "cpu": "2 PCB", "mem": "3 Back", "disk": "4 Die",
    "net": "5 Cores", "proc": "6 GPU procs", "sens": "7 Overview", "ipc": "8 IPC",
}
IPC_ROWS = 300  # the links table; the map aggregates the rest
GPU_TAB = {"board": "gpu-card", "cpu": "gpu-front", "mem": "gpu-back", "disk": "gpu-die", "net": "gpu-cores"}
LAYER_TAB = {v: k for k, v in GPU_TAB.items()}


def find_gputui(explicit: str | None = None) -> list[str] | None:
    """Command that launches gputui: --gputui / $SYSTUI_GPUTUI, PATH, or this checkout."""
    cmd = explicit or os.environ.get("SYSTUI_GPUTUI")
    if cmd:
        return shlex.split(cmd)
    exe = shutil.which("gputui")
    if exe:
        return [exe]
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists() and (parent / "src" / "gputui").is_dir() and shutil.which("uv"):
            return ["uv", "run", "--project", str(parent), "gputui"]
    return None


class SysTuiApp(App):
    TITLE = "systui"
    CSS = """
    Screen { layout: vertical; }
    #hdr { height: 1; background: $boost; padding: 0 1; }
    #tabs { height: 1fr; }
    #tabs.bar-only { height: 3; min-height: 3; max-height: 3; }
    #tabs.bar-only ContentSwitcher { display: none; height: 0; }
    #stage { height: 1fr; }
    #stage.hidden { display: none; }
    TabPane { padding: 0; }
    BoardView, PanelView { border: round #3a4658; border-title-color: #8fb8de; border-title-style: bold; }
    BoardView:focus { border: round #39c5ff; }
    #side, #ipc-side { width: 58; }
    #detail-scroll, #ipc-scroll, #ipc-ev-scroll { height: 1fr; border: round #3a4658; border-title-color: #8fb8de; padding: 0 1; }
    #ipc-ev-scroll { height: 14; }
    IpcView:focus { border: round #39c5ff; }
    #ipcmap { height: 3fr; min-height: 14; }
    #ipc-links { height: 2fr; min-height: 6; }
    #ipc-filter { display: none; }
    #gpu { height: auto; border: round #3a4658; border-title-color: #8fb8de; padding: 0 1; }
    #overview { height: 18; }
    #overview.hidden, #gpu-hist.hidden, #gpu-tabs.hidden { display: none; }
    #board-top { height: 1fr; }
    #gpu-tabs { height: 16; }
    #gpu-hist { height: auto; border: round #3a4658; border-title-color: #8fb8de; padding: 0 1; }
    .sl-row { height: 1; }
    .sl-label { width: 16; color: #8fb8de; }
    Sparkline { width: 1fr; height: 1; }
    #cores { height: 3fr; }
    #cpu-bottom { height: 2fr; }
    #mem-row PanelView { height: 1fr; }
    .disk { height: 1fr; min-height: 7; }
    .net { height: 1fr; min-height: 7; }
    #parts { height: auto; max-height: 12; }
    #fans { height: 2fr; min-height: 10; }
    #sens-scroll { height: 3fr; border: round #3a4658; border-title-color: #8fb8de; padding: 0 1; }
    #proc-head { height: 1; padding: 0 1; color: #8a9aad; }
    #procs { height: 1fr; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        *[Binding(str(i + 1), f"tab('{tid}')", name, show=False) for i, (tid, name) in enumerate(TABS)],
        Binding("h", "mode", "Overlay"),
        Binding("l", "labels", "Labels"),
        Binding("c", "card", "Card"),
        Binding("g", "gputui", "GPU"),
        Binding("G", "gputui", "GPU", show=False),
        Binding("b", "browse", "Boards", show=False),
        Binding("enter", "enter_part", "Zoom", show=False),
        Binding("escape", "unzoom", "Back", show=False),
        Binding("o", "sort", "Sort procs"),
        Binding("p", "pause", "Pause"),
        Binding("plus,equals_sign", "interval(-0.25)", "Faster", show=False),
        Binding("minus", "interval(0.25)", "Slower", show=False),
        Binding("s", "save", "Save JSON"),
        Binding("r", "record", "Record"),
        Binding("R", "replay", "Replay"),
        Binding("left_square_bracket", "step(-1)", show=False),
        Binding("right_square_bracket", "step(1)", show=False),
        Binding("d", "cpu_die", "CPU die"),
        Binding("i", "ipc_level", "Rows", show=False),
        Binding("f", "ipc_kind", "Kinds", show=False),
        Binding("slash", "ipc_filter", "Filter", show=False),
    ]

    def __init__(self, source, interval: float = 1.0, force_board: str | None = None, gputui: str | None = None,
                 initial_tab: str = "board") -> None:
        super().__init__()
        self.source = source
        self.interval = interval
        self.paused = False
        self.history = History()
        self._want_ipc(initial_tab == "ipc")
        self.snapshot = source.collect()
        self.history.ingest(self.snapshot)
        self.match: Match = identify(self.snapshot, force_board)
        self.gputui_cmd = find_gputui(gputui)
        self.gputui_explicit = gputui is not None
        self.gpu_source = None
        self.gpu_hist = None
        self.initial_tab = initial_tab
        self.proc_sort = "cpu"
        self.part_filter: str | None = None
        self.tape: deque = deque(maxlen=3600)
        self.recording = False
        self.replay_i: int | None = None
        self._timer = None
        self._polling = False
        self._syncing_tabs = False
        self._pending_tab: str | None = None  # a system tab asked for from GPU mode, opened once back on the board
        self._ipc_links: list = []  # rows of #ipc-links, in order
        self._ipc_table_w = -1  # #ipc-links width its columns were sized for
        self._ipc_kind = 0  # index into tables.KIND_FILTERS
        self._proc_pid: int | None = None  # put the Processes cursor on this PID at its next refresh

    # --------------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        s, h = self.snapshot, self.history
        yield Static(id="hdr")
        with TabbedContent(id="tabs", initial=self.initial_tab):
            with TabPane("1 Board", id="board"):
                yield Static()
            with TabPane("2 CPU", id="cpu"):
                with Vertical():
                    yield PanelView(P.CoreGrid(), h, id="cores")
                    with Horizontal(id="cpu-bottom"):
                        yield PanelView(P.Threads(), h)
                        yield PanelView(P.Package(), h)
            with TabPane("3 Memory", id="mem"):
                with Horizontal(id="mem-row"):
                    yield PanelView(P.Memory(), h)
                    yield PanelView(P.Dimms(self.match.profile), h)
            with TabPane("4 Storage", id="disk"):
                with Vertical():
                    for d in s.physical_disks():
                        yield PanelView(P.Disk(d.name), h, classes="disk")
                    pv = PanelView(P.Partitions(), h, id="parts")
                    pv.styles.height = len(s.parts) + 2
                    yield pv
            with TabPane("5 Network", id="net"):
                with Vertical():
                    nets = s.physical_nets() or [n for n in s.nets if n.kind != "loopback"][:2]
                    for n in nets:
                        yield PanelView(P.Net(n.name), h, classes="net")
            with TabPane("6 Processes", id="proc"):
                with Vertical():
                    yield Static(id="proc-head")
                    yield DataTable(id="procs", zebra_stripes=True, cursor_type="row")
            with TabPane("7 Sensors", id="sens"):
                with Vertical():
                    yield PanelView(P.Fans(), h, id="fans")
                    with VerticalScroll(id="sens-scroll"):
                        yield Static(id="sensors")
            with TabPane("8 IPC", id="ipc"):
                with Horizontal():
                    with Vertical():
                        yield IpcView(IpcMap(), h, id="ipcmap")
                        yield IpcFilter(placeholder="filter links by name, PID, port or path  ·  Esc clears",
                                        id="ipc-filter")
                        yield DataTable(id="ipc-links", zebra_stripes=True, cursor_type="row")
                    with Vertical(id="ipc-side"):
                        with VerticalScroll(id="ipc-scroll"):
                            yield IpcDetail(id="ipc-detail")
                        with VerticalScroll(id="ipc-ev-scroll"):
                            yield IpcEvents(id="ipc-events")
        with Vertical(id="stage"):
            with Horizontal(id="board-top"):
                yield BoardView(self.match, id="boardview")
                with Vertical(id="side"):
                    with VerticalScroll(id="detail-scroll"):
                        yield DetailPanel(id="detail")
                    yield GpuPanel(id="gpu")
                    yield PanelView(P.Overview(), h, id="overview")
                    with Vertical(id="gpu-hist", classes="hidden"):
                        zeros = [0.0] * 120
                        for key, label in (("util", "GPU util %"), ("power", "Power W"),
                                           ("temp", "Temp °C"), ("vram", "VRAM GiB")):
                            with Horizontal(classes="sl-row"):
                                yield Static(label, classes="sl-label", id=f"sl-{key}-label")
                                yield Sparkline(list(zeros), id=f"sl-{key}", summary_function=max)
            from gputui import tables as gpu_tables
            with TabbedContent(id="gpu-tabs", classes="hidden"):
                for i, name in enumerate(gpu_tables.TABS):
                    with TabPane(name, id=f"gtab{i}"):
                        with VerticalScroll():
                            yield Static(id=f"gtabc{i}")
        yield Footer()

    def on_unmount(self) -> None:
        src = self.gpu_source
        self.gpu_source = None
        if src is not None:
            close = getattr(src, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass

    def on_mount(self) -> None:
        self.query_one("#detail-scroll").border_title = "Part"
        self.query_one("#gpu").border_title = "Graphics card"
        self.query_one("#gpu-hist").border_title = "GPU history (2 min)"
        self.query_one("#sens-scroll").border_title = "hwmon sensors (min / max since start)"
        self._sync_stage()
        self.query_one("#ipc-scroll").border_title = "Node"
        self.query_one("#ipc-ev-scroll").border_title = "Events"
        table = self.query_one("#procs", DataTable)
        for col in tables.PROC_COLS:
            table.add_column(col, key=col)
        board = self.query_one(BoardView)
        board.snapshot = self.snapshot
        board.focus()
        self._apply(self.snapshot, ingest=False)
        self._timer = self.set_interval(self.interval, self.poll)

    # --------------------------------------------------------------------- polling
    def poll(self) -> None:
        if self.paused or self._polling or self.replay_i is not None:
            return
        self._polling = True
        self._collect()

    @work(thread=True, exclusive=True, group="poll")
    def _collect(self) -> None:
        try:
            snap = self.source.collect()
            if self.gpu_source is not None:
                try:
                    gpu_snap = self.gpu_source.collect()
                except Exception:
                    gpu_snap = None
            else:
                gpu_snap = None
        except Exception as e:  # keep the UI alive if a sensor read fails
            if self.is_running:
                self.call_from_thread(self.notify, f"poll failed: {e}", severity="error")
            snap, gpu_snap = None, None
        if self.is_running:
            self.call_from_thread(self._apply, snap, True, gpu_snap)

    def _apply(self, snap, ingest: bool = True, gpu_snap=None) -> None:
        self._polling = False
        if snap is None or not self.is_running:
            return
        self.snapshot = snap
        if ingest:
            self.history.ingest(snap)
            if self.recording:
                self.tape.append(snap)
        board = self.query_one(BoardView)
        board.snapshot = snap
        if gpu_snap is not None:
            board.gpu_snap = gpu_snap
            if getattr(self, "gpu_hist", None) is not None:
                self.gpu_hist.push(gpu_snap)
                board.gpu_history = self.gpu_hist.data
        self._header()
        self._refresh_detail()
        gpu_w = self.query_one("#side").size.width - 4
        if board.scene_layer.startswith("gpu-") and board.gpu_snap is not None:
            self.query_one(GpuPanel).show_gpu(board.gpu_snap, gpu_w)
            self._refresh_gpu_sparklines()
            self._refresh_gpu_tables()
        else:
            self.query_one(GpuPanel).show(snap, gpu_w)
        active = self.query_one("#tabs", TabbedContent).active
        for pv in self.query(PanelView):
            pv.update_data(snap)
        if active == "proc":
            self._refresh_procs()
        elif active == "sens":
            self.query_one("#sensors", Static).update(tables.sensors_table(snap, self.history))
        elif active == "ipc":
            self._refresh_ipc()

    def _header(self) -> None:
        s, m = self.snapshot, self.match
        t = Text()
        t.append(" ▣ ", style="bold #39c5ff")
        t.append(s.hostname or "host", style="bold")
        t.append(f"  ·  {m.profile.short}", style="#c9d1d9")
        badge = {"exact": ("EXACT", "#3dff7a"), "generic": ("GENERIC", "#f2c14e"), "forced": ("MANUAL", "#c58cff")}[m.level]
        t.append(f"  [{badge[0]}]", style=f"bold {badge[1]}")
        if s.source == "demo":
            t.append("  [DEMO DATA]", style="bold #e5383b")
        up = int(s.uptime_s)
        t.append(f"   {s.kernel} · up {up // 86400}d {up % 86400 // 3600}h{up % 3600 // 60:02d}m", style="#8a9aad")
        board = self.query_one(BoardView)
        if board.scene_layer.startswith("gpu-") and board.gpu_match is not None:
            t.append(f"   › {board.gpu_match.profile.short} · {board.scene_layer[4:]}  Esc back", style="#c58cff")
        elif board.scene_layer == "cpu-die":
            t.append("   › CPU die  Esc back", style="#c58cff")
        t.append(f"   overlay: {board.mode}", style="#8a9aad")
        if self.recording:
            t.append(f"  ● REC {len(self.tape)}", style="bold #e5383b")
        if self.replay_i is not None:
            t.append(f"  REPLAY {self.replay_i + 1}/{len(self.tape)}", style="bold #c58cff")
        if self.paused:
            t.append("  ⏸ PAUSED", style="bold #f2c14e")
        self.query_one("#hdr", Static).update(t)

    def _refresh_detail(self) -> None:
        board = self.query_one(BoardView)
        if board.scene_layer.startswith("gpu-"):
            self.query_one(DetailPanel).show(self.match, board.scene, board.selected, board.gpu_snap,
                                             gpu_match=board.gpu_match)
        else:
            self.query_one(DetailPanel).show(self.match, board.scene, board.selected, self.snapshot)

    def _refresh_procs(self) -> None:
        table = self.query_one("#procs", DataTable)
        procs = tables.sorted_procs(self.snapshot, self.proc_sort)
        if self.part_filter:
            hit = [p for p in procs if self.part_filter in B.clients(self.snapshot, self.match.profile, p)]
            if hit:
                procs = hit
        row = table.cursor_row
        if self._proc_pid is not None:
            pid, self._proc_pid = self._proc_pid, None
            at = next((i for i, p in enumerate(procs) if p.pid == pid), None)
            if at is None:
                self.notify(f"PID {pid} is not among the {len(procs)} busiest processes listed", severity="warning")
            else:
                row = at
        table.clear()
        threads = self.snapshot.cpu.threads
        for p in procs:
            table.add_row(*tables.proc_row(p, threads), key=str(p.pid))
        if procs:
            table.move_cursor(row=min(row, len(procs) - 1))
        name = tables.SORTS[self.proc_sort][0]
        filt = f"  ·  clients of {self.part_filter}" if self.part_filter and procs else ""
        self.query_one("#proc-head", Static).update(
            Text.assemble((f" {len(procs)} processes", "bold"), f"  ·  sorted by {name}{filt}  ·  o: change sort  ·  click a row to light the board"))

    def _want_ipc(self, on: bool) -> None:
        """The IPC walk reads every process's fds: only run it while the tab shows."""
        if hasattr(self.source, "want_ipc"):
            self.source.want_ipc = on

    def _refresh_ipc(self) -> None:
        view = self.query_one(IpcView)
        ipc = self.snapshot.ipc
        members = view.panel.members(view.panel.selected)  # a folded row stands for all its processes
        if ipc is not None and members and not any(ipc.node(m) for m in members):
            view.panel.selected, members = None, set()  # the process exited or the container stopped
        sel = view.panel.selected
        self.query_one(IpcDetail).show(ipc, sel, self.history, members)
        self.query_one(IpcEvents).show(ipc, members)
        table = self.query_one("#ipc-links", DataTable)
        mine = [k for k in ipc.links if k.a in members or k.b in members] if ipc and sel else ipc.links if ipc else []
        nodes = {n.id: n for n in ipc.nodes} if ipc else {}
        kinds = tables.KIND_FILTERS[self._ipc_kind]
        text = self.query_one(IpcFilter).value.strip()
        mine = tables.ipc_filter(mine, nodes, kinds, text)
        status = " · ".join((f"f: {'+'.join(kinds) if kinds else 'all kinds'}",
                             f"/ “{text}”" if text else "/ filter", "Enter → process"))
        if view.panel.status != status:
            view.panel.status = status
            view.redraw()
        links = tables.ipc_sorted(mine)[:IPC_ROWS]
        row = table.cursor_row
        if table.size.width != self._ipc_table_w:
            # fixed widths: auto-sized columns only ever grow, and a long Unix path once would
            # push State and the rates off-screen for good
            self._ipc_table_w = table.size.width
            table.clear(columns=True)
            for col, w in zip(tables.IPC_COLS, tables.ipc_widths(table.size.width)):
                table.add_column(col, key=col or "tag", width=w)
        table.clear()
        for i, k in enumerate(links):
            table.add_row(*tables.ipc_row(k, nodes), key=str(i))
        self._ipc_links = links
        if links:
            table.move_cursor(row=min(row, len(links) - 1))

    def on_ipc_view_selected(self, event: IpcView.Selected) -> None:
        self._refresh_ipc()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "ipc-filter":
            self._refresh_ipc()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action.startswith("ipc_"):
            return self.query_one("#tabs", TabbedContent).active == "ipc"
        return True

    def action_ipc_level(self) -> None:
        from .render.ipc import LEVELS

        view = self.query_one(IpcView)
        view.panel.level = (view.panel.level + 1) % len(LEVELS)
        view.redraw()

    def action_ipc_kind(self) -> None:
        self._ipc_kind = (self._ipc_kind + 1) % len(tables.KIND_FILTERS)
        self._refresh_ipc()

    def action_ipc_filter(self) -> None:
        box = self.query_one(IpcFilter)
        box.display = True
        box.focus()

    def action_ipc_jump(self) -> None:
        """Enter on the map: show the selected node's busiest process on the Processes tab."""
        view = self.query_one(IpcView)
        ipc = self.snapshot.ipc
        pids = [p for m in view.panel.members(view.panel.selected) if ipc and (n := ipc.node(m)) for p in n.pids]
        if not pids:
            self.notify("select a process on the map first", severity="warning")
            return
        cpu = {p.pid: p.cpu for p in self.snapshot.procs}
        self._proc_pid = max(pids, key=lambda p: (cpu.get(p, -1.0), -p))
        self.part_filter = None
        self.set_focus(None)  # TabbedContent follows focus: the map would pull it straight back
        self.action_tab("proc")
        self.call_after_refresh(self.query_one("#procs", DataTable).focus)

    def on_board_view_selected(self, event: BoardView.Selected) -> None:
        self.part_filter = event.cid
        self._refresh_detail()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "ipc-links":
            try:
                k = self._ipc_links[int(str(event.row_key.value))]
            except (IndexError, TypeError, ValueError):
                return
            view = self.query_one(IpcView)
            view.panel.highlight = {k.a, k.b} - {""}
            view.redraw()
            return
        if event.data_table.id != "procs":
            return
        try:
            pid = int(str(event.row_key.value))
        except (TypeError, ValueError):
            return
        proc = next((p for p in self.snapshot.procs if p.pid == pid), None)
        board = self.query_one(BoardView)
        board.highlight = set(B.clients(self.snapshot, self.match.profile, proc)) if proc else set()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.tabbed_content.id == "gpu-tabs" or event.pane.id.startswith("gtab"):
            self._refresh_gpu_tables()
            return
        if self._syncing_tabs:
            return
        board = self.query_one(BoardView)
        if board.scene_layer.startswith("gpu-") or board.transitioning:
            tid = event.pane.id
            if tid in GPU_TAB:
                target = GPU_TAB[tid]
                if target != board.scene_layer and not board.transitioning:
                    board.begin_transition(target, inbound=True, duration=max(0.08, BoardView.TRANSITION_S * 0.5))
            elif tid == "proc":
                from gputui import tables as GT
                self.query_one("#gpu-tabs", TabbedContent).active = f"gtab{GT.TABS.index('Processes')}"
            elif tid == "sens":
                self.query_one("#gpu-tabs", TabbedContent).active = "gtab0"
            elif tid == "ipc" and not board.transitioning:
                self._pending_tab = "ipc"  # a whole-system view: fly back out to the board first
                self.action_unzoom()
            return
        self._sync_stage()
        on_ipc = event.pane.id == "ipc"
        was = getattr(self.source, "want_ipc", True)
        self._want_ipc(on_ipc)
        self._apply(self.snapshot, ingest=False)
        if event.pane.id == "board":
            self.query_one(BoardView).focus()
        elif on_ipc:
            self.query_one(IpcView).focus()
            if not was:
                self.poll()  # don't wait a whole interval for the first walk

    # --------------------------------------------------------------------- actions
    def action_tab(self, tid: str) -> None:
        board = self.query_one(BoardView)
        if board.transitioning:
            return
        tabs = self.query_one("#tabs", TabbedContent)
        if board.scene_layer.startswith("gpu-"):
            tabs.active = tid
            return
        tabs.active = tid

    def action_mode(self) -> None:
        b = self.query_one(BoardView)
        b.mode = MODES[(MODES.index(b.mode) + 1) % len(MODES)]
        self._header()

    def action_labels(self) -> None:
        b = self.query_one(BoardView)
        b.labels = not b.labels
        b.rebuild()

    def action_card(self) -> None:
        b = self.query_one(BoardView)
        b.card = not b.card
        b.rebuild()

    def action_sort(self) -> None:
        keys = list(tables.SORTS)
        self.proc_sort = keys[(keys.index(self.proc_sort) + 1) % len(keys)]
        self.action_tab("proc")
        self._refresh_procs()

    def action_pause(self) -> None:
        self.paused = not self.paused
        self._header()

    def action_interval(self, delta: float) -> None:
        self.interval = max(0.25, min(10.0, self.interval + delta))
        if self._timer:
            self._timer.stop()
        self._timer = self.set_interval(self.interval, self.poll)
        self.notify(f"poll interval {self.interval:.2f} s")

    def action_save(self) -> None:
        if self.tape:
            path = f"systui-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
            with open(path, "w") as f:
                for snap in self.tape:
                    d = snap.to_dict()
                    d["board_profile"] = self.match.profile.id
                    f.write(json.dumps(d, default=str) + "\n")
            self.notify(f"saved {path} ({len(self.tape)} frames)")
            return
        path = f"systui-{datetime.now():%Y%m%d-%H%M%S}.json"
        data = self.snapshot.to_dict()
        data["board_profile"] = self.match.profile.id
        data["match_level"] = self.match.level
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        self.notify(f"saved {path}")

    def action_record(self) -> None:
        self.recording = not self.recording
        if self.recording:
            self.tape.clear()
            self.tape.append(self.snapshot)
        self._header()
        self.notify("recording" if self.recording else f"recorded {len(self.tape)} frames")

    def action_replay(self) -> None:
        if not self.tape:
            self.notify("nothing recorded — press r first", severity="warning")
            return
        self.replay_i = 0
        self.paused = True
        self._apply(self.tape[0], ingest=False)
        self.notify(f"replay 1/{len(self.tape)}  [ ] to step  Esc live")

    def action_step(self, delta: int) -> None:
        if self.replay_i is None or not self.tape:
            return
        self.replay_i = max(0, min(len(self.tape) - 1, self.replay_i + delta))
        self._apply(self.tape[self.replay_i], ingest=False)

    def action_cpu_die(self) -> None:
        board = self.query_one(BoardView)
        if board.transitioning:
            return
        if board.scene_layer == "cpu-die":
            self.action_unzoom()
            return
        self._set_chrome("cpu-die")
        board.begin_transition("cpu-die", focus_id="CPU", inbound=True)

    def action_enter_part(self) -> None:
        board = self.query_one(BoardView)
        if board.scene_layer != "board":
            return
        if self._is_gpu_part(board.selected):
            self._zoom_gpu()
        elif board.selected == "CPU":
            self.action_cpu_die()

    def action_unzoom(self) -> None:
        if self.replay_i is not None:
            self.replay_i = None
            self.paused = False
            self._header()
            return
        board = self.query_one(BoardView)
        if board.transitioning or board.scene_layer == "board":
            return
        focus = "GPU" if board.scene_layer.startswith("gpu-") else "CPU"
        board.begin_transition("board", focus_id=focus, inbound=False)

    def on_board_view_transitioned(self, event: BoardView.Transitioned) -> None:
        self._set_chrome(event.layer)  # forces the Board tab on the way out of GPU mode
        self._refresh_detail()
        self._header()
        if self._pending_tab and event.layer == "board":
            tab, self._pending_tab = self._pending_tab, None
            self.query_one("#tabs", TabbedContent).active = tab

    def _set_tab_labels(self, labels: dict[str, str]) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        for pane_id, title in labels.items():
            try:
                tabs.get_tab(pane_id).label = title
            except Exception:
                pass

    def _sync_stage(self, layer: str | None = None) -> None:
        board = self.query_one(BoardView)
        if layer is None:
            layer = board.scene_layer
        gpu_mode = layer.startswith("gpu-")
        tabs = self.query_one("#tabs", TabbedContent)
        show_stage = gpu_mode or (tabs.active or "board") == "board"
        self.query_one("#stage").set_class(not show_stage, "hidden")
        tabs.set_class(show_stage, "bar-only")

    def _set_chrome(self, layer: str) -> None:
        gpu_mode = layer.startswith("gpu-")
        deep = gpu_mode or layer == "cpu-die"
        if gpu_mode:
            self._want_ipc(False)  # GPU tab switches return early in on_tabbed_content_tab_activated
            if self.focused is not None and self.focused is not self.query_one(BoardView):
                # focus would fall to the next widget in the pane being hidden, and TabbedContent
                # follows focus: it would switch straight back to that tab
                self.set_focus(None)
                self.call_after_refresh(self.query_one(BoardView).focus)
        self.query_one("#overview").set_class(deep, "hidden")
        self.query_one("#gpu-hist").set_class(not gpu_mode, "hidden")
        self.query_one("#gpu-tabs").set_class(not gpu_mode, "hidden")
        gpu = self.query_one("#gpu")
        gpu.border_title = "Live" if gpu_mode else "Graphics card"
        self._syncing_tabs = True
        try:
            self._set_tab_labels(GPU_TAB_LABELS if gpu_mode else SYS_TAB_LABELS)
            tabs = self.query_one("#tabs", TabbedContent)
            if gpu_mode:
                tabs.active = LAYER_TAB.get(layer, "cpu")
            else:
                tabs.active = "board"
        finally:
            self._syncing_tabs = False
        self._sync_stage(layer)
        if gpu_mode:
            board = self.query_one(BoardView)
            if board.gpu_snap is not None:
                self.query_one(GpuPanel).show_gpu(board.gpu_snap, self.query_one("#side").size.width - 4)
            self._refresh_gpu_sparklines()
            self._refresh_gpu_tables()

    def _is_gpu_part(self, cid: str | None) -> bool:
        if cid == "GPU":
            return True
        board = self.query_one(BoardView)
        if board.scene is None or cid not in board.scene.comps:
            return False
        c = board.scene.comps[cid]
        return getattr(c, "bind", None) == "gpu:x16"

    def _zoom_gpu(self) -> None:
        from gputui.identify import identify as gpu_identify
        from gputui.telemetry import DemoSource as GpuDemo
        from gputui.widgets import History as GpuHistory

        board = self.query_one(BoardView)
        if board.transitioning or board.scene_layer.startswith("gpu-"):
            return
        if self.gpu_source is None:
            if self.snapshot.source == "demo":
                sku = "5080"
                g = self.snapshot.gpu("nvidia")
                if g and "5090" in (g.name or ""):
                    sku = "5090"
                self.gpu_source = GpuDemo(sku)
            else:
                try:
                    from gputui.telemetry import NvmlSource
                    self.gpu_source = NvmlSource(0)
                except Exception as e:
                    self.notify(f"cannot open GPU telemetry ({e})", severity="warning")
                    return
        snap = self.gpu_source.collect()
        match = gpu_identify(snap)
        if match is None:
            from gputui.boards import load_profiles
            from gputui.chips import SKU_BY_KEY
            from gputui.identify import Match as GpuMatch

            sku = SKU_BY_KEY["5080"]
            match = GpuMatch(sku, load_profiles()["zotac-5080"], "forced", None)
            self.notify("no exact board match — using ZOTAC 5080 layout", severity="warning")
        self.gpu_hist = GpuHistory()
        self.gpu_hist.push(snap)
        board.gpu_match = match
        board.gpu_snap = snap
        board.gpu_history = self.gpu_hist.data
        self._set_chrome("gpu-front")
        self.notify("GPU PCB — 1–5 views · click a part · Esc back")
        board.begin_transition("gpu-front", focus_id="GPU", inbound=True)

    def action_gputui(self) -> None:
        board = self.query_one(BoardView)
        if board.transitioning:
            return
        if not self.gputui_explicit:
            if board.scene_layer.startswith("gpu-"):
                self.notify("already on the GPU — Esc returns to the motherboard")
                return
            self._zoom_gpu()
            return
        if not self.gputui_cmd:
            self.notify("gputui not found: put it on PATH or pass --gputui 'command'", severity="warning")
            return
        try:
            with self.suspend():
                try:
                    subprocess.run(self.gputui_cmd)
                except OSError as e:
                    print(f"could not start gputui: {e}")
        except SuspendNotSupported:
            self.notify("this terminal can't suspend the app; run gputui separately", severity="warning")
            return
        self.refresh()

    def action_browse(self) -> None:
        board = self.query_one(BoardView)
        if not board.scene_layer.startswith("gpu-") or board.gpu_match is None:
            return
        from gputui.app import BoardBrowser
        from gputui.boards import load_profiles
        from gputui.chips import SKU_BY_KEY
        from gputui.identify import Match as GpuMatch
        from gputui.telemetry import DemoSource as GpuDemo

        def done(pid: str | None) -> None:
            if pid is None:
                return
            if pid == "__auto__":
                try:
                    from gputui.telemetry import NvmlSource
                    self.gpu_source = NvmlSource(0)
                except Exception as e:
                    self.notify(f"cannot open GPU ({e})", severity="warning")
                    return
                snap = self.gpu_source.collect()
                match = gpu_identify_fn(snap)
                if match is None:
                    return
            else:
                prof = load_profiles()[pid]
                sku = SKU_BY_KEY[prof.skus[0]]
                gb = round(len(prof.of_kind("mem")) * prof.mem_gb_each) or None
                self.gpu_source = GpuDemo(sku.key, prof.vendor or 0x10DE, (prof.subsystems or [0])[0], mem_gb=gb)
                snap = self.gpu_source.collect()
                match = GpuMatch(sku, prof, "forced", None)
            board.gpu_match = match
            board.gpu_snap = snap
            self.gpu_hist.push(snap)
            board.gpu_history = self.gpu_hist.data
            board.selected = None
            board.rebuild()
            self._refresh_detail()
            self._refresh_gpu_tables()
            self._header()

        from gputui.identify import identify as gpu_identify_fn
        sku_key = board.gpu_match.sku.key if board.gpu_match else None
        self.push_screen(BoardBrowser(board.gpu_match.profile.id, sku_key), done)

    def _refresh_gpu_sparklines(self) -> None:
        hist = self.gpu_hist
        if hist is None:
            return
        from gputui.widgets import History as GpuHistory
        for key in GpuHistory.KEYS:
            sl = self.query_one(f"#sl-{key}", Sparkline)
            sl.data = list(hist.data[key])
            last = hist.data[key][-1]
            label = {"util": "GPU util", "power": "Power", "temp": "Temp", "vram": "VRAM"}[key]
            unit = {"util": "%", "power": "W", "temp": "°C", "vram": "G"}[key]
            self.query_one(f"#sl-{key}-label", Static).update(f"{label} {last:5.1f}{unit}")

    def _refresh_gpu_tables(self) -> None:
        from gputui import tables as GT

        board = self.query_one(BoardView)
        snap, match = board.gpu_snap, board.gpu_match
        if snap is None or match is None:
            return
        tc = self.query_one("#gpu-tabs", TabbedContent)
        active = tc.active or "gtab0"
        try:
            i = int(str(active).replace("gtab", ""))
        except ValueError:
            i = 0
        name = GT.TABS[i]
        target = self.query_one(f"#gtabc{i}", Static)
        if name == "Components":
            extra = [c for cid, c in (board.scene.comps.items() if board.scene else [])
                     if match.profile.by_id(cid) is None]
            target.update(GT.components_table(snap, match, board.selected, extra))
        else:
            target.update(GT.BUILDERS[name](snap, match))
