import pytest

from systui.boards import identify, load_profiles
from systui.canvas import Canvas
from systui.history import History
from systui.render import panels as P
from systui.render.board import BoardScene, level
from systui.telemetry.demo import DemoSource

SIZES = [(40, 14), (120, 40), (200, 60)]
SRC = DemoSource()
HIST = History()
for i in range(130):
    SNAP = SRC.collect(SRC.t0 + i)
    HIST.ingest(SNAP)
MSI = load_profiles()["msi-b650m-gaming-plus-wifi"]
GENERIC = identify(DemoSource(board="X").collect(1.0)).profile


@pytest.mark.parametrize("profile", [MSI, GENERIC], ids=["msi", "generic"])
@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize("card", [True, False], ids=["card", "nocard"])
def test_board_renders(profile, size, card):
    w, h = size
    sc = BoardScene(profile, w, h, hint=SNAP, card=card)
    for mode in ("live", "thermal", "off"):
        for t in (0.1, 0.4):
            cv = sc.frame(t, SNAP, selected="CPU", hover="DIMMA1", mode=mode)
            assert (cv.w, cv.h) == (w, h)
            assert len(cv.px) == w * h * 2
    cv = sc.frame(0.5, None)
    assert sum(len(s.text) for s in cv.segments(h // 2)) == w


def test_hit_and_navigation():
    sc = BoardScene(MSI, 160, 56, hint=SNAP)
    for cid in ("CPU", "DIMMA1", "M2_1", "PCH"):
        x0, y0, x1, y1 = sc.boxes[cid]
        assert sc.hit((x0 + x1) // 2, ((y0 + y1) // 2) // 2) == cid
    right = sc.neighbour("CPU", 1, 0)
    assert right is not None and sc.boxes[right][0] > sc.boxes["CPU"][0]
    assert "GPU" in sc.comps  # card ghost present when a GPU is reported


def test_card_hidden_without_gpu():
    snap = SRC.collect(SRC.t0 + 5)
    snap.gpus = []
    sc = BoardScene(MSI, 120, 40, hint=snap)
    assert "GPU" not in sc.comps
    sc.frame(0.2, snap)


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_panels_render(size):
    w, h = size
    panels = [P.CoreGrid(), P.Threads(), P.Package(), P.Memory(), P.Dimms(MSI), P.Dimms(), P.Disk("nvme0n1"),
              P.Partitions(), P.Net("enp6s0"), P.Fans(), P.Overview()]
    for p in panels:
        for snap in (SNAP, None):
            cv = p.canvas(w, h)
            p.draw(cv, snap, HIST, 1.0)
            assert (cv.w, cv.h) == (w, h)


def test_memory_panel_shows_the_running_clock_and_fills_the_chart():
    from systui.telemetry import MemStat, SystemSnapshot

    snap = SystemSnapshot(t=0.0)
    snap.mem = MemStat(total=64 << 30, available=8 << 30, used=50 << 30, cached=6 << 30,
                        swap_total=16 << 30, swap_used=7 << 30, memclk_mhz=2800,
                        peak_bps=89.6e9, bw_note="needs root")
    h = History()
    h.ingest(snap)
    panel = P.Memory()
    cv = panel.canvas(90, 28)
    panel.draw(cv, snap, h, 0.0)
    rows: dict[int, list] = {}
    for (x, y), item in cv.text.items():
        rows.setdefault(y, []).append((x, item[0]))
    text = "\n".join("".join(ch for _, ch in sorted(row)) for row in rows.values())
    assert "DDR5-5600" in text and "2800 MHz" in text and "89.6 GB/s" in text
    assert "0% of peak" not in text
    # one sample, held across the window, should paint the gauge instead of an empty grid
    assert sum(1 for c in cv.px if c not in (0x0D1117, 0x1C222C)) > 800


def test_dimm_panel_lists_each_sticks_details():
    from systui.telemetry import Dimm, SystemSnapshot

    snap = SystemSnapshot(t=0.0)
    snap.mem.total = 64 << 30
    snap.mem.memclk_mhz = 2800
    snap.mem.dimms = [Dimm(0x50 + i, 46.0 + i * 0.4, speed="5600 CL46", maker="Micron", dram_maker="Micron",
                           ranks=1, width=8, die_gbit=16, timings="46-45-45-90", made="2025 wk10", temp_max=55.0,
                           vdd=1.1, vddq=1.1, vpp=1.8) for i in range(4)]
    h = History()
    h.ingest(snap)
    panel = P.Dimms()
    cv = panel.canvas(120, 36)
    panel.draw(cv, snap, h, 0.0)
    text = "".join(item[0] for _, item in sorted(cv.text.items(), key=lambda kv: (kv[0][1], kv[0][0])))
    assert "16G" in text and "2800 MHz" in text
    assert text.count("Micron · own dies") == 4 and "1R x8 · 16Gb dies" in text and "JEDEC 46-45-45-90" in text
    assert "46–46°C seen · warn 55" in text and "V 1.10/1.10/1.80" in text and "made 2025 wk10" in text
    assert "VDD/VDDQ/VPP" in text  # the footer says what the three volts are


def test_dimm_details_shorten_to_fit_narrow_columns():
    from systui.telemetry import Dimm

    d = Dimm(0x50, 46.0, maker="Kingston", dram_maker="SK hynix", ranks=2, width=8, die_gbit=16,
             timings="40-40-40-77", temp_max=55.0)
    lines = P._dimm_lines(d, (41.0, 47.0), 0, 14)
    assert all(len(t) <= 14 for t, _ in lines)
    assert [t for t, _ in lines][:3] == ["Kingston", "2R x8 16Gb", "40-40-40-77"]


def test_dimm_panel_shows_each_slots_channel_traffic():
    from systui.telemetry import Dimm, SystemSnapshot

    snap = SystemSnapshot(t=0.0)
    snap.mem.total = 64 << 30
    snap.mem.dimms = [Dimm(0x50 + i, 46.0) for i in range(4)]
    snap.mem.channel_bps = [(5 << 30, 2 << 30), (3 << 30, 1 << 30)]
    h = History()
    h.ingest(snap)
    cv = P.Dimms(MSI).canvas(80, 36)
    P.Dimms(MSI).draw(cv, snap, h, 0.0)
    text = "".join(item[0] for _, item in sorted(cv.text.items(), key=lambda kv: (kv[0][1], kv[0][0])))
    assert text.count("▲5.0G") == 2 and text.count("▼1.0G") == 2  # A1/A2 on A, B1/B2 on B
    assert "ch A" in text and "ch B" in text
    assert "busy" not in text  # no data-bus counter: no bar rather than a made-up 0 %
    snap.mem.channel_busy = [0.42, 0.07]
    cv = P.Dimms(MSI).canvas(80, 36)
    P.Dimms(MSI).draw(cv, snap, h, 0.0)
    text = "".join(item[0] for _, item in sorted(cv.text.items(), key=lambda kv: (kv[0][1], kv[0][0])))
    assert text.count("busy  42%") == 2 and text.count("busy   7%") == 2
    assert P._channel("A2", 2) == 0 and P._channel("B1", 2) == 1
    assert P._channel("B1", 1) is None and P._channel("0x50", 2) is None


def test_dimm_panel_says_why_channel_traffic_is_missing():
    from systui.telemetry import Dimm, SystemSnapshot

    snap = SystemSnapshot(t=0.0)
    snap.mem.total = 64 << 30
    snap.mem.dimms = [Dimm(0x50, 46.0)]
    snap.mem.bw_note = "needs root"
    cv = P.Dimms().canvas(80, 20)
    P.Dimms().draw(cv, snap, History(), 0.0)
    text = "".join(item[0] for _, item in sorted(cv.text.items(), key=lambda kv: (kv[0][1], kv[0][0])))
    assert "channel R/W: needs root" in text


def test_activity_level_is_monotonic_and_bounded():
    vals = [level(x) for x in (0, 1e3, 1e5, 1e7, 1e9, 1e12)]
    assert vals == sorted(vals) and vals[0] == 0 and vals[-1] == 1


def test_canvas_text_and_pixels():
    cv = Canvas(4, 2, bg=0)
    cv.fill(0, 0, 4, 4, 0xFF0000)
    cv.put_text(1, 1, "ab", 0xFFFFFF)
    assert "".join(s.text for s in cv.segments(1)) == "▀ab▀"


def test_data_routes_have_parallel_in_and_out_lanes():
    sc = BoardScene(MSI, 170, 58, hint=SNAP)
    assert {"M2_1", "GPU", "MEM", "UPLINK", "LAN"} <= set(sc.data_paths)
    for key, lanes in sc.data_paths.items():
        for in_lane, out_lane in lanes:
            assert in_lane and out_lane and set(in_lane) != set(out_lane), key
            ends = (in_lane[0], out_lane[0]), (in_lane[-1], out_lane[-1])
            for (ax, ay), (bx, by) in ends:
                assert max(abs(ax - bx), abs(ay - by)) == 1, key


def test_board_traffic_key_splits_in_and_out():
    sc = BoardScene(MSI, 170, 58, hint=SNAP)
    cv = sc.frame(0.3, SNAP, mode="live")
    rows = ["".join(cv.text.get((x, y), (" ",))[0] for x in range(cv.w)) for y in range(cv.h)]
    assert any("▲ to CPU" in r and "▼ from CPU" in r for r in rows)
    assert any("DRAM" in r and "▲" in r and "▼" in r for r in rows)


def test_dimm_panel_shows_live_power_and_the_total():
    from systui.telemetry import Dimm, SystemSnapshot

    snap = SystemSnapshot(t=0.0)
    snap.mem.total = 32 << 30
    snap.mem.dimms = [Dimm(0x50 + i, 45.0, vdd=1.1, vddq=1.1, vpp=1.8, rail_w=(1.65, 0.55, 0.225)) for i in range(2)]
    cv = P.Dimms().canvas(120, 30)
    P.Dimms().draw(cv, snap, History(), 0.0)
    text = "".join(item[0] for _, item in sorted(cv.text.items(), key=lambda kv: (kv[0][1], kv[0][0])))
    assert text.count("2.42 W · 1.65/0.55/0.23") == 2 and "DIMMs 4.8 W" in text


def test_dimm_footer_says_why_live_power_is_missing():
    from systui.telemetry import Dimm, SystemSnapshot

    snap = SystemSnapshot(t=0.0)
    snap.mem.total = 32 << 30
    snap.mem.dimms = [Dimm(0x50, 45.0)]
    snap.mem.dimm_power_note = "no access"
    cv = P.Dimms().canvas(120, 20)
    P.Dimms().draw(cv, snap, History(), 0.0)
    text = "".join(item[0] for _, item in sorted(cv.text.items(), key=lambda kv: (kv[0][1], kv[0][0])))
    assert "DIMM power: no access" in text
