"""Blackwell (RTX 50) chip and SKU specifications.

Spec-sheet values, not live telemetry. Sources: NVIDIA product pages, TechPowerUp GPU
database / reviews, VideoCardz, Igor's Lab (see README "Sources").
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ChipSpec:
    name: str
    gpcs: int
    tpcs_per_gpc: int
    bus_width: int  # full-die memory bus, bits
    l2_mb: int  # full-die L2
    die_mm2: float
    transistors_b: float | None
    nvenc: int
    nvdec: int
    process: str = "TSMC 4N"
    sms_per_tpc: int = 2
    fp32_per_sm: int = 128
    tensor_per_sm: int = 4
    arch: str = "Blackwell"

    @property
    def sms(self) -> int:
        return self.gpcs * self.tpcs_per_gpc * self.sms_per_tpc

    @property
    def mem_controllers(self) -> int:
        return self.bus_width // 32


CHIPS: dict[str, ChipSpec] = {
    "GB202": ChipSpec("GB202", gpcs=12, tpcs_per_gpc=8, bus_width=512, l2_mb=128, die_mm2=750,
                      transistors_b=92.2, nvenc=3, nvdec=2),
    "GB203": ChipSpec("GB203", gpcs=7, tpcs_per_gpc=6, bus_width=256, l2_mb=64, die_mm2=378,
                      transistors_b=45.6, nvenc=2, nvdec=2),
    "GB205": ChipSpec("GB205", gpcs=5, tpcs_per_gpc=5, bus_width=192, l2_mb=48, die_mm2=263,
                      transistors_b=31.1, nvenc=1, nvdec=1),
    "GB206": ChipSpec("GB206", gpcs=3, tpcs_per_gpc=6, bus_width=128, l2_mb=32, die_mm2=181,
                      transistors_b=21.9, nvenc=1, nvdec=1),
    "GB207": ChipSpec("GB207", gpcs=2, tpcs_per_gpc=5, bus_width=128, l2_mb=32, die_mm2=149,
                      transistors_b=None, nvenc=1, nvdec=1),
}


@dataclass(frozen=True)
class SkuSpec:
    key: str  # stable id, e.g. "5080"
    name: str  # marketing name
    chip: str
    variant: str  # die variant, e.g. "GB203-400-A1"
    device_ids: tuple[int, ...]
    sms: int
    gpcs: int
    cuda_cores: int
    tensor_cores: int
    rt_cores: int
    rops: int
    tmus: int
    l2_mb: int
    bus_width: int
    mem_gb: tuple[int, ...]  # possible VRAM sizes
    mem_type: str
    mem_gbps: float
    tgp_w: int
    pcie_lanes: int
    nvenc: int
    nvdec: int
    power_connector: str = "12V-2x6"
    notes: str = ""
    extra: dict = field(default_factory=dict, hash=False, compare=False)

    @property
    def chip_spec(self) -> ChipSpec:
        return CHIPS[self.chip]

    @property
    def bandwidth_gbs(self) -> float:
        return self.bus_width / 8 * self.mem_gbps


SKUS: list[SkuSpec] = [
    SkuSpec("5090", "GeForce RTX 5090", "GB202", "GB202-300-A1", (0x2B85,), sms=170, gpcs=11,
            cuda_cores=21760, tensor_cores=680, rt_cores=170, rops=176, tmus=680, l2_mb=96,
            bus_width=512, mem_gb=(32,), mem_type="GDDR7", mem_gbps=28, tgp_w=575, pcie_lanes=16,
            nvenc=3, nvdec=2),
    SkuSpec("5090D", "GeForce RTX 5090 D", "GB202", "GB202-250-A1", (0x2B87,), sms=170, gpcs=11,
            cuda_cores=21760, tensor_cores=680, rt_cores=170, rops=176, tmus=680, l2_mb=96,
            bus_width=512, mem_gb=(32,), mem_type="GDDR7", mem_gbps=28, tgp_w=575, pcie_lanes=16,
            nvenc=3, nvdec=2, notes="China SKU"),
    SkuSpec("5090Dv2", "GeForce RTX 5090 D v2", "GB202", "GB202-250-A1", (0x2B8C,), sms=170, gpcs=11,
            cuda_cores=21760, tensor_cores=680, rt_cores=170, rops=176, tmus=680, l2_mb=96,
            bus_width=384, mem_gb=(24,), mem_type="GDDR7", mem_gbps=28, tgp_w=575, pcie_lanes=16,
            nvenc=3, nvdec=2, notes="China SKU, 24 GB / 384-bit"),
    SkuSpec("5080", "GeForce RTX 5080", "GB203", "GB203-400-A1", (0x2C02,), sms=84, gpcs=7,
            cuda_cores=10752, tensor_cores=336, rt_cores=84, rops=112, tmus=336, l2_mb=64,
            bus_width=256, mem_gb=(16,), mem_type="GDDR7", mem_gbps=30, tgp_w=360, pcie_lanes=16,
            nvenc=2, nvdec=2),
    SkuSpec("5070Ti", "GeForce RTX 5070 Ti", "GB203", "GB203-300-A1", (0x2C05,), sms=70, gpcs=6,
            cuda_cores=8960, tensor_cores=280, rt_cores=70, rops=96, tmus=280, l2_mb=48,
            bus_width=256, mem_gb=(16,), mem_type="GDDR7", mem_gbps=28, tgp_w=300, pcie_lanes=16,
            nvenc=2, nvdec=1),
    SkuSpec("5070-GB203", "GeForce RTX 5070 (GB203)", "GB203", "GB203-xxx", (0x2C09,), sms=48, gpcs=4,
            cuda_cores=6144, tensor_cores=192, rt_cores=48, rops=80, tmus=192, l2_mb=48,
            bus_width=192, mem_gb=(12,), mem_type="GDDR7", mem_gbps=28, tgp_w=250, pcie_lanes=16,
            nvenc=1, nvdec=1, notes="Late-2026 variant on a cut GB203; config assumed equal to RTX 5070"),
    SkuSpec("5070", "GeForce RTX 5070", "GB205", "GB205-300-A1", (0x2F04,), sms=48, gpcs=5,
            cuda_cores=6144, tensor_cores=192, rt_cores=48, rops=80, tmus=192, l2_mb=48,
            bus_width=192, mem_gb=(12,), mem_type="GDDR7", mem_gbps=28, tgp_w=250, pcie_lanes=16,
            nvenc=1, nvdec=1),
    SkuSpec("5060-GB205", "GeForce RTX 5060 (GB205)", "GB205", "GB205-200", (0x2F06,), sms=30, gpcs=3,
            cuda_cores=3840, tensor_cores=120, rt_cores=30, rops=48, tmus=120, l2_mb=32,
            bus_width=128, mem_gb=(8,), mem_type="GDDR7", mem_gbps=28, tgp_w=145, pcie_lanes=8,
            nvenc=1, nvdec=1, notes="Salvaged GB205; config assumed equal to RTX 5060"),
    SkuSpec("5060Ti", "GeForce RTX 5060 Ti", "GB206", "GB206-300-A1", (0x2D04,), sms=36, gpcs=3,
            cuda_cores=4608, tensor_cores=144, rt_cores=36, rops=48, tmus=144, l2_mb=32,
            bus_width=128, mem_gb=(8, 16), mem_type="GDDR7", mem_gbps=28, tgp_w=180, pcie_lanes=8,
            nvenc=1, nvdec=1, power_connector="8-pin"),
    SkuSpec("5060", "GeForce RTX 5060", "GB206", "GB206-250-A1", (0x2D05,), sms=30, gpcs=3,
            cuda_cores=3840, tensor_cores=120, rt_cores=30, rops=48, tmus=120, l2_mb=32,
            bus_width=128, mem_gb=(8,), mem_type="GDDR7", mem_gbps=28, tgp_w=145, pcie_lanes=8,
            nvenc=1, nvdec=1, power_connector="8-pin"),
    SkuSpec("5050", "GeForce RTX 5050", "GB207", "GB207-300-A1", (0x2D83,), sms=20, gpcs=2,
            cuda_cores=2560, tensor_cores=80, rt_cores=20, rops=32, tmus=80, l2_mb=24,
            bus_width=128, mem_gb=(8,), mem_type="GDDR6", mem_gbps=20, tgp_w=130, pcie_lanes=8,
            nvenc=1, nvdec=1, power_connector="8-pin"),
]

SKU_BY_KEY = {s.key: s for s in SKUS}
SKU_BY_DEVICE = {d: s for s in SKUS for d in s.device_ids}


def enabled_sm_map(chip: ChipSpec, sku: SkuSpec) -> list[list[bool]]:
    """Per-GPC list of per-SM enabled flags.

    NVIDIA fuses off units per die, and which ones varies chip to chip; this is a
    deterministic, plausible distribution (whole GPCs first, then TPC pairs spread from
    the last GPCs), not a readout of this specific die.
    """
    per_gpc = chip.tpcs_per_gpc * chip.sms_per_tpc
    grid = [[True] * per_gpc for _ in range(chip.gpcs)]
    for g in range(sku.gpcs, chip.gpcs):
        grid[g] = [False] * per_gpc
    excess = sku.gpcs * per_gpc - sku.sms
    g = sku.gpcs - 1
    tpc = chip.tpcs_per_gpc - 1
    while excess > 0 and sku.gpcs > 0:
        for s in range(chip.sms_per_tpc):
            if excess > 0:
                grid[g][tpc * chip.sms_per_tpc + s] = False
                excess -= 1
        g -= 1
        if g < 0:
            g = sku.gpcs - 1
            tpc -= 1
    return grid
