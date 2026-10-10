"""Collectors against a fake sysfs tree."""

import errno
import os
import struct

import pytest

from systui.telemetry import Rates, SysFS
from systui.telemetry import cpu, memory, network, perf, sensors, storage

PCI = "sys/devices/pci0000:00"


def _w(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(text) + "\n")


def _hwmon(root, n, name, device_rel, files):
    dev = root / device_rel
    dev.mkdir(parents=True, exist_ok=True)
    hw = root / f"sys/class/hwmon/hwmon{n}"
    hw.mkdir(parents=True, exist_ok=True)
    _w(root, f"sys/class/hwmon/hwmon{n}/name", name)
    os.symlink(dev, hw / "device")
    for k, v in files.items():
        _w(root, f"sys/class/hwmon/hwmon{n}/{k}", v)


@pytest.fixture
def fs(tmp_path):
    r = tmp_path
    _hwmon(r, 0, "nct6687", "sys/devices/platform/nct6687.2592", {
        "fan1_input": 1200, "fan1_label": "CPU Fan", "fan3_input": 0, "fan3_label": "System Fan #1",
        "temp3_input": 45500, "temp3_label": "VRM MOS", "in4_input": 1254, "in4_label": "CPU Vcore", "pwm1": 128})
    _hwmon(r, 1, "spd5118", f"{PCI}/0000:00:14.0/i2c-13/13-0051", {"temp1_input": 46250})
    _hwmon(r, 2, "spd5118", f"{PCI}/0000:00:14.0/i2c-13/13-0050", {"temp1_input": 44000})
    _hwmon(r, 3, "zenergy", "sys/devices/platform/zenergy.0", {
        "energy1_input": 1_000_000, "energy1_label": "Ecore000", "energy2_input": 2_000_000,
        "energy2_label": "Ecore001", "energy3_input": 50_000_000, "energy3_label": "Esocket0"})
    _hwmon(r, 4, "k10temp", f"{PCI}/0000:00:18.3", {"temp1_input": 60000, "temp1_label": "Tctl",
                                                     "temp3_input": 52125, "temp3_label": "Tccd1"})
    _hwmon(r, 5, "nvme", f"{PCI}/0000:00:01.2/0000:02:00.0/nvme/nvme0", {"temp1_input": 45850, "temp1_label": "Composite"})
    _hwmon(r, 6, "r8169_0_600:00", f"{PCI}/0000:00:02.1/0000:03:00.0/0000:04:01.0/0000:06:00.0/mdio_bus/r8169-0-600",
           {"temp1_input": 44500})
    for n in range(1, 7):
        d = r / f"{PCI}/0000:00:02.1/0000:12:00.0/ata{n}/ata_port/ata{n}"
        d.mkdir(parents=True)
        (r / "sys/class/ata_port").mkdir(parents=True, exist_ok=True)
        os.symlink(d, r / f"sys/class/ata_port/ata{n}")
    for t, core in enumerate([0, 1, 0, 1]):
        _w(r, f"sys/devices/system/cpu/cpu{t}/topology/core_id", core)
        _w(r, f"sys/devices/system/cpu/cpu{t}/cpufreq/scaling_cur_freq", 4_500_000 + t)
        _w(r, f"sys/devices/system/cpu/cpu{t}/cpufreq/amd_pstate_prefcore_ranking", 200 - t)
    _w(r, "sys/devices/system/cpu/cpu0/cpufreq/scaling_governor", "powersave")
    _w(r, "sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference", "balance_performance")
    _w(r, "sys/devices/system/cpu/cpufreq/boost", "1")
    _w(r, "proc/pressure/cpu", "some avg10=0.00 avg60=0.16 avg300=2.22 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    _w(r, "proc/pressure/memory", "some avg10=0.00 avg60=0.51 avg300=1.29 total=1\nfull avg10=0.00 avg60=0.51 avg300=1.28 total=1\n")
    _w(r, "proc/pressure/io", "some avg10=0.87 avg60=1.36 avg300=2.01 total=1\nfull avg10=0.70 avg60=1.04 avg300=1.47 total=1\n")
    for i, name in enumerate(["POLL", "C1", "C2", "C3"]):
        _w(r, f"sys/devices/system/cpu/cpu0/cpuidle/state{i}/name", name)
        _w(r, f"sys/devices/system/cpu/cpu0/cpuidle/state{i}/time", 10 ** (i + 3))
    _w(r, "proc/net/wireless", "Inter-| sta-|   Quality\n face | tus | link level noise\nwlp8s0: 0000   55.  -61.  -256 0 0 0 0 0 0\n")
    return SysFS(r)


def test_hwmon_walk_and_units(fs):
    readings, chips = sensors.read_all(fs)
    by = {(r.chip, r.label): r for r in readings}
    assert by[("nct6687", "CPU Fan")].value == 1200 and by[("nct6687", "CPU Fan")].unit == "rpm"
    assert by[("nct6687", "VRM MOS")].value == pytest.approx(45.5)
    assert by[("nct6687", "CPU Vcore")].value == pytest.approx(1.254)
    assert by[("nct6687", "PWM 1")].value == pytest.approx(128 / 255 * 100)
    assert by[("k10temp", "Tccd1")].value == pytest.approx(52.125)
    nvme = next(c for c in chips if c.name == "nvme")
    assert nvme.device.endswith("/0000:02:00.0/nvme/nvme0")


def test_dimms_by_spd_address(fs):
    _, chips = sensors.read_all(fs)
    ds = memory.dimms(chips)
    assert [d.addr for d in ds] == [0x50, 0x51]
    assert ds[1].temp == pytest.approx(46.25)


def test_spd_eeprom_decode():
    from pathlib import Path

    raw = (Path(__file__).parent / "fixtures" / "spd_cp16g56c46u5.bin").read_bytes()
    info = memory.decode_spd(raw)
    assert info["part"].startswith("CP16G56C46U5")
    assert info["serial"].startswith("DEMO0000001")
    assert info["speed"] == "5600 CL46"
    assert info["expo"] is True


def test_spd_eeprom_from_sysfs(fs):
    from pathlib import Path
    fixture = Path(__file__).parent / "fixtures" / "spd_cp16g56c46u5.bin"
    eep = fs.root / "sys/devices/pci0000:00/0000:00:14.0/i2c-13/13-0050/eeprom"
    eep.parent.mkdir(parents=True, exist_ok=True)
    eep.write_bytes(fixture.read_bytes())
    _, chips = sensors.read_all(fs)
    ds = memory.dimms(chips, fs)
    d50 = next(d for d in ds if d.addr == 0x50)
    assert d50.part.startswith("CP16G56C46U5")
    assert d50.expo is True


def test_energy_counters_become_watts(fs):
    rates = Rates()
    _, chips = sensors.read_all(fs)
    pkg, per, src = cpu.energy(fs, rates, chips, 2, now=10.0)
    assert src == "zenergy" and pkg is None
    _w(fs.root, "sys/class/hwmon/hwmon3/energy1_input", 3_000_000)  # +2 J over 0.5 s
    _w(fs.root, "sys/class/hwmon/hwmon3/energy3_input", 80_000_000)  # +30 J
    _, chips = sensors.read_all(fs)
    pkg, per, _ = cpu.energy(fs, rates, chips, 2, now=10.5)
    assert pkg == pytest.approx(60.0)
    assert per[0] == pytest.approx(4.0) and per[1] == pytest.approx(0.0)


def test_counter_wrap_gives_no_rate():
    r = Rates()
    assert r.rate("x", 100, 1.0) is None
    assert r.rate("x", 50, 2.0) is None
    assert r.rate("x", 150, 3.0) == pytest.approx(100)


def test_topology_and_clocks(fs):
    assert cpu.topology(fs, 4) == [0, 1, 0, 1]
    assert cpu.thread_mhz(fs, 3) == pytest.approx(4500.003)
    assert cpu.prefcore_ranking(fs, 4)[0] == 200
    psi = cpu.read_psi(fs)
    assert psi.memory.full300 == pytest.approx(1.28)
    share = dict(cpu.cstate_share(fs))
    assert share["C3"] > share["POLL"]


def test_pci_chain_and_sata_port(fs):
    dev = f"/{PCI}/0000:00:02.1/0000:12:00.0/ata2/host1/target1:0:0/1:0:0:0"
    assert storage.pci_chain(dev) == ["0000:00:02.1", "0000:12:00.0"]
    assert storage.sata_port(fs, dev) == 2


def test_nic_temp_lookup(fs):
    _, chips = sensors.read_all(fs)
    c = sensors.chip_under(chips, f"/{PCI}/0000:00:02.1/0000:03:00.0/0000:04:01.0/0000:06:00.0")
    assert c is not None and c.first("temp").value == pytest.approx(44.5)
    assert sensors.chip_under(chips, f"/{PCI}/0000:00:02.1/0000:03:00.0/0000:04:01.0/0000:06:00") is None


def test_wireless_signal(fs):
    assert network.wireless_signal(fs) == {"wlp8s0": -61.0}


def test_memclk_comes_from_the_igpu_not_the_nvidia_card(tmp_path):
    nv = tmp_path / "sys/class/drm/card0/device"
    amd = tmp_path / "sys/class/drm/card1/device"
    nv.mkdir(parents=True)
    amd.mkdir(parents=True)
    drivers = tmp_path / "sys/bus/pci/drivers"
    (drivers / "nvidia").mkdir(parents=True)
    (drivers / "amdgpu").mkdir(parents=True)
    (nv / "driver").symlink_to(drivers / "nvidia")
    (amd / "driver").symlink_to(drivers / "amdgpu")
    (nv / "pp_dpm_mclk").write_text("0: 15001Mhz *\n")
    (amd / "pp_dpm_mclk").write_text("0: 400Mhz\n1: 2800Mhz *\n")
    _w(tmp_path, "sys/bus/event_source/devices/amd_umc_0/type", 8)
    _w(tmp_path, "sys/bus/event_source/devices/amd_umc_1/type", 9)
    from systui.telemetry import MemStat

    m = MemStat()
    memory.fill_clock(SysFS(tmp_path), m)
    assert m.memclk_mhz == 2800
    assert m.peak_bps == pytest.approx(2800e6 * 2 * 8 * 2)


def test_umc_bandwidth_without_pmu(tmp_path):
    umc = perf.UmcBandwidth(SysFS(tmp_path))
    assert not umc.available and "amd_umc" in umc.note
    assert umc.sample(Rates(), 1.0) == (None, None, None, [], [])


def test_umc_bandwidth_permission_denied(tmp_path, monkeypatch):
    for i in range(2):
        _w(tmp_path, f"sys/bus/event_source/devices/amd_umc_{i}/type", 14)
        _w(tmp_path, f"sys/bus/event_source/devices/amd_umc_{i}/cpumask", 0)
    monkeypatch.setattr(perf, "NR_PERF_EVENT_OPEN", 298)

    def deny(*a):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(perf, "perf_event_open", deny)
    umc = perf.UmcBandwidth(SysFS(tmp_path))
    assert not umc.available and "perf_event_paranoid" in umc.note


def test_umc_bandwidth_counts_cas_lines(tmp_path, monkeypatch):
    _w(tmp_path, "sys/bus/event_source/devices/amd_umc_0/type", 14)
    _w(tmp_path, "sys/bus/event_source/devices/amd_umc_0/cpumask", 0)
    monkeypatch.setattr(perf, "NR_PERF_EVENT_OPEN", 298)
    opened, counts = [], {}

    def fake_open(type_, config, cpu):
        opened.append((type_, config, cpu))
        fd = os.open(os.devnull, os.O_RDONLY)
        series = {perf.UMC_CAS_CMD: {perf.RD: [100, 1100], perf.WR: [50, 250]},
                  perf.UMC_MEM_CLK: {0: [0, 6_000_000_000]}, perf.UMC_DATA_SLOT_CLKS: {0: [0, 3_000_000_000]}}
        counts[fd] = iter(series[config & 0xFF][config >> 8])
        return fd

    def fake_read(fd, n):  # value, time enabled, time running
        return struct.pack("QQQ", next(counts[fd]), 1, 1) if fd in counts else real_read(fd, n)

    real_read = os.read
    monkeypatch.setattr(perf, "perf_event_open", fake_open)
    monkeypatch.setattr(perf.os, "read", fake_read)
    umc = perf.UmcBandwidth(SysFS(tmp_path))
    assert opened == [(14, 0x10A, 0), (14, 0x20A, 0), (14, 0x00, 0), (14, 0x14, 0)]
    rates = Rates()
    assert umc.sample(rates, 0.0) == (None, None, None, [(None, None)], [None])
    rd, wr, peak, ch, busy = umc.sample(rates, 2.0)
    assert busy == [pytest.approx(0.25)]  # 3e9 data clocks / 2 over 6e9 memory clocks
    assert (rd, wr) == (pytest.approx(1000 * 64 / 2), pytest.approx(200 * 64 / 2))
    assert ch == [(pytest.approx(1000 * 64 / 2), pytest.approx(200 * 64 / 2))]
    assert peak == pytest.approx(3e9 * 2 * 8)  # 3 GHz MEMCLK → DDR5-6000 → 48 GB/s per channel
    umc.close()


def test_umc_counter_without_a_slot_is_not_a_reading(monkeypatch):
    monkeypatch.setattr(perf.os, "read", lambda fd, n: struct.pack("QQQ", 0, 5, 0))
    with pytest.raises(OSError):
        perf._count(99)


def test_process_io_is_split_into_read_and_write(monkeypatch):
    from types import SimpleNamespace

    from systui.telemetry import processes

    io = {"r": 0, "w": 0}

    def fake_iter(attrs, ad_value=None):
        counters = SimpleNamespace(read_bytes=io["r"], write_bytes=io["w"])
        info = {"pid": 42, "name": "dd", "username": "u", "cpu_percent": 0.0, "memory_info": None,
                "memory_percent": 0.0, "num_threads": 1, "status": "running", "io_counters": counters,
                "cmdline": ["dd"], "cpu_num": 0}
        return [SimpleNamespace(info=info, open_files=lambda: [])]

    monkeypatch.setattr(processes.psutil, "process_iter", fake_iter)
    tr = processes.ProcTracker()
    assert tr.collect(0.0)[0].io_read_bps is None
    io["r"], io["w"] = 3_000_000, 1_000_000
    p = tr.collect(1.0)[0]
    assert (p.io_read_bps, p.io_write_bps, p.io_bps) == (3e6, 1e6, 4e6)
    monkeypatch.setattr(processes.psutil, "process_iter", lambda *a, **k: [])
    tr.collect(2.0)
    assert not tr.rates.prev  # rates of exited processes are dropped


def test_umc_busy_counter_is_dropped_when_it_has_to_share(tmp_path, monkeypatch):
    _w(tmp_path, "sys/bus/event_source/devices/amd_umc_0/type", 14)
    _w(tmp_path, "sys/bus/event_source/devices/amd_umc_0/cpumask", 0)
    monkeypatch.setattr(perf, "NR_PERF_EVENT_OPEN", 298)
    slot = []

    def fake_open(type_, config, cpu):
        fd = os.open(os.devnull, os.O_RDONLY)
        if config == perf.UMC_DATA_SLOT_CLKS:
            slot.append(fd)
        return fd

    real_read = os.read
    # the busy counter ran for only half the time it was enabled: perf was time-slicing it
    monkeypatch.setattr(perf.os, "read", lambda fd, n: struct.pack("QQQ", 7, 10, 5 if fd in slot else 10)
                        if n == 24 else real_read(fd, n))
    monkeypatch.setattr(perf, "perf_event_open", fake_open)
    umc = perf.UmcBandwidth(SysFS(tmp_path))
    assert umc.slot_fds
    assert umc.sample(Rates(), 0.0)[4] == []
    assert umc.slot_fds == [] and umc.available
    umc.close()


def test_spd_eeprom_decodes_ddr5_organisation():
    from pathlib import Path

    info = memory.decode_spd((Path(__file__).parent / "fixtures" / "spd_cp16g56c46u5.bin").read_bytes())
    assert (info["maker"], info["dram_maker"]) == ("Micron", "Micron")
    assert (info["ranks"], info["width"], info["die_gbit"]) == (1, 8, 16)
    assert info["timings"] == "46-45-45-90"  # tAA 16 ns at tCK 357 ps → 44.8 → CL45 → even → CL46
    assert info["made"] == "2025 wk10"


def test_dimm_limits_and_unreadable_pmic(fs):
    _w(fs.root, "sys/class/hwmon/hwmon2/temp1_max", 55000)
    _w(fs.root, "sys/class/hwmon/hwmon2/temp1_crit", 85000)
    _, chips = sensors.read_all(fs)
    d50 = next(d for d in memory.dimms(chips, fs) if d.addr == 0x50)
    assert (d50.temp_max, d50.temp_crit) == (55.0, 85.0)
    assert (d50.vdd, d50.vddq, d50.vpp) == (None, None, None)  # no /dev/i2c-13 in the fake tree


def test_pmic_setpoints_decode_vid_codes(tmp_path, monkeypatch):
    from systui.telemetry import pmic

    regs = {0x21: 0x78, 0x25: 0x78, 0x27: 0x78}
    node = tmp_path / "dev/i2c-13"
    node.parent.mkdir()
    node.write_bytes(b"")
    seen = []

    def fake_ioctl(fd, req, arg):
        if req == pmic.I2C_SLAVE:
            seen.append(arg)
        else:
            arg.data.contents.byte = regs[arg.command]

    monkeypatch.setattr(pmic.fcntl, "ioctl", fake_ioctl)
    monkeypatch.setattr(pmic, "_cache", {})
    dev = "/sys/devices/pci0000:00/0000:00:14.0/i2c-13/13-0052"
    assert pmic.setpoints(SysFS(tmp_path), dev, 0x52) == (1.1, 1.1, 1.8)
    assert seen == [0x4A]  # PMIC sits at 0x48 | (SPD address & 7)


class _FakePmic:
    """Register file of one PMIC5100 behind a fake /dev/i2c-13; logs every write."""

    def __init__(self, monkeypatch, tmp_path, r30=0x00, sticky=False):
        from systui.telemetry import pmic

        self.regs = {0x0C: 12, 0x0E: 0xC4, 0x0F: 1, 0x21: 0x78, 0x25: 0x78, 0x27: 0x78, 0x30: r30}
        self.writes: list[tuple[int, int, int]] = []
        self.sticky = sticky  # R30 ignores writes, as a locked or odd part might
        self.addr = None
        node = tmp_path / "dev/i2c-13"
        node.parent.mkdir(exist_ok=True)
        node.write_bytes(b"")
        monkeypatch.setattr(pmic.fcntl, "ioctl", self.ioctl)
        monkeypatch.setattr(pmic, "_cache", {})

    def ioctl(self, fd, req, arg):
        from systui.telemetry import pmic

        if req == pmic.I2C_SLAVE:
            self.addr = arg
        elif arg.read_write == pmic.I2C_SMBUS_WRITE:
            self.writes.append((self.addr, arg.command, arg.data.contents.byte))
            if not self.sticky:
                self.regs[arg.command] = arg.data.contents.byte
        else:
            arg.data.contents.byte = self.regs.get(arg.command, 0)


def _stick(**kw):
    from systui.telemetry import Dimm

    return Dimm(0x50, 45.0, **({"bus": 13, "vdd": 1.1, "vddq": 1.1, "vpp": 1.8} | kw))


def test_pmic_meter_switches_the_adc_on_reads_watts_and_restores(tmp_path, monkeypatch):
    from systui.telemetry import pmic

    chip = _FakePmic(monkeypatch, tmp_path)
    m = pmic.Meter(SysFS(tmp_path))
    d = _stick()
    m.fill([d])
    m.fill([d])  # the ADC is switched on once, not every poll
    assert chip.writes == [(0x48, 0x30, 0x83)]
    # 12 × 125 mA at 1.1 V; R0E's reserved top bits are masked off (0xC4 → 4 steps); 125 mA at 1.8 V
    assert d.rail_w == pytest.approx((1.65, 0.55, 0.225))
    assert d.power_w == pytest.approx(2.425)
    m.close()
    assert chip.writes[-1] == (0x48, 0x30, 0x00) and chip.regs[0x30] == 0x00
    assert {reg for _, reg, _ in chip.writes} == {0x30}  # never anything but the ADC control register


def test_pmic_meter_leaves_an_adc_someone_else_runs(tmp_path, monkeypatch):
    from systui.telemetry import pmic

    chip = _FakePmic(monkeypatch, tmp_path, r30=0x81)
    m = pmic.Meter(SysFS(tmp_path))
    d = _stick()
    m.fill([d])
    m.close()
    assert chip.writes == [] and d.rail_w is not None


def test_pmic_meter_never_writes_to_a_pmic_it_could_not_read(tmp_path, monkeypatch):
    from systui.telemetry import pmic

    chip = _FakePmic(monkeypatch, tmp_path)
    m = pmic.Meter(SysFS(tmp_path))
    m.fill([_stick(vdd=None), _stick(bus=None)])
    assert chip.writes == []


def test_pmic_meter_gives_up_when_the_adc_will_not_start(tmp_path, monkeypatch):
    from systui.telemetry import pmic

    chip = _FakePmic(monkeypatch, tmp_path, sticky=True)
    m = pmic.Meter(SysFS(tmp_path))
    d = _stick()
    m.fill([d])
    m.fill([d])
    assert d.rail_w is None and "did not switch on" in m.note
    assert len(chip.writes) == 1  # no retry loop hammering the part
    m.close()
    assert chip.writes[-1] == (0x48, 0x30, 0x00)  # still put back what it found
