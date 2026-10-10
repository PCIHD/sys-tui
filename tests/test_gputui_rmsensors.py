from gputui.boards import load_profiles
from gputui.chips import SKU_BY_KEY
from gputui.components import rows_for
from gputui.rmsensors import collapse_modules, gddr_c, therm_c
from gputui.telemetry import DemoSource, GpuSnapshot


def test_therm_decode_keeps_tagged_readings_only():
    assert therm_c(0x40002E00) == 46.0  # 0x2E00 / 256 = 46
    assert therm_c(0x00002E00) is None
    assert therm_c(0x40000000) is None
    assert therm_c(0x4000FF00) is None


def test_gddr_decode_uses_the_mode_register_code():
    # code 45 -> (45 - 20) * 2 = 50 °C, valid bit for slot 0 is bit 24
    assert gddr_c(0x002D0000, 1 << 24, 0) == 50.0
    assert gddr_c(0x002D0000, 0, 0) is None
    assert gddr_c(0xBADF0000, 1 << 24, 0) is None


def test_collapse_drops_a_duplicate_ic_readback():
    # partition 0: both ICs of subpartition 0 agree at 50, subpartition 1 is 54
    slots = [(0, 0, 50.0), (0, 1, 54.0), (0, 2, 50.0), (0, 3, 54.0)]
    assert collapse_modules(slots) == [50.0, 54.0]


def test_collapse_keeps_both_ics_when_they_disagree():
    slots = [(0, 0, 50.0), (0, 2, 56.0)]
    assert collapse_modules(slots) == [50.0, 56.0]


def test_memory_chip_shows_its_own_channel_temperature(demo_snap):
    p = load_profiles()["zotac-5080"]
    rows = {r.key: r.value for r in rows_for(p.by_id("M0"), p, SKU_BY_KEY["5080"], demo_snap)}
    assert rows["Temperature"].endswith("°C")
    assert "NVML" not in rows["Temperature"]
    assert "Hottest module" in rows


def test_nvvdd_phase_shows_the_rail_voltage(demo_snap):
    p = load_profiles()["zotac-5080"]
    choke = next(c for c in p.components if c.kind == "choke" and c.rail == "NVVDD")
    rows = {r.key: r.value for r in rows_for(choke, p, SKU_BY_KEY["5080"], demo_snap)}
    assert rows["Rail voltage"].endswith(" V")
    assert "unavailable" in rows["Per-phase current"]


def test_gpu_panel_shows_margin_and_names_the_root_gap():
    s = GpuSnapshot(temp_gpu=46, temp_margin=42, sensor_note="hot spot and per-module readings need root")
    p = load_profiles()["zotac-5080"]
    rows = {r.key: r.value for r in rows_for(p.by_id("GPU"), p, SKU_BY_KEY["5080"], s)}
    assert rows["Thermal margin"] == "42 °C under the limit"
    assert "need root" in rows["Hot spot"]


def test_module_temp_follows_the_bus_width():
    s = DemoSource("5080").collect(0)
    assert len(s.mem_modules) == 8
    assert s.module_temp(3) == s.mem_modules[3]
    s.mem_modules = s.mem_modules[:4]
    assert s.module_temp(3) is None
