import pytest

from systui import bindings as B
from systui.boards import load_profiles
from systui.components import mem_bandwidth, rows_for
from systui.telemetry.demo import DemoSource

P = load_profiles()["msi-b650m-gaming-plus-wifi"]
S = DemoSource().collect(5.0)


def test_binds_resolve_against_demo_machine():
    assert B.nvme(S, P.by_id("M2_1")).name == "nvme0n1"
    assert B.nvme(S, P.by_id("M2_2")).name == "nvme1n1"
    assert B.dimm(S, P.by_id("DIMMB2")).addr == 0x53
    assert B.fan(S, P.by_id("CPU_FAN1")).label == "CPU Fan"
    assert B.temp(S, P.by_id("PCH")).label == "PCH"
    assert B.net(S, P.by_id("WIFI")).name == "wlp8s0"
    assert B.gpu(S).name.endswith("5080")


def test_chipset_uplink_and_traffic():
    up = B.chipset_uplink(S)
    assert up == "0000:00:02.1"
    assert B.chipset_traffic(S, up) > 0


def test_every_part_has_detail_rows():
    for c in P.components:
        rows = rows_for(c, P, S)
        assert rows, c.id
        assert all(r.kind in ("live", "derived", "spec") for r in rows)
    assert rows_for(P.by_id("CPU"), P, None)  # spec rows before any data


def test_bandwidth_rows():
    gpu = {r.key: r.value for r in rows_for(P.by_id("PCI_E2"), P, S)}
    assert gpu["PCIe traffic"].startswith("▲ card → CPU ")
    dimm = {r.key: r.value for r in rows_for(P.by_id("DIMMA1"), P, S)}
    assert dimm["System memory bandwidth"].startswith("▲ read ")
    m = DemoSource().collect(5.0).mem
    m.read_bps = m.write_bps = None
    m.bw_note = "needs root (./systui-root) or perf_event_paranoid ≤ 0"
    assert mem_bandwidth(m) == "N/A (needs root (./systui-root) or perf_event_paranoid ≤ 0)"


def test_process_maps_onto_cpu_gpu_and_nvme():
    py = next(p for p in S.procs if p.name == "python3")
    ids = B.clients(S, P, py)
    assert "CPU" in ids and "GPU" in ids and "M2_1" in ids
    dimm = {r.key: r.value for r in rows_for(P.by_id("DIMMA1"), P, S)}
    assert dimm["Module"].startswith("CP16G56C46U5")
    assert "EXPO" in dimm["Module"]
    assert dimm["Rated"] == "5600 CL46"


def test_link_loads():
    assert B.link_bps("PCIe 4.0 x4") == pytest.approx(7.877e9, rel=1e-3)
    assert B.link_bps("SATA") == 600e6 and B.link_bps("") is None
    assert B.load(1e9, 3e9, 4e9) == 0.75  # full duplex: the busier direction
    assert B.load(1e9, 3e9, 4e9, duplex=False) == 1.0
    assert B.load(None, None, 4e9) is None and B.load(1.0, 1.0, None) is None
    g = B.gpu(S)
    assert 0 <= B.gpu_pcie_load(g) <= 1
    assert B.mem_load(S) == pytest.approx((S.mem.read_bps + S.mem.write_bps) / S.mem.peak_bps)
    up, down = B.chipset_split(S, B.chipset_uplink(S))
    assert up + down == pytest.approx(B.chipset_traffic(S, B.chipset_uplink(S)))
