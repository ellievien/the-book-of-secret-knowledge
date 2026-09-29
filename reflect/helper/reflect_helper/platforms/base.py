"""Interface every platform backend implements."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..frames import RawFrame
from ..geometry import Rect


@dataclass
class WindowTarget:
    """The Claude window we mirror.

    ``bounds`` is the visible window rectangle in the coordinate space the
    input injector uses: physical pixels on Windows (the helper is
    per-monitor DPI aware), points on macOS. ``scale`` converts logical
    pixels/points to that space's capture pixels.
    """

    handle: int
    pid: int
    bounds: Rect
    scale: float
    title: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def logical_size(self) -> tuple[int, int]:
        factor = self.scale if self.input_is_physical else 1.0
        return round(self.bounds.w / factor), round(self.bounds.h / factor)

    @property
    def input_is_physical(self) -> bool:
        return bool(self.extra.get("physical", False))


class Grabber(Protocol):
    closed: bool
    description: str

    def grab(self, target: WindowTarget) -> RawFrame | None:
        """Newest frame, or None when nothing new arrived since the last call."""

    def close(self) -> None: ...


class Backend(Protocol):
    name: str

    def find_claude_window(self) -> WindowTarget | None: ...

    def is_claude_running(self) -> bool: ...

    def refresh(self, target: WindowTarget) -> WindowTarget | None:
        """Re-read the window's bounds; None if it no longer exists."""

    def bring_to_front(self, target: WindowTarget) -> None: ...

    def enter_phone_mode(self, target: WindowTarget, aspect: float) -> None: ...

    def exit_phone_mode(self, target: WindowTarget | None) -> None: ...

    def open_capture(self, target: WindowTarget, fps: int) -> Grabber: ...

    def scroll(self, mouse, target: WindowTarget, dx_logical: float, dy_logical: float) -> None:
        """Scroll so content moves by (dx, dy) logical pixels, like a finger drag."""

    def permissions_problem(self) -> str | None:
        """Human readable description of a missing OS permission, if any."""
