"""Coordinate math shared by the platform backends (pure, unit tested)."""

from __future__ import annotations

from dataclasses import dataclass

from .config import PHONE_HEIGHT, PHONE_WIDTH


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    def same_size(self, other: "Rect | None", tolerance: float = 1.0) -> bool:
        return other is not None and abs(self.w - other.w) <= tolerance and abs(self.h - other.h) <= tolerance


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def normalized_to_point(rect: Rect, nx: float, ny: float) -> tuple[int, int]:
    """Map a 0..1 position on the mirrored image to a point inside the window."""
    nx = clamp(nx, 0.0, 1.0)
    ny = clamp(ny, 0.0, 1.0)
    # Stay one unit inside the window so an edge tap never lands on a neighbour.
    x = rect.x + min(nx * rect.w, max(rect.w - 1, 0))
    y = rect.y + min(ny * rect.h, max(rect.h - 1, 0))
    return round(x), round(y)


def phone_aspect(viewport: tuple[float, float] | None) -> float:
    """Height/width ratio for phone mode: the phone's mirror area, else 440x956."""
    if viewport:
        width, height = viewport
        if width > 0 and height > 0:
            return clamp(height / width, 1.3, 2.6)
    return PHONE_HEIGHT / PHONE_WIDTH


def fit_phone_size(width: float, aspect: float, max_height: float) -> tuple[int, int]:
    """Size with the requested aspect, shrunk if it would be taller than max_height."""
    height = width * aspect
    if height > max_height > 0:
        height = max_height
        width = height / aspect
    return round(width), round(height)


def min_width_fallback(actual_width: float, aspect: float, max_height: float) -> tuple[int, int]:
    """The window refused to get narrower: keep its minimum width, get the aspect as close as possible."""
    height = actual_width * aspect
    if max_height > 0:
        height = min(height, max_height)
    return round(actual_width), round(height)


def crop_box(frame_w: int, frame_h: int, outer: Rect, visible: Rect) -> tuple[int, int, int, int] | None:
    """Crop for captures that include invisible window borders (Windows resize frame).

    Returns (left, top, right, bottom) in frame pixels, or None when no crop is needed.
    """
    if abs(frame_w - visible.w) <= 1 and abs(frame_h - visible.h) <= 1:
        return None
    if abs(frame_w - outer.w) > 1 or abs(frame_h - outer.h) > 1:
        return None  # mid-resize or unexpected size; show the frame as is
    left = int(clamp(visible.x - outer.x, 0, frame_w))
    top = int(clamp(visible.y - outer.y, 0, frame_h))
    right = int(clamp(left + visible.w, left + 1, frame_w))
    bottom = int(clamp(top + visible.h, top + 1, frame_h))
    if (left, top, right, bottom) == (0, 0, frame_w, frame_h):
        return None
    return left, top, right, bottom
