"""Board profiles: where each physical component sits on a specific RTX 50 PCB.

Profiles live next to this module as TOML files. Coordinates are percentages of the main
PCB, viewed from the front (component side) with the display bracket on the LEFT and the
PCIe fingers on the BOTTOM edge. Back-side parts use the same front-view coordinates (as if
seen through the board); the back view mirrors them.

A component entry may describe a row/column of identical parts with ``n`` + ``axis``: the
``rect`` is then the bounding box of the whole group and is split evenly.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

Rect = tuple[float, float, float, float]

KIND_NAMES = {
    "gpu": "GPU package",
    "mem": "Memory module",
    "pad": "Empty memory pad",
    "choke": "VRM choke",
    "stage": "Power stage (DrMOS)",
    "ctrl": "PWM controller",
    "power": "Power connector",
    "shunt": "Current-sense shunt",
    "fuse": "Fuse",
    "cap": "Capacitor bank",
    "mlcc": "MLCC array",
    "header": "Header",
    "mcu": "Microcontroller",
    "switch": "BIOS switch",
    "output": "Display output",
    "b2b": "Board-to-board connector",
    "fingers": "PCIe edge connector",
    "fan": "Fan",
    "cooler": "Cooler",
    "gpc": "Graphics processing cluster",
    "l2": "L2 cache",
    "mc": "Memory controller",
    "nvenc": "Video encoder (NVENC)",
    "nvdec": "Video decoder (NVDEC)",
    "ofa": "Optical-flow accelerator",
    "nvjpg": "JPEG decoder (NVJPG)",
    "host": "PCIe host interface",
    "gigathread": "GigaThread engine",
    "display": "Display engine",
    "pipes": "SM pipelines",
}


@dataclass
class Component:
    id: str
    kind: str
    rect: Rect
    side: str = "front"
    label: str | None = None
    part: str | None = None
    rating: str | None = None
    rail: str | None = None
    channel: int | None = None
    marking: str | None = None
    note: str | None = None
    conf: str = "measured"  # "measured" from a PCB photo, or "inferred"
    group: str | None = None  # id of the group entry this part was expanded from
    die: Rect | None = None  # gpu only: measured die position inside the package

    @property
    def kind_name(self) -> str:
        return KIND_NAMES.get(self.kind, self.kind)

    @property
    def cx(self) -> float:
        return (self.rect[0] + self.rect[2]) / 2

    @property
    def cy(self) -> float:
        return (self.rect[1] + self.rect[3]) / 2


@dataclass
class AuxBoard:
    """A separate PCB of a multi-board design (FE PCIe board / I/O board)."""

    name: str
    rect: Rect  # in main-PCB % coordinates, may lie outside 0..100
    role: str  # "pcie" | "io"


@dataclass
class Fingers:
    x0: float
    x1: float
    physical: int = 16
    electrical: int = 16
    board: str = "main"  # or the name of the aux board carrying them


@dataclass
class BoardProfile:
    id: str
    name: str
    short: str
    skus: list[str]
    kind: str  # "fe" | "reference" | "aib"
    pcb_mm: tuple[float, float]
    card_mm: tuple[float, float]
    fans: int
    fan_mm: float
    cooler: str = ""
    flow_through: bool = False
    vendor: int | None = None
    subsystems: list[int] = field(default_factory=list)
    board_code: str | None = None
    mem_part: str | None = None
    mem_gb_each: float = 2
    gpu_vrm: str | None = None
    mem_vrm: str | None = None
    outline: list[tuple[float, float]] | None = None
    fingers: Fingers | None = None
    aux_boards: list[AuxBoard] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    generic: bool = False

    def by_id(self, cid: str) -> Component | None:
        for c in self.components:
            if c.id == cid:
                return c
        return None

    def of_kind(self, *kinds: str, side: str | None = None) -> list[Component]:
        return [c for c in self.components if c.kind in kinds and (side is None or c.side == side)]

    @property
    def memory(self) -> list[Component]:
        return sorted(self.of_kind("mem"), key=lambda c: c.channel if c.channel is not None else 99)

    def outline_points(self) -> list[tuple[float, float]]:
        return self.outline or [(0, 0), (100, 0), (100, 100), (0, 100)]


def _split(rect: Rect, n: int, axis: str, gap: float) -> list[Rect]:
    x0, y0, x1, y1 = rect
    out = []
    if axis == "x":
        step = (x1 - x0) / n
        for i in range(n):
            a = x0 + i * step
            out.append((a + step * gap / 2, y0, a + step * (1 - gap / 2), y1))
    else:
        step = (y1 - y0) / n
        for i in range(n):
            a = y0 + i * step
            out.append((x0, a + step * gap / 2, x1, a + step * (1 - gap / 2)))
    return out


_PER_ITEM = ("labels", "rails", "markings", "parts", "ids", "channels")


def _expand(entry: dict[str, Any], defaults: dict[str, Any]) -> list[Component]:
    e = dict(entry)
    n = int(e.pop("n", 1))
    axis = e.pop("axis", "y")
    gap = float(e.pop("gap", 0.14))
    lists = {k: e.pop(k) for k in _PER_ITEM if k in e}
    base_id = e.pop("id")
    rect = tuple(float(v) for v in e.pop("rect"))
    if "die" in e:
        e["die"] = tuple(float(v) for v in e["die"])
    kind = e.pop("kind")
    rects = [rect] if n == 1 else _split(rect, n, axis, gap)
    out = []
    for i, r in enumerate(rects):
        kw = {**defaults, **e}
        cid = lists["ids"][i] if "ids" in lists else (base_id if n == 1 else f"{base_id}{i + 1}")
        for plural, single in (("labels", "label"), ("rails", "rail"), ("markings", "marking"),
                               ("parts", "part"), ("channels", "channel")):
            if plural in lists:
                kw[single] = lists[plural][i]
        out.append(Component(id=cid, kind=kind, rect=r, group=base_id if n > 1 else None, **kw))
    return out


def parse_profile(data: dict[str, Any]) -> BoardProfile:
    comps: list[Component] = []
    for entry in data.get("c", []):
        defaults: dict[str, Any] = {}
        if entry["kind"] == "mem" and data.get("mem_part"):
            defaults["part"] = data["mem_part"]
        comps.extend(_expand(entry, defaults))
    # memory channels: explicit, else in declaration order
    mems = [c for c in comps if c.kind == "mem"]
    next_ch = 0
    for c in mems:
        if c.channel is None:
            c.channel = next_ch
        next_ch = max(next_ch, c.channel) + 1
        if c.label is None:
            c.label = f"M{c.channel}"
    seen = set()
    for c in comps:
        if c.id in seen:
            raise ValueError(f"{data['id']}: duplicate component id {c.id}")
        seen.add(c.id)
    fingers = Fingers(**data["fingers"]) if "fingers" in data else None
    return BoardProfile(
        id=data["id"], name=data["name"], short=data.get("short", data["name"]), skus=list(data["skus"]),
        kind=data["kind"], pcb_mm=tuple(data["pcb_mm"]), card_mm=tuple(data["card_mm"]),
        fans=data["fans"], fan_mm=data["fan_mm"], cooler=data.get("cooler", ""),
        flow_through=data.get("flow_through", False), vendor=data.get("vendor"),
        subsystems=list(data.get("subsystems", [])), board_code=data.get("board_code"),
        mem_part=data.get("mem_part"), mem_gb_each=data.get("mem_gb_each", 2),
        gpu_vrm=data.get("gpu_vrm"), mem_vrm=data.get("mem_vrm"),
        outline=[tuple(p) for p in data["outline"]] if "outline" in data else None,
        fingers=fingers,
        aux_boards=[AuxBoard(a["name"], tuple(a["rect"]), a["role"]) for a in data.get("aux", [])],
        components=comps, sources=list(data.get("sources", [])), notes=list(data.get("notes", [])),
    )


_CACHE: dict[str, BoardProfile] | None = None


def load_profiles() -> dict[str, BoardProfile]:
    global _CACHE
    if _CACHE is None:
        out = {}
        for f in sorted(resources.files(__package__).iterdir(), key=lambda p: p.name):
            if f.name.endswith(".toml"):
                p = parse_profile(tomllib.loads(f.read_text()))
                out[p.id] = p
        _CACHE = out
    return _CACHE
