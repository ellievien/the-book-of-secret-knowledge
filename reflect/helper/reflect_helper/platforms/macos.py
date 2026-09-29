"""macOS backend.

* Window discovery: Quartz CGWindowListCopyWindowInfo, owner "Claude".
* Coordinates: everything is in points (global display space, top-left
  origin), which is also what pynput's CGEvents use, so Retina scaling only
  matters for the capture size.
* Resize / focus: Accessibility API (needs the Accessibility permission).
* Capture: ScreenCaptureKit (SCScreenshotManager, macOS 14+), falling back to
  an mss screen-region copy.
"""

from __future__ import annotations

import logging
import threading
import time

import AppKit
import ApplicationServices as AS
import Quartz

from ..config import PHONE_WIDTH
from ..frames import RawFrame
from ..geometry import Rect, clamp, fit_phone_size, min_width_fallback
from .base import WindowTarget
from .common import MssGrabber, ScrollAccumulator

log = logging.getLogger(__name__)

OWNER_NAME = "Claude"
BUNDLE_ID = "com.anthropic.claudefordesktop"
MIN_WINDOW_SIDE = 150
PIXEL_FORMAT_BGRA = 0x42475241  # kCVPixelFormatType_32BGRA ('BGRA')
AX_ERROR_API_DISABLED = -25211

ON_SCREEN = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements


def init_window_server_connection() -> None:
    """A command-line process must touch CoreGraphics/AppKit before ScreenCaptureKit works."""
    Quartz.CGMainDisplayID()
    app = AppKit.NSApplication.sharedApplication()
    # Menu-bar helper: no Dock icon, never steals focus by itself.
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)


def request_permissions() -> None:
    """Show the macOS permission prompts once (no-op when already granted)."""
    try:
        if not Quartz.CGPreflightScreenCaptureAccess():
            Quartz.CGRequestScreenCaptureAccess()
    except AttributeError:
        pass
    AS.AXIsProcessTrustedWithOptions({AS.kAXTrustedCheckOptionPrompt: True})


# -- window list ----------------------------------------------------------------------


def _infos(option: int, window_id: int = 0) -> list:
    return list(Quartz.CGWindowListCopyWindowInfo(option, window_id or Quartz.kCGNullWindowID) or [])


def _bounds(info) -> Rect:
    b = info[Quartz.kCGWindowBounds]
    return Rect(float(b["X"]), float(b["Y"]), float(b["Width"]), float(b["Height"]))


def _claude_pids() -> set[int]:
    apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE_ID) or []
    return {int(app.processIdentifier()) for app in apps}


def _is_claude(info, pids: set[int]) -> bool:
    return info.get(Quartz.kCGWindowOwnerName) == OWNER_NAME or int(info.get(Quartz.kCGWindowOwnerPID, -1)) in pids


def _is_main_window(info) -> bool:
    if int(info.get(Quartz.kCGWindowLayer, 1)) != 0:
        return False
    b = _bounds(info)
    return b.w >= MIN_WINDOW_SIDE and b.h >= MIN_WINDOW_SIDE


def _display_scale(rect: Rect) -> float:
    err, displays, count = Quartz.CGGetDisplaysWithRect(Quartz.CGRectMake(rect.x, rect.y, rect.w, rect.h), 8, None, None)
    display = displays[0] if (err == 0 and count) else Quartz.CGMainDisplayID()
    mode = Quartz.CGDisplayCopyDisplayMode(display)
    points = Quartz.CGDisplayModeGetWidth(mode)
    return Quartz.CGDisplayModeGetPixelWidth(mode) / points if points else 2.0


def _visible_frame(rect: Rect) -> Rect:
    """Usable area (no menu bar / Dock) of the screen holding rect, in top-left global points."""
    screens = list(AppKit.NSScreen.screens() or [])
    if screens:
        primary_height = screens[0].frame().size.height
        cx, cy = rect.x + rect.w / 2, rect.y + rect.h / 2
        chosen = screens[0]
        for screen in screens:
            f = screen.frame()
            top = primary_height - (f.origin.y + f.size.height)
            if f.origin.x <= cx < f.origin.x + f.size.width and top <= cy < top + f.size.height:
                chosen = screen
                break
        v = chosen.visibleFrame()
        return Rect(v.origin.x, primary_height - (v.origin.y + v.size.height), v.size.width, v.size.height)
    b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    return Rect(b.origin.x, b.origin.y + 40, b.size.width, b.size.height - 40)


# -- accessibility ------------------------------------------------------------------------


def _ax_get(element, attribute):
    err, value = AS.AXUIElementCopyAttributeValue(element, attribute, None)
    if err == AX_ERROR_API_DISABLED:
        raise PermissionError("Accessibility permission is missing")
    return value if err == 0 else None


def _ax_point(element) -> tuple[float, float] | None:
    value = _ax_get(element, AS.kAXPositionAttribute)
    if value is None:
        return None
    ok, point = AS.AXValueGetValue(value, AS.kAXValueCGPointType, None)
    return (point.x, point.y) if ok else None


def _ax_size(element) -> tuple[float, float] | None:
    value = _ax_get(element, AS.kAXSizeAttribute)
    if value is None:
        return None
    ok, size = AS.AXValueGetValue(value, AS.kAXValueCGSizeType, None)
    return (size.width, size.height) if ok else None


def _ax_set_frame(element, x: float, y: float, width: float, height: float) -> None:
    position = AS.AXValueCreate(AS.kAXValueCGPointType, Quartz.CGPoint(x, y))
    size = AS.AXValueCreate(AS.kAXValueCGSizeType, Quartz.CGSize(width, height))
    # Size, position, size: macOS clamps a size that would not fit at the old position.
    errors = [
        AS.AXUIElementSetAttributeValue(element, AS.kAXSizeAttribute, size),
        AS.AXUIElementSetAttributeValue(element, AS.kAXPositionAttribute, position),
        AS.AXUIElementSetAttributeValue(element, AS.kAXSizeAttribute, size),
    ]
    if AX_ERROR_API_DISABLED in errors:
        raise PermissionError("Accessibility permission is missing")
    time.sleep(0.15)


def _ax_window(pid: int, bounds: Rect):
    """The Accessibility element of the window whose frame matches bounds."""
    app = AS.AXUIElementCreateApplication(pid)
    windows = _ax_get(app, AS.kAXWindowsAttribute) or []
    fallback = None
    for window in windows:
        point, size = _ax_point(window), _ax_size(window)
        if point is None or size is None:
            continue
        if abs(point[0] - bounds.x) <= 2 and abs(point[1] - bounds.y) <= 2 and abs(size[0] - bounds.w) <= 2 and abs(size[1] - bounds.h) <= 2:
            return window
        if fallback is None and size[0] >= MIN_WINDOW_SIDE:
            fallback = window
    return fallback


# -- capture ------------------------------------------------------------------------------


def _wait(start, timeout: float, what: str):
    done = threading.Event()
    box: dict = {}

    def handler(result, error):
        box["result"], box["error"] = result, error
        done.set()

    start(handler)
    if not done.wait(timeout):
        raise TimeoutError(f"{what} did not answer (is Screen Recording allowed?)")
    if box["error"] is not None:
        raise RuntimeError(f"{what}: {box['error'].localizedDescription()}")
    return box["result"]


def _cgimage_to_raw(image) -> RawFrame:
    width = Quartz.CGImageGetWidth(image)
    height = Quartz.CGImageGetHeight(image)
    stride = Quartz.CGImageGetBytesPerRow(image)
    data = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(image)))
    little = (Quartz.CGImageGetBitmapInfo(image) & Quartz.kCGBitmapByteOrderMask) == Quartz.kCGBitmapByteOrder32Little
    alpha_first = Quartz.CGImageGetAlphaInfo(image) in (
        Quartz.kCGImageAlphaPremultipliedFirst,
        Quartz.kCGImageAlphaFirst,
        Quartz.kCGImageAlphaNoneSkipFirst,
    )
    if little:
        mode = "BGRX" if alpha_first else "XBGR"
    else:
        mode = "XRGB" if alpha_first else "RGBX"
    return RawFrame(width, height, data, stride, mode)


class SckGrabber:
    """ScreenCaptureKit screenshot of one window (works even when it is covered)."""

    description = "ScreenCaptureKit"

    def __init__(self, target: WindowTarget):
        import ScreenCaptureKit as SCK

        if not hasattr(SCK, "SCScreenshotManager"):
            raise RuntimeError("ScreenCaptureKit screenshots need macOS 14 or newer")
        self._sck = SCK
        self.closed = False
        content = _wait(
            lambda h: SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                True, True, h
            ),
            5,
            "ScreenCaptureKit",
        )
        window = next((w for w in content.windows() if int(w.windowID()) == target.handle), None)
        if window is None:
            raise RuntimeError("ScreenCaptureKit cannot see the Claude window")
        self._filter = SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(window)
        config = SCK.SCStreamConfiguration.alloc().init()
        config.setShowsCursor_(False)
        config.setPixelFormat_(PIXEL_FORMAT_BGRA)
        if config.respondsToSelector_(b"setIgnoreShadowsSingleWindow:"):
            config.setIgnoreShadowsSingleWindow_(True)
        if config.respondsToSelector_(b"setCaptureResolution:"):
            config.setCaptureResolution_(SCK.SCCaptureResolutionBest)
        self._config = config

    def grab(self, target: WindowTarget) -> RawFrame | None:
        self._config.setWidth_(max(1, round(target.bounds.w * target.scale)))
        self._config.setHeight_(max(1, round(target.bounds.h * target.scale)))
        try:
            image = _wait(
                lambda h: self._sck.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
                    self._filter, self._config, h
                ),
                3,
                "ScreenCaptureKit",
            )
        except Exception:
            self.closed = True
            raise
        if image is None:
            return None
        return _cgimage_to_raw(image)

    def close(self) -> None:
        self.closed = True


# -- backend --------------------------------------------------------------------------------------


class MacBackend:
    name = "macos"

    def __init__(self) -> None:
        self._saved: dict[int, tuple[int, Rect]] = {}
        self._scroll = ScrollAccumulator()
        self._sck_failed = False
        self._last_unhide = 0.0

    def _target(self, info) -> WindowTarget:
        bounds = _bounds(info)
        return WindowTarget(
            handle=int(info[Quartz.kCGWindowNumber]),
            pid=int(info[Quartz.kCGWindowOwnerPID]),
            bounds=bounds,
            scale=_display_scale(bounds),
            title=str(info.get(Quartz.kCGWindowName) or ""),
            extra={"physical": False},
        )

    def find_claude_window(self) -> WindowTarget | None:
        pids = _claude_pids()
        windows = [i for i in _infos(ON_SCREEN) if _is_claude(i, pids) and _is_main_window(i)]
        if windows:
            best = max(windows, key=lambda i: _bounds(i).w * _bounds(i).h)
            return self._target(best)
        self._unhide_claude(pids)
        return None

    def _unhide_claude(self, pids: set[int]) -> None:
        """Claude is running but its window is minimized or hidden: bring it back (at most every 5 s)."""
        if time.monotonic() - self._last_unhide < 5:
            return
        self._last_unhide = time.monotonic()
        pids |= {int(i[Quartz.kCGWindowOwnerPID]) for i in _infos(Quartz.kCGWindowListOptionAll) if _is_claude(i, set())}
        for pid in pids:
            app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if app is not None and app.isHidden():
                app.unhide()
            try:
                ax_app = AS.AXUIElementCreateApplication(pid)
                for window in _ax_get(ax_app, AS.kAXWindowsAttribute) or []:
                    if _ax_get(window, AS.kAXMinimizedAttribute):
                        AS.AXUIElementSetAttributeValue(window, AS.kAXMinimizedAttribute, False)
            except PermissionError:
                pass

    def is_claude_running(self) -> bool:
        if _claude_pids():
            return True
        return any(_is_claude(i, set()) for i in _infos(Quartz.kCGWindowListOptionAll))

    def refresh(self, target: WindowTarget) -> WindowTarget | None:
        infos = _infos(Quartz.kCGWindowListOptionIncludingWindow, target.handle)
        if not infos or not infos[0].get(Quartz.kCGWindowIsOnscreen, False):
            return None
        return self._target(infos[0])

    # focus -----------------------------------------------------------------------------
    def _is_frontmost(self, target: WindowTarget) -> bool:
        for info in _infos(ON_SCREEN):  # ordered front to back
            if int(info.get(Quartz.kCGWindowLayer, 1)) == 0:
                return int(info[Quartz.kCGWindowOwnerPID]) == target.pid
        return False

    def bring_to_front(self, target: WindowTarget) -> None:
        if self._is_frontmost(target):
            return
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(target.pid)
        if app is not None:
            app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
        try:
            ax_app = AS.AXUIElementCreateApplication(target.pid)
            AS.AXUIElementSetAttributeValue(ax_app, AS.kAXFrontmostAttribute, True)
            window = _ax_window(target.pid, target.bounds)
            if window is not None:
                AS.AXUIElementPerformAction(window, AS.kAXRaiseAction)
        except PermissionError:
            log.warning("Allow Accessibility for the app running Reflect to control Claude")
        time.sleep(0.12)

    # phone mode ------------------------------------------------------------------------
    def enter_phone_mode(self, target: WindowTarget, aspect: float) -> None:
        window = _ax_window(target.pid, target.bounds)
        if window is None:
            raise RuntimeError("Accessibility cannot see the Claude window")
        if _ax_get(window, "AXFullScreen"):
            AS.AXUIElementSetAttributeValue(window, "AXFullScreen", False)
            time.sleep(1.0)  # leave the full-screen Space animation
            refreshed = self.refresh(target)
            if refreshed is not None:
                target = refreshed
        if target.handle not in self._saved:
            self._saved[target.handle] = (target.pid, target.bounds)
        area = _visible_frame(target.bounds)
        width, height = fit_phone_size(PHONE_WIDTH, aspect, area.h)
        x = clamp(target.bounds.x, area.x, max(area.x, area.right - width))
        y = clamp(target.bounds.y, area.y, max(area.y, area.bottom - height))
        _ax_set_frame(window, x, y, width, height)
        actual = _ax_size(window)
        if actual and actual[0] > width + 2:
            log.info("Claude's minimum width is %d pt; keeping the phone aspect as close as possible", actual[0])
            width, height = min_width_fallback(actual[0], aspect, area.h)
            y = clamp(target.bounds.y, area.y, max(area.y, area.bottom - height))
            _ax_set_frame(window, x, y, width, height)
            actual = _ax_size(window)
        log.info("Phone mode: window is %s pt", actual)

    def exit_phone_mode(self, target: WindowTarget | None) -> None:
        for handle, (pid, original) in list(self._saved.items()):
            current = self.refresh(WindowTarget(handle, pid, original, 1.0)) if handle else None
            window = _ax_window(pid, current.bounds if current else original)
            if window is not None:
                try:
                    _ax_set_frame(window, original.x, original.y, original.w, original.h)
                    log.info("Restored Claude's original window size")
                except PermissionError as exc:
                    log.warning("Could not restore window size: %s", exc)
        self._saved.clear()

    # capture -------------------------------------------------------------------------------
    def open_capture(self, target: WindowTarget, fps: int):
        if not self._sck_failed:
            try:
                return SckGrabber(target)
            except Exception as exc:
                self._sck_failed = True
                log.warning("ScreenCaptureKit unavailable (%s); falling back to screen-region capture", exc)
        return MssGrabber()

    # input ---------------------------------------------------------------------------------
    def scroll(self, mouse, target: WindowTarget, dx_logical: float, dy_logical: float) -> None:
        # pynput posts pixel-unit scroll events of _SCROLL_SPEED points per step (set to 1 by the injector).
        speed = getattr(mouse, "_SCROLL_SPEED", 1) or 1
        steps_x, steps_y = self._scroll.take(dx_logical / speed, dy_logical / speed)
        if steps_x or steps_y:
            mouse.scroll(steps_x, steps_y)

    def permissions_problem(self) -> str | None:
        missing = []
        try:
            if not Quartz.CGPreflightScreenCaptureAccess():
                missing.append("Screen Recording")
        except AttributeError:
            pass
        if not AS.AXIsProcessTrusted():
            missing.append("Accessibility")
        if not missing:
            return None
        return (
            "macOS permission needed: " + " and ".join(missing)
            + ". Open System Settings → Privacy & Security and switch it on for the app that runs Reflect"
            " (Terminal, or sshd-keygen-wrapper when started over SSH), then restart Reflect."
        )
