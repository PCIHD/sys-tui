import pytest

from gputui.boards import load_profiles
from gputui.canvas import Canvas
from gputui.chips import SKU_BY_KEY, CHIPS, enabled_sm_map
from gputui.render.card import CardScene
from gputui.render.cores import CoresScene
from gputui.render.die import DieScene
from gputui.render.pcb import PcbScene
from gputui.telemetry import DemoSource, GpuSnapshot
from gputui.widgets import History

PROFILES = list(load_profiles().values())
SIZES = [(40, 14), (120, 40), (200, 60)]


def scenes(p, w, h):
    sku = SKU_BY_KEY[p.skus[0]]
    hist = History()
    hist.push(DemoSource(sku.key).collect())
    return [PcbScene(p, sku, w, h, "front"), PcbScene(p, sku, w, h, "back"),
            CardScene(p, sku, w, h), DieScene(p, sku, w, h),
            CoresScene(p, sku, w, h), CoresScene(p, sku, w, h, history=hist.data)]


@pytest.mark.parametrize("p", PROFILES, ids=lambda p: p.id)
@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_all_views_render(p, size):
    w, h = size
    src = DemoSource(p.skus[0])
    snap = src.collect(src.t0 + 10)
    for sc in scenes(p, w, h):
        for mode in ("live", "thermal", "off"):
            cv = sc.frame(0.3, snap, selected=next(iter(sc.order), None), mode=mode)
            assert (cv.w, cv.h) == (w, h)
            assert len(cv.px) == w * h * 2
        cv = sc.frame(0.4, None)  # no telemetry yet
        assert sum(len(s.text) for s in cv.segments(h // 2)) == w


@pytest.mark.parametrize("p", PROFILES, ids=lambda p: p.id)
def test_click_hits_gpu_and_memory(p):
    sc = PcbScene(p, SKU_BY_KEY[p.skus[0]], 160, 50)
    for cid in ("GPU", p.memory[0].id):
        x0, y0, x1, y1 = sc.boxes[cid]
        assert sc.hit((x0 + x1) // 2, ((y0 + y1) // 2) // 2) == cid


def test_neighbour_navigation_moves_right():
    p = load_profiles()["zotac-5080"]
    sc = PcbScene(p, SKU_BY_KEY["5080"], 160, 50)
    right = sc.neighbour("GPU", 1, 0)
    assert right is not None and sc.boxes[right][0] > sc.boxes["GPU"][0]


def test_enabled_sm_counts():
    for sku in SKU_BY_KEY.values():
        grid = enabled_sm_map(sku.chip_spec, sku)
        assert sum(map(sum, grid)) == sku.sms
        assert sum(1 for g in grid if any(g)) == sku.gpcs


def test_die_marks_fused_units():
    p = load_profiles()["msi-5070ti"]
    sc = DieScene(p, SKU_BY_KEY["5070Ti"], 160, 50)
    assert sum(1 for *_, en in sc.sms if en) == 70
    assert len(sc.sms) == CHIPS["GB203"].sms


def test_canvas_text_and_pixels():
    cv = Canvas(4, 2, bg=0)
    cv.fill(0, 0, 4, 4, 0xFF0000)
    cv.put_text(1, 1, "ab", 0xFFFFFF)
    line = "".join(s.text for s in cv.segments(1))
    assert line == "▀ab▀"


def test_cores_tiles_follow_topology():
    p = load_profiles()["msi-5070ti"]
    sc = CoresScene(p, SKU_BY_KEY["5070Ti"], 160, 48)
    gpcs = [c for c in sc.comps.values() if c.kind == "gpc"]
    assert len(gpcs) == CHIPS["GB203"].gpcs
    assert sc.comps["GPC6"].note == "0/12 SMs enabled"
    assert sum(en for _, sms in sc.tiles.values() for _, en in sms) == 70
    assert "PIPES" in sc.comps
    x0, y0, x1, y1 = sc.boxes["GPC2"]
    assert sc.hit((x0 + x1) // 2, ((y0 + y1) // 2) // 2) == "GPC2"


def _pcb_5080(w=120, h=40):
    p = load_profiles()["zotac-5080"]
    return PcbScene(p, SKU_BY_KEY[p.skus[0]], w, h, "front")


def _snap(**kw):
    from dataclasses import replace

    snap = DemoSource("5080").collect()
    return replace(snap, **kw)


def _bus_pixels(sc, cid):
    return [q for _, _, paths in [b for b in sc.mem_buses if b[0] == cid] for path in paths for q in path]


def test_every_front_memory_chip_gets_a_visible_bus_and_the_fingers_get_lanes():
    sc = _pcb_5080()
    chips = [c.id for c in sc.profile.of_kind("mem", side="front")]
    assert [b[0] for b in sc.mem_buses] == chips
    assert all(_bus_pixels(sc, cid) for cid in chips)
    assert sc.pcie_lanes and all(path for _, path in sc.pcie_lanes)
    back = PcbScene(sc.profile, sc.sku, 120, 40, "back")
    assert back.mem_buses == [] and back.pcie_lanes == []


def test_bus_traces_never_light_up_over_a_chip_or_the_die():
    sc = _pcb_5080()
    covered = [sc.boxes[c] for c in sc.boxes if sc.comps[c].kind in ("mem", "choke")] + [sc.die_box]
    for _, _, paths in sc.mem_buses:
        for x, y in (q for path in paths for q in path):
            assert not any(x0 <= x < x1 and y0 <= y < y1 for x0, y0, x1, y1 in covered)


def test_memory_buses_brighten_with_dram_activity():
    sc = _pcb_5080()
    pts = [q for _, _, paths in sc.mem_buses for path in paths for q in path]

    def light(snap):
        cv = sc.frame(1.0, snap)
        return sum(sum((cv.px[y * cv.pw + x] >> s) & 0xFF for s in (0, 8, 16)) for x, y in pts)

    assert light(_snap(util_mem=95, gpm={})) > light(_snap(util_mem=0, gpm={})) * 1.3


def test_a_hot_memory_chip_turns_its_bus_warm():
    sc = _pcb_5080()
    n = len(sc.profile.of_kind("mem"))
    snap = _snap(util_mem=50, gpm={}, mem_modules=[50.0] * (n - 1) + [98.0])
    cv = sc.frame(1.0, snap, mode="thermal")
    hot_chip = next(c.id for c in sc.profile.of_kind("mem") if c.channel == n - 1)
    cool_chip = next(c.id for c in sc.profile.of_kind("mem") if c.channel == 0)

    def red_minus_blue(cid):
        vals = [cv.px[y * cv.pw + x] for x, y in _bus_pixels(sc, cid)]
        return sum((v >> 16) - (v & 0xFF) for v in vals) / len(vals)

    assert red_minus_blue(hot_chip) > 60 > red_minus_blue(cool_chip)


def test_pcie_lanes_past_the_link_width_stay_dark():
    sc = _pcb_5080(200, 60)
    snap = _snap(pcie_width=8, pcie_tx_kbs=5_000_000, pcie_rx_kbs=5_000_000)
    cv = sc.frame(1.0, snap)
    shared = {q for _, _, paths in sc.mem_buses for path in paths for q in path}
    shared |= {q for path in sc.power_paths for q in path}  # crossings light up for their own reasons
    lit = [any(cv.px[y * cv.pw + x] != sc.static.px[y * cv.pw + x] for x, y in path if (x, y) not in shared)
           for _, path in sc.pcie_lanes]
    half = len(lit) / 2
    assert all(lit[: int(half)]) and not any(lit[round(half):])
