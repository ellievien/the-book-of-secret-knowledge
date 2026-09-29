"""Turns phone input messages into pynput mouse/keyboard events on the Claude window."""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

from .geometry import normalized_to_point
from .platforms.base import Backend, WindowTarget
from .protocol import INPUT_KINDS, ProtocolError, number

log = logging.getLogger(__name__)

MAX_TYPE_LENGTH = 20_000
FOCUS_RECHECK_SECONDS = 0.4

MODIFIERS = {"ctrl", "shift", "alt", "cmd", "mod"}
NAMED_KEYS = {
    "enter", "esc", "backspace", "delete", "tab", "space", "up", "down", "left", "right",
    "home", "end", "page_up", "page_down", *(f"f{i}" for i in range(1, 13)),
}
ALIASES = {
    "return": "enter", "escape": "esc", "del": "delete", "control": "ctrl", "command": "cmd",
    "meta": "cmd", "win": "cmd", "super": "cmd", "option": "alt", "opt": "alt", "pageup": "page_up",
    "pgup": "page_up", "pagedown": "page_down", "pgdn": "page_down", "arrowup": "up",
    "arrowdown": "down", "arrowleft": "left", "arrowright": "right", "spacebar": "space",
}


def parse_combo(spec: str) -> tuple[list[str], str]:
    """"ctrl+v" -> (["ctrl"], "v"); "enter" -> ([], "enter"). "mod" means cmd on macOS, ctrl elsewhere."""
    if not isinstance(spec, str) or not spec.strip():
        raise ProtocolError("bad_input", "'key' must be a non-empty string")
    spec = spec.strip()
    if spec == "+":
        parts, key = [], "+"
    elif spec.endswith("++"):
        head = spec[:-2]
        parts, key = (head.split("+") if head else []), "+"
    else:
        *parts, key = spec.split("+")
    mods = [ALIASES.get(p.strip().lower(), p.strip().lower()) for p in parts]
    key = key if len(key) == 1 else ALIASES.get(key.strip().lower(), key.strip().lower())
    for mod in mods:
        if mod not in MODIFIERS:
            raise ProtocolError("bad_input", f"unknown modifier {mod!r}")
    if len(key) != 1 and key not in NAMED_KEYS and key not in MODIFIERS:
        raise ProtocolError("bad_input", f"unknown key {key!r}")
    return mods, key


def platform_modifier(name: str, platform: str = sys.platform) -> str:
    if name == "mod":
        return "cmd" if platform == "darwin" else "ctrl"
    return name


def validate_input(msg: dict[str, Any]) -> dict[str, Any]:
    """Check an input message before it is queued; returns a normalized copy."""
    kind = msg.get("kind")
    if kind not in INPUT_KINDS:
        raise ProtocolError("bad_input", f"unknown input kind {kind!r}")
    event: dict[str, Any] = {"kind": kind}
    if kind in ("tap", "long_press", "scroll"):
        event["x"] = number(msg, "x")
        event["y"] = number(msg, "y")
    if kind == "scroll":
        event["dx"] = number(msg, "dx", 0.0)
        event["dy"] = number(msg, "dy", 0.0)
    if kind == "type":
        text = msg.get("text")
        if not isinstance(text, str):
            raise ProtocolError("bad_input", "'text' must be a string")
        if len(text) > MAX_TYPE_LENGTH:
            raise ProtocolError("bad_input", f"text longer than {MAX_TYPE_LENGTH} characters")
        event["text"] = text
    if kind == "key":
        event["mods"], event["key"] = parse_combo(msg.get("key", ""))
    return event


def coalesce(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge back-to-back scroll events so a slow machine never lags behind the finger."""
    merged: list[dict[str, Any]] = []
    for event in events:
        if merged and event["kind"] == "scroll" and merged[-1]["kind"] == "scroll":
            last = merged[-1]
            merged[-1] = {**last, "dx": last["dx"] + event["dx"], "dy": last["dy"] + event["dy"]}
        else:
            merged.append(event)
    return merged


class InputInjector:
    def __init__(self, backend: Backend):
        from pynput import keyboard, mouse

        self._backend = backend
        self._mouse = mouse.Controller()
        self._keyboard = keyboard.Controller()
        self._button = mouse.Button
        self._key = keyboard.Key
        if sys.platform == "darwin" and hasattr(self._mouse, "_SCROLL_SPEED"):
            self._mouse._SCROLL_SPEED = 1  # 1-point scroll steps instead of 10
        self._focused_at = 0.0

    def _focus(self, target: WindowTarget, kind: str) -> None:
        now = time.monotonic()
        if kind == "scroll" and now - self._focused_at < FOCUS_RECHECK_SECONDS:
            return
        self._backend.bring_to_front(target)
        self._focused_at = now

    def perform(self, target: WindowTarget, event: dict[str, Any]) -> None:
        kind = event["kind"]
        self._focus(target, kind)
        if kind in ("tap", "long_press", "scroll"):
            target = self._backend.refresh(target) or target
            x, y = normalized_to_point(target.bounds, event["x"], event["y"])
            if self._mouse.position != (x, y):
                self._mouse.position = (x, y)
                time.sleep(0.012)
        if kind == "tap":
            self._mouse.click(self._button.left)
        elif kind == "long_press":
            self._mouse.click(self._button.right)
        elif kind == "scroll":
            width, height = target.logical_size
            self._backend.scroll(self._mouse, target, event["dx"] * width, event["dy"] * height)
        elif kind == "type":
            self._type(event["text"])
        elif kind == "key":
            self._press(event["mods"], event["key"])

    def _type(self, text: str) -> None:
        # Enter would send the message in Claude's composer, so newlines become Shift+Enter.
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
        for index, line in enumerate(text.split("\n")):
            if index:
                self._press(["shift"], "enter")
            if line:
                self._keyboard.type(line)

    def _resolve(self, name: str):
        name = platform_modifier(name)
        if len(name) == 1:
            return name
        return getattr(self._key, name)

    def _press(self, mods: list[str], key: str) -> None:
        held = [self._resolve(m) for m in mods]
        for mod in held:
            self._keyboard.press(mod)
        try:
            target = self._resolve(key)
            self._keyboard.press(target)
            self._keyboard.release(target)
        finally:
            for mod in reversed(held):
                self._keyboard.release(mod)
