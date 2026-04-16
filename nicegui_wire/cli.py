"""Top-level ``ngwire`` CLI.

Subcommands:

    sniff URL [-o LOGFILE]     dump the wire to stderr / JSONL
    tui   URL                  render the site as a Textual TUI
    fb    URL                  render the site in a 320x240 framebuffer sim
"""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ngwire")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sniff = sub.add_parser("sniff", help="dump wire traffic as JSONL")
    p_sniff.add_argument("url")
    p_sniff.add_argument("-o", "--output")
    p_sniff.add_argument("-q", "--quiet", action="store_true")
    p_sniff.add_argument("-v", "--verbose-log", action="store_true")

    p_tui = sub.add_parser("tui", help="render site as Textual TUI")
    p_tui.add_argument("url")

    p_fb = sub.add_parser("fb", help="render site in framebuffer sim")
    p_fb.add_argument("url")
    p_fb.add_argument("--width", type=int, default=320)
    p_fb.add_argument("--height", type=int, default=240)
    p_fb.add_argument("--scale", type=int, default=2)

    args = parser.parse_args(argv)

    if args.cmd == "sniff":
        from .sniffer import main as sniff_main
        return sniff_main([args.url] + (
            ["-o", args.output] if args.output else []
        ) + (
            ["-q"] if args.quiet else []
        ) + (
            ["-v"] if args.verbose_log else []
        ))

    if args.cmd == "tui":
        try:
            from .textual_app import run as tui_run
        except ImportError as e:
            print(f"ngwire tui: {e}. Install with `pip install nicegui-wire[tui]`", file=sys.stderr)
            return 1
        return tui_run(args.url)

    if args.cmd == "fb":
        try:
            from .fb_sim import run as fb_run
        except ImportError as e:
            print(f"ngwire fb: {e}. Install with `pip install nicegui-wire[fb]`", file=sys.stderr)
            return 1
        return fb_run(args.url, width=args.width, height=args.height, scale=args.scale)

    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
