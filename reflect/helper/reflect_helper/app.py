"""Wires backend, pairing, controller, server, mDNS and tray together."""

from __future__ import annotations

import asyncio
import atexit
import logging
import signal
import sys
import threading

from . import __version__
from .config import DEFAULT_PORT, computer_name, load_state, platform_name
from .controller import Controller
from .mdns import Advertiser
from .pairing import Pairing
from .platforms import load_backend, prepare_process
from .server import ReflectServer

log = logging.getLogger(__name__)


class HelperApp:
    def __init__(self, *, port: int = DEFAULT_PORT, headless: bool = False, open_pairing: bool = False, backend=None):
        prepare_process()
        self.port = port
        self.headless = headless
        self.state = load_state()
        self.name = computer_name()
        self.backend = backend or load_backend()
        self.tray = None
        self.pairing = Pairing(self.state, on_change=self._refresh_tray)
        if open_pairing and not self.pairing.code:
            self.pairing.open_window()
        self.controller = Controller(self.backend, self.pairing, self.state, name=self.name)
        self.controller.add_listener(self._refresh_tray)
        self.server = ReflectServer(self.controller)
        self.advertiser: Advertiser | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._controller_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._lock = threading.Lock()
        self._shut_down = False
        self._stopped = threading.Event()
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        atexit.register(self.controller.restore_window_now)

    # -- helpers used by the tray ------------------------------------------------------
    def pairing_label(self) -> str:
        code = self.pairing.code
        return f"Pairing code: {code}" if code else "Pair a new phone…"

    def call(self, fn) -> None:
        """Run fn(controller) on the event loop thread."""
        if self.loop is not None:
            self.loop.call_soon_threadsafe(fn, self.controller)

    def _refresh_tray(self) -> None:
        if self.tray is not None:
            self.tray.refresh()

    # -- async lifecycle ------------------------------------------------------------------
    async def _start(self) -> None:
        self.loop = asyncio.get_running_loop()
        port = await self.server.start(self.port)
        self.advertiser = Advertiser(self.name, self.controller.helper_id, port, platform_name())
        await self.advertiser.start()
        self._controller_task = asyncio.create_task(self.controller.run())
        self._banner(port)

    async def _stop(self) -> None:
        try:
            await self.controller.shutdown()
        finally:
            await self.server.stop()
            if self.advertiser is not None:
                await self.advertiser.stop()
            if self._controller_task is not None:
                self._controller_task.cancel()
            self._stopped.set()
            log.info("Reflect stopped")

    def _banner(self, port: int) -> None:
        addresses = ", ".join(self.advertiser.addresses) if self.advertiser else "?"
        lines = [
            "",
            f"  Reflect helper {__version__} on {self.name}",
            f"  Listening on port {port} (local network only): {addresses or 'no network yet'}",
        ]
        code = self.pairing.code
        if code:
            lines.append(f"  Pairing code: {code}   <- type this on your iPhone the first time")
        else:
            names = ", ".join(d.get("name", "?") for d in self.pairing.devices())
            lines.append(f"  Paired phones: {names}. To add another, choose “Pair a new phone” in the tray menu.")
        problem = self.backend.permissions_problem()
        if problem:
            lines.append(f"  ! {problem}")
        lines.append("")
        print("\n".join(lines), flush=True)

    # -- running ------------------------------------------------------------------------------
    def run(self) -> int:
        self._install_console_handler()
        if sys.platform == "darwin":
            from .platforms.macos import request_permissions

            request_permissions()
        if self.headless:
            try:
                asyncio.run(self._run_headless())
            except KeyboardInterrupt:
                pass
            return 0
        thread = threading.Thread(target=self._loop_thread, name="reflect-loop", daemon=True)
        thread.start()
        self._ready.wait(30)
        if self._start_error is not None:
            raise self._start_error
        from .tray import Tray

        self.tray = Tray(self)
        try:
            self.tray.run()
        finally:
            self.shutdown()
        return 0

    async def _run_headless(self) -> None:
        self._stop_event = asyncio.Event()
        await self._start()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass  # Windows: asyncio.run turns Ctrl+C into cancellation instead
        try:
            await self._stop_event.wait()
        finally:
            await self._stop()

    def _loop_thread(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._start())
        except BaseException as exc:
            self._start_error = exc
            self._ready.set()
            return
        self._ready.set()
        loop.run_forever()
        loop.close()

    def shutdown(self) -> None:
        """Stop everything and restore Claude's window. Safe to call from any non-loop thread, twice."""
        with self._lock:
            if self._shut_down:
                return
            self._shut_down = True
        loop = self.loop
        if loop is not None and loop.is_running() and not self.headless:
            future = asyncio.run_coroutine_threadsafe(self._stop(), loop)
            try:
                future.result(timeout=8)
            except Exception:
                log.exception("clean shutdown failed; restoring the window directly")
                self.controller.restore_window_now()
            loop.call_soon_threadsafe(loop.stop)
        elif loop is not None and self.headless and self._stop_event is not None:
            loop.call_soon_threadsafe(self._stop_event.set)
            self._stopped.wait(8)
        else:
            self.controller.restore_window_now()

    def _install_console_handler(self) -> None:
        """Windows: closing the console window or Ctrl+C must still restore Claude's window."""
        if sys.platform != "win32":
            return
        import win32api
        import win32con

        def handler(ctrl_type: int) -> bool:
            if ctrl_type == win32con.CTRL_C_EVENT and self.headless:
                return False  # let Python raise KeyboardInterrupt
            self.shutdown()
            if self.tray is not None:
                self.tray.stop()
            return True

        win32api.SetConsoleCtrlHandler(handler, True)
