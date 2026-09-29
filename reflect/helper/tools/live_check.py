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


async def recv_json(ws, kind: str, timeout: float = 15.0, where=lambda m: True) -> dict:
    async def loop():
        while True:
            message = await ws.recv()
            if isinstance(message, str):
                data = json.loads(message)
                if data["type"] == kind and where(data):
                    return data

    return await asyncio.wait_for(loop(), timeout)


async def recv_frame(ws, timeout: float = 15.0) -> bytes:
    async def loop():
        while True:
            message = await ws.recv()
            if isinstance(message, bytes):
                return message

    return await asyncio.wait_for(loop(), timeout)


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
    try:
        host, found_port, props = await discover()
        check.ok("bonjour discovery", f"{host}:{found_port} {props.get('name')!r}")
        url = f"ws://{host}:{found_port}/"

        async with connect(url, compression=None, max_size=2**25) as ws:
            hello = json.loads(await ws.recv())
            check.ok("hello", f"os={hello['os']} pairing_open={hello['pairing_open']}")
            await ws.send(protocol.encode("pair", code=code, device="live-check"))
            reply = await recv_json(ws, "pair")
            if not reply.get("ok"):
                check.fail("pair", json.dumps(reply))
                return
            check.ok("pair")
            device_id, token = reply["device_id"], protocol.unb64(reply["token"])
            await ws.send(protocol.encode("settings", phone_mode=True, viewport={"w": 440, "h": 956}))
            status = await recv_json(ws, "status", where=lambda s: s.get("phone_mode_active"))
            check.ok("phone mode", f"window {status.get('window')}")

            frames = []
            deadline = time.monotonic() + 20
            while len(frames) < 3 and time.monotonic() < deadline:
                frames.append(await recv_frame(ws))
                if len(frames) == 1:
                    # nudge the UI so more frames follow (hash compare skips identical ones)
                    await ws.send(protocol.encode("input", kind="scroll", x=0.5, y=0.5, dx=0, dy=-0.05))
            width, height, jpeg = protocol.unpack_frame(frames[-1])
            image = Image.open(io.BytesIO(jpeg))
            out.mkdir(parents=True, exist_ok=True)
            (out / "stream-frame.jpg").write_bytes(jpeg)
            stddev = ImageStat.Stat(image.convert("L")).stddev[0]
            if image.size == (width, height) and stddev > 2:
                check.ok("frames", f"{len(frames)} frames, {width}x{height}, {len(jpeg)} bytes, stddev {stddev:.1f}")
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
            await recv_json(ws, "pong")
            check.ok("ping/pong")

        async with connect(url, compression=None, max_size=2**25) as ws:
            hello = json.loads(await ws.recv())
            proof = protocol.auth_proof(token, protocol.unb64(hello["nonce"]))
            await ws.send(protocol.encode("auth", device_id=device_id, proof=proof))
            reply = await recv_json(ws, "auth")
            check.ok("token auth") if reply.get("ok") else check.fail("token auth", json.dumps(reply))
            await recv_frame(ws)
            await ws.send(protocol.encode("settings", phone_mode=False))
            await recv_json(ws, "status", where=lambda s: not s.get("phone_mode_active"))
            await asyncio.sleep(1.0)
            restored = backend.find_claude_window()
            if restored and restored.bounds.same_size(before.bounds, tolerance=4):
                check.ok("restore size", f"{restored.bounds}")
            else:
                check.fail("restore size", f"before {before.bounds}, after {restored and restored.bounds}")
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


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
