from gputui.boards import load_profiles
from gputui.identify import identify
from gputui.telemetry import GpuSnapshot


def snap(dev, vendor, sub=0x0000, gb=16):
    return GpuSnapshot(pci_device_id=dev, pci_subsys_vendor=vendor, pci_subsys_device=sub, mem_total=gb * 2**30)


def test_users_zotac_5080_is_vendor_family_match():
    m = identify(snap(0x2C02, 0x19DA, 0x1765))
    assert m.sku.key == "5080"
    assert m.profile.id == "zotac-5080"
    assert m.level == "vendor"


def test_founders_edition_by_nvidia_subsystem():
    assert identify(snap(0x2C02, 0x10DE)).profile.id == "nvidia-5080-fe"
    assert identify(snap(0x2B85, 0x10DE, gb=32)).profile.id == "nvidia-5090-fe"
    assert identify(snap(0x2F04, 0x10DE, gb=12)).profile.id == "nvidia-5070-fe"


def test_unknown_vendor_gets_reference_layout():
    m = identify(snap(0x2C02, 0x1043))  # ASUS 5080 — no ASUS profile
    assert m.profile.id == "ref-5080" and m.level == "reference"
    m = identify(snap(0x2B85, 0x1043, gb=32))
    assert m.profile.id == "ref-5090"


def test_5060ti_memory_size_picks_board():
    assert identify(snap(0x2D04, 0x19DA, gb=16)).profile.id == "zotac-5060ti-16"
    m8 = identify(snap(0x2D04, 0x10B0, gb=8))
    assert m8.profile.id == "gainward-5060ti-8"
    # a Zotac 8 GB card must not get the 16 GB clamshell board
    assert identify(snap(0x2D04, 0x19DA, gb=8)).profile.id == "gainward-5060ti-8"


def test_sku_without_profile_is_generic():
    m = identify(snap(0x2C09, 0x1458, gb=12))  # GB203-based RTX 5070
    assert m.level == "generic" and m.profile.generic
    assert len(m.profile.of_kind("mem")) == 6


def test_non_rtx50_returns_none():
    assert identify(snap(0x2684, 0x10DE, gb=24)) is None  # RTX 4090


def test_forced_board():
    m = identify(snap(0x2C02, 0x19DA), force_board="nvidia-5080-fe")
    assert m.level == "forced" and m.profile.id == "nvidia-5080-fe"


def test_every_sku_resolves():
    from gputui.chips import SKUS

    for sku in SKUS:
        m = identify(snap(sku.device_ids[0], 0x1043, gb=sku.mem_gb[-1]))
        assert m is not None and m.sku.key == sku.key
        assert load_profiles() is not None
