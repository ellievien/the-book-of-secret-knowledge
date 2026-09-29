"""Windows backend.

* Window discovery: pywin32 EnumWindows, keeping Electron top-level windows
  whose process image is claude.exe.
* DPI: the process is made per-monitor DPI aware (v2) before anything else,
  so every coordinate here (window rects, cursor, SendInput) is in physical
  pixels and matches the captured image 1:1 at any Windows scaling level.
* Capture: Windows.Graphics.Capture through the windows-capture package,
  falling back to an mss screen-region copy.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import threading
import time

import win32api
import win32con
import win32gui
import win32process

from ..config import PHONE_WIDTH
from ..frames import RawFrame
from ..geometry import Rect, clamp, crop_box, fit_phone_size, min_width_fallback
from .base import WindowTarget
from .common import MssGrabber, ScrollAccumulator

log = logging.getLogger(__name__)

PROCESS_NAME = "claude.exe"
ELECTRON_CLASS = "Chrome_WidgetWin_1"
MIN_WINDOW_SIDE = 150

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ACCESS_DENIED = 5
MOUSEEVENTF_MOVE = 0x0001
SWP_FLAGS = win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE | win32con.SWP_NOOWNERZORDER

# Chromium scrolls 100 DIPs per 120-unit wheel notch and honours smaller deltas.
WHEEL_UNITS_PER_PIXEL = 120 / 100

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.GetCurrentThreadId.restype = wt.DWORD
dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowPos.restype = wt.BOOL
user32.IsZoomed.argtypes = [wt.HWND]
user32.IsZoomed.restype = wt.BOOL


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


DPI_MODE = "not set"


def enable_dpi_awareness() -> str:
    """Per-monitor v2 awareness: physical pixels everywhere, no OS scaling surprises."""
    global DPI_MODE
    try:
        set_context = user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = wt.BOOL
        if set_context(ctypes.c_void_p(-4)):  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            DPI_MODE = "per-monitor-v2"
            return DPI_MODE
        if ctypes.get_last_error() == ERROR_ACCESS_DENIED:
            DPI_MODE = "already set"
            return DPI_MODE
    except AttributeError:
        pass
    try:
        if ctypes.WinDLL("shcore").SetProcessDpiAwareness(2) == 0:  # PROCESS_PER_MONITOR_DPI_AWARE
            DPI_MODE = "per-monitor"
            return DPI_MODE
    except (AttributeError, OSError):
        pass
    user32.SetProcessDPIAware()
    DPI_MODE = "system"
    return DPI_MODE


# -- small Win32 helpers ----------------------------------------------------------------


def _process_name(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _window_rect(hwnd: int) -> Rect:
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return Rect(left, top, right - left, bottom - top)


def _visible_rect(hwnd: int) -> Rect:
    """Window rectangle without the invisible resize borders (DWM extended frame bounds)."""
    rect = wt.RECT()
    hr = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect))
    if hr != 0:
        return _window_rect(hwnd)
    return Rect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)


def _is_cloaked(hwnd: int) -> bool:
    value = wt.DWORD(0)
    hr = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
    return hr == 0 and value.value != 0


def _dpi_scale(hwnd: int) -> float:
    try:
        dpi = user32.GetDpiForWindow(wt.HWND(hwnd))
    except AttributeError:
        dpi = 0
    return (dpi or 96) / 96.0


def _work_area(hwnd: int) -> Rect:
    monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
    left, top, right, bottom = win32api.GetMonitorInfo(monitor)["Work"]
    return Rect(left, top, right - left, bottom - top)


def _nudge_input() -> None:
    """Inject a zero-length mouse move: the last-input process may take the foreground."""
    event = INPUT(type=0, u=_INPUTUNION(mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_MOVE, 0, 0)))
    user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))


def _wheel_steps(units: int) -> float:
    # pynput sends int(steps * 120); nudge away from zero so float rounding never loses a unit.
    return units / 120 + (1e-9 if units > 0 else -1e-9)


# -- capture ---------------------------------------------------------------------------------


class WgcGrabber:
    """Windows.Graphics.Capture session for one window (runs on its own thread)."""

    description = "Windows.Graphics.Capture"

    def __init__(self, hwnd: int, fps: int):
        from windows_capture import WindowsCapture

        self.hwnd = hwnd
        self.closed = False
        self._stopping = False
        self._lock = threading.Lock()
        self._frame: RawFrame | None = None
        self._fresh = False
        self._control = None
        interval = max(1, int(1000 / max(1, fps)))
        # Newer options first; older Windows builds reject some of them.
        option_sets = [
            dict(cursor_capture=False, draw_border=False, minimum_update_interval=interval),
            dict(cursor_capture=False, draw_border=False),
            dict(cursor_capture=False),
            dict(),
        ]
        errors = []
        for options in option_sets:
            self.closed = False
            try:
                capture = WindowsCapture(window_hwnd=hwnd, **options)
                capture.event(self.on_frame_arrived)
                capture.event(self.on_closed)
                control = capture.start_free_threaded()
            except Exception as exc:  # the native layer raises plain Exceptions
                errors.append(f"{options}: {exc}")
                continue
            # Unsupported options make the capture thread exit straight away.
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not control.is_finished() and not self._has_frame():
                time.sleep(0.02)
            if control.is_finished():
                try:
                    control.wait()
                    errors.append(f"{options}: capture thread ended")
                except Exception as exc:
                    errors.append(f"{options}: {exc}")
                continue
            self._capture = capture
            self._control = control
            log.info("Windows.Graphics.Capture started with %s", options or "default options")
            return
        raise RuntimeError("Windows.Graphics.Capture could not start: " + " | ".join(errors))

    def _has_frame(self) -> bool:
        with self._lock:
            return self._frame is not None

    def on_frame_arrived(self, frame, capture_control) -> None:
        if self._stopping:
            capture_control.stop()
            return
        buffer = frame.frame_buffer  # (height, width, 4) BGRA, only valid during this call
        height, width = buffer.shape[:2]
        if width < 16 or height < 16:
            return  # minimized
        try:
            box = crop_box(width, height, _window_rect(self.hwnd), _visible_rect(self.hwnd))
        except Exception:
            box = None
        if box:
            left, top, right, bottom = box
            buffer = buffer[top:bottom, left:right]
            height, width = buffer.shape[:2]
        raw = RawFrame(width, height, buffer.tobytes(), width * 4, "BGRX")
        with self._lock:
            self._frame = raw
            self._fresh = True

    def on_closed(self) -> None:
        self.closed = True

    def grab(self, target: WindowTarget) -> RawFrame | None:
        if self._control is not None and self._control.is_finished():
            self.closed = True
        with self._lock:
            if not self._fresh:
                return None
            self._fresh = False
            return self._frame

    def close(self) -> None:
        self._stopping = True
        self.closed = True
        if self._control is not None:
            try:
                self._control.stop()
            except Exception:
                pass


# -- backend ------------------------------------------------------------------------------------


class WindowsBackend:
    name = "windows"

    def __init__(self) -> None:
        self._saved_placements: dict[int, tuple] = {}
        self._scroll = ScrollAccumulator()
        self._wgc_failed = False

    # discovery ------------------------------------------------------------
    def _candidates(self) -> list[tuple[int, int]]:
        names: dict[int, str] = {}
        found: list[tuple[int, int]] = []

        def visit(hwnd, _):
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                if win32gui.GetClassName(hwnd) != ELECTRON_CLASS:
                    return True
                if win32gui.GetWindow(hwnd, win32con.GW_OWNER):
                    return True
                if win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW:
                    return True
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid not in names:
                    names[pid] = _process_name(pid)
                if names[pid] != PROCESS_NAME or _is_cloaked(hwnd):
                    return True
                found.append((hwnd, pid))
            except win32gui.error:
                pass  # window vanished while we looked at it
            return True

        win32gui.EnumWindows(visit, None)
        return found

    def _target(self, hwnd: int, pid: int) -> WindowTarget:
        return WindowTarget(
            handle=hwnd,
            pid=pid,
            bounds=_visible_rect(hwnd),
            scale=_dpi_scale(hwnd),
            title=win32gui.GetWindowText(hwnd),
            extra={"physical": True, "iconic": bool(win32gui.IsIconic(hwnd))},
        )

    def find_claude_window(self) -> WindowTarget | None:
        best = None
        best_key = None
        for hwnd, pid in self._candidates():
            iconic = bool(win32gui.IsIconic(hwnd))
            if iconic:
                placement = win32gui.GetWindowPlacement(hwnd)
                left, top, right, bottom = placement[4]
                area = (right - left) * (bottom - top)
            else:
                rect = _visible_rect(hwnd)
                if rect.w < MIN_WINDOW_SIDE or rect.h < MIN_WINDOW_SIDE:
                    continue
                area = rect.w * rect.h
            key = (not iconic, area)
            if best_key is None or key > best_key:
                best, best_key = (hwnd, pid), key
        if best is None:
            return None
        hwnd, pid = best
        if win32gui.IsIconic(hwnd):
            # A minimized window produces no frames; bring it back.
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.3)
        return self._target(hwnd, pid)

    def is_claude_running(self) -> bool:
        for pid in win32process.EnumProcesses():
            if pid and _process_name(pid) == PROCESS_NAME:
                return True
        return False

    def refresh(self, target: WindowTarget) -> WindowTarget | None:
        hwnd = target.handle
        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return None  # gone, hidden or minimized: find_claude_window() will restore it
        return self._target(hwnd, target.pid)

    # focus ----------------------------------------------------------------------
    def bring_to_front(self, target: WindowTarget) -> None:
        hwnd = target.handle
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        if user32.GetForegroundWindow() == hwnd:
            return
        _nudge_input()
        user32.SetForegroundWindow(hwnd)
        if user32.GetForegroundWindow() == hwnd:
            return
        foreground = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        own_thread = kernel32.GetCurrentThreadId()
        if fg_thread and fg_thread != own_thread:
            user32.AttachThreadInput(own_thread, fg_thread, True)
            try:
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                user32.AttachThreadInput(own_thread, fg_thread, False)
        if user32.GetForegroundWindow() != hwnd:
            log.debug("Windows refused to bring Claude to the front; the click will focus it")

    # phone mode ------------------------------------------------------------------
    def _set_visible_size(self, hwnd: int, width: int, height: int, work: Rect) -> None:
        outer = _window_rect(hwnd)
        visible = _visible_rect(hwnd)
        left_border = visible.x - outer.x
        top_border = visible.y - outer.y
        right_border = outer.right - visible.right
        bottom_border = outer.bottom - visible.bottom
        x = clamp(visible.x, work.x, max(work.x, work.right - width))
        y = clamp(visible.y, work.y, max(work.y, work.bottom - height))
        ok = user32.SetWindowPos(
            hwnd,
            None,
            int(x - left_border),
            int(y - top_border),
            int(width + left_border + right_border),
            int(height + top_border + bottom_border),
            SWP_FLAGS,
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        time.sleep(0.15)  # let Electron apply its minimum size and relayout

    def enter_phone_mode(self, target: WindowTarget, aspect: float) -> None:
        hwnd = target.handle
        if hwnd not in self._saved_placements:
            self._saved_placements[hwnd] = win32gui.GetWindowPlacement(hwnd)
        if win32gui.IsIconic(hwnd) or user32.IsZoomed(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.3)
        scale = _dpi_scale(hwnd)
        work = _work_area(hwnd)
        width, height = fit_phone_size(PHONE_WIDTH * scale, aspect, work.h)
        self._set_visible_size(hwnd, width, height, work)
        actual = _visible_rect(hwnd)
        if actual.w > width + 2:
            log.info("Claude's minimum width is %d px; keeping the phone aspect as close as possible", actual.w)
            width, height = min_width_fallback(actual.w, aspect, work.h)
            self._set_visible_size(hwnd, width, height, work)
            actual = _visible_rect(hwnd)
        log.info(
            "Phone mode: window is %dx%d px (%dx%d logical at %.0f%% scaling)",
            actual.w, actual.h, round(actual.w / scale), round(actual.h / scale), scale * 100,
        )

    def exit_phone_mode(self, target: WindowTarget | None) -> None:
        for hwnd, placement in list(self._saved_placements.items()):
            try:
                if win32gui.IsWindow(hwnd):
                    win32gui.SetWindowPlacement(hwnd, placement)
                    log.info("Restored Claude's original window size")
            except win32gui.error as exc:
                log.warning("Could not restore window size: %s", exc)
        self._saved_placements.clear()

    # capture --------------------------------------------------------------------
    def open_capture(self, target: WindowTarget, fps: int):
        if not self._wgc_failed:
            try:
                return WgcGrabber(target.handle, fps)
            except Exception as exc:
                self._wgc_failed = True
                log.warning("%s; falling back to screen-region capture", exc)
        return MssGrabber()

    # input ------------------------------------------------------------------------
    def scroll(self, mouse, target: WindowTarget, dx_logical: float, dy_logical: float) -> None:
        # Wheel "up" (positive) moves content down, like dragging a finger down.
        units_x, units_y = self._scroll.take(-dx_logical * WHEEL_UNITS_PER_PIXEL, dy_logical * WHEEL_UNITS_PER_PIXEL)
        if units_y:
            mouse.scroll(0, _wheel_steps(units_y))
        if units_x:
            mouse.scroll(_wheel_steps(units_x), 0)

    def permissions_problem(self) -> str | None:
        return None
