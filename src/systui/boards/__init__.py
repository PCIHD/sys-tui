"""Motherboard profiles: where each physical part sits on a specific board.

Profiles are TOML files next to this module. Coordinates are percentages of the PCB seen
from the component side with the rear I/O on the LEFT and the 24-pin ATX connector on the
RIGHT (the orientation of every vendor manual's layout figure). Rects may extend past
0..100 for things that overhang the board (a graphics card).

A component entry may describe a row/column of identical parts with ``n`` + ``axis``: the
``rect`` is then the bounding box of the whole group and is split evenly.

``bind`` ties a part to live data:
  ``dimm:0x50``            SPD hub at that i2c address (DDR5 spd5118 / DDR4 jc42)
  ``nvme:0000:00:01.2``    the NVMe drive behind that PCIe root port
  ``sata:2``               the disk on that SATA port of the chipset controller
  ``fan:CPU Fan``          an nct/it87 fan reading by label
  ``temp:VRM MOS``         a Super I/O temperature by label
  ``net:ethernet`` / ``net:wifi``
  ``gpu:x16``              the discrete GPU in the primary x16 slot
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

Rect = tuple[float, float, float, float]

KIND_NAMES = {
    "socket": "CPU socket",
    "choke": "VRM choke",
    "heatsink": "VRM heatsink",
    "power": "Power connector",
    "dimm": "DIMM slot",
    "pcie": "PCIe slot",
    "m2": "M.2 slot (Key M)",
    "wifi": "M.2 Key-E Wi-Fi module",
    "chipset": "Chipset",
    "sata": "SATA port",
    "fan": "Fan header",
    "header": "Header",
    "chip": "IC",
    "io": "Rear I/O",
    "battery": "CMOS battery",
    "gpu": "Graphics card",
    "core": "CPU core",
    "jumper": "Jumper",
}


@dataclass
class Component:
    id: str
    kind: str
    rect: Rect
    label: str | None = None
    part: str | None = None
    bind: str | None = None
    note: str | None = None
    rail: str | None = None  # VRM: Vcore | VSOC | VMISC
    lanes: int | None = None  # PCIe slots / M.2
    source: str | None = None  # "cpu" | "chipset" lane source
    pins: int | None = None  # power connectors / headers
    shield: bool = False  # M.2 heatsink
    ports: list[str] = field(default_factory=list)  # rear I/O block contents
    spec: dict[str, str] = field(default_factory=dict)
    conf: str = "measured"  # "measured" from the manual figure / a photo, or "inferred"
    group: str | None = None

    @property
    def kind_name(self) -> str:
        if self.kind == "pcie" and self.lanes:
            return f"PCIe x{self.lanes} slot"
        return KIND_NAMES.get(self.kind, self.kind)

    @property
    def cx(self) -> float:
        return (self.rect[0] + self.rect[2]) / 2

    @property
    def cy(self) -> float:
        return (self.rect[1] + self.rect[3]) / 2

    @property
    def w(self) -> float:
        return self.rect[2] - self.rect[0]

    @property
    def h(self) -> float:
        return self.rect[3] - self.rect[1]

    def bind_kind(self) -> tuple[str, str] | None:
        if not self.bind or ":" not in self.bind:
            return None
        k, v = self.bind.split(":", 1)
        return k, v


@dataclass
class BoardProfile:
    id: str
    name: str
    vendor: str
    short: str
    match: list[str]  # substrings of DMI board_name
    form: str
    pcb_mm: tuple[float, float]
    socket: str = ""
    chipset: str = ""
    superio: str = ""
    vrm: str = ""
    sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    holes: list[tuple[float, float]] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    generic: bool = False

    def by_id(self, cid: str) -> Component | None:
        return next((c for c in self.components if c.id == cid), None)

    def of_kind(self, *kinds: str) -> list[Component]:
        return [c for c in self.components if c.kind in kinds]

    def bound(self, bind: str) -> Component | None:
        return next((c for c in self.components if c.bind == bind), None)


def _expand(entry: dict[str, Any]) -> list[Component]:
    entry = dict(entry)
    n = int(entry.pop("n", 1))
    axis = entry.pop("axis", "x")
    gap = float(entry.pop("gap", 0.0))
    ids = entry.pop("ids", None)
    labels = entry.pop("labels", None)
    binds = entry.pop("binds", None)
    rails = entry.pop("rails", None)
    rect = tuple(float(v) for v in entry.pop("rect"))
    if n == 1:
        return [Component(rect=rect, **entry)]
    x0, y0, x1, y1 = rect
    out = []
    span = (x1 - x0) if axis == "x" else (y1 - y0)
    size = (span - gap * (n - 1)) / n
    base_id = entry.pop("id")
    for i in range(n):
        a = (x0 if axis == "x" else y0) + i * (size + gap)
        r = (a, y0, a + size, y1) if axis == "x" else (x0, a, x1, a + size)
        e = dict(entry)
        e["id"] = ids[i] if ids else f"{base_id}{i + 1}"
        if labels:
            e["label"] = labels[i]
        if binds:
            e["bind"] = binds[i]
        if rails:
            e["rail"] = rails[i]
        out.append(Component(rect=r, group=base_id, **e))
    return out


def load_profile(data: dict[str, Any], pid: str) -> BoardProfile:
    comps = []
    for e in data.get("component", []):
        comps.extend(_expand(e))
    b = data["board"]
    return BoardProfile(
        id=pid, name=b["name"], vendor=b.get("vendor", ""), short=b.get("short", b["name"]),
        match=b.get("match", []), form=b.get("form", "ATX"), pcb_mm=tuple(b.get("pcb_mm", (305, 244))),
        socket=b.get("socket", ""), chipset=b.get("chipset", ""), superio=b.get("superio", ""), vrm=b.get("vrm", ""),
        sources=b.get("sources", []), notes=b.get("notes", []),
        holes=[tuple(h) for h in b.get("holes", [])], components=comps)


_CACHE: dict[str, BoardProfile] | None = None


def load_profiles() -> dict[str, BoardProfile]:
    global _CACHE
    if _CACHE is None:
        out = {}
        for f in sorted(resources.files(__package__).iterdir(), key=lambda p: p.name):
            if f.name.endswith(".toml"):
                pid = f.name[:-5].replace("_", "-")
                out[pid] = load_profile(tomllib.loads(f.read_text()), pid)
        _CACHE = out
    return _CACHE


@dataclass
class Match:
    profile: BoardProfile
    level: str  # "exact" | "generic" | "forced"

    @property
    def level_text(self) -> str:
        return {"exact": "exact board match (DMI)", "generic": "generic layout (board not profiled)",
                "forced": "manually selected"}[self.level]


def identify(snap, force: str | None = None) -> Match:
    from .generic import generic_profile

    profiles = load_profiles()
    if force:
        return Match(profiles[force], "forced")
    name = (snap.board.name or "").upper()
    for p in profiles.values():
        if any(m.upper() in name for m in p.match):
            return Match(p, "exact")
    return Match(generic_profile(snap), "generic")
