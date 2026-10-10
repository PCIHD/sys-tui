"""Motherboard identity from DMI."""

from __future__ import annotations

from . import BoardInfo, SysFS


def read(fs: SysFS) -> BoardInfo:
    d = "sys/class/dmi/id/"
    return BoardInfo(vendor=fs.read(d + "board_vendor") or "", name=fs.read(d + "board_name") or "",
                     product=fs.read(d + "product_name") or "", bios=fs.read(d + "bios_version") or "",
                     bios_date=fs.read(d + "bios_date") or "")
