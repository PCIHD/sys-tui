"""A plausible ATX layout for boards without a profile, populated from what the system reports."""

from __future__ import annotations

from . import BoardProfile, Component


def generic_profile(snap) -> BoardProfile:
    name = snap.board.name or "Unknown board"
    vendor = snap.board.vendor or ""
    c: list[Component] = []
    add = lambda *a, **k: c.append(Component(*a, conf="inferred", **k))  # noqa: E731

    add("CPU", "socket", (34.0, 20.0, 56.0, 48.0), label="CPU", part=snap.cpu.model or "CPU socket")
    add("HS_L", "heatsink", (14.0, 8.0, 25.0, 46.0), label="VRM", part="VRM heatsink", rail="Vcore")
    add("HS_T", "heatsink", (28.0, 7.0, 60.0, 13.5), label="VRM", part="VRM heatsink", rail="Vcore")
    for i in range(6):
        y0 = 19.0 + i * 5.0
        add(f"L{i + 1}", "choke", (26.5, y0, 30.5, y0 + 4.2), rail="Vcore", group="L")
    for i in range(4):
        x0 = 36.0 + i * 4.8
        add(f"LT{i + 1}", "choke", (x0, 14.5, x0 + 4.0, 17.2), rail="Vcore" if i < 2 else "VSOC", group="LT")
    add("CPU_PWR1", "power", (16.0, 0.8, 23.0, 5.2), pins=8, label="EPS", part="8-pin EPS +12V")
    add("ATX_PWR1", "power", (95.0, 26.0, 99.4, 47.0), pins=24, label="ATX", part="24-pin ATX")

    dimms = snap.mem.dimms
    n = 4 if len(dimms) != 2 else 2
    addrs = [d.addr for d in dimms] + [0x50 + i for i in range(8)]
    addrs = list(dict.fromkeys(addrs))[:n]
    addrs.sort()
    names = ["A1", "A2", "B1", "B2"] if n == 4 else ["A", "B"]
    w = 3.0
    for i in range(n):
        x0 = 68.0 + i * (w + 1.0)
        add(f"DIMM{names[i]}", "dimm", (x0, 8.0, x0 + w, 62.0), label=names[i], bind=f"dimm:0x{addrs[i]:02x}",
            part="DIMM slot")

    add("PCI_E1", "pcie", (16.0, 70.0, 60.0, 73.4), lanes=16, source="cpu", label="PCIE_x16", bind="gpu:x16",
        part="PCIe x16 (CPU)")
    add("PCI_E2", "pcie", (16.0, 88.0, 30.0, 90.8), lanes=1, source="chipset", label="PCIE_x1", part="PCIe x1")
    ports = []
    for d in snap.disks:
        if d.kind == "nvme" and d.root_port and d.root_port not in ports:
            ports.append(d.root_port)
    slots = [(26.0, 58.0, 60.0, 66.8), (26.0, 76.0, 60.0, 84.8), (62.0, 76.0, 94.0, 84.8)]
    for i, rect in enumerate(slots[: max(1, len(ports))]):
        bind = f"nvme:{ports[i]}" if i < len(ports) else None
        add(f"M2_{i + 1}", "m2", rect, lanes=4, label=f"M2_{i + 1}", bind=bind, part="M.2 Key M slot",
            shield=i == 0)
    add("PCH", "chipset", (66.0, 87.0, 78.0, 97.0), label="PCH", part="Chipset", bind="temp:PCH")
    for i in range(2):
        y0 = 70.0 + i * 7.5
        add(f"SATA_{i + 1}", "sata", (94.0, y0, 99.6, y0 + 6.5), label=f"SATA{i + 1}", bind=f"sata:{i + 1}",
            part="SATA 6Gb/s")
    add("IO_USB1", "io", (0.0, 4.0, 8.0, 12.0), ports=["usb5", "usb5"], part="USB")
    add("IO_VIDEO", "io", (0.0, 15.0, 11.0, 22.0), ports=["hdmi", "dp"], part="Video outputs")
    add("IO_USB2", "io", (0.0, 25.0, 8.0, 33.0), ports=["usb10", "usb10"], part="USB")
    add("IO_LAN", "io", (0.0, 36.0, 8.0, 44.0), ports=["lan", "usb10"], part="LAN + USB", bind="net:ethernet")
    add("IO_AUDIO", "io", (0.0, 48.0, 8.0, 56.0), ports=["audio"], part="Audio jacks")
    if any(nt.kind == "wifi" for nt in snap.nets):
        add("WIFI", "wifi", (0.4, 58.0, 12.0, 61.5), label="Wi-Fi", part="Wi-Fi module", bind="net:wifi")
    add("LAN", "chip", (14.0, 48.0, 18.0, 52.0), label="LAN", part="Ethernet controller", bind="net:ethernet")
    add("SIO", "chip", (42.0, 89.0, 48.0, 94.5), label="SIO", part="Super I/O / hardware monitor")
    add("BAT1", "battery", (32.0, 86.5, 39.0, 93.5), label="BAT", part="CR2032 CMOS battery", bind="in:VBat")

    fan_spots = [(60.0, 8.0), (62.0, 1.0), (70.0, 1.0), (8.0, 64.0), (46.0, 96.0), (56.0, 96.0), (84.0, 96.0)]
    fans = [r for r in snap.sensors if r.kind == "fan"]
    for i, r in enumerate(fans[: len(fan_spots)]):
        x, y = fan_spots[i]
        add(f"FAN{i + 1}", "fan", (x, y, x + 4.0, y + 2.6), label=r.label, bind=f"fan:{r.label}", part="Fan header")

    return BoardProfile(id="generic", name=name, vendor=vendor, short=f"{name} (generic layout)", match=[],
                        form="ATX", pcb_mm=(305, 244), socket="", chipset="", superio="", vrm="",
                        notes=["No profile for this board: positions are a generic ATX template."],
                        holes=[(4.0, 4.0), (60.0, 4.0), (96.0, 4.0), (96.0, 64.0), (4.0, 96.0), (96.0, 96.0)],
                        components=c, generic=True)
