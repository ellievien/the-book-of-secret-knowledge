"""Wait until the Claude desktop window exists (used by CI after installing Claude)."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reflect_helper.platforms import load_backend, prepare_process  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    prepare_process()
    backend = load_backend()
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        target = backend.find_claude_window()
        if target is not None:
            print(f"Claude window found: {target.title!r} {target.bounds} scale={target.scale}", flush=True)
            return 0
        time.sleep(2)
    print(f"No Claude window after {args.timeout:.0f} s (running: {backend.is_claude_running()})", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
