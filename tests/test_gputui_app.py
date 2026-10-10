import pytest

from gputui.app import GpuTuiApp
from gputui.telemetry import DemoSource
from gputui.widgets import BoardView, DetailPanel


@pytest.mark.asyncio
async def test_app_select_views_and_browse():
    app = GpuTuiApp(DemoSource("5080"), interval=0.3)
    async with app.run_test(size=(200, 56)) as pilot:
        await pilot.pause(0.5)
        board = app.query_one(BoardView)
        assert board.scene is not None and board.selected == "GPU"
        # click the middle of memory module M3
        x0, y0, x1, y1 = board.scene.boxes["M3"]
        await pilot.click(BoardView, offset=((x0 + x1) // 2 + 1, ((y0 + y1) // 2) // 2 + 1))
        await pilot.pause(0.2)
        assert board.selected == "M3"
        assert app.query_one(DetailPanel).current == "M3"
        await pilot.press("tab")
        assert board.selected != "M3"
        for key, view in (("1", "card"), ("3", "back"), ("4", "die"), ("5", "cores")):
            await pilot.press(key)
            await pilot.pause(0.1)
            assert board.view == view
        x0, y0, x1, y1 = board.scene.boxes["GPC1"]
        await pilot.click(BoardView, offset=((x0 + x1) // 2 + 1, ((y0 + y1) // 2) // 2 + 1))
        await pilot.pause(0.2)
        assert app.query_one(DetailPanel).current == "GPC1"
        assert board.scene.history is app.history.data
        await pilot.press("2")
        await pilot.pause(0.1)
        assert board.view == "front"
        await pilot.press("h", "l", "p", "t", "]", "]")
        await pilot.pause(0.4)
        # browse to the 5090 FE (demo data for a different SKU)
        await pilot.press("b")
        await pilot.pause(0.2)
        from textual.widgets import OptionList

        ol = app.screen.query_one(OptionList)
        idx = next(i for i in range(ol.option_count) if ol.get_option_at_index(i).id == "nvidia-5090-fe")
        ol.highlighted = idx
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.match.profile.id == "nvidia-5090-fe"
        assert app.demo_override
        assert "M15" in board.scene.comps
