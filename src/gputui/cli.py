"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.rule import Rule

from . import tables
from .boards import load_profiles
from .chips import SKU_BY_KEY
from .identify import identify
from .telemetry import DemoSource, NvmlSource

SCENE_VIEWS = ("card", "front", "back", "die", "cores")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gputui", description="Live PCB-level view of your NVIDIA RTX 50-series card.")
    ap.add_argument("--gpu", type=int, default=0, help="GPU index (default 0)")
    ap.add_argument("--interval", type=float, default=1.0, help="poll interval in seconds (default 1)")
    ap.add_argument("--view", choices=SCENE_VIEWS, default="front", help="initial view")
    ap.add_argument("--board", help="force a board profile id (see --list-boards)")
    ap.add_argument("--demo", nargs="?", const="5080", metavar="SKU",
                    help=f"synthetic data instead of NVML; SKU one of {', '.join(SKU_BY_KEY)}")
    ap.add_argument("--once", action="store_true", help="print the view + all tables once and exit")
    ap.add_argument("--json", action="store_true", help="print one snapshot as JSON and exit")
    ap.add_argument("--png", metavar="FILE", help="write the chosen view to a PNG (needs Pillow) and exit")
    ap.add_argument("--gif", metavar="FILE", help="write an animated GIF (needs Pillow) and exit")
    ap.add_argument("--seconds", type=float, default=8.0, help="duration for --gif (default 8)")
    ap.add_argument("--fps", type=float, default=10.0, help="frame rate for --gif (default 10)")
    ap.add_argument("--size", default=None, metavar="WxH", help="render size in cells for --once/--png")
    ap.add_argument("--list-boards", action="store_true", help="list known board profiles and exit")
    return ap


def _source(args):
    if args.demo:
        if args.demo not in SKU_BY_KEY:
            sys.exit(f"unknown SKU {args.demo!r}; choose from {', '.join(SKU_BY_KEY)}")
        if args.board:
            p = load_profiles()[args.board]
            gb = round(len(p.of_kind("mem")) * p.mem_gb_each) or None
            return DemoSource(p.skus[0], p.vendor or 0x10DE, (p.subsystems or [0])[0], mem_gb=gb)
        return DemoSource(args.demo)
    try:
        return NvmlSource(args.gpu)
    except Exception as e:
        sys.exit(f"gputui: cannot read the GPU through NVML ({e}).\n"
                 "Is the NVIDIA driver loaded? Try `gputui --demo` to explore without a GPU.")


def _scene(view, match, w, h):
    from .render.card import CardScene
    from .render.cores import CoresScene
    from .render.die import DieScene
    from .render.pcb import PcbScene

    if view == "card":
        return CardScene(match.profile, match.sku, w, h)
    if view == "die":
        return DieScene(match.profile, match.sku, w, h)
    if view == "cores":
        return CoresScene(match.profile, match.sku, w, h)
    return PcbScene(match.profile, match.sku, w, h, side=view)


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.list_boards:
        for p in load_profiles().values():
            print(f"{p.id:<20} {SKU_BY_KEY[p.skus[0]].name:<22} {p.kind:<10} {p.short}")
        return
    if args.board and args.board not in load_profiles():
        sys.exit(f"unknown board {args.board!r} (see --list-boards)")
    src = _source(args)
    if args.json:
        snap = src.collect()
        d = snap.to_dict()
        m = identify(snap, args.board)
        if m:
            d["board_profile"], d["match_level"] = m.profile.id, m.level
        print(json.dumps(d, indent=2, default=str))
        return
    if args.gif:
        from pixboard.png import canvases_to_gif

        console = Console()
        snap = src.collect()
        m = identify(snap, args.board)
        if m is None:
            sys.exit(f"gputui: {snap.name} is not an RTX 50-series card this tool knows.")
        if args.size:
            w, h = (int(v) for v in args.size.lower().split("x"))
        else:
            w = min(console.width, 160)
            h = max(20, int(w * 0.32))
        scene = _scene(args.view, m, w, h)
        n = max(2, int(args.seconds * args.fps))
        frames = []
        t0 = getattr(src, "t0", 0.0)
        for i in range(n):
            snap = src.collect(t0 + i / args.fps) if args.demo else src.collect()
            scene.frame(0.0, snap)
            frames.append(scene.frame(i / args.fps, snap))
        try:
            canvases_to_gif(frames, args.gif, args.fps)
        except ImportError:
            sys.exit("gputui: --gif needs Pillow (uv pip install pillow)")
        print(f"wrote {args.gif} ({n} frames, {scene.title})")
        return
    if args.once or args.png:
        console = Console()
        snap = src.collect()
        m = identify(snap, args.board)
        if m is None:
            sys.exit(f"gputui: {snap.name} is not an RTX 50-series card this tool knows.")
        if args.size:
            w, h = (int(v) for v in args.size.lower().split("x"))
        else:
            w = min(console.width, 160)
            h = max(20, int(w * 0.32))
        scene = _scene(args.view, m, w, h)
        scene.frame(0.0, snap)
        cv = scene.frame(0.5, snap)
        if args.png:
            try:
                from .snapshot import canvas_to_png

                canvas_to_png(cv, args.png)
            except ImportError:
                sys.exit("gputui: --png needs Pillow (uv pip install pillow)")
            print(f"wrote {args.png} ({scene.title})")
            return
        console.print(Rule(f"{scene.title}  ·  {m.level_text}"))
        console.print(cv.to_rich())
        for name in tables.TABS:
            console.print(Rule(name))
            if name == "Components":
                console.print(tables.components_table(snap, m, None))
            else:
                console.print(tables.BUILDERS[name](snap, m))
        return
    from .app import GpuTuiApp

    make = None if args.demo else (lambda i: NvmlSource(i))
    GpuTuiApp(src, interval=args.interval, view=args.view, force_board=args.board, make_source=make).run()
    src.close()
