"""Pure-logic tests: protocol, pairing, geometry, input parsing, frames."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from reflect_helper import protocol
from reflect_helper.config import State
from reflect_helper.frames import RawFrame, encode_jpeg, frame_message
from reflect_helper.geometry import (
    Rect,
    crop_box,
    fit_phone_size,
    min_width_fallback,
    normalized_to_point,
    phone_aspect,
)
from reflect_helper.inputs import coalesce, parse_combo, platform_modifier, validate_input
from reflect_helper.pairing import MAX_FAILURES, Pairing, PairingError
from reflect_helper.server import is_lan_address

# Shared with ios/ReflectTests/ProtocolTests.swift so both sides agree on the auth proof.
VECTOR_TOKEN = bytes(range(32))
VECTOR_NONCE = bytes(range(32, 64))
VECTOR_PROOF = "62215de7bddcea7e2c4047ff6bb94f8d18262fc8b3f3648134bb7d44158ff84d"


def test_auth_proof_vector():
    assert protocol.auth_proof(VECTOR_TOKEN, VECTOR_NONCE) == VECTOR_PROOF
    assert protocol.b64(VECTOR_TOKEN) == "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="


def test_frame_header_roundtrip():
    packed = protocol.pack_frame(440, 956, b"\xff\xd8jpeg")
    assert packed[:8] == bytes([0, 0, 1, 0xB8, 0, 0, 3, 0xBC])
    assert protocol.unpack_frame(packed) == (440, 956, b"\xff\xd8jpeg")
    with pytest.raises(protocol.ProtocolError):
        protocol.unpack_frame(b"\x00\x01")


def test_decode_rejects_garbage():
    for bad in ("nope", "[]", '{"type": 3}', '{"type": "launch_missiles"}'):
        with pytest.raises(protocol.ProtocolError):
            protocol.decode(bad)
    assert protocol.decode('{"type":"ping","t":5}') == {"type": "ping", "t": 5}


def test_normalized_mapping_stays_inside_window():
    rect = Rect(100, 50, 880, 1912)  # e.g. 440x956 logical at 200 %
    assert normalized_to_point(rect, 0.5, 0.5) == (540, 1006)
    assert normalized_to_point(rect, 0, 0) == (100, 50)
    assert normalized_to_point(rect, 1, 1) == (979, 1961)
    assert normalized_to_point(rect, -3, 7) == (100, 1961)


def test_phone_sizing():
    assert phone_aspect(None) == pytest.approx(956 / 440)
    assert phone_aspect((402, 780)) == pytest.approx(780 / 402)
    assert fit_phone_size(440, 956 / 440, 2000) == (440, 956)
    assert fit_phone_size(880, 956 / 440, 1400) == (644, 1400)  # shrink to fit the screen height
    assert min_width_fallback(500, 956 / 440, 2000) == (500, 1086)
    assert min_width_fallback(500, 956 / 440, 1000) == (500, 1000)


def test_crop_box_for_invisible_borders():
    outer = Rect(-7, 0, 894, 1919)
    visible = Rect(0, 0, 880, 1912)
    assert crop_box(894, 1919, outer, visible) == (7, 0, 887, 1912)
    assert crop_box(880, 1912, outer, visible) is None
    assert crop_box(500, 500, outer, visible) is None


def test_key_combos():
    assert parse_combo("enter") == ([], "enter")
    assert parse_combo("Return") == ([], "enter")
    assert parse_combo("ctrl+v") == (["ctrl"], "v")
    assert parse_combo("cmd+shift+z") == (["cmd", "shift"], "z")
    assert parse_combo("mod+a") == (["mod"], "a")
    assert parse_combo("shift++") == (["shift"], "+")
    assert platform_modifier("mod", "darwin") == "cmd"
    assert platform_modifier("mod", "win32") == "ctrl"
    for bad in ("", "hyper+x", "ctrl+banana"):
        with pytest.raises(protocol.ProtocolError):
            parse_combo(bad)


def test_validate_and_coalesce_inputs():
    tap = validate_input({"type": "input", "kind": "tap", "x": 0.25, "y": 0.75})
    assert tap == {"kind": "tap", "x": 0.25, "y": 0.75}
    with pytest.raises(protocol.ProtocolError):
        validate_input({"kind": "tap", "x": "left", "y": 0})
    with pytest.raises(protocol.ProtocolError):
        validate_input({"kind": "tap", "x": float("nan"), "y": 0})
    s1 = validate_input({"kind": "scroll", "x": 0.5, "y": 0.5, "dx": 0, "dy": 0.01})
    s2 = validate_input({"kind": "scroll", "x": 0.5, "y": 0.5, "dx": 0.002, "dy": 0.02})
    merged = coalesce([tap, s1, s2, tap])
    assert [e["kind"] for e in merged] == ["tap", "scroll", "tap"]
    assert merged[1]["dy"] == pytest.approx(0.03)


def test_lan_only():
    for ok in ("192.168.1.20", "10.0.0.5", "172.16.3.4", "127.0.0.1", "::1", "fe80::1%en0", "::ffff:192.168.0.2"):
        assert is_lan_address(ok), ok
    for bad in ("8.8.8.8", "2001:4860:4860::8888", "::ffff:1.1.1.1", "garbage"):
        assert not is_lan_address(bad), bad


def test_pairing_flow(tmp_path):
    state = State(tmp_path / "state.json")
    pairing = Pairing(state)
    code = pairing.code
    assert code and len(code) == 6 and code.isdigit()
    with pytest.raises(PairingError) as err:
        pairing.pair("000000" if code != "000000" else "111111", "iPhone")
    assert err.value.code == "bad_code"
    creds = pairing.pair(code, "Ellie's iPhone")
    assert pairing.code is None  # single use
    token = protocol.unb64(creds.token)
    assert len(token) == 32
    nonce = b"n" * 32
    device = pairing.verify(creds.device_id, nonce, protocol.auth_proof(token, nonce))
    assert device and device["name"] == "Ellie's iPhone"
    assert pairing.verify(creds.device_id, nonce, "00" * 32) is None
    assert pairing.verify("unknown", nonce, protocol.auth_proof(token, nonce)) is None
    # Persisted: a new Pairing over the same file still knows the phone and opens no code.
    again = Pairing(State(tmp_path / "state.json"))
    assert again.code is None and len(again.devices()) == 1
    with pytest.raises(PairingError) as err:
        again.pair("123456", "x")
    assert err.value.code == "pairing_closed"
    assert again.open_window()


def test_pairing_lockout_rotates_code(tmp_path):
    pairing = Pairing(State(tmp_path / "state.json"))
    first = pairing.code
    wrong = "999999" if first != "999999" else "888888"
    for _ in range(MAX_FAILURES - 1):
        with pytest.raises(PairingError):
            pairing.pair(wrong, "x")
    with pytest.raises(PairingError) as err:
        pairing.pair(wrong, "x")
    assert err.value.code == "locked"
    with pytest.raises(PairingError) as err:
        pairing.pair(pairing.code, "x")  # even the right (new) code waits out the lockout
    assert err.value.code == "locked"


def test_jpeg_encoding_of_bgrx_frame():
    width, height = 40, 20
    pixels = bytearray()
    for _y in range(height):
        for _x in range(width):
            pixels += bytes([255, 0, 0, 255])  # BGRX blue
    frame = RawFrame(width, height, bytes(pixels), width * 4, "BGRX")
    w, h, jpeg = encode_jpeg(frame, 70)
    image = Image.open(io.BytesIO(jpeg))
    assert image.format == "JPEG" and image.size == (40, 20)
    r, g, b = image.convert("RGB").getpixel((20, 10))
    assert b > 200 and r < 40 and g < 40
    w2, h2, message = frame_message(frame, 70)
    assert protocol.unpack_frame(message)[:2] == (40, 20)
    assert frame.digest() == RawFrame(width, height, bytes(pixels), width * 4).digest()
