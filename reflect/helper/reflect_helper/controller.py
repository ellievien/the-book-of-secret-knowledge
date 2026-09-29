"""The helper's core: capture loop, connected phones, phone mode and status."""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Callable

from .config import DEFAULT_FPS, DEFAULT_QUALITY, MAX_FPS, State
from .frames import RawFrame, frame_message
from .geometry import clamp, phone_aspect
from .inputs import coalesce
from .pairing import Pairing
from .platforms.base import Backend, WindowTarget
from .protocol import encode

if TYPE_CHECKING:
    from .server import ClientSession

log = logging.getLogger(__name__)

SCAN_INTERVAL = 1.0
ERROR_BACKOFF = 1.0

MSG_NOT_RUNNING = "Claude desktop not running"
MSG_WINDOW_CLOSED = "Claude is running but its window is closed. Open Claude on the computer."
MSG_PAUSED = "Streaming is paused on the computer"


class Controller:
    def __init__(
        self,
        backend: Backend,
        pairing: Pairing,
        state: State,
        *,
        name: str,
        injector_factory: Callable[[Backend], Any] | None = None,
        fps: int = DEFAULT_FPS,
        quality: int = DEFAULT_QUALITY,
    ):
        self.backend = backend
        self.pairing = pairing
        self.state = state
        self.name = name
        self.helper_id: str = state.get("helper_id")
        self.fps = fps
        self.quality = quality
        self.streaming = True
        self.phone_mode = bool(state.get("phone_mode", True))
        self.viewport: tuple[float, float] | None = None

        self.sessions: set[ClientSession] = set()
        self._injector_factory = injector_factory
        self._injector = None
        self._listeners: list[Callable[[], None]] = []

        # Owned by the capture thread.
        self._target: WindowTarget | None = None
        self._grabber = None
        self._applied_aspect: float | None = None
        self._next_scan = 0.0
        self._claude_running: bool | None = None
        self._window_hidden = False

        # Owned by the event loop.
        self._error: str | None = None
        self._last_error_logged = ""
        self._last_digest: bytes | None = None
        self.last_frame: bytes | None = None
        self.frame_size: tuple[int, int] | None = None
        self._status: dict[str, Any] = {}
        self._closing = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._inputs: asyncio.Queue | None = None
        self._capture_pool = ThreadPoolExecutor(1, "reflect-capture")
        self._encode_pool = ThreadPoolExecutor(1, "reflect-encode")
        self._input_pool = ThreadPoolExecutor(1, "reflect-input")

    # -- observers (tray) --------------------------------------------------------------
    def add_listener(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def _changed(self) -> None:
        for callback in self._listeners:
            try:
                callback()
            except Exception:
                log.exception("listener failed")

    # -- state ---------------------------------------------------------------------------------
    def _active(self) -> bool:
        return self.streaming and bool(self.sessions) and not self._closing

    def status(self) -> dict[str, Any]:
        if not self.streaming:
            message = MSG_PAUSED
        elif self._claude_running is False:
            message = MSG_NOT_RUNNING
        elif self._window_hidden:
            message = MSG_WINDOW_CLOSED
        else:
            message = self._error or ""
        target = self._target
        return {
            "claude_running": self._claude_running is not False,
            "window_found": target is not None,
            "streaming": self.streaming,
            "phone_mode": self.phone_mode,
            "phone_mode_active": self._applied_aspect is not None,
            "window": dict(zip(("w", "h"), target.logical_size)) if target else None,
            "fps": self.fps,
            "message": message,
        }

    def status_line(self) -> str:
        if not self.streaming:
            return "Paused"
        if self._claude_running is False:
            return MSG_NOT_RUNNING
        count = len(self.sessions)
        if count:
            return "Phone connected" if count == 1 else f"{count} phones connected"
        return "Waiting for your phone"

    def _publish_status(self, force: bool = False) -> None:
        status = self.status()
        if force or status != self._status:
            self._status = status
            message = encode("status", **status)
            for session in list(self.sessions):
                session.send_soon(message)
            self._changed()

    def wake(self) -> None:
        if self._wake is not None:
            self._wake.set()

    # -- sessions ---------------------------------------------------------------------------
    def add_session(self, session: ClientSession) -> None:
        was_active = self._active()
        self.sessions.add(session)
        session.send_soon(encode("status", **self.status()))
        if self.last_frame is not None and was_active:
            session.offer_frame(self.last_frame)
        if not was_active:
            self._last_digest = None  # first frame after idling is always sent
        self.wake()
        self._changed()

    def remove_session(self, session: ClientSession) -> None:
        if session in self.sessions:
            self.sessions.discard(session)
            self.wake()
            self._changed()

    # -- settings from phone or tray -----------------------------------------------------------
    def set_streaming(self, on: bool) -> None:
        self.streaming = bool(on)
        self._last_digest = None
        self.wake()
        self._publish_status()

    def set_phone_mode(self, on: bool) -> None:
        self.phone_mode = bool(on)
        self.state.set("phone_mode", self.phone_mode)
        self._next_scan = 0.0
        self.wake()
        self._publish_status()

    def apply_settings(self, msg: dict[str, Any]) -> None:
        viewport = msg.get("viewport")
        if isinstance(viewport, dict):
            w, h = viewport.get("w"), viewport.get("h")
            if isinstance(w, (int, float)) and isinstance(h, (int, float)) and w > 0 and h > 0:
                self.viewport = (float(w), float(h))
                self._next_scan = 0.0
        if isinstance(msg.get("fps"), (int, float)):
            self.fps = int(clamp(msg["fps"], 1, MAX_FPS))
        if isinstance(msg.get("quality"), (int, float)):
            self.quality = int(clamp(msg["quality"], 30, 90))
            self._last_digest = None
        if isinstance(msg.get("phone_mode"), bool):
            self.set_phone_mode(msg["phone_mode"])
        self.wake()
        self._publish_status()

    # -- input --------------------------------------------------------------------------------
    def enqueue_input(self, event: dict[str, Any]) -> None:
        if self._inputs is not None and self.streaming:
            self._inputs.put_nowait(event)

    async def _input_loop(self) -> None:
        assert self._inputs is not None and self._loop is not None
        while True:
            batch = [await self._inputs.get()]
            while not self._inputs.empty():
                batch.append(self._inputs.get_nowait())
            for event in coalesce(batch):
                target = self._target
                if target is None:
                    continue
                try:
                    await self._loop.run_in_executor(self._input_pool, self._perform, target, event)
                except Exception:
                    log.exception("input %s failed", event.get("kind"))

    def _perform(self, target: WindowTarget, event: dict[str, Any]) -> None:
        if self._injector is None:
            if self._injector_factory is None:
                from .inputs import InputInjector

                self._injector = InputInjector(self.backend)
            else:
                self._injector = self._injector_factory(self.backend)
        self._injector.perform(target, event)

    # -- capture thread ---------------------------------------------------------------------
    def _drop_grabber(self) -> None:
        if self._grabber is not None:
            try:
                self._grabber.close()
            finally:
                self._grabber = None

    def _update_target(self) -> None:
        current = self._target
        fresh = self.backend.refresh(current) if current else None
        if fresh is None:
            fresh = self.backend.find_claude_window()
        if fresh is None:
            if current is not None:
                log.info("Lost the Claude window")
            self._drop_grabber()
            self._target = None
            self._applied_aspect = None
            self._claude_running = self.backend.is_claude_running()
            self._window_hidden = bool(self._claude_running)
            return
        self._claude_running = True
        self._window_hidden = False
        if current is None or fresh.handle != current.handle:
            w, h = fresh.logical_size
            log.info("Mirroring Claude window %r (%dx%d logical, scale %.2f)", fresh.title, w, h, fresh.scale)
            self._drop_grabber()
            self._applied_aspect = None
        elif not fresh.bounds.same_size(current.bounds):
            self._drop_grabber()  # restart capture at the new size
        self._target = fresh

    def _apply_phone_mode(self) -> None:
        target = self._target
        if target is None:
            return
        aspect = phone_aspect(self.viewport)
        if self.phone_mode:
            if self._applied_aspect is None or abs(self._applied_aspect - aspect) > 0.01:
                self.backend.enter_phone_mode(target, aspect)
                self._applied_aspect = aspect
                self._after_resize()
        elif self._applied_aspect is not None:
            self.backend.exit_phone_mode(target)
            self._applied_aspect = None
            self._after_resize()

    def _after_resize(self) -> None:
        if self._target is not None:
            self._target = self.backend.refresh(self._target) or self._target
        self._drop_grabber()

    def _capture_step(self) -> tuple[RawFrame, bytes] | None:
        now = time.monotonic()
        if self._target is None or now >= self._next_scan:
            self._next_scan = now + SCAN_INTERVAL
            self._update_target()
            if self._target is None:
                return None
            self._apply_phone_mode()
        if self._grabber is None or self._grabber.closed:
            self._drop_grabber()
            self._grabber = self.backend.open_capture(self._target, self.fps)
            log.info("Capturing with %s", self._grabber.description)
        frame = self._grabber.grab(self._target)
        if frame is None:
            return None
        return frame, frame.digest()

    def _release(self) -> None:
        """No phone watching (or quitting): stop capturing and give the window its size back."""
        self._drop_grabber()
        try:
            self.backend.exit_phone_mode(self._target)
        except Exception:
            log.exception("could not restore the Claude window")
        self._applied_aspect = None
        self._target = None

    # -- main loop -----------------------------------------------------------------------------
    async def _in_capture_thread(self, fn, *args):
        assert self._loop is not None
        return await self._loop.run_in_executor(self._capture_pool, fn, *args)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._inputs = asyncio.Queue()
        input_task = asyncio.create_task(self._input_loop())
        idle = True
        try:
            while not self._closing:
                if not self._active():
                    if not idle:
                        await self._in_capture_thread(self._release)
                        idle = True
                        self._publish_status()
                    self._wake.clear()
                    if not self._active() and not self._closing:
                        await self._wake.wait()
                    continue
                idle = False
                started = self._loop.time()
                try:
                    result = await self._in_capture_thread(self._capture_step)
                    self._error = None
                except Exception as exc:
                    result = None
                    self._error = f"Capture problem: {exc}"
                    if self._error != self._last_error_logged:
                        log.exception("capture failed")
                        self._last_error_logged = self._error
                    await self._in_capture_thread(self._drop_grabber)
                    self._next_scan = 0.0
                    await asyncio.sleep(ERROR_BACKOFF)
                if result is not None:
                    frame, digest = result
                    if digest != self._last_digest:
                        self._last_digest = digest
                        width, height, message = await self._loop.run_in_executor(
                            self._encode_pool, frame_message, frame, self.quality
                        )
                        self.last_frame = message
                        self.frame_size = (width, height)
                        for session in list(self.sessions):
                            session.offer_frame(message)
                self._publish_status()
                delay = 1.0 / max(1, self.fps) - (self._loop.time() - started)
                if delay > 0:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), delay)
                    except asyncio.TimeoutError:
                        pass
        finally:
            input_task.cancel()

    async def shutdown(self) -> None:
        self._closing = True
        self.wake()
        for session in list(self.sessions):
            await session.close(1001, "Reflect helper stopped")
        if self._loop is not None:
            await self._in_capture_thread(self._release)
        else:
            self._release()
        for pool in (self._capture_pool, self._encode_pool, self._input_pool):
            pool.shutdown(wait=False, cancel_futures=True)

    def restore_window_now(self) -> None:
        """Last-resort synchronous restore (atexit / console close)."""
        try:
            self.backend.exit_phone_mode(self._target)
        except Exception:
            pass
