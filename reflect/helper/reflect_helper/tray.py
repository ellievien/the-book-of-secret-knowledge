"""System tray / menu bar icon (pystray)."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

if TYPE_CHECKING:
    from .app import HelperApp

log = logging.getLogger(__name__)

ACCENT = (217, 119, 87, 255)  # warm orange
IDLE = (140, 140, 140, 255)


def make_icon(connected: bool, size: int = 64) -> Image.Image:
    """A phone outline; filled with the accent colour while a phone is connected."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = ACCENT if connected else IDLE
    s = size / 64
    body = (18 * s, 4 * s, 46 * s, 60 * s)
    draw.rounded_rectangle(body, radius=7 * s, outline=color, width=max(2, round(4 * s)))
    if connected:
        draw.rounded_rectangle((23 * s, 10 * s, 41 * s, 50 * s), radius=3 * s, fill=color)
    draw.ellipse((29 * s, 52 * s, 35 * s, 58 * s), fill=color)
    return image


class Tray:
    def __init__(self, app: "HelperApp"):
        import pystray

        self.app = app
        self._connected = False
        menu = pystray.Menu(
            pystray.MenuItem(lambda _: f"Reflect — {app.controller.status_line()}", None, enabled=False),
            pystray.MenuItem(lambda _: app.pairing_label(), self._on_pair),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _: "Stop" if app.controller.streaming else "Start", self._on_toggle_streaming),
            pystray.MenuItem("Phone mode", self._on_toggle_phone_mode, checked=lambda _: app.controller.phone_mode),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )
        self.icon = pystray.Icon("Reflect", make_icon(False), "Reflect", menu)

    def run(self) -> None:
        self.icon.run(setup=self._setup)

    def _setup(self, icon) -> None:
        icon.visible = True
        if sys.platform == "darwin":
            self._observe_termination()
        code = self.app.pairing.code
        if code:
            self.notify(f"Pairing code: {code}")

    def _observe_termination(self) -> None:
        """Ctrl+C / logout terminate NSApp directly; restore the window before that happens."""
        import AppKit

        center = AppKit.NSNotificationCenter.defaultCenter()
        center.addObserverForName_object_queue_usingBlock_(
            AppKit.NSApplicationWillTerminateNotification, None, None, lambda _note: self.app.shutdown()
        )

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            pass

    def notify(self, message: str) -> None:
        try:
            self.icon.notify(message, "Reflect")
        except Exception:
            pass  # notifications are a convenience; the code is also in the menu and console

    def refresh(self) -> None:
        if sys.platform == "darwin":
            from PyObjCTools import AppHelper

            AppHelper.callAfter(self._refresh)
        else:
            self._refresh()

    def _refresh(self) -> None:
        connected = bool(self.app.controller.sessions)
        try:
            if connected != self._connected:
                self._connected = connected
                self.icon.icon = make_icon(connected)
            self.icon.title = f"Reflect — {self.app.controller.status_line()}"
            self.icon.update_menu()
        except Exception:
            log.debug("tray refresh failed", exc_info=True)

    # -- menu actions (tray thread) --------------------------------------------------
    def _on_pair(self, icon, item) -> None:
        code = self.app.pairing.code or self.app.pairing.open_window()
        print(f"\n  Pairing code: {code}\n", flush=True)
        self.notify(f"Pairing code: {code}")

    def _on_toggle_streaming(self, icon, item) -> None:
        self.app.call(lambda c: c.set_streaming(not c.streaming))

    def _on_toggle_phone_mode(self, icon, item) -> None:
        self.app.call(lambda c: c.set_phone_mode(not c.phone_mode))

    def _on_quit(self, icon, item) -> None:
        self.app.shutdown()
        icon.stop()
