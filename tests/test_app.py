import pytest
from textual.widgets import DataTable, TabbedContent

from systui.app import SysTuiApp
from systui.telemetry.demo import DemoSource
from systui.widgets import BoardView, DetailPanel, PanelView


@pytest.mark.asyncio
async def test_app_tabs_selection_and_keys():
    app = SysTuiApp(DemoSource(), interval=0.3, gputui="true")
    async with app.run_test(size=(200, 56)) as pilot:
        await pilot.pause(0.6)
        board = app.query_one(BoardView)
        assert board.scene is not None and board.selected == "CPU"
        x0, y0, x1, y1 = board.scene.boxes["DIMMA2"]
        await pilot.click(BoardView, offset=((x0 + x1) // 2 + 1, ((y0 + y1) // 2) // 2 + 1))
        await pilot.pause(0.2)
        assert board.selected == "DIMMA2"
        assert app.query_one(DetailPanel).current == "DIMMA2"
        await pilot.press("tab")
        assert board.selected != "DIMMA2"
        await pilot.press("h", "h", "l", "c")
        await pilot.pause(0.3)
        assert board.mode == "off" and not board.labels and not board.card
        tabs = app.query_one(TabbedContent)
        for key, tid in (("2", "cpu"), ("3", "mem"), ("4", "disk"), ("5", "net"), ("6", "proc"), ("7", "sens"),
                         ("8", "ipc"), ("1", "board")):
            await pilot.press(key)
            await pilot.pause(0.4)
            assert tabs.active == tid
        await pilot.press("o")
        await pilot.pause(0.4)
        assert app.proc_sort == "mem" and app.query_one("#procs", DataTable).row_count > 0
        assert all(pv._frame is not None for pv in app.query(PanelView) if pv.is_on_screen)
        await pilot.press("G")  # launches `true` under suspend()
        await pilot.press("p")
        assert app.paused


@pytest.mark.asyncio
async def test_ipc_tab_map_table_and_selection():
    from systui.widgets import IpcDetail, IpcView

    app = SysTuiApp(DemoSource(), interval=0.3, initial_tab="ipc")
    async with app.run_test(size=(200, 56)) as pilot:
        await pilot.pause(0.6)
        view = app.query_one(IpcView)
        table = app.query_one("#ipc-links", DataTable)
        everything = table.row_count
        assert everything == len(app.snapshot.ipc.links) and view._frame is not None
        assert app.query_one("#ipc-side").size.width > 0 and app.query_one("#side").size.width == 0
        x0, y0, x1, y1 = view.panel.boxes["ctr:44254bc720fd"]
        await pilot.click(IpcView, offset=(x0 + 3 + 1, y0 + 1))  # +1: the border
        await pilot.pause(0.4)
        assert view.panel.selected == "ctr:44254bc720fd"
        assert table.row_count == len(app.snapshot.ipc.links_of("ctr:44254bc720fd")) < everything
        assert app.query_one(IpcDetail).current == "ctr:44254bc720fd"
        table.focus()
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert "ctr:44254bc720fd" in view.panel.highlight
        view.focus()
        await pilot.press("tab")
        await pilot.pause(0.2)
        assert view.panel.selected not in (None, "ctr:44254bc720fd")


@pytest.mark.asyncio
async def test_ipc_tab_from_gpu_mode_flies_back_to_the_board_first():
    BoardView.TRANSITION_S = 0.08
    app = SysTuiApp(DemoSource(), interval=0.3)
    try:
        async with app.run_test(size=(200, 56)) as pilot:
            await pilot.pause(0.5)
            board = app.query_one(BoardView)
            await pilot.press("g")
            await pilot.pause(0.45)
            assert board.scene_layer.startswith("gpu-")
            await pilot.press("8")
            await pilot.pause(0.6)
            assert board.scene_layer == "board"
            assert app.query_one(TabbedContent).active == "ipc"
            assert "IPC" in app.query_one("#tabs").get_tab("ipc").label_text
    finally:
        BoardView.TRANSITION_S = 0.55


class LazyIpcDemo(DemoSource):
    """Like LinuxSource: no IPC data unless something asked for it."""

    want_ipc = False

    def collect(self, now=None):
        s = super().collect(now)
        if not self.want_ipc:
            s.ipc = None
        return s


@pytest.mark.asyncio
async def test_switching_tabs_starts_and_stops_the_ipc_walk():
    BoardView.TRANSITION_S = 0.08
    src = LazyIpcDemo()
    app = SysTuiApp(src, interval=0.3)
    try:
        async with app.run_test(size=(200, 56)) as pilot:
            await pilot.pause(0.4)
            assert not src.want_ipc
            await pilot.press("8")
            await pilot.pause(0.5)
            assert src.want_ipc and app.query_one("#ipc-links", DataTable).row_count > 0
            await pilot.press("1")
            await pilot.pause(0.3)
            assert not src.want_ipc
            await pilot.press("8")
            await pilot.pause(0.3)
            assert src.want_ipc
            await pilot.press("g")  # straight into the GPU from the IPC tab
            await pilot.pause(0.5)
            assert app.query_one(BoardView).scene_layer.startswith("gpu-")
            assert not src.want_ipc
    finally:
        BoardView.TRANSITION_S = 0.55


def test_ipc_tab_turns_the_walk_on_only_while_it_shows():
    class Src(DemoSource):
        want_ipc = False

    src = Src()
    SysTuiApp(src, initial_tab="ipc")
    assert src.want_ipc
    src2 = Src()
    SysTuiApp(src2)
    assert not src2.want_ipc


@pytest.mark.asyncio
async def test_g_zooms_into_gpu_pcb():
    BoardView.TRANSITION_S = 0.08
    app = SysTuiApp(DemoSource(), interval=0.3)
    async with app.run_test(size=(200, 56)) as pilot:
        await pilot.pause(0.5)
        board = app.query_one(BoardView)
        await pilot.press("g")
        await pilot.pause(0.45)
        assert board.scene_layer.startswith("gpu-")
        assert board._xfade is None
        assert "GPU" in board.scene.comps
        assert app.query_one(DetailPanel).current is not None
        assert not app.query_one("#gpu-tabs").has_class("hidden")
        assert "PCB" in app.query_one("#tabs").get_tab("cpu").label_text
        assert app.query_one("#tabs").has_class("bar-only")
        assert app.query_one("#tabs").size.height >= 2
        app._refresh_gpu_tables()
        await pilot.press("4")
        await pilot.pause(0.4)
        assert board.scene_layer == "gpu-die"
        await pilot.press("escape")
        await pilot.pause(0.45)
        assert board.scene_layer == "board"
        assert "CPU" in board.scene.comps
    BoardView.TRANSITION_S = 0.55


@pytest.mark.asyncio
async def test_cpu_die_and_record():
    app = SysTuiApp(DemoSource(), interval=0.3)
    async with app.run_test(size=(200, 56)) as pilot:
        await pilot.pause(0.4)
        BoardView.TRANSITION_S = 0.08
        await pilot.press("d")
        await pilot.pause(0.4)
        board = app.query_one(BoardView)
        assert board.scene_layer == "cpu-die"
        assert "CORE0" in board.scene.comps
        assert "FUSED6" in board.scene.comps
        await pilot.press("escape")
        await pilot.pause(0.5)
        assert board.scene_layer == "board"
        BoardView.TRANSITION_S = 0.55
        await pilot.press("r")
        await pilot.pause(0.2)
        assert app.recording and len(app.tape) >= 1
        await pilot.press("R")
        await pilot.pause(0.2)
        assert app.replay_i == 0


@pytest.mark.asyncio
async def test_ipc_kind_cycle_text_filter_and_jump_to_process():
    from systui.widgets import IpcFilter, IpcView

    app = SysTuiApp(DemoSource(), interval=0.3, initial_tab="ipc")
    async with app.run_test(size=(200, 56)) as pilot:
        await pilot.pause(0.6)
        table = app.query_one("#ipc-links", DataTable)
        everything = table.row_count
        await pilot.press("f", "f")  # all → tcp+udp → unix
        await pilot.pause(0.2)
        assert 0 < table.row_count < everything and all(k.kind == "unix" for k in app._ipc_links)
        assert "unix" in app.query_one(IpcView).panel.status
        await pilot.press("f", "f", "f", "f")  # → pipe → shm → listen → all
        await pilot.pause(0.2)
        assert table.row_count == everything
        await pilot.press("slash")
        box = app.query_one(IpcFilter)
        assert box.display and box.has_focus
        await pilot.press(*"postgres")
        await pilot.pause(0.2)
        assert 0 < table.row_count < everything
        assert all("ctr:df517a104788" in (k.a, k.b) for k in app._ipc_links)
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert not box.display and box.value == "" and table.row_count == everything
        assert app.query_one(TabbedContent).active == "ipc"  # Escape did not reach the app
        view = app.query_one(IpcView)
        view.focus()
        view.select("proc:1004")
        await pilot.press("enter")
        await pilot.pause(0.5)
        assert app.query_one(TabbedContent).active == "proc"
        procs = app.query_one("#procs", DataTable)
        assert procs.coordinate_to_cell_key(procs.cursor_coordinate).row_key.value == "1004"


@pytest.mark.asyncio
async def test_ipc_level_key_cycles_which_rows_show():
    from systui.render.ipc import LEVELS
    from systui.widgets import IpcView

    app = SysTuiApp(DemoSource(), interval=0.3, initial_tab="ipc")
    async with app.run_test(size=(200, 56)) as pilot:
        await pilot.pause(0.4)
        view = app.query_one(IpcView)
        assert view.panel.level == 0
        await pilot.press("i")
        assert view.panel.level == 1
        await pilot.press(*"i" * (len(LEVELS) - 1))
        assert view.panel.level == 0
        await pilot.press("1")
        await pilot.pause(0.3)
        await pilot.press("i")  # off the IPC tab the key does nothing
        assert view.panel.level == 0
