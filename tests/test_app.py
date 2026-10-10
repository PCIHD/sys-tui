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
        for key, tid in (("2", "cpu"), ("3", "mem"), ("4", "disk"), ("5", "net"), ("6", "proc"), ("7", "sens"), ("1", "board")):
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
