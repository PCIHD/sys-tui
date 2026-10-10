import pytest

from gputui.boards import load_profiles
from gputui.boards.generic import generic_profile
from gputui.chips import SKU_BY_KEY, SKUS

PROFILES = list(load_profiles().values())
SOLID = ("gpu", "mem", "choke", "power", "pad", "output")


def _overlap(a, b):
    return a[0] < b[2] - 0.05 and b[0] < a[2] - 0.05 and a[1] < b[3] - 0.05 and b[1] < a[3] - 0.05


def _inside_outline(p, x, y):
    pts = p.outline_points()
    inside = False
    for (ax, ay), (bx, by) in zip(pts, pts[1:] + pts[:1]):
        if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
            inside = not inside
    return inside


def test_twelve_profiles():
    assert len(PROFILES) == 12


@pytest.mark.parametrize("p", PROFILES, ids=lambda p: p.id)
def test_profile_sane(p):
    for s in p.skus:
        assert s in SKU_BY_KEY
    sku = SKU_BY_KEY[p.skus[0]]
    gpus = p.of_kind("gpu")
    assert len(gpus) == 1
    mems = p.of_kind("mem")
    assert mems, "board has memory"
    assert round(len(mems) * p.mem_gb_each) in sku.mem_gb
    assert len({m.label for m in mems}) == len(mems), "memory labels unique"
    assert p.of_kind("power")


@pytest.mark.parametrize("p", PROFILES, ids=lambda p: p.id)
def test_parts_on_a_board(p):
    aux = [a.rect for a in p.aux_boards]
    for c in p.components:
        x0, y0, x1, y1 = c.rect
        assert x0 < x1 and y0 < y1, c.id
        on_main = all(_inside_outline(p, x, y) for x, y in ((x0 + 0.2, y0 + 0.2), (x1 - 0.2, y0 + 0.2),
                                                             (x0 + 0.2, y1 - 0.2), (x1 - 0.2, y1 - 0.2)))
        on_aux = any(a[0] <= x0 and x1 <= a[2] and a[1] <= y0 and y1 <= a[3] for a in aux)
        assert on_main or on_aux, f"{c.id} {c.rect} off-board"


@pytest.mark.parametrize("p", PROFILES, ids=lambda p: p.id)
def test_no_overlapping_major_parts(p):
    for side in ("front", "back"):
        parts = [c for c in p.components if c.side == side and c.kind in SOLID]
        for i, a in enumerate(parts):
            for b in parts[i + 1:]:
                assert not _overlap(a.rect, b.rect), f"{a.id} overlaps {b.id}"


@pytest.mark.parametrize("sku", SKUS, ids=lambda s: s.key)
def test_generic_layouts(sku):
    for gb in sku.mem_gb:
        p = generic_profile(sku, gb)
        mems = p.of_kind("mem")
        assert len(mems) * p.mem_gb_each == pytest.approx(gb)
        parts = [c for c in p.components if c.side == "front" and c.kind in SOLID]
        for i, a in enumerate(parts):
            assert 0 <= a.rect[0] and a.rect[2] <= 100 and 0 <= a.rect[1] and a.rect[3] <= 100, a.id
            for b in parts[i + 1:]:
                assert not _overlap(a.rect, b.rect), f"{sku.key}: {a.id} overlaps {b.id}"
