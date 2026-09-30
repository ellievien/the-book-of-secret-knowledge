"""WebSocket server: one ClientSession per connected phone."""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import logging
import secrets
from http import HTTPStatus
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from . import protocol
from .config import PORT_ATTEMPTS, platform_name
from .controller import Controller
from .inputs import validate_input
from .pairing import PairingError
from .protocol import ProtocolError, encode

log = logging.getLogger(__name__)

AUTH_DEADLINE_SECONDS = 300
MAX_SESSIONS = 4
MAX_FAILED_ATTEMPTS = 10
ADDRINUSE = {errno.EADDRINUSE, 10048}  # 10048 = WSAEADDRINUSE


def is_lan_address(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


class ClientSession:
    def __init__(self, ws: ServerConnection, controller: Controller):
        self.ws = ws
        self.controller = controller
        self.nonce = secrets.token_bytes(32)
        self.authed = False
        self.device: dict[str, Any] | None = None
        self.peer = ws.remote_address[0] if ws.remote_address else "?"
        self._failures = 0
        self._frame: bytes | None = None
        self._frame_ready = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()

    # -- sending ------------------------------------------------------------------------
    def send_soon(self, text: str) -> None:
        task = asyncio.get_running_loop().create_task(self._send(text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, data) -> None:
        try:
            await self.ws.send(data)
        except ConnectionClosed:
            pass

    def offer_frame(self, message: bytes) -> None:
        """Latest frame wins: a slow link skips frames instead of building a backlog."""
        self._frame = message
        self._frame_ready.set()

    async def _frame_sender(self) -> None:
        while True:
            await self._frame_ready.wait()
            self._frame_ready.clear()
            frame, self._frame = self._frame, None
            if frame is not None:
                await self._send(frame)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            await self.ws.close(code, reason)
        except Exception:
            pass

    # -- lifecycle ------------------------------------------------------------------------
    async def run(self) -> None:
        c = self.controller
        await self._send(protocol.hello(c.helper_id, c.name, platform_name(), self.nonce, c.pairing.code is not None))
        sender = asyncio.create_task(self._frame_sender())
        deadline = asyncio.create_task(self._auth_deadline())
        try:
            async for raw in self.ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    await self._handle(protocol.decode(raw))
                except ProtocolError as exc:
                    await self._send(protocol.error(exc.code, str(exc)))
        except ConnectionClosed:
            pass
        finally:
            sender.cancel()
            deadline.cancel()
            for task in list(self._tasks):
                task.cancel()
            c.remove_session(self)
            log.info("Phone %s disconnected", self.peer)

    async def _auth_deadline(self) -> None:
        await asyncio.sleep(AUTH_DEADLINE_SECONDS)
        if not self.authed:
            await self.close(4001, "authentication timeout")

    async def _failed(self) -> None:
        self._failures += 1
        if self._failures >= MAX_FAILED_ATTEMPTS:
            await self.close(4003, "too many failed attempts")

    def _authenticated(self, device: dict[str, Any]) -> None:
        self.authed = True
        self.device = device
        log.info("Phone %r connected from %s", device.get("name"), self.peer)
        self.controller.add_session(self)

    # -- messages ----------------------------------------------------------------------------
    async def _handle(self, msg: dict[str, Any]) -> None:
        kind = msg["type"]
        if kind == "ping":
            await self._send(encode("pong", t=msg.get("t")))
        elif kind in ("pong", "hello"):
            pass
        elif kind == "pair":
            await self._pair(msg)
        elif kind == "auth":
            await self._auth(msg)
        elif not self.authed:
            await self._send(protocol.error("not_authenticated", "Pair or authenticate first."))
        elif kind == "input":
            self.controller.enqueue_input(validate_input(msg))
        elif kind == "settings":
            self.controller.apply_settings(msg)

    async def _pair(self, msg: dict[str, Any]) -> None:
        if self.authed:
            return
        try:
            creds = self.controller.pairing.pair(str(msg.get("code", "")), str(msg.get("device") or "iPhone"))
        except PairingError as exc:
            await self._send(encode("pair", ok=False, error=exc.code, message=str(exc)))
            await self._failed()
            return
        await self._send(encode("pair", ok=True, device_id=creds.device_id, token=creds.token))
        device = next((d for d in self.controller.pairing.devices() if d["id"] == creds.device_id), {"name": "iPhone"})
        self._authenticated(device)

    async def _auth(self, msg: dict[str, Any]) -> None:
        if self.authed:
            return
        device = self.controller.pairing.verify(msg.get("device_id"), self.nonce, msg.get("proof"))
        if device is None:
            await self._send(
                encode(
                    "auth",
                    ok=False,
                    error="unauthorized",
                    message="This phone is no longer paired with this computer. Pair again.",
                )
            )
            await self._failed()
            return
        await self._send(encode("auth", ok=True, name=device.get("name", "")))
        self._authenticated(device)


class ReflectServer:
    def __init__(self, controller: Controller):
        self.controller = controller
        self.server: Server | None = None
        self.port: int | None = None
        self._open: set[ServerConnection] = set()

    def _process_request(self, connection: ServerConnection, request):
        if request.headers.get("Origin"):
            return connection.respond(HTTPStatus.FORBIDDEN, "Web pages may not connect to Reflect.\n")
        host = connection.remote_address[0] if connection.remote_address else ""
        if not is_lan_address(host):
            log.warning("Refused connection from non-LAN address %s", host)
            return connection.respond(HTTPStatus.FORBIDDEN, "Reflect only accepts connections from your local network.\n")
        if len(self._open) >= MAX_SESSIONS:
            return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "Too many phones connected.\n")
        return None

    async def _handler(self, ws: ServerConnection) -> None:
        self._open.add(ws)
        try:
            await ClientSession(ws, self.controller).run()
        finally:
            self._open.discard(ws)

    async def start(self, port: int, host: str | None = None) -> int:
        last_error: OSError | None = None
        for candidate in range(port, port + PORT_ATTEMPTS):
            try:
                self.server = await serve(
                    self._handler,
                    host,
                    candidate,
                    process_request=self._process_request,
                    compression=None,  # JPEG does not compress; deflate only costs CPU
                    max_size=2**20,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=2,
                    write_limit=2**16,
                )
            except OSError as exc:
                if exc.errno in ADDRINUSE or getattr(exc, "winerror", None) == 10048:
                    last_error = exc
                    continue
                raise
            self.port = self.server.sockets[0].getsockname()[1] if candidate == 0 else candidate
            return self.port
        raise OSError(f"No free port between {port} and {port + PORT_ATTEMPTS - 1}: {last_error}")

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
