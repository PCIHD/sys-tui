"""CPU: per-thread load and clocks, core topology, k10temp/coretemp, energy → watts."""

from __future__ import annotations

import os
from functools import lru_cache

import psutil

from . import CpuStat, Rates, SysFS
from .sensors import Chip, chips_named


@lru_cache(maxsize=4)
def _model(root: str) -> str:
    try:
        with open(os.path.join(root, "proc/cpuinfo")) as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "CPU"


def topology(fs: SysFS, threads: int) -> list[int]:
    """Thread index → dense core index (0..cores-1), ordered by (package, core_id)."""
    keys = []
    for t in range(threads):
        pkg = fs.read_int(f"sys/devices/system/cpu/cpu{t}/topology/physical_package_id") or 0
        core = fs.read_int(f"sys/devices/system/cpu/cpu{t}/topology/core_id")
        keys.append((pkg, core if core is not None else t))
    order = {k: i for i, k in enumerate(sorted(set(keys)))}
    return [order[k] for k in keys]


def thread_mhz(fs: SysFS, t: int) -> float | None:
    base = f"sys/devices/system/cpu/cpu{t}/cpufreq/"
    for name in ("cpuinfo_avg_freq", "scaling_cur_freq"):
        v = fs.read_int(base + name)
        if v:
            return v / 1000
    return None


def prefcore_ranking(fs: SysFS, threads: int) -> list[int | None]:
    return [fs.read_int(f"sys/devices/system/cpu/cpu{t}/cpufreq/amd_pstate_prefcore_ranking") for t in range(threads)]


def energy(fs: SysFS, rates: Rates, chips: list[Chip], cores: int, now: float) -> tuple[float | None, list[float | None], str]:
    """Package and per-core watts from zenergy/amd_energy, else from RAPL powercap (often root-only)."""
    for chip in chips_named(chips, "zenergy", "amd_energy"):
        pkg = None
        per: list[float | None] = [None] * cores
        core_labels = sorted(k for k in chip.raw if k.lower().startswith("ecore"))
        for i, lab in enumerate(core_labels):
            w = rates.rate(f"e:{chip.hwmon}:{lab}", chip.raw[lab], now)
            if i < cores:
                per[i] = None if w is None else w / 1e6
        for lab, raw in chip.raw.items():
            if lab.lower().startswith("esocket"):
                w = rates.rate(f"e:{chip.hwmon}:{lab}", raw, now)
                if w is not None:
                    pkg = (pkg or 0.0) + w / 1e6
        return pkg, per, chip.name
    raw = fs.read_int("sys/class/powercap/intel-rapl:0/energy_uj")
    if raw is not None:
        w = rates.rate("e:rapl", raw, now)
        return (None if w is None else w / 1e6), [None] * cores, "rapl"
    return None, [None] * cores, ""


def collect(fs: SysFS, rates: Rates, chips: list[Chip], now: float) -> CpuStat:
    per = psutil.cpu_percent(percpu=True)
    threads = len(per)
    core_of = topology(fs, threads)
    cores = max(core_of) + 1 if core_of else 0
    s = CpuStat(model=_model(str(fs.root)), threads=threads, cores=cores, per_thread=per,
                util=sum(per) / threads if threads else 0.0, core_of_thread=core_of)
    s.mhz = [thread_mhz(fs, t) for t in range(threads)]
    mx = fs.read_int("sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    s.max_mhz = mx / 1000 if mx else None
    for chip in chips_named(chips, "k10temp", "coretemp", "zenpower"):
        for r in chip.readings:
            if r.kind != "temp":
                continue
            if r.label in ("Tctl", "Tdie", "Package id 0") and s.tctl is None:
                s.tctl = r.value
            elif r.label.startswith("Tccd"):
                s.tccd.append(r.value)
        if s.tctl is None and chip.first("temp"):
            s.tctl = chip.first("temp").value
    s.package_w, s.core_w, s.power_source = energy(fs, rates, chips, cores, now)
    s.prefcore = prefcore_ranking(fs, threads)
    s.governor = fs.read("sys/devices/system/cpu/cpu0/cpufreq/scaling_governor") or ""
    s.epp = fs.read("sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference") or ""
    b = fs.read("sys/devices/system/cpu/cpufreq/boost")
    s.boost = None if b is None else b not in ("0", "off")
    s.cstates = cstate_share(fs)
    try:
        s.load = os.getloadavg()
    except OSError:
        pass
    st = psutil.cpu_stats()
    s.ctx_rate = rates.rate("ctx", st.ctx_switches, now)
    s.irq_rate = rates.rate("irq", st.interrupts, now)
    s.procs = len(psutil.pids())
    return s


def cstate_share(fs: SysFS) -> list[tuple[str, float]]:
    """Package-wide C-state time share from cpu0 (all cores share the same idle table)."""
    rows: list[tuple[str, int]] = []
    total = 0
    for d in fs.glob("sys/devices/system/cpu/cpu0/cpuidle/state*"):
        name = fs.read(d / "name") or d.name
        t = fs.read_int(d / "time") or 0
        rows.append((name, t))
        total += t
    if not total:
        return []
    return [(n, t / total) for n, t in rows]


def read_psi(fs: SysFS):
    from . import Psi, PsiLine

    def line(rel: str) -> PsiLine:
        text = fs.read(rel) or ""
        out = PsiLine()
        for row in text.splitlines():
            parts = row.split()
            if not parts:
                continue
            kv = {p.split("=")[0]: p.split("=")[1] for p in parts[1:] if "=" in p}
            if parts[0] == "some":
                out.some10, out.some60, out.some300 = float(kv.get("avg10", 0)), float(kv.get("avg60", 0)), float(kv.get("avg300", 0))
            elif parts[0] == "full":
                out.full10, out.full60, out.full300 = float(kv.get("avg10", 0)), float(kv.get("avg60", 0)), float(kv.get("avg300", 0))
        return out

    return Psi(cpu=line("proc/pressure/cpu"), memory=line("proc/pressure/memory"), io=line("proc/pressure/io"))
