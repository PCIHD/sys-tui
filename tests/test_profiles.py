import itertools

import pytest

from systui.boards import identify, load_profiles
from systui.boards.generic import generic_profile
from systui.telemetry.demo import DemoSource

PROFILES = list(load_profiles().values())
SNAP = DemoSource().collect(1.0)
ALL = PROFILES + [generic_profile(SNAP)]


@pytest.mark.parametrize("p", ALL, ids=lambda p: p.id)
def test_parts_sit_on_the_board(p):
    for c in p.components:
        x0, y0, x1, y1 = c.rect
        assert x1 > x0 and y1 > y0, c.id
        assert -0.5 <= x0 and x1 <= 100.5 and -0.5 <= y0 and y1 <= 100.5, c.id


@pytest.mark.parametrize("p", ALL, ids=lambda p: p.id)
def test_no_parts_overlap(p):
    for a, b in itertools.combinations(p.components, 2):
        ix = min(a.rect[2], b.rect[2]) - max(a.rect[0], b.rect[0])
        iy = min(a.rect[3], b.rect[3]) - max(a.rect[1], b.rect[1])
        assert not (ix > 0.05 and iy > 0.05), f"{a.id} overlaps {b.id}"


@pytest.mark.parametrize("p", ALL, ids=lambda p: p.id)
def test_ids_unique_and_required_parts(p):
    ids = [c.id for c in p.components]
    assert len(ids) == len(set(ids))
    assert p.of_kind("socket") and p.of_kind("dimm") and p.of_kind("pcie")


def test_msi_identified_by_dmi():
    assert identify(SNAP).profile.id == "msi-b650m-gaming-plus-wifi"
    assert identify(SNAP).level == "exact"


def test_unknown_board_gets_generic_layout():
    snap = DemoSource(board="PRIME X670-P").collect(1.0)
    m = identify(snap)
    assert m.level == "generic" and m.profile.generic
    assert len(m.profile.of_kind("dimm")) == 4
    assert m.profile.of_kind("m2")[0].bind == "nvme:0000:00:01.2"


def test_group_expansion():
    p = load_profiles()["msi-b650m-gaming-plus-wifi"]
    dimms = p.of_kind("dimm")
    assert [d.id for d in dimms] == ["DIMMA1", "DIMMA2", "DIMMB1", "DIMMB2"]
    assert [d.bind for d in dimms] == ["dimm:0x50", "dimm:0x51", "dimm:0x52", "dimm:0x53"]
    assert all(dimms[i].rect[2] <= dimms[i + 1].rect[0] for i in range(3))
    rails = [c.rail for c in p.of_kind("choke")]
    assert rails.count("Vcore") == 8 and rails.count("VSOC") == 2 and rails.count("VMISC") == 1
