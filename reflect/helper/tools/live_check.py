"""Live end-to-end check against the real Claude desktop window.

Starts the helper, finds it over Bonjour like the iPhone does, pairs,
authenticates, receives frames, sends a tap/scroll/typing and checks that the
pointer landed where the tap said, then turns phone mode off and checks the
window got its size back. Run it on a machine where Claude desktop is open:

    python tools/live_check.py --out live-check
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from reflect_helper import protocol  # noqa: E402
from reflect_helper.config import SERVICE_TYPE  # noqa: E402
from reflect_helper.geometry import normalized_to_point  # noqa: E402
from reflect_helper.platforms import load_backend, prepare_process  # noqa: E402

TAP = (0.5, 0.03)  # title-bar strip: a harmless place to click


class Check:
    def __init__(self):
        self.results: list[dict] = []

    def ok(self, name: str, detail: str = "") -> None:
        self.results.append({"check": name, "ok": True, "detail": detail})
        print(f"PASS {name} {detail}", flush=True)

    def fail(self, name: str, detail: str) -> None:
        self.results.append({"check": name, "ok": False, "detail": detail})
        print(f"FAIL {name} {detail}", flush=True)

    @property
    def passed(self) -> bool:
        return all(r["ok"] for r in self.results)


def start_helper(home: str, port: int) -> tuple[subprocess.Popen, str]:
    env = dict(os.environ, REFLECT_HOME=home, PYTHONUNBUFFERED="1")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [sys.executable, "-m", "reflect_helper", "--no-tray", "--port", str(port), "--verbose"],
        cwd=HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", creationflags=flags,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(f"helper exited with {proc.returncode}")
            continue
        print("helper>", line.rstrip(), flush=True)
        match = re.search(r"Pairing code: (\d{6})", line)
        if match:
            return proc, match.group(1)
    raise RuntimeError("helper did not print a pairing code")


def pump_output(proc: subprocess.Popen) -> None:
    import threading

    def run():
        for line in proc.stdout:
            print("helper>", line.rstrip(), flush=True)

    threading.Thread(target=run, daemon=True).start()


async def discover(timeout: float = 20.0) -> tuple[str, int, dict]:
    from zeroconf import IPVersion, ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    zc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    names: asyncio.Queue = asyncio.Queue()

    def on_change(zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Added:
            names.put_nowait(name)

    browser = AsyncServiceBrowser(zc.zeroconf, [SERVICE_TYPE], handlers=[on_change])
    try:
        name = await asyncio.wait_for(names.get(), timeout)
        info = AsyncServiceInfo(SERVICE_TYPE, name)
        if not await info.async_request(zc.zeroconf, 5000):
            raise RuntimeError(f"could not resolve {name}")
        props = {k.decode(): (v or b"").decode() for k, v in info.properties.items()}
        return socket.inet_ntoa(info.addresses[0]), info.port, props
    finally:
        await browser.async_cancel()
        await zc.async_close()


class Link:
    """Reads one connection in order, keeping frames and JSON messages that arrive early."""

    def __init__(self, ws):
        self.ws = ws
        self.frames: list[bytes] = []
        self.texts: list[dict] = []

    async def _read(self) -> None:
        message = await self.ws.recv()
        if isinstance(message, bytes):
            self.frames.append(message)
        else:
            self.texts.append(json.loads(message))

    async def json(self, kind: str, timeout: float = 15.0, where=lambda m: True) -> dict:
        async def wait() -> dict:
            while True:
                for index, message in enumerate(self.texts):
                    if message["type"] == kind and where(message):
                        return self.texts.pop(index)
                await self._read()

        return await asyncio.wait_for(wait(), timeout)

    async def frame(self, timeout: float = 15.0) -> bytes:
        async def wait() -> bytes:
            while not self.frames:
                await self._read()
            return self.frames.pop(0)

        return await asyncio.wait_for(wait(), timeout)

    async def drain_frames(self, seconds: float) -> list[bytes]:
        """Frames arriving within `seconds` (a still picture legitimately sends none)."""
        try:
            await asyncio.wait_for(self._forever(), seconds)
        except asyncio.TimeoutError:
            pass
        frames, self.frames = self.frames, []
        return frames

    async def _forever(self) -> None:
        while True:
            await self._read()


def stop_helper(proc: subprocess.Popen) -> bool:
    """Stop the helper the way a user does (Ctrl+Break / Ctrl+C), so it runs its cleanup."""
    if proc.poll() is not None:
        return True
    try:
        proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
        proc.wait(20)
        return True
    except (subprocess.TimeoutExpired, OSError):
        proc.kill()
        proc.wait(5)
        return False


async def wait_restored(backend, before, seconds: float = 8.0):
    deadline = time.monotonic() + seconds
    current = None
    while time.monotonic() < deadline:
        current = backend.find_claude_window()
        if current is not None and current.bounds.same_size(before.bounds, tolerance=4):
            return True, current
        await asyncio.sleep(0.5)
    return False, current


def cursor_position() -> tuple[int, int]:
    from pynput.mouse import Controller

    x, y = Controller().position
    return round(x), round(y)


async def run(out: Path, port: int, check: Check) -> None:
    from PIL import Image, ImageStat
    from websockets.asyncio.client import connect

    backend = load_backend()
    before = backend.find_claude_window()
    if before is None:
        check.fail("claude window", "Claude desktop window not found")
        return
    check.ok("claude window", f"{before.title!r} {before.bounds}")

    home = tempfile.mkdtemp(prefix="reflect-live-")
    proc, code = start_helper(home, port)
    pump_output(proc)
    stopped = False
    try:
        host, found_port, props = await discover()
        check.ok("bonjour discovery", f"{host}:{found_port} {props.get('name')!r}")
        url = f"ws://{host}:{found_port}/"

        async with connect(url, compression=None, max_size=2**25) as ws:
            link = Link(ws)
            hello = await link.json("hello")
            check.ok("hello", f"os={hello['os']} pairing_open={hello['pairing_open']}")
            await ws.send(protocol.encode("pair", code=code, device="live-check"))
            reply = await link.json("pair")
            if not reply.get("ok"):
                check.fail("pair", json.dumps(reply))
                return
            check.ok("pair")
            device_id, token = reply["device_id"], protocol.unb64(reply["token"])
            await ws.send(protocol.encode("settings", phone_mode=True, viewport={"w": 440, "h": 956}))
            status = await link.json("status", where=lambda s: s.get("phone_mode_active"))
            check.ok("phone mode", f"window {status.get('window')}")

            frames = [await link.frame()]
            # Nudge the UI; more frames follow only if the picture changes (identical ones are skipped).
            await ws.send(protocol.encode("input", kind="scroll", x=0.5, y=0.5, dx=0, dy=-0.05))
            frames += await link.drain_frames(3.0)
            width, height, jpeg = protocol.unpack_frame(frames[-1])
            image = Image.open(io.BytesIO(jpeg))
            out.mkdir(parents=True, exist_ok=True)
            (out / "stream-frame.jpg").write_bytes(jpeg)
            stddev = ImageStat.Stat(image.convert("L")).stddev[0]
            if image.size == (width, height) and stddev > 2:
                check.ok("frames", f"{len(frames)} frame(s), {width}x{height}, {len(jpeg)} bytes, stddev {stddev:.1f}")
            else:
                check.fail("frames", f"size {image.size} vs header {(width, height)}, stddev {stddev:.1f}")

            target = backend.find_claude_window()
            expected = normalized_to_point(target.bounds, *TAP)
            await ws.send(protocol.encode("input", kind="tap", x=TAP[0], y=TAP[1]))
            await asyncio.sleep(1.0)
            actual = cursor_position()
            if abs(actual[0] - expected[0]) <= 2 and abs(actual[1] - expected[1]) <= 2:
                check.ok("tap lands", f"pointer at {actual}, expected {expected}")
            else:
                check.fail("tap lands", f"pointer at {actual}, expected {expected}")

            await ws.send(protocol.encode("input", kind="key", key="esc"))
            await ws.send(protocol.encode("ping", t=1))
            await link.json("pong")
            check.ok("ping/pong")

        restored, current = await wait_restored(backend, before)
        if restored:
            check.ok("restore when phone leaves", f"{current.bounds}")
        else:
            check.fail("restore when phone leaves", f"before {before.bounds}, now {current and current.bounds}")

        async with connect(url, compression=None, max_size=2**25) as ws:
            link = Link(ws)
            hello = await link.json("hello")
            proof = protocol.auth_proof(token, protocol.unb64(hello["nonce"]))
            await ws.send(protocol.encode("auth", device_id=device_id, proof=proof))
            reply = await link.json("auth")
            check.ok("token auth") if reply.get("ok") else check.fail("token auth", json.dumps(reply))
            await link.frame()
            await ws.send(protocol.encode("settings", phone_mode=False))
            await link.json("status", where=lambda s: s.get("phone_mode") is False and not s.get("phone_mode_active"))
            restored, current = await wait_restored(backend, before)
            if restored:
                check.ok("restore when phone mode is turned off", f"{current.bounds}")
            else:
                check.fail("restore when phone mode is turned off", f"before {before.bounds}, now {current and current.bounds}")

            await ws.send(protocol.encode("settings", phone_mode=True))
            await link.json("status", where=lambda s: s.get("phone_mode_active"))
            # Stop while this phone is still connected; keep this loop free to answer the close.
            stopped = await asyncio.to_thread(stop_helper, proc)
        restored, current = await wait_restored(backend, before)
        if stopped and restored:
            check.ok("restore when the helper quits", f"exit code {proc.returncode}, {current.bounds}")
        else:
            check.fail("restore when the helper quits",
                       f"graceful stop {'ok' if stopped else 'failed'}, before {before.bounds}, now {current and current.bounds}")
    finally:
        if not stopped:
            stop_helper(proc)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="live-check")
    parser.add_argument("--port", type=int, default=47811)
    args = parser.parse_args()
    prepare_process()
    check = Check()
    out = Path(args.out)
    try:
        asyncio.run(run(out, args.port, check))
    except Exception as exc:
        check.fail("run", f"{type(exc).__name__}: {exc}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "live-check.json").write_text(json.dumps(check.results, indent=2))
    print("ALL PASSED" if check.passed else "SOME CHECKS FAILED", flush=True)
    return 0 if check.passed else 1


if __name__ == "__main__":
    sys.exit(main())
