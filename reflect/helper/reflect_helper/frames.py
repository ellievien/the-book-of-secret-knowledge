"""Raw captured frames, change detection and JPEG encoding."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image

from .config import MAX_FRAME_EDGE
from .protocol import pack_frame


@dataclass
class RawFrame:
    """A captured window image as packed pixels.

    ``mode`` is a Pillow raw mode for 32-bit pixels: "BGRX" (Windows, and
    ScreenCaptureKit), "XRGB", "RGBX" or "XBGR".
    """

    width: int
    height: int
    data: bytes
    stride: int
    mode: str = "BGRX"

    def digest(self) -> bytes:
        return hashlib.blake2b(self.data, digest_size=16).digest()

    def to_image(self) -> Image.Image:
        return Image.frombytes("RGB", (self.width, self.height), self.data, "raw", self.mode, self.stride, 1)


def encode_jpeg(frame: RawFrame, quality: int) -> tuple[int, int, bytes]:
    image = frame.to_image()
    longest = max(image.size)
    if longest > MAX_FRAME_EDGE:
        scale = MAX_FRAME_EDGE / longest
        image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality, optimize=False, progressive=False, subsampling="4:2:0")
    return image.width, image.height, buffer.getvalue()


def frame_message(frame: RawFrame, quality: int) -> tuple[int, int, bytes]:
    width, height, jpeg = encode_jpeg(frame, quality)
    return width, height, pack_frame(width, height, jpeg)
