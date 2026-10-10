from pathlib import Path

from pixboard.canvas import Canvas
from pixboard.png import canvas_to_png, canvases_to_gif
from pixboard.scene import Scene


def test_transition_frame_endpoints():
    from pixboard.canvas import Canvas, transition_frame

    a = Canvas(8, 4, bg=0x112233)
    a.fill(0, 0, a.pw, a.ph, 0xFF0000)
    b = Canvas(8, 4, bg=0x00FF00)
    b.fill(0, 0, b.pw, b.ph, 0x00AA00)
    start = transition_frame(a, b, (2, 2, 6, 6), 0.0, inbound=True)
    assert start.px[0] == 0xFF0000
    end = transition_frame(a, b, (2, 2, 6, 6), 1.0, inbound=True)
    assert end.px[0] == 0x00AA00
    mid = transition_frame(a, b, (2, 2, 6, 6), 0.5, inbound=True)
    assert mid.px[0] != start.px[0]


def test_canvas_copy_and_segments():
    cv = Canvas(4, 2, bg=0x112233)
    cv.fill(0, 0, 4, 2, 0xFF0000)
    cv.put_text(0, 0, "ab", 0xFFFFFF)
    segs = cv.segments(0)
    assert sum(len(s.text) for s in segs) == 4
    assert all(s.style is not None for s in segs)
    cp = cv.copy()
    assert cp.px == cv.px
    cp.set(0, 0, 0x00FF00)
    assert cv.get(0, 0) == 0xFF0000


def test_scene_hit_neighbour_highlight(tmp_path: Path):
    sc = Scene(20, 10)
    sc.boxes = {"A": (2, 2, 8, 8), "B": (12, 2, 18, 8)}
    sc.comps = {"A": object(), "B": object()}
    ax0, ay0, ax1, ay1 = sc.boxes["A"]
    assert sc.hit((ax0 + ax1) // 2, ((ay0 + ay1) // 2) // 2) == "A"
    assert sc.neighbour("A", 1, 0) == "B"
    cv = sc.frame(0.3, None, selected="A", highlight={"B"})
    assert (cv.w, cv.h) == (20, 10)
    png = tmp_path / "s.png"
    canvas_to_png(cv, str(png))
    assert png.stat().st_size > 50
    gif = tmp_path / "s.gif"
    canvases_to_gif([cv, sc.frame(0.6, None, selected="B")], str(gif), fps=8)
    assert gif.stat().st_size > png.stat().st_size


def test_systui_gif_cli(tmp_path):
    from systui.cli import main
    out = tmp_path / "b.gif"
    main(["--demo", "--gif", str(out), "--seconds", "0.4", "--fps", "5", "--size", "80x24"])
    assert out.stat().st_size > 200


def test_gputui_gif_cli(tmp_path):
    from gputui.cli import main
    out = tmp_path / "g.gif"
    main(["--demo", "5080", "--gif", str(out), "--seconds", "0.4", "--fps", "5", "--size", "80x24"])
    assert out.stat().st_size > 200


def test_cpu_die_scene():
    from systui.render.cpudie import CpuDieScene
    from systui.telemetry.demo import DemoSource
    snap = DemoSource().collect(5.0)
    sc = CpuDieScene(80, 28, snap)
    assert "CORE0" in sc.comps and "CORE5" in sc.comps
    assert "FUSED6" in sc.comps
    cv = sc.frame(0.2, snap, selected="CORE0")
    assert (cv.w, cv.h) == (80, 28)


def test_human_rate_pair():
    from pixboard.charts import human_rate_pair

    assert human_rate_pair(2 * 2**20, 512) == "▲2.0M/s ▼512B/s"
    assert human_rate_pair(125_000, None, bits=True) == "▲1.0Mb/s ▼—"
