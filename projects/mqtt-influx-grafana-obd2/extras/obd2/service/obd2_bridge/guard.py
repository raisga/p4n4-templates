"""
The read-only guard: every line obd2 sends to the adapter is checked here first.

python-OBD opens the adapter with serial.serial_for_url(). install() hands it a
version that wraps the port, so each write is checked before it reaches the adapter.
Only diagnostic requests (modes 01, 03, 06, 07, 09 and 0A) and the AT commands that
set the adapter up get through. Anything else, from mode 04 (clear codes) to a UDS
write, raises ReadOnlyViolation and never reaches the car. python-OBD then drops the
connection, and the bridge reconnects.
"""

from __future__ import annotations

import logging
import re
import threading

import obd.elm327
import serial

log = logging.getLogger(__name__)

# Current data, stored codes, test results, pending codes, vehicle info, permanent codes
ALLOWED_MODES = frozenset({"01", "03", "06", "07", "09", "0A"})

# What python-OBD sends to set the adapter up and read it: reset, defaults, echo,
# headers, linefeeds, spaces, version, voltage, protocol, request header
ALLOWED_AT = re.compile(
    r"^AT(Z|D|E[01]|H[01]|L[01]|S[01]|I|RV|DPN?|SP[0-9A-C]|TP[1-9A-C]|SH[0-9A-F]{3}|SH[0-9A-F]{6})$"
)
_REQUEST = re.compile(r"^([0-9A-F]{2})[0-9A-F]*$")

# python-OBD's baud-rate probe. A bare CR repeats the last request, which was checked
# when it was sent; a space wakes the adapter.
BAUD_PROBE = b"\x7f\x7f"

_lock = threading.Lock()
_blocked = 0
_installed = False


class ReadOnlyViolation(serial.SerialException):
    """A write the read-only guard refused to send."""


def check(line: bytes) -> str | None:
    """None if `line` (one command, without its CR) may be sent; otherwise why not."""
    if line == BAUD_PROBE or not line.strip():
        return None
    text = line.decode("ascii", "replace")
    cmd = text.upper().replace(" ", "")
    if cmd.startswith("AT"):
        return None if ALLOWED_AT.match(cmd) else f"AT command {text!r} isn't on the allowlist"
    m = _REQUEST.match(cmd)
    if m is None:
        return f"{text!r} isn't an OBD-II request"
    if m.group(1) not in ALLOWED_MODES:
        return f"mode {m.group(1)} ({text!r}) isn't a diagnostic read"
    return None


def blocked() -> int:
    """How many writes the guard has refused since start."""
    return _blocked


class GuardedPort:
    """A pyserial port whose writes go through check() first."""

    def __init__(self, port):
        object.__setattr__(self, "_port", port)

    def write(self, data: bytes) -> int:
        global _blocked
        lines = bytes(data).split(b"\r")
        if len(lines) > 1 and lines[-1] == b"":
            lines.pop()  # the CR that ends the command
        for line in lines:
            problem = check(line)
            if problem:
                with _lock:
                    _blocked += 1
                log.error("read-only guard: blocked %s", problem)
                raise ReadOnlyViolation(problem)
        return self._port.write(data)

    def __getattr__(self, name):
        return getattr(self._port, name)

    def __setattr__(self, name, value):
        setattr(self._port, name, value)


class _GuardedSerial:
    """Stands in for the serial module inside python-OBD's elm327."""

    def __init__(self, real):
        self._real = real

    def serial_for_url(self, *args, **kwargs):
        return GuardedPort(self._real.serial_for_url(*args, **kwargs))

    def __getattr__(self, name):
        return getattr(self._real, name)


def install() -> None:
    """Put the guard between python-OBD and every adapter it opens from now on."""
    global _installed
    with _lock:
        if not _installed:
            obd.elm327.serial = _GuardedSerial(serial)
            _installed = True
