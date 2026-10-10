"""Display connector state from Linux DRM sysfs (/sys/class/drm/cardN-<TYPE>-<n>)."""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass

DRM = "/sys/class/drm"


@dataclass
class Connector:
    name: str  # e.g. "DP-2", "HDMI-A-1"
    kind: str  # "DP" | "HDMI"
    number: int
    connected: bool
    monitor: str | None
    mode: str | None
    enabled: bool


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def edid_monitor_name(edid: bytes) -> str | None:
    """Monitor name (descriptor 0xFC) from a base EDID block."""
    if len(edid) < 128:
        return None
    for i in range(4):
        d = edid[54 + 18 * i: 72 + 18 * i]
        if d[:3] == b"\x00\x00\x00" and d[3] == 0xFC:
            return d[5:].split(b"\n")[0].decode("ascii", "replace").strip() or None
    return None


def _norm_bus(bus_id: str) -> str:
    # NVML: "00000000:01:00.0"; sysfs: "0000:01:00.0"
    parts = bus_id.lower().split(":")
    if len(parts) == 3:
        parts[0] = parts[0][-4:]
    return ":".join(parts)


def read_connectors(bus_id: str | None, root: str = DRM) -> list[Connector]:
    """Connectors of the DRM card whose PCI device matches ``bus_id``."""
    if not bus_id or not os.path.isdir(root):
        return []
    want = _norm_bus(bus_id)
    out: list[Connector] = []
    for card in sorted(glob.glob(os.path.join(root, "card[0-9]*"))):
        base = os.path.basename(card)
        if "-" in base:
            continue
        dev = os.path.realpath(os.path.join(card, "device"))
        if os.path.basename(dev).lower() != want:
            continue
        for con in sorted(glob.glob(card + "-*")):
            m = re.match(r".*?-(DP|HDMI-A)-(\d+)$", con)
            if not m:
                continue
            kind = "DP" if m.group(1) == "DP" else "HDMI"
            connected = _read(os.path.join(con, "status")) == "connected"
            monitor = mode = None
            if connected:
                try:
                    with open(os.path.join(con, "edid"), "rb") as f:
                        monitor = edid_monitor_name(f.read())
                except OSError:
                    pass
                modes = _read(os.path.join(con, "modes"))
                mode = modes.splitlines()[0] if modes else None
            out.append(Connector(f"{m.group(1)}-{m.group(2)}", kind, int(m.group(2)), connected,
                                 monitor, mode, _read(os.path.join(con, "enabled")) == "enabled"))
    # DP ports first in their numbering order, then HDMI — the usual bracket order
    out.sort(key=lambda c: (c.kind != "DP", c.number))
    return out
