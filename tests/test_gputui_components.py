import pytest

from gputui.boards import load_profiles
from gputui.chips import SKU_BY_KEY
from gputui.components import rows_for
from gputui.render.card import CardScene
from gputui.render.cores import CoresScene
from gputui.render.die import DieScene
from gputui.tables import BUILDERS, components_table
from gputui.identify import Match
from gputui.telemetry import DemoSource, GpuSnapshot

PROFILES = list(load_profiles().values())


@pytest.mark.parametrize("p", PROFILES, ids=lambda p: p.id)
def test_rows_for_every_component_with_and_without_data(p):
    sku = SKU_BY_KEY[p.skus[0]]
    src = DemoSource(sku.key)
    full = src.collect(src.t0 + 5)
    comps = list(p.components)
    comps += list(CardScene(p, sku, 80, 30).comps.values()) + list(DieScene(p, sku, 80, 30).comps.values())
    comps += list(CoresScene(p, sku, 80, 30).comps.values())
    for s in (full, GpuSnapshot(), None):
        for c in comps:
            rows = rows_for(c, p, sku, s)
            assert all(r.kind in ("live", "derived", "spec") for r in rows)


def test_memory_detail_mentions_channel_and_bandwidth(demo_snap):
    p = load_profiles()["zotac-5080"]
    rows = {r.key: r.value for r in rows_for(p.by_id("M3"), p, SKU_BY_KEY["5080"], demo_snap)}
    assert "controller #3 of 8" in rows["Channel"]
    assert "GB/s" in rows["Bandwidth now"]
    assert "Samsung" in rows["Part"]


def test_tables_build_with_empty_snapshot():
    p = load_profiles()["zotac-5080"]
    m = Match(SKU_BY_KEY["5080"], p, "vendor", "ZOTAC")
    for fn in BUILDERS.values():
        fn(GpuSnapshot(), m)
    components_table(GpuSnapshot(), m, "M0")


def test_pipes_and_gpc_detail(demo_snap):
    p = load_profiles()["zotac-5080"]
    sku = SKU_BY_KEY["5080"]
    sc = CoresScene(p, sku, 160, 48)
    pipes = {r.key: r for r in rows_for(sc.comps["PIPES"], p, sku, demo_snap)}
    assert pipes["SM active"].kind == "live" and pipes["SM active"].value.endswith("%")
    assert pipes["≈ busy CUDA cores"].kind == "derived" and "of 10752" in pipes["≈ busy CUDA cores"].value
    gpc = {r.key: r.value for r in rows_for(sc.comps["GPC0"], p, sku, demo_snap)}
    assert gpc["Cores in this GPC"].startswith("1536 CUDA")
