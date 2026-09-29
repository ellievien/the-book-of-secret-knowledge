"""`--capture-test`: find the Claude window, capture one frame, save it to disk."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .frames import encode_jpeg
from .geometry import phone_aspect
from .platforms import load_backend, prepare_process


def _blank(image) -> bool:
    from PIL import ImageStat

    stddev = ImageStat.Stat(image.convert("L")).stddev[0]
    return stddev < 2.0


def run_capture_test(path: str, phone_mode: bool = False, timeout: float = 8.0) -> int:
    prepare_process()
    backend = load_backend()
    problem = backend.permissions_problem()
    if problem:
        print(problem, file=sys.stderr)

    target = backend.find_claude_window()
    if target is None:
        running = backend.is_claude_running()
        print(
            json.dumps({"ok": False, "error": "Claude window not found", "claude_running": running,
                        "message": "Claude desktop not running" if not running else "Claude's window is closed"}),
        )
        return 2

    info = {
        "ok": False,
        "backend": backend.name,
        "window": {"handle": target.handle, "pid": target.pid, "title": target.title,
                   "bounds": [target.bounds.x, target.bounds.y, target.bounds.w, target.bounds.h],
                   "scale": target.scale, "logical_size": list(target.logical_size)},
    }
    if sys.platform == "win32":
        from .platforms import windows

        info["dpi_awareness"] = windows.DPI_MODE

    grabber = None
    try:
        if phone_mode:
            backend.enter_phone_mode(target, phone_aspect(None))
            target = backend.refresh(target) or target
            info["phone_mode_bounds"] = [target.bounds.x, target.bounds.y, target.bounds.w, target.bounds.h]
            info["phone_mode_logical_size"] = list(target.logical_size)
        grabber = backend.open_capture(target, 15)
        info["capture"] = grabber.description
        frame = None
        deadline = time.monotonic() + timeout
        while frame is None and time.monotonic() < deadline:
            frame = grabber.grab(target)
            if frame is None:
                time.sleep(0.05)
    finally:
        if grabber is not None:
            grabber.close()
        if phone_mode:
            backend.exit_phone_mode(target)

    if frame is None:
        info["error"] = f"no frame within {timeout:.0f} s"
        print(json.dumps(info, indent=2))
        return 3

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    image = frame.to_image()
    image.save(out)
    width, height, jpeg = encode_jpeg(frame, 70)
    out.with_suffix(".jpg").write_bytes(jpeg)
    info.update(
        ok=not _blank(image),
        frame=[frame.width, frame.height],
        jpeg_bytes=len(jpeg),
        saved=str(out.resolve()),
        blank=_blank(image),
    )
    print(json.dumps(info, indent=2))
    return 0 if info["ok"] else 4
