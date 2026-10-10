"""Procedural board layout for SKUs/boards without a measured profile.

Follows the arrangement every documented Blackwell board of that memory width uses
(see README): 16 chips → 4 above / 5 left / 5 right / 2 below; 8 → 2 above / 4 right / 2 below;
6 → 8-pad layout with the top pair empty; 4 → 2 above / 2 right (clamshell adds 4 behind).
"""

from __future__ import annotations

from ..chips import SkuSpec
from . import BoardProfile, Component, Fingers

PCB_MM = {"GB202": (205, 125), "GB203": (190, 110), "GB205": (185, 105), "GB206": (160, 108), "GB207": (160, 108)}
PKG_MM = {"GB202": (50, 50), "GB203": (40, 40), "GB205": (36, 36), "GB206": (32, 32), "GB207": (30, 30)}
CHIP_W, CHIP_H = 12.0, 14.0  # GDDR7 package, mm (portrait)
GAP = 2.5


def generic_profile(sku: SkuSpec, vram_gb: int | None = None) -> BoardProfile:
    W, H = PCB_MM[sku.chip]
    pw, ph = PKG_MM[sku.chip]
    gx, gy = W * 0.50, H * 0.47  # GPU centre, mm
    comps: list[Component] = []

    def pct(x0, y0, x1, y1):
        return (x0 / W * 100, y0 / H * 100, x1 / W * 100, y1 / H * 100)

    def chip(cid, cx, cy, rot=False, kind="mem", side="front"):
        w, h = (CHIP_H, CHIP_W) if rot else (CHIP_W, CHIP_H)
        comps.append(Component(cid, kind, pct(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), side=side, conf="inferred"))

    comps.append(Component("GPU", "gpu", pct(gx - pw / 2, gy - ph / 2, gx + pw / 2, gy + ph / 2),
                           part=sku.variant, label=sku.chip, conf="inferred"))
    n = sku.bus_width // 32
    ring_x = pw / 2 + GAP + CHIP_W / 2
    ring_y = ph / 2 + GAP + CHIP_H / 2
    slots: list[tuple[float, float, bool]] = []  # (cx, cy, rotated)

    def row(y, count):
        for i in range(count):
            slots.append((gx + (i - (count - 1) / 2) * (CHIP_W + 1.5), y, False))

    def col(x, count):
        for i in range(count):
            slots.append((x, gy + (i - (count - 1) / 2) * (CHIP_W + 1.5), True))

    pads: list[tuple[float, float, bool]] = []
    if n >= 12:
        per_side = (n - 6) // 2 if n == 16 else (n - 4) // 2
        row(gy - ring_y, 4)
        col(gx - ring_x - 1.5, per_side)
        col(gx + ring_x + 1.5, per_side)
        row(gy + ring_y, 2 if n == 16 else 0)
    elif n == 8:
        row(gy - ring_y, 2)
        col(gx + ring_x + 1.5, 4)
        row(gy + ring_y, 2)
    elif n == 6:
        col(gx + ring_x + 1.5, 4)
        row(gy + ring_y, 2)
        before = len(slots)
        row(gy - ring_y, 2)
        pads, slots = slots[before:], slots[:before]
    else:
        row(gy - ring_y, 2)
        col(gx + ring_x + 1.5, 2)
    for i, (cx, cy, rot) in enumerate(slots):
        chip(f"M{i}", cx, cy, rot)
    for j, (cx, cy, rot) in enumerate(pads):
        chip(f"PAD{j}", cx, cy, rot, kind="pad")
    clamshell = bool(vram_gb and n and vram_gb / n > 2.5)
    if clamshell:
        for i, (cx, cy, rot) in enumerate(slots):
            chip(f"M{len(slots) + i}", cx, cy, rot, side="back")

    # VRM: GPU phases in a column left of the GPU, memory phases right of the memory
    gpu_ph = max(4, min(20, round(sku.tgp_w / 26)))
    mem_ph = 2 if n <= 4 else 3
    top, bot = H * 0.10, H * 0.84
    lx = gx - pw / 2 - (CHIP_W + 2 * GAP if n >= 12 else GAP) - 12
    comps.append(Component("LV", "choke", pct(lx, top, lx + 6.5, bot), rail="NVVDD", conf="inferred"))
    comps.append(Component("LS", "stage", pct(lx - 5, top, lx - 1, bot), rail="NVVDD", conf="inferred"))
    rx = gx + pw / 2 + GAP + CHIP_H + 8
    comps.append(Component("RV", "choke", pct(rx, H * 0.25, rx + 6, H * 0.25 + mem_ph * 9), rail="FBVDD", conf="inferred"))
    comps.append(Component("U_GPU", "ctrl", pct(lx - 6, 4, lx - 2, 8), label="PWM", rail="NVVDD", conf="inferred"))
    # expand the grouped VRM entries into individual parts
    expanded = []
    for c in comps:
        if c.id in ("LV", "LS", "RV"):
            k = gpu_ph if c.id != "RV" else mem_ph
            x0, y0, x1, y1 = c.rect
            step = (y1 - y0) / k
            for i in range(k):
                expanded.append(Component(f"{c.id}{i + 1}", c.kind, (x0, y0 + i * step + step * 0.07, x1, y0 + (i + 1) * step - step * 0.07),
                                          rail=c.rail, marking="R22" if c.id == "RV" else ("LR10" if c.kind == "choke" else None),
                                          conf="inferred", group=c.id))
        else:
            expanded.append(c)
    comps = expanded
    pc = "12V-2x6 (16-pin)" if sku.power_connector == "12V-2x6" else "PCIe 8-pin"
    comps.append(Component("J_PWR", "power", (86, 2, 95 if "16" in pc else 93, 13), part=pc, conf="inferred"))
    for i in range(3):
        comps.append(Component(f"DP{i + 1}", "output", (0.5, 26 + i * 15, 6.5, 38 + i * 15), label="DP", part="DisplayPort 2.1b", conf="inferred"))
    comps.append(Component("HDMI", "output", (0.5, 72, 6.5, 83), label="HDMI", part="HDMI 2.1b", conf="inferred"))
    comps.append(Component("B_MLCC", "mlcc", pct(gx - pw / 4, gy - ph / 4, gx + pw / 4, gy + ph / 4), side="back", conf="inferred"))
    mems = [c for c in comps if c.kind == "mem"]
    for i, c in enumerate(sorted(mems, key=lambda c: (c.side != "front", int(c.id[1:])))):
        c.channel = i % n if n else i  # clamshell partners share a controller
        c.label = c.id
    fx0 = 88 / W * 100 * 0.25
    return BoardProfile(
        id=f"generic-{sku.key.lower()}", name=f"{sku.name} (generic {sku.chip} layout)",
        short=f"{sku.name} (generic)", skus=[sku.key], kind="reference", pcb_mm=(W, H),
        card_mm=(max(W + 60, 240), 120), fans=2 if W < 180 else 3, fan_mm=90,
        cooler="Unknown cooler", mem_part=f"{sku.mem_type} {sku.mem_gbps:g} Gbps",
        mem_gb_each=(vram_gb or sku.mem_gb[-1]) / max(1, len([c for c in comps if c.kind == 'mem'])),
        gpu_vrm=f"~{gpu_ph}-phase NVVDD (estimated from TGP)", mem_vrm=f"~{mem_ph}-phase FBVDD (estimated)",
        fingers=Fingers(x0=22, x1=22 + 89 / W * 100, physical=16, electrical=sku.pcie_lanes),
        components=comps, generic=True,
        notes=["No measured profile for this board: layout generated from the chip's documented memory arrangement. Positions are approximate."],
    )
