"""Command line entry point: ``screen-agent serve | doctor | info | shot | click | ...``

``screen-agent serve`` is what MCP clients launch. Everything else is a thin, friendly
wrapper around the same backend calls, so you can smoke-test the installation by hand.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from . import __version__
from .backends import detect_backends, load_backend
from .core import BackendUnavailable, default_shot_dir, parse_region, scale_image

__all__ = ["main", "build_parser"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="screen-agent",
        description="MCP server that gives an AI screenshots and mouse/keyboard control.",
    )
    parser.add_argument("--version", action="version", version=f"screen-agent {__version__}")
    parser.add_argument(
        "--backend",
        default="",
        help="force a backend (mutter, wayland-tools, x11, macos, windows); default: auto-detect",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="run the MCP server on stdio (default)")

    doctor = sub.add_parser("doctor", help="explain which backend was picked and why")
    doctor.add_argument("--json", action="store_true", help="machine readable output")

    info = sub.add_parser("info", help="show desktop size and backend capabilities")
    info.add_argument("--json", action="store_true")

    shot = sub.add_parser("shot", help="take a screenshot")
    shot.add_argument("path", nargs="?", default="", help="output file (default: cache dir)")
    shot.add_argument("region", nargs="?", default="full", help="'full' or 'x,y,w,h'")
    shot.add_argument("--scale", type=int, default=0, help="downscale to this width")
    shot.add_argument("--no-cursor", action="store_true")

    rec = sub.add_parser("record", help="record the screen for N seconds")
    rec.add_argument("seconds", type=float, nargs="?", default=5.0)
    rec.add_argument("path", nargs="?", default="")

    move = sub.add_parser("move", help="move the mouse to x y")
    move.add_argument("x", type=int)
    move.add_argument("y", type=int)

    click = sub.add_parser("click", help="click at x y")
    click.add_argument("x", type=int, nargs="?", default=-1)
    click.add_argument("y", type=int, nargs="?", default=-1)
    click.add_argument("button", nargs="?", default="left", choices=["left", "middle", "right"])
    click.add_argument("--clicks", type=int, default=1)

    drag = sub.add_parser("drag", help="drag from x1 y1 to x2 y2")
    drag.add_argument("coords", type=int, nargs=4, metavar="X1 Y1 X2 Y2")
    drag.add_argument("--button", default="left")

    scroll = sub.add_parser("scroll", help="scroll the wheel")
    scroll.add_argument("amount", type=int, nargs="?", default=3)
    scroll.add_argument("direction", nargs="?", default="down", choices=["up", "down", "left", "right"])
    scroll.add_argument("--horizontal", action="store_true")

    type_cmd = sub.add_parser("type", help="type text into the focused window")
    type_cmd.add_argument("text", nargs="+")

    key = sub.add_parser("key", help="press a key or combo, e.g. 'ctrl+c' or 'Return'")
    key.add_argument("keys")

    sub.add_parser("list-backends", help="list backends and whether they can be used here")

    return parser


# ----------------------------------------------------------------- subcommands
async def _doctor(args: argparse.Namespace) -> int:
    rows = detect_backends()
    if args.json:
        import json  # noqa: PLC0415

        print(
            json.dumps(
                [{"backend": n, "usable": not reason, "reason": reason} for n, reason in rows],
                indent=2,
            )
        )
        return 0
    print(f"screen-agent {__version__} on {sys.platform}")
    for name, reason in rows:
        mark = "OK  " if not reason else "no  "
        print(f"  [{mark}] {name:<15} {reason or 'usable here'}")
    try:
        backend = load_backend(args.backend or None)
    except BackendUnavailable as exc:
        print(f"\n{exc}")
        return 1
    print(f"\nselected backend: {backend.name} ({backend.display_name})")
    print((await backend.info()).render())
    return 0


async def _info(args: argparse.Namespace) -> int:
    backend = load_backend(args.backend or None)
    info = await backend.info()
    if args.json:
        import dataclasses  # noqa: PLC0415
        import json  # noqa: PLC0415

        print(
            json.dumps(
                {
                    "backend": info.backend,
                    "platform": info.platform,
                    "session": info.session,
                    "desktop": dataclasses.asdict(info.desktop),
                    "capabilities": info.capabilities,
                    "notes": info.notes,
                },
                indent=2,
            )
        )
    else:
        print(info.render())
    return 0


async def _shot(args: argparse.Namespace) -> int:
    backend = load_backend(args.backend or None)
    rect = parse_region(args.region, backend.desktop)
    target = _out(args.path, ".png")
    size = await backend.capture_png(target, rect, cursor=not args.no_cursor)
    if args.scale:
        size = scale_image(target, args.scale)
    print(f"{target} ({size[0]}x{size[1]})")
    return 0


async def _record(args: argparse.Namespace) -> int:
    backend = load_backend(args.backend or None)
    # the backend picks the container it can actually write (webm/mp4, or an animated
    # png when ffmpeg is missing) and returns the real path
    target = _out(args.path, ".webm")
    result = await backend.record_video(target, args.seconds)
    if not result.exists():
        print(f"error: the recorder produced no file at {result}", file=sys.stderr)
        return 1
    print(f"{result} ({result.stat().st_size / 1024:.0f} KiB)")
    return 0


async def _move(args: argparse.Namespace) -> int:
    backend = load_backend(args.backend or None)
    await backend.move(args.x, args.y)
    print(f"mouse -> ({args.x}, {args.y})")
    return 0


async def _click(args: argparse.Namespace) -> int:
    backend = load_backend(args.backend or None)
    x = args.x if args.x >= 0 and args.y >= 0 else None
    y = args.y if args.x >= 0 and args.y >= 0 else None
    await backend.click(x, y, button=args.button, clicks=args.clicks)
    print(f"clicked {args.button} x{args.clicks}")
    return 0


async def _drag(args: argparse.Namespace) -> int:
    backend = load_backend(args.backend or None)
    x1, y1, x2, y2 = args.coords
    await backend.drag(x1, y1, x2, y2, button=args.button)
    print(f"dragged ({x1},{y1}) -> ({x2},{y2})")
    return 0


async def _scroll(args: argparse.Namespace) -> int:
    backend = load_backend(args.backend or None)
    await backend.scroll(args.amount, args.direction, args.horizontal)
    print(f"scrolled {args.direction} {args.amount}")
    return 0


async def _type(args: argparse.Namespace) -> int:
    backend = load_backend(args.backend or None)
    text = " ".join(args.text)
    count = await backend.type_text(text)
    print(f"typed {count} chars")
    return 0


async def _key(args: argparse.Namespace) -> int:
    backend = load_backend(args.backend or None)
    await backend.key_press(args.keys)
    print(f"pressed {args.keys}")
    return 0


def _out(path: str, suffix: str) -> Path:
    if path:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.suffix:
            p = p.with_suffix(suffix)
        return p
    d = default_shot_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{time.strftime('%Y%m%d-%H%M%S')}{suffix}"


_HANDLERS = {
    "doctor": _doctor,
    "info": _info,
    "shot": _shot,
    "record": _record,
    "move": _move,
    "click": _click,
    "drag": _drag,
    "scroll": _scroll,
    "type": _type,
    "key": _key,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command in ("list-backends",):
        for name, reason in detect_backends():
            print(f"{'OK  ' if not reason else 'no  '} {name:<15} {reason or 'usable here'}")
        return 0

    if command == "serve":
        from .mcp_server import run_stdio  # noqa: PLC0415

        backend = None
        if args.backend:
            backend = load_backend(args.backend)
        run_stdio(backend)
        return 0

    handler = _HANDLERS.get(command)
    if handler is None:  # pragma: no cover - argparse already guards this
        parser.print_help()
        return 2

    try:
        return asyncio.run(handler(args))
    except BackendUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("\nrun 'screen-agent doctor' for details", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
