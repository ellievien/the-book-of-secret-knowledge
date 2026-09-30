"""End-to-end protocol test over a real WebSocket connection.

The desktop side is an in-memory stand-in for the Claude window (this test
runs on any OS, including CI Linux); the real window/capture/input backends
are exercised by tools/live_check.py on Windows and macOS.
"""

from __future__ import annotations

import asyncio
import io
import json
from dataclasses import replace

import pytest
from PIL import Image
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from reflect_helper import protocol
from reflect_helper.config import State
from reflect_helper.controller import MSG_NOT_RUNNING, Controller
from reflect_helper.frames import RawFrame
from reflect_helper.geometry import Rect
from reflect_helper.pairing import Pairing
from reflect_helper.platforms.base import WindowTarget
from reflect_helper.server import ReflectServer


class StandInGrabber:
    description = "stand-in"

    def __init__(self, backend):
        self.backend = backend
        self.closed = False

    def grab(self, target):
        w, h = int(target.bounds.w), int(target.bounds.h)
        pixel = bytes([self.backend.shade, 80, 40, 255])
        return RawFrame(w, h, pixel * (w * h), w * 4, "BGRX")

    def close(self):
        self.closed = True


class StandInBackend:
    name = "stand-in"

    def __init__(self):
        self.running = True
        self.shade = 200
        self.window = WindowTarget(7, 42, Rect(0, 0, 1000, 700), 1.0, "Claude", {"physical": True})
        self.calls: list[tuple] = []

    def find_claude_window(self):
        return self.window if self.running else None

    def is_claude_running(self):
        return self.running

    def refresh(self, target):
        return self.window if self.running else None

    def bring_to_front(self, target):
        self.calls.append(("front",))

    def enter_phone_mode(self, target, aspect):
        self.calls.append(("enter", round(aspect, 3)))
        self.window = replace(self.window, bounds=Rect(0, 0, 440, round(440 * aspect)))

    def exit_phone_mode(self, target):
        self.calls.append(("exit",))
        self.window = replace(self.window, bounds=Rect(0, 0, 1000, 700))

    def open_capture(self, target, fps):
        return StandInGrabber(self)

    def scroll(self, mouse, target, dx, dy):
        pass

    def permissions_problem(self):
        return None


class RecordingInjector:
    def __init__(self):
        self.events = []

    def perform(self, target, event):
        self.events.append((target.handle, event))


async def _recv_until(ws, predicate, timeout=5.0):
    async def loop():
        while True:
            message = await ws.recv()
            if predicate(message):
                return message

    return await asyncio.wait_for(loop(), timeout)


def _is(kind):
    return lambda m: isinstance(m, str) and json.loads(m)["type"] == kind


def _frame(m):
    return isinstance(m, bytes)


async def _wait_for(condition, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def test_full_session(tmp_path):
    asyncio.run(_full_session(tmp_path))


async def _full_session(tmp_path):
    backend = StandInBackend()
    injector = RecordingInjector()
    state = State(tmp_path / "state.json")
    pairing = Pairing(state)
    controller = Controller(backend, pairing, state, name="Test PC", injector_factory=lambda _b: injector)
    server = ReflectServer(controller)
    port = await server.start(0, "127.0.0.1")
    runner = asyncio.create_task(controller.run())
    url = f"ws://127.0.0.1:{port}/"
    try:
        # --- first connection: pair -------------------------------------------------------
        async with connect(url, compression=None, max_size=2**24) as ws:
            hello = json.loads(await ws.recv())
            assert hello["type"] == "hello" and hello["version"] == 1
            assert hello["id"] == state.get("helper_id") and hello["pairing_open"] is True
            assert len(protocol.unb64(hello["nonce"])) == 32

            await ws.send(protocol.encode("input", kind="tap", x=0.5, y=0.5))
            err = json.loads(await _recv_until(ws, _is("error")))
            assert err["code"] == "not_authenticated"

            wrong = "000000" if pairing.code != "000000" else "111111"
            await ws.send(protocol.encode("pair", code=wrong, device="Test iPhone"))
            reply = json.loads(await _recv_until(ws, _is("pair")))
            assert reply["ok"] is False and reply["error"] == "bad_code"

            await ws.send(protocol.encode("pair", code=pairing.code, device="Test iPhone"))
            reply = json.loads(await _recv_until(ws, _is("pair")))
            assert reply["ok"] is True
            device_id, token = reply["device_id"], protocol.unb64(reply["token"])

            status = json.loads(await _recv_until(ws, _is("status")))
            assert set(status) >= {"claude_running", "phone_mode", "streaming", "window", "message"}

            frame = await _recv_until(ws, _frame)
            width, height, jpeg = protocol.unpack_frame(frame)
            assert (width, height) == (440, 956)  # phone mode on by default: 440x956
            assert Image.open(io.BytesIO(jpeg)).size == (440, 956)
            assert ("enter", round(956 / 440, 3)) in backend.calls

            # Unchanged picture: no new frames (hash compare), but pings still answered.
            await ws.send(protocol.encode("ping", t=123))
            pong = json.loads(await _recv_until(ws, _is("pong")))
            assert pong["t"] == 123
            with pytest.raises(asyncio.TimeoutError):
                await _recv_until(ws, _frame, timeout=0.5)

            # Phone reports its mirror area -> window reshaped to that aspect.
            await ws.send(protocol.encode("settings", viewport={"w": 400, "h": 800}))
            frame = await _recv_until(ws, _frame)
            assert protocol.unpack_frame(frame)[:2] == (440, 880)

            # Changed picture -> a new frame is sent.
            backend.shade = 30
            await _recv_until(ws, _frame)

            await ws.send(protocol.encode("input", kind="tap", x=0.25, y=0.5))
            await ws.send(protocol.encode("input", kind="key", key="ctrl+v"))
            await ws.send(protocol.encode("input", kind="type", text="hi\nthere"))
            await _wait_for(lambda: len(injector.events) >= 3)
            kinds = [e["kind"] for _, e in injector.events]
            assert kinds == ["tap", "key", "type"]
            assert injector.events[1][1]["mods"] == ["ctrl"]

            await ws.send(protocol.encode("settings", phone_mode=False))
            status = json.loads(await _recv_until(ws, lambda m: _is("status")(m) and not json.loads(m)["phone_mode"]))
            assert status["phone_mode"] is False
            await _wait_for(lambda: ("exit",) in backend.calls)
            await ws.send(protocol.encode("settings", phone_mode=True))

        # Last phone left -> window restored.
        backend.calls.clear()
        await _wait_for(lambda: ("exit",) in backend.calls)
        assert pairing.code is None

        # --- second connection: authenticate with the stored token -------------------------
        async with connect(url, compression=None, max_size=2**24) as ws:
            hello = json.loads(await ws.recv())
            assert hello["pairing_open"] is False
            nonce = protocol.unb64(hello["nonce"])
            await ws.send(protocol.encode("auth", device_id=device_id, proof=protocol.auth_proof(token, nonce)))
            reply = json.loads(await _recv_until(ws, _is("auth")))
            assert reply["ok"] is True and reply["name"] == "Test iPhone"
            await _recv_until(ws, _frame)

            # Claude quits -> the phone is told.
            backend.running = False
            status = json.loads(await _recv_until(ws, lambda m: _is("status")(m) and not json.loads(m)["claude_running"]))
            assert status["message"] == MSG_NOT_RUNNING
            backend.running = True
            await _recv_until(ws, lambda m: _is("status")(m) and json.loads(m)["claude_running"])

        # --- a proof for another nonce (replay) is refused ---------------------------------------
        async with connect(url, compression=None) as ws:
            json.loads(await ws.recv())
            await ws.send(protocol.encode("auth", device_id=device_id, proof=protocol.auth_proof(token, nonce)))
            reply = json.loads(await _recv_until(ws, _is("auth")))
            assert reply["ok"] is False and reply["error"] == "unauthorized"

        # --- browsers are refused ------------------------------------------------------------
        with pytest.raises(InvalidStatus):
            async with connect(url, origin="http://evil.example"):
                pass
    finally:
        await controller.shutdown()
        await server.stop()
        runner.cancel()


def test_shutdown_restores_window_before_closing_phones(tmp_path):
    asyncio.run(_shutdown_order(tmp_path))


async def _shutdown_order(tmp_path):
    from reflect_helper import server as server_module

    backend = StandInBackend()
    state = State(tmp_path / "state.json")
    pairing = Pairing(state)
    controller = Controller(backend, pairing, state, name="Test PC", injector_factory=lambda _b: RecordingInjector())
    srv = ReflectServer(controller)
    port = await srv.start(0, "127.0.0.1")
    runner = asyncio.create_task(controller.run())
    original_close = server_module.ClientSession.close

    async def recording_close(self, code=1000, reason=""):
        backend.calls.append(("close",))
        await original_close(self, code, reason)

    server_module.ClientSession.close = recording_close
    try:
        async with connect(f"ws://127.0.0.1:{port}/", compression=None, max_size=2**24) as ws:
            json.loads(await ws.recv())
            await ws.send(protocol.encode("pair", code=pairing.code, device="Test iPhone"))
            await _recv_until(ws, _frame)  # streaming in phone mode
            backend.calls.clear()
            started = asyncio.get_running_loop().time()
            await controller.shutdown()
            assert asyncio.get_running_loop().time() - started < 3
        assert backend.calls.index(("exit",)) < backend.calls.index(("close",))
    finally:
        server_module.ClientSession.close = original_close
        await srv.stop()
        runner.cancel()
