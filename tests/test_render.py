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


def test_card_leaves_parts_under_it_visible():
    sc = BoardScene(MSI, 160, 56, hint=SNAP)
    cv = sc.frame(0.1, SNAP, mode="off")
    under = [i for cid in ("M2_2", "PCH") for i in sc.card_px if not sc.card_gap[i]
             and sc.boxes[cid][0] <= i % cv.pw < sc.boxes[cid][2] and sc.boxes[cid][1] <= i // cv.pw < sc.boxes[cid][3]]
    assert under and all(cv.px[i] == sc.static.px[i] for i in under if i not in set(sc.card_edge))
    assert any(cv.px[i] != sc.static.px[i] for i in sc.card_body)


def test_card_fades_when_peeking_underneath():
    sc = BoardScene(MSI, 160, 56, hint=SNAP)
    for k in range(6):
        sc.frame(k * 0.1, SNAP, hover="M2_2")
    assert sc._card_alpha < 0.3
    for k in range(6, 12):
        sc.frame(k * 0.1, SNAP, selected="GPU")
    assert sc._card_alpha > 0.9


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


def _overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


@pytest.mark.parametrize("size", [(60, 16), (120, 36), (170, 44)], ids=lambda s: f"{s[0]}x{s[1]}")
def test_ipc_map_renders_and_places_every_node_once(size):
    from systui.render.ipc import IpcMap

    w, h = size
    m = IpcMap()
    for t in (0.1, 0.4):
        cv = m.canvas(w, h)
        m.draw(cv, SNAP, HIST, t)
        assert (cv.w, cv.h) == (w, h) and len(cv.px) == w * h * 2
    boxes = list(m.boxes.items())
    for i, (nid, b) in enumerate(boxes):
        assert 0 <= b[0] < b[2] <= w and 0 <= b[1] < b[3] <= h, nid
        for other, c in boxes[i + 1:]:
            assert not _overlap(b, c), (nid, other)
    ids = {n.id for n in SNAP.ipc.nodes}
    if w >= 120:  # room for every guest, the LAN and the Internet
        assert ids <= set(m.boxes) | {"host"}
    assert "ctr:44254bc720fd" in m.boxes


def test_ipc_map_waits_for_data_and_survives_tiny_panels():
    from systui.render.ipc import IpcMap
    from systui.telemetry import SystemSnapshot

    cv = IpcMap().canvas(80, 20)
    IpcMap().draw(cv, SystemSnapshot(t=0.0), History(), 0.0)
    assert "waiting" in "".join(v[0] for v in cv.text.values())
    cv = IpcMap().canvas(30, 6)
    IpcMap().draw(cv, SNAP, HIST, 0.0)


def test_ipc_map_keeps_rows_stable_and_folds_the_rest():
    from systui.render.ipc import IpcMap
    from systui.telemetry import IpcLink, IpcNode, IpcStat, SystemSnapshot

    nodes = [IpcNode("host", "host", "box")] + [IpcNode(f"proc:{i}", "process", f"p{i:02d}") for i in range(40)]
    links = [IpcLink(f"proc:{i}", f"proc:{i + 1}", "unix") for i in range(39)]
    snap = SystemSnapshot(t=0.0, ipc=IpcStat(nodes, links, {"unix": 39}))
    m = IpcMap()
    cv = m.canvas(120, 20)
    m.draw(cv, snap, History(), 0.0)
    rows = [nid for nid in m.boxes if nid.startswith("proc:")]
    assert 0 < len(rows) < 40
    text = "".join(item[0] for _, item in sorted(cv.text.items(), key=lambda kv: (kv[0][1], kv[0][0])))
    assert f"+{40 - len(rows)} more processes" in text
    m.draw(m.canvas(120, 20), snap, History(), 0.5)
    assert [nid for nid in m.boxes if nid.startswith("proc:")] == rows


def test_ipc_map_folds_same_name_processes_into_one_row():
    from systui.render.ipc import IpcMap
    from systui.tables import ipc_node, ipc_rows
    from systui.telemetry import IpcLink, IpcNode, IpcStat, SystemSnapshot

    nodes = [IpcNode("host", "host", "box"), IpcNode("wan", "wan", "Internet"), IpcNode("proc:3", "process", "dbus")]
    nodes += [IpcNode(f"proc:{i}", "process", "claude", pids=[i]) for i in (1, 2)]
    links = [IpcLink("proc:1", "wan", "tcp", in_bps=100.0, out_bps=10.0),
             IpcLink("proc:2", "wan", "tcp", in_bps=50.0, out_bps=5.0), IpcLink("proc:2", "proc:3", "unix")]
    from systui.telemetry.ipc import rollup

    rollup({n.id: n for n in nodes}, links)
    ipc = IpcStat(nodes, links, {"tcp": 2, "unix": 1})
    m = IpcMap()
    m.draw(m.canvas(120, 24), SystemSnapshot(t=0.0, ipc=ipc), History(), 0.0)
    assert "group:claude" in m.boxes and "proc:1" not in m.boxes
    assert m.members("group:claude") == {"proc:1", "proc:2"} and m.members("proc:3") == {"proc:3"}
    g = ipc_node(ipc, "group:claude", m.members("group:claude"))
    assert (g.name, g.pids, g.in_bps) == ("claude ×2", [1, 2], 150.0)
    rows = {r.key: r.value for r in ipc_rows(ipc, "group:claude", None, m.members("group:claude"))}
    assert rows["Links"] == "2 tcp · 1 unix" and "dbus (unix)" in rows["Talks to"]
