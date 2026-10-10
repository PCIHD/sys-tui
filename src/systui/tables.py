"""Text tables: processes and every hwmon sensor (with min/max since start)."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text

from .history import History
from .render.charts import IN, OUT, human_bytes
from .telemetry import ProcStat, SystemSnapshot

SORTS = {
    "cpu": ("CPU", lambda p: p.cpu),
    "mem": ("memory", lambda p: p.rss),
    "io": ("I/O", lambda p: p.io_bps or 0.0),
}
PROC_COLS = ("PID", "Name", "User", "CPU %", "RSS", "Mem %", "I/O", "Core", "GPU", "State")


def minibar(frac: float, width: int = 8) -> Text:
    frac = max(0.0, min(1.0, frac))
    full = frac * width
    n = int(full)
    color = "#3fae7a" if frac < 0.5 else "#f2c14e" if frac < 0.8 else "#e5383b"
    t = Text("█" * n, style=color)
    if n < width:
        t.append(" ▏▎▍▌▋▊▉"[int((full - n) * 8)], style=color)
        t.append(" " * (width - n - 1))
    return t


def proc_row(p: ProcStat, threads: int) -> tuple:
    cpu = Text.assemble(minibar(p.cpu / 100, 6),
                        (f" {p.cpu:6.1f}", "bold" if p.cpu >= 50 else ""))
    mem = Text.assemble(minibar(p.mem_pct / 100 * 4, 6), f" {p.mem_pct:5.1f}")
    io = io_cell(p)
    state = {"running": Text("run", style="#3dff7a"), "sleeping": Text("sleep", style="dim"),
             "disk-sleep": Text("disk", style="#f2c14e"), "zombie": Text("zombie", style="#e5383b")}.get(
        p.status, Text(p.status or "?", style="dim"))
    core = "—" if p.last_cpu is None else str(p.last_cpu)
    gpu = (p.gpu_kind or "GPU") if p.gpu else "—"
    return (str(p.pid), Text(p.name, style="bold" if p.cpu >= 10 or p.gpu else ""), p.user[:12], cpu,
            human_bytes(p.rss), mem, io, core, gpu, state)


def io_cell(p: ProcStat) -> Text | str:
    """``▲read ▼write``; the arrows and colours match the board traces and the I/O charts."""
    if p.io_read_bps is None and p.io_write_bps is None:
        return "—" if p.io_bps is None else human_bytes(p.io_bps, True)
    t = Text()
    if p.io_read_bps:
        t.append("▲" + human_bytes(p.io_read_bps, True), style=f"#{IN:06x}")
    if p.io_write_bps:
        t.append((" " if t else "") + "▼" + human_bytes(p.io_write_bps, True), style=f"#{OUT:06x}")
    return t if t else Text("0", style="dim")


def sorted_procs(s: SystemSnapshot, key: str) -> list[ProcStat]:
    return sorted(s.procs, key=SORTS[key][1], reverse=True)


def sensors_table(s: SystemSnapshot | None, h: History) -> Table:
    t = Table(expand=True, box=None, header_style="bold #8fb8de", padding=(0, 1), show_edge=False)
    for col, kw in (("Chip", {"style": "#8fb8de"}), ("Sensor", {}), ("Value", {"justify": "right"}),
                    ("Min", {"justify": "right", "style": "dim"}), ("Max", {"justify": "right", "style": "dim"}),
                    ("hwmon", {"style": "dim"})):
        t.add_column(col, **kw)
    if s is None:
        return t
    last = None
    for r in s.sensors:
        if r.kind == "energy":
            continue
        lo, hi = h.minmax.get((r.chip, r.hwmon, r.key), (r.value, r.value))
        digits = 0 if r.kind in ("fan", "pwm") else 3 if r.kind == "in" else 1
        fmt = f"{{:.{digits}f}} {r.unit}"
        val = Text(fmt.format(r.value), justify="right")
        if r.kind == "temp":
            val.stylize("#e5383b" if r.value >= 85 else "#f2c14e" if r.value >= 70 else "#3dff7a" if r.value > 0 else "dim")
        elif r.kind == "fan" and r.value == 0:
            val.stylize("dim")
        chip = "" if (r.chip, r.hwmon) == last else r.chip
        if chip and last is not None:
            t.add_row("", "", "", "", "", "")
        last = (r.chip, r.hwmon)
        t.add_row(chip, r.label, val, fmt.format(lo), fmt.format(hi), r.hwmon if chip else "")
    return t
