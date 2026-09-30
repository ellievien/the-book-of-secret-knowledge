"""Pieces shared by the Windows and macOS backends."""

from __future__ import annotations

import logging

from ..frames import RawFrame
from .base import WindowTarget

log = logging.getLogger(__name__)


class MssGrabber:
    """Fallback capture: copies the window's rectangle from the screen.

    Only correct while the window is unobscured, so it is used only when the
    native window capture API is unavailable.
    """

    description = "mss screen region (fallback)"

    def __init__(self) -> None:
        self.closed = False
        self._sct = None

    def grab(self, target: WindowTarget) -> RawFrame | None:
        import mss

        if self._sct is None:
            self._sct = mss.mss()
        b = target.bounds
        shot = self._sct.grab(
            {"left": int(b.x), "top": int(b.y), "width": max(1, int(b.w)), "height": max(1, int(b.h))}
        )
        width, height = shot.size
        return RawFrame(width, height, bytes(shot.bgra), width * 4, "BGRX")

    def close(self) -> None:
        self.closed = True
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None


class ScrollAccumulator:
    """Keeps sub-step remainders so slow finger drags still scroll smoothly."""

    def __init__(self) -> None:
        self.x = 0.0
        self.y = 0.0

    def take(self, dx: float, dy: float) -> tuple[int, int]:
        self.x += dx
        self.y += dy
        step_x, step_y = int(self.x), int(self.y)
        self.x -= step_x
        self.y -= step_y
        return step_x, step_y
