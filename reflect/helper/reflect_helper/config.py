"""Constants, per-user storage location and the persisted helper state."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

APP_NAME = "Reflect"
SERVICE_TYPE = "_reflect._tcp.local."
DEFAULT_PORT = 47800
PORT_ATTEMPTS = 10

# Phone mode target, in logical pixels (points on macOS).
PHONE_WIDTH = 440
PHONE_HEIGHT = 956

DEFAULT_FPS = 15
MAX_FPS = 15
DEFAULT_QUALITY = 70
# Longest edge of a streamed frame, in pixels. Keeps huge windows affordable.
MAX_FRAME_EDGE = 2200


def platform_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def state_dir() -> Path:
    """%APPDATA%\\Reflect on Windows, ~/Library/Application Support/Reflect on macOS."""
    override = os.environ.get("REFLECT_HOME")
    if override:
        path = Path(override)
    elif sys.platform == "win32":
        path = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "reflect"
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o700)
    return path


def computer_name() -> str:
    """Friendly name shown on the phone ("Ellie's MacBook Pro", "DESKTOP-1234")."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=3
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    if sys.platform == "win32" and os.environ.get("COMPUTERNAME"):
        return os.environ["COMPUTERNAME"]
    return socket.gethostname().split(".")[0] or "Computer"


class State:
    """Small JSON document persisted atomically in the state directory."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                # A corrupt file must not stop the helper; keep a copy for inspection.
                path.replace(path.with_suffix(".corrupt.json"))
                self._data = {}
        if "helper_id" not in self._data:
            self._data["helper_id"] = uuid.uuid4().hex
            self._save()

    @property
    def path(self) -> Path:
        return self._path

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            value = self._data.get(key, default)
            return json.loads(json.dumps(value)) if isinstance(value, (dict, list)) else value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if value is None:
                self._data.pop(key, None)
            else:
                self._data[key] = value
            self._save()

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), "utf-8")
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)


def load_state() -> State:
    return State(state_dir() / "state.json")
