"""Work out which card is installed: PCI IDs → SKU → board profile (+ how sure we are)."""

from __future__ import annotations

from dataclasses import dataclass

from .boards import BoardProfile, load_profiles
from .boards.generic import generic_profile
from .chips import SKU_BY_DEVICE, SKU_BY_KEY, SkuSpec
from .telemetry import GpuSnapshot

VENDORS = {
    0x10DE: "NVIDIA", 0x1043: "ASUS", 0x1462: "MSI", 0x1458: "Gigabyte", 0x19DA: "ZOTAC",
    0x196E: "PNY", 0x1569: "Palit", 0x10B0: "Gainward", 0x1B4C: "Galax / KFA2", 0x7377: "Colorful",
    0x1849: "ASRock", 0x3842: "EVGA", 0x1DD5: "Manli", 0x1ED3: "Yeston", 0x6688: "Maxsun",
    0x107D: "Leadtek", 0x1565: "Biostar", 0x172F: "Sparkle", 0x17AA: "Lenovo", 0x1028: "Dell",
    0x103C: "HP", 0x1025: "Acer",
}

LEVELS = {
    "exact": "exact board match (PCI subsystem ID)",
    "vendor": "vendor-family match — same maker & SKU; exact model not confirmed",
    "reference": "reference / Founders Edition layout for this SKU",
    "similar": "closest documented board for this SKU (different maker)",
    "generic": "generic layout generated for this chip (no profile for this board)",
    "forced": "board chosen manually",
}


@dataclass
class Match:
    sku: SkuSpec
    profile: BoardProfile
    level: str
    vendor_name: str | None

    @property
    def level_text(self) -> str:
        return LEVELS[self.level]


def sku_for(snap: GpuSnapshot) -> SkuSpec | None:
    sku = SKU_BY_DEVICE.get(snap.pci_device_id or -1)
    if sku is None and snap.name:
        # unknown device id: fall back on the marketing name
        name = snap.name.replace("NVIDIA ", "").lower()
        for s in SKU_BY_KEY.values():
            if s.name.lower() == name:
                return s
    return sku


def vram_gb(snap: GpuSnapshot) -> int | None:
    if not snap.mem_total:
        return None
    return round(snap.mem_total / 2**30)


def identify(snap: GpuSnapshot, force_board: str | None = None) -> Match | None:
    sku = sku_for(snap)
    if sku is None:
        return None
    vendor = snap.pci_subsys_vendor
    vname = VENDORS.get(vendor or -1)
    profiles = load_profiles()
    if force_board:
        return Match(sku, profiles[force_board], "forced", vname)
    gb = vram_gb(snap)

    def fits(p: BoardProfile) -> bool:
        if sku.key not in p.skus:
            return False
        mems = len(p.of_kind("mem"))
        if gb and mems and p.mem_gb_each and abs(mems * p.mem_gb_each - gb) > 1.5:
            return False  # e.g. 5060 Ti 8 GB vs 16 GB boards
        return True

    cands = [p for p in profiles.values() if fits(p)]
    for p in cands:
        if p.vendor == vendor and snap.pci_subsys_device in p.subsystems:
            return Match(sku, p, "exact", vname)
    if vendor == 0x10DE:
        for p in cands:
            if p.kind == "fe":
                return Match(sku, p, "exact", vname)
    for p in cands:
        if p.vendor is not None and p.vendor == vendor:
            return Match(sku, p, "vendor", vname)
    for kind in ("reference", "fe", "aib"):
        for p in cands:
            if p.kind == kind:
                return Match(sku, p, "reference" if kind != "aib" else "similar", vname)
    return Match(sku, generic_profile(sku, gb), "generic", vname)
