"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
import time

from rich.console import Console
from rich.rule import Rule

from .boards import identify, load_profiles

VIEWS = ("board", "cpu", "threads", "package", "mem", "dimms", "disk", "net", "fans", "overview")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="systui", description="Live motherboard-level view of the whole system.")
    ap.add_argument("--interval", type=float, default=1.0, help="poll interval in seconds (default 1)")
    ap.add_argument("--board", help="force a board profile id (see --list-boards)")
    ap.add_argument("--demo", action="store_true", help="synthetic data instead of this machine's sensors")
    ap.add_argument("--tab", default="board", choices=("board", "cpu", "mem", "disk", "net", "proc", "sens"),
                    help="tab to open on")
    ap.add_argument("--once", action="store_true", help="print the board, overview charts and sensors once and exit")
    ap.add_argument("--json", action="store_true", help="print one snapshot as JSON and exit")
    ap.add_argument("--png", metavar="FILE", help="write a view to PNG (needs Pillow) and exit")
    ap.add_argument("--gif", metavar="FILE", help="write an animated GIF (needs Pillow) and exit")
    ap.add_argument("--seconds", type=float, default=8.0, help="duration for --gif (default 8)")
    ap.add_argument("--fps", type=float, default=10.0, help="frame rate for --gif (default 10)")
    ap.add_argument("--view", choices=VIEWS, default="board", help="view for --png / --once")
    ap.add_argument("--overlay", choices=("live", "thermal", "off"), default="live", help="board overlay for --png/--once")
    ap.add_argument("--size", default=None, metavar="WxH", help="render size in cells for --once/--png")
    ap.add_argument("--list-boards", action="store_true", help="list known board profiles and exit")
    ap.add_argument("--gputui", metavar="CMD", help="command that launches gputui (G key)")
    ap.add_argument("--no-gpu", action="store_true", help="skip NVML")
    ap.add_argument("--dimm-power", action="store_true",
                    help="live DIMM watts: switches on each DDR5 PMIC's ADC (one register write, undone on exit)")
    return ap


def _source(args):
    if args.demo:
        from .telemetry.demo import DemoSource

        return DemoSource()
    from .telemetry import LinuxSource

    return LinuxSource(gpu=not args.no_gpu, dimm_power=args.dimm_power)


def _warm(src, args, n: int = 1):
    """Two samples ~0.5 s apart so rates (CPU %, W, I/O) are populated; returns (snap, history)."""
    from .history import History

    h = History()
    if args.demo:  # a full window of synthetic history
        for i in range(150):
            snap = src.collect(src.t0 + i)
            h.ingest(snap)
        return snap, h
    snap = src.collect()
    h.ingest(snap)
    for _ in range(n):
        time.sleep(0.5)
        snap = src.collect()
        h.ingest(snap)
    return snap, h


def render_view(view: str, match, snap, hist, w: int, h: int, overlay: str = "live"):
    from .render import panels as P
    from .render.board import BoardScene

    if view == "board":
        sc = BoardScene(match.profile, w, h, hint=snap)
        sc.frame(0.0, snap, mode=overlay)
        return sc.frame(0.5, snap, selected=None, mode=overlay), sc.title
    panel = {
        "cpu": P.CoreGrid(), "threads": P.Threads(), "package": P.Package(), "mem": P.Memory(),
        "dimms": P.Dimms(match.profile), "fans": P.Fans(), "overview": P.Overview(),
        "disk": P.Disk((snap.physical_disks() or snap.disks)[0].name) if snap.disks else P.Partitions(),
        "net": P.Net((snap.physical_nets() or snap.nets)[0].name) if snap.nets else P.Overview(),
    }[view]
    cv = panel.canvas(w, h)
    panel.draw(cv, snap, hist, 0.5)
    return cv, panel.title


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.list_boards:
        for p in load_profiles().values():
            print(f"{p.id:<30} {p.form:<10} {p.short}")
        return
    if args.board and args.board not in load_profiles():
        sys.exit(f"unknown board {args.board!r} (see --list-boards)")
    src = _source(args)
    try:
        if args.json:
            snap, _ = _warm(src, args)
            d = snap.to_dict()
            m = identify(snap, args.board)
            d["board_profile"], d["match_level"] = m.profile.id, m.level
            print(json.dumps(d, indent=2, default=str))
            return
        if args.gif:
            from pixboard.png import canvases_to_gif

            console = Console()
            snap, hist = _warm(src, args)
            m = identify(snap, args.board)
            if args.size:
                w, h = (int(v) for v in args.size.lower().split("x"))
            else:
                w = min(console.width, 170)
                h = max(20, int(w * 0.36) if args.view == "board" else 16)
            n = max(2, int(args.seconds * args.fps))
            frames = []
            t0 = getattr(src, "t0", 0.0)
            for i in range(n):
                snap = src.collect(t0 + i / args.fps) if args.demo else src.collect()
                hist.ingest(snap)
                cv, title = render_view(args.view, m, snap, hist, w, h, args.overlay)
                frames.append(cv)
            try:
                canvases_to_gif(frames, args.gif, args.fps)
            except ImportError:
                sys.exit("systui: --gif needs Pillow (uv pip install pillow)")
            print(f"wrote {args.gif} ({n} frames, {title})")
            return
        if args.once or args.png:
            console = Console()
            snap, hist = _warm(src, args)
            m = identify(snap, args.board)
            if args.size:
                w, h = (int(v) for v in args.size.lower().split("x"))
            else:
                w = min(console.width, 170)
                h = max(20, int(w * 0.36)) if args.view == "board" else 16
            cv, title = render_view(args.view, m, snap, hist, w, h, args.overlay)
            if args.png:
                try:
                    from .snapshot import canvas_to_png

                    canvas_to_png(cv, args.png)
                except ImportError:
                    sys.exit("systui: --png needs Pillow (uv pip install pillow)")
                print(f"wrote {args.png} ({title})")
                return
            from . import tables

            console.print(Rule(f"{title}  ·  {m.level_text}"))
            console.print(cv.to_rich())
            if args.view == "board":
                for v in ("overview", "cpu"):
                    cv2, t2 = render_view(v, m, snap, hist, w, 12)
                    console.print(Rule(t2))
                    console.print(cv2.to_rich())
            console.print(Rule("Sensors"))
            console.print(tables.sensors_table(snap, hist))
            return
    finally:
        if args.json or args.once or args.png or args.gif:
            try:
                src.close()
            except Exception:
                pass
    from .app import SysTuiApp

    try:
        SysTuiApp(src, interval=args.interval, force_board=args.board, gputui=args.gputui, initial_tab=args.tab).run()
    finally:
        try:
            src.close()
        except Exception:
            pass
