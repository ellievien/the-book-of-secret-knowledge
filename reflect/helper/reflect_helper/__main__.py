"""Entry point: `python -m reflect_helper [--no-tray] [--pair] [--capture-test FILE]`."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys

from . import __version__
from .config import DEFAULT_PORT, state_dir


def _setup_logging(verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    root.addHandler(console)
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            state_dir() / "reflect.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(file_handler)
    except OSError:
        pass
    for noisy in ("websockets", "zeroconf", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reflect_helper", description="Mirror the Claude desktop app to your iPhone.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="WebSocket port (default %(default)s)")
    parser.add_argument("--no-tray", action="store_true", help="run without the tray icon (e.g. over SSH)")
    parser.add_argument("--pair", action="store_true", help="show a fresh pairing code for another phone")
    parser.add_argument("--capture-test", metavar="FILE", help="capture one frame of the Claude window to FILE and exit")
    parser.add_argument("--phone-mode", action="store_true", help="with --capture-test: resize to phone shape first")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True, errors="replace")
    _setup_logging(args.verbose)

    if args.capture_test:
        from .capture_test import run_capture_test

        return run_capture_test(args.capture_test, phone_mode=args.phone_mode)

    from .app import HelperApp

    return HelperApp(port=args.port, headless=args.no_tray, open_pairing=args.pair).run()


if __name__ == "__main__":
    sys.exit(main())
