"""Generic hwmon walker: every ``*_input`` under /sys/class/hwmon, unit-normalised."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import Reading, SysFS

# sysfs prefix → (kind, divisor, unit)
UNITS = {
    "temp": ("temp", 1000.0, "°C"),
    "fan": ("fan", 1.0, "rpm"),
    "in": ("in", 1000.0, "V"),
    "power": ("power", 1e6, "W"),
    "curr": ("curr", 1000.0, "A"),
    "freq": ("freq", 1e6, "MHz"),
    "energy": ("energy", 1e6, "J"),
    "humidity": ("humidity", 1000.0, "%"),
}
_KEY = re.compile(r"^(temp|fan|in|power|curr|freq|energy|humidity)(\d+)_(input|average)$")


@dataclass
class Chip:
    name: str
    hwmon: str
    device: str  # resolved device path ("" if none)
    readings: list[Reading] = field(default_factory=list)
    raw: dict[str, int] = field(default_factory=dict)  # raw counters (energy µJ) by label

    def get(self, label: str) -> Reading | None:
        return next((r for r in self.readings if r.label == label or r.key == label), None)

    def first(self, kind: str) -> Reading | None:
        return next((r for r in self.readings if r.kind == kind), None)


class HwmonReader:
    """``read_all`` that re-reads slow chips (NVMe SMART queries can take ~40 ms) only every
    ``every``-th poll, reusing their previous readings in between."""

    SLOW_S = 0.015

    def __init__(self, fs: SysFS, every: int = 5) -> None:
        self.fs, self.every = fs, every
        self.n = 0
        self.slow: dict[str, Chip] = {}

    def read_all(self) -> tuple[list[Reading], list[Chip]]:
        self.n += 1
        skip = dict(self.slow) if self.n % self.every else {}
        return read_all(self.fs, skip=skip, timings=self._note)

    def _note(self, chip: Chip, dt: float) -> None:
        if dt >= self.SLOW_S and not chip.raw:  # never hold back energy counters: watts come from deltas
            self.slow[chip.hwmon] = chip
        else:
            self.slow.pop(chip.hwmon, None)


def read_all(fs: SysFS, skip: dict[str, Chip] | None = None, timings=None) -> tuple[list[Reading], list[Chip]]:
    import time

    chips: list[Chip] = []
    for d in fs.glob("sys/class/hwmon/hwmon*"):
        if skip and d.name in skip:
            chips.append(skip[d.name])
            continue
        t0 = time.perf_counter()
        name = fs.read(d / "name") or d.name
        dev = fs.real(d / "device") if (d / "device").exists() else ""
        chip = Chip(name, d.name, dev)
        seen: set[str] = set()
        try:
            entries = sorted((p.name for p in d.iterdir()), key=lambda s: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)])
        except OSError:
            continue
        for fname in entries:
            m = _KEY.match(fname)
            if not m:
                continue
            prefix, n, _ = m.groups()
            key = f"{prefix}{n}"
            if key in seen:
                continue
            seen.add(key)
            raw = fs.read_int(d / fname)
            if raw is None:
                continue
            kind, div, unit = UNITS[prefix]
            label = fs.read(d / f"{key}_label") or key
            if kind == "energy":
                chip.raw[label] = raw
            chip.readings.append(Reading(name, key, label, kind, raw / div, unit, d.name, dev))
        # fan duty from pwmN (0..255) where present
        for p in entries:
            m = re.match(r"^pwm(\d+)$", p)
            if m:
                raw = fs.read_int(d / p)
                if raw is not None:
                    chip.readings.append(Reading(name, p, f"PWM {m.group(1)}", "pwm", raw / 255 * 100, "%", d.name, dev))
        if timings is not None:
            timings(chip, time.perf_counter() - t0)
        chips.append(chip)
    readings = [r for c in chips for r in c.readings]
    return readings, chips


def chips_named(chips: list[Chip], *names: str) -> list[Chip]:
    return [c for c in chips if c.name in names]


def chip_under(chips: list[Chip], device_prefix: str, name: str | None = None) -> Chip | None:
    """The hwmon chip whose device lives at or below ``device_prefix`` in the device tree."""
    if not device_prefix:
        return None
    for c in chips:
        if c.device and (c.device == device_prefix or c.device.startswith(device_prefix + "/")):
            if name is None or c.name == name:
                return c
    return None
