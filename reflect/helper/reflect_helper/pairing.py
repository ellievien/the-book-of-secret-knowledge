"""One-time 6-digit pairing codes and per-phone tokens."""

from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .config import State
from .protocol import auth_proof, b64, unb64

log = logging.getLogger(__name__)

CODE_WINDOW_SECONDS = 10 * 60
MAX_FAILURES = 5
LOCKOUT_SECONDS = 30


class PairingError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Credentials:
    device_id: str
    token: str  # base64 of 32 random bytes


class Pairing:
    """Owns the pairing code and the list of paired phones.

    A code is open while no phone is paired, or for 10 minutes after the user
    asks for a new one. It is single use; 5 wrong guesses rotate it and pause
    pairing for 30 s, which makes guessing over the LAN impractical.
    """

    def __init__(self, state: State, on_change: Callable[[], None] | None = None):
        self._state = state
        self._lock = threading.Lock()
        self._on_change = on_change
        self._code: str | None = None
        self._expires: float | None = None  # None = open until used
        self._failures = 0
        self._locked_until = 0.0
        if not self.devices():
            self._new_code(expires=None)

    # -- devices -----------------------------------------------------------
    def devices(self) -> list[dict]:
        return self._state.get("devices", [])

    def forget_all(self) -> None:
        self._state.set("devices", [])
        self._new_code(expires=None)

    # -- code ----------------------------------------------------------------
    @property
    def code(self) -> str | None:
        with self._lock:
            if self._code and self._expires is not None and time.monotonic() > self._expires:
                self._code = None
            return self._code

    def open_window(self) -> str:
        """User asked to pair another phone: new code valid for 10 minutes."""
        return self._new_code(expires=time.monotonic() + CODE_WINDOW_SECONDS)

    def _new_code(self, expires: float | None) -> str:
        with self._lock:
            self._code = f"{secrets.randbelow(1_000_000):06d}"
            self._expires = expires
            self._failures = 0
            code = self._code
        log.info("Pairing code: %s", code)
        self._notify()
        return code

    def _notify(self) -> None:
        if self._on_change:
            try:
                self._on_change()
            except Exception:  # never let a UI refresh break pairing
                log.exception("pairing change callback failed")

    # -- pairing ---------------------------------------------------------------
    def pair(self, code: str, device_name: str) -> Credentials:
        now = time.monotonic()
        with self._lock:
            if now < self._locked_until:
                raise PairingError("locked", "Too many wrong codes. Wait 30 seconds and try again.")
            current = self._code
            if current and self._expires is not None and now > self._expires:
                current = self._code = None
            if not current:
                raise PairingError(
                    "pairing_closed",
                    "Pairing is closed. On the computer choose “Pair a new phone” in the Reflect menu.",
                )
            if not isinstance(code, str) or not hmac.compare_digest(code.strip(), current):
                self._failures += 1
                if self._failures >= MAX_FAILURES:
                    self._locked_until = now + LOCKOUT_SECONDS
                    rotate = True
                else:
                    rotate = False
                if not rotate:
                    raise PairingError("bad_code", "That code is not right. Check the code on the computer.")
            else:
                rotate = False
                self._code = None
        if rotate:
            self._new_code(expires=self._expires)
            raise PairingError("locked", "Too many wrong codes. A new code is shown on the computer.")

        creds = Credentials(device_id=secrets.token_hex(8), token=b64(secrets.token_bytes(32)))
        devices = self.devices()
        devices.append(
            {
                "id": creds.device_id,
                "name": (device_name or "iPhone")[:64],
                "token": creds.token,
                "paired_at": int(time.time()),
            }
        )
        self._state.set("devices", devices)
        log.info("Paired new device %r (%s)", device_name, creds.device_id)
        self._notify()
        return creds

    def verify(self, device_id: str, nonce: bytes, proof: str) -> dict | None:
        """Return the device record if proof == HMAC-SHA256(token, nonce)."""
        if not isinstance(device_id, str) or not isinstance(proof, str):
            return None
        for device in self.devices():
            if hmac.compare_digest(device.get("id", ""), device_id):
                expected = auth_proof(unb64(device["token"]), nonce)
                if hmac.compare_digest(expected, proof.lower()):
                    return device
                return None
        return None
