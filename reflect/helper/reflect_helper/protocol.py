"""Wire format shared with the iPhone app. See PROTOCOL.md."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
from typing import Any

from . import PROTOCOL_VERSION

FRAME_HEADER = struct.Struct(">II")  # width, height (pixels), big-endian uint32
MAX_TEXT_MESSAGE = 1 << 20

CLIENT_TYPES = {"hello", "pair", "auth", "input", "settings", "ping", "pong"}
INPUT_KINDS = {"tap", "long_press", "scroll", "type", "key"}


class ProtocolError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def encode(msg_type: str, **fields: Any) -> str:
    return json.dumps({"type": msg_type, **fields}, separators=(",", ":"), ensure_ascii=False)


def decode(text: str) -> dict[str, Any]:
    if len(text) > MAX_TEXT_MESSAGE:
        raise ProtocolError("too_large", "message too large")
    try:
        msg = json.loads(text)
    except ValueError as exc:
        raise ProtocolError("bad_json", f"invalid JSON: {exc}") from None
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ProtocolError("bad_message", "message must be an object with a string 'type'")
    if msg["type"] not in CLIENT_TYPES:
        raise ProtocolError("unknown_type", f"unknown message type {msg['type']!r}")
    return msg


def pack_frame(width: int, height: int, jpeg: bytes) -> bytes:
    return FRAME_HEADER.pack(width, height) + jpeg


def unpack_frame(data: bytes) -> tuple[int, int, bytes]:
    if len(data) < FRAME_HEADER.size:
        raise ProtocolError("bad_frame", "frame shorter than header")
    width, height = FRAME_HEADER.unpack_from(data)
    return width, height, data[FRAME_HEADER.size:]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def auth_proof(token: bytes, nonce: bytes) -> str:
    """Hex HMAC-SHA256(token, nonce): proves the token without sending it."""
    return hmac.new(token, nonce, hashlib.sha256).hexdigest()


def hello(helper_id: str, name: str, os_name: str, nonce: bytes, pairing_open: bool) -> str:
    return encode(
        "hello",
        version=PROTOCOL_VERSION,
        id=helper_id,
        name=name,
        os=os_name,
        nonce=b64(nonce),
        pairing_open=pairing_open,
    )


def error(code: str, message: str) -> str:
    return encode("error", code=code, message=message)


def number(msg: dict[str, Any], key: str, default: float | None = None) -> float:
    value = msg.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError("bad_input", f"'{key}' must be a number")
    if value != value or value in (float("inf"), float("-inf")):
        raise ProtocolError("bad_input", f"'{key}' must be finite")
    return float(value)
