"""Platform backend selection."""

from __future__ import annotations

import sys

from .base import Backend, Grabber, WindowTarget

__all__ = ["Backend", "Grabber", "WindowTarget", "load_backend", "prepare_process"]


def prepare_process() -> None:
    """Must run before any window, tray or input library is touched."""
    if sys.platform == "win32":
        from . import windows

        windows.enable_dpi_awareness()
    elif sys.platform == "darwin":
        from . import macos

        macos.init_window_server_connection()


def load_backend() -> Backend:
    if sys.platform == "win32":
        from .windows import WindowsBackend

        return WindowsBackend()
    if sys.platform == "darwin":
        from .macos import MacBackend

        return MacBackend()
    raise RuntimeError("Reflect's helper runs on Windows and macOS only.")
