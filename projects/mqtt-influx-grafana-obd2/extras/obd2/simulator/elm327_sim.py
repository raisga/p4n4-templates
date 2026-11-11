#!/usr/bin/env python3
"""
ELM327 simulator for the obd2 extra: answers an OBD-II client (python-OBD) over TCP
the way an ELM327 on a CAN car (ISO 15765-4, 11-bit, 500 kbit/s) would, from a
scenario file. Every request is recorded, so tests can prove what a client sent.

Not a full ELM327: the AT commands python-OBD uses, the mode 01 PIDs the scenario
gives values for, codes for modes 03, 07 and 0A, and the VIN (mode 09). Anything
else answers "?" or NO DATA. A mode 04 request clears the codes as a car would, and
is recorded like every other request, so a test can fail on it.

Usage: python3 elm327_sim.py [--scenario scenarios/warmup.yaml] [--port 35000]
Environment: OBD_SIM_SCENARIO, OBD_SIM_PORT, OBD_SIM_DROP_EVERY (seconds, 0 = never)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
DEFAULT_SCENARIO = HERE / "scenarios" / "warmup.yaml"
ENGINE_RX = "7E8"
PROTOCOL = "6"
PROTOCOL_NAME = "ISO 15765-4 (CAN 11/500)"
HEX = set("0123456789ABCDEF")


def _byte(v: float) -> int:
    return max(0, min(255, round(v)))


def _word(v: float) -> list[int]:
    v = max(0, min(65535, round(v)))
    return [v >> 8, v & 0xFF]


# Mode 01 PIDs the scenario can give values for: measurement → (PID, encoder)
PIDS: dict[str, tuple[int, object]] = {
    "engine_load": (0x04, lambda v: [_byte(v * 255 / 100)]),
    "coolant_temp": (0x05, lambda v: [_byte(v + 40)]),
    "stft_b1": (0x06, lambda v: [_byte(v * 128 / 100 + 128)]),
    "ltft_b1": (0x07, lambda v: [_byte(v * 128 / 100 + 128)]),
    "rpm": (0x0C, lambda v: _word(v * 4)),
    "speed": (0x0D, lambda v: [_byte(v)]),
    "intake_temp": (0x0F, lambda v: [_byte(v + 40)]),
    "maf": (0x10, lambda v: _word(v * 100)),
    "throttle_pos": (0x11, lambda v: [_byte(v * 255 / 100)]),
    "fuel_level": (0x2F, lambda v: [_byte(v * 255 / 100)]),
}
PID_STATUS, PID_FREEZE_DTC, PID_VOLTAGE = 0x01, 0x02, 0x42
INFO_VIN = 0x02


def dtc_bytes(code: str) -> list[int]:
    """'P0171' → [0x01, 0x71] (SAE J2012: two bits of letter, then four nibbles)."""
    letter = "PCBU".index(code[0].upper())
    return [(letter << 6) | (int(code[1]) << 4) | int(code[2], 16), int(code[3:5], 16)]


def bitmap(pids: set[int], base: int) -> list[int]:
    """The 'supported PIDs [base+1 .. base+0x20]' answer, bit 31 = base+1."""
    bits = 0
    for p in pids:
        if base < p <= base + 0x20:
            bits |= 1 << (32 - (p - base))
    return list(bits.to_bytes(4, "big"))


class Scenario:
    """Values, codes and engine-off windows on a timeline that repeats every `loop` s."""

    def __init__(self, data: dict):
        self.name = data.get("name", "scenario")
        self.adapter = data.get("adapter", "ELM327 v1.5")
        self.vin = data.get("vin", "P4N4SIM0000000001")
        self.loop = float(data.get("loop", 0))
        self.voltage = {"running": 14.1, "off": 12.4, **data.get("voltage", {})}
        self.off = [tuple(w) for w in data.get("off", [])]
        self.values = data.get("values", {})
        self.dtcs = data.get("dtcs", [])
        unknown = set(self.values) - set(PIDS)
        if unknown:
            raise ValueError(f"scenario {self.name}: no PID for {', '.join(sorted(unknown))}")

    @classmethod
    def load(cls, path: Path) -> Scenario:
        return cls(yaml.safe_load(Path(path).read_text()))

    def at(self, elapsed: float) -> float:
        return elapsed % self.loop if self.loop else elapsed

    def running(self, t: float) -> bool:
        return not any(start <= t < end for start, end in self.off)

    def value(self, name: str, t: float) -> float:
        spec = self.values[name]
        if isinstance(spec, (int, float)):
            return float(spec)
        if "period" in spec:
            return spec["base"] + spec.get("amplitude", 0) * math.sin(2 * math.pi * t / spec["period"])
        frac = min(1.0, t / spec["over"]) if spec.get("over") else 1.0
        return spec["from"] + (spec["to"] - spec["from"]) * frac

    def codes(self, t: float, kind: str, cleared_at: float | None) -> list[str]:
        out = []
        for d in self.dtcs:
            start, end = d.get("from", 0), d.get("to", math.inf)
            if d.get("type", "stored") != kind or not start <= t < end:
                continue
            # A clear (mode 04) removes stored and pending codes until they set again
            if kind != "permanent" and cleared_at is not None and start <= cleared_at:
                continue
            out.append(d["code"])
        return out


class Simulator:
    """The car and its adapter: a scenario plus a TCP server, one ELM327 per connection."""

    def __init__(self, scenario: Scenario, host: str = "127.0.0.1", port: int = 0,
                 drop_every: float = 0, log=None):
        self.scenario = scenario
        self.drop_every = drop_every
        self.log = log
        self.start_time = time.monotonic()
        self.cleared_at: float | None = None
        self.requests: list[str] = []
        self.connections = 0
        self._lock = threading.Lock()
        sim = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                sim._serve(self.request)

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.server = socketserver.ThreadingTCPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self._thread: threading.Thread | None = None

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> Simulator:
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # ---- state -------------------------------------------------------------

    def now(self) -> float:
        return self.scenario.at(time.monotonic() - self.start_time)

    def record(self, cmd: str) -> None:
        with self._lock:
            self.requests.append(cmd)
        if self.log:
            self.log.write(json.dumps({"t": round(time.time(), 3), "rx": cmd}) + "\n")
            self.log.flush()

    def obd_requests(self) -> list[str]:
        """Recorded requests that went to the car: not AT commands or line noise."""
        with self._lock:
            return [c for c in self.requests if c and set(c) <= HEX]

    # ---- one connection ----------------------------------------------------

    def _serve(self, conn: socket.socket) -> None:
        self.connections += 1
        session = Session(self)
        opened = time.monotonic()
        conn.settimeout(0.2)
        buf = b""
        while True:
            if self.drop_every and time.monotonic() - opened >= self.drop_every:
                return  # the adapter "unplugged": close without a word
            try:
                data = conn.recv(1024)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            buf += data
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                try:
                    conn.sendall(session.handle(line.decode("ascii", "replace")).encode())
                except OSError:
                    return


class Session:
    """One ELM327's state: echo, headers, spaces and the last command (a bare CR repeats it)."""

    def __init__(self, sim: Simulator):
        self.sim = sim
        self.reset()
        self.last = ""

    def reset(self) -> None:
        self.echo, self.headers, self.spaces = True, False, True
        self.auto, self.bus_up = True, False

    def handle(self, line: str) -> str:
        raw = line.strip()
        cmd = raw.upper().replace(" ", "")
        if cmd == "" and self.last:
            cmd = self.last
        self.sim.record(cmd)
        if cmd.startswith("AT"):
            body = self.at_command(cmd[2:])
        elif len(cmd) >= 2 and set(cmd) <= HEX:
            body = self.obd_request(cmd)
            self.last = cmd
        else:
            body = "?"
        echo = raw + "\r" if self.echo and raw else ""
        return f"{echo}{body}\r\r>"

    # ---- AT commands -------------------------------------------------------

    def at_command(self, c: str) -> str:
        s = self.sim.scenario
        if c == "Z":
            self.reset()
            return "\r" + s.adapter
        if c == "D":
            self.reset()
            return "OK"
        if c in ("E0", "E1"):
            self.echo = c == "E1"
            return "OK"
        if c in ("H0", "H1"):
            self.headers = c == "H1"
            return "OK"
        if c in ("S0", "S1"):
            self.spaces = c == "S1"
            return "OK"
        if c in ("L0", "L1", "AT0", "AT1", "AT2", "CAF1") or c.startswith("ST"):
            return "OK"
        if c == "I":
            return s.adapter
        if c == "@1":
            return "OBDII to RS232 Interpreter"
        if c == "RV":
            t = self.sim.now()
            return f"{s.voltage['running' if s.running(t) else 'off']:.1f}V"
        if c.startswith(("SP", "TP")) and len(c) == 3:
            self.auto = c[2] in "0A" or c.startswith("TP")
            self.bus_up = False
            return "OK"
        if c == "DPN":
            return ("A" if self.auto else "") + PROTOCOL
        if c == "DP":
            return ("AUTO, " if self.auto else "") + PROTOCOL_NAME
        if c.startswith("SH") and len(c) in (5, 8):
            return "OK"
        return "?"

    # ---- requests to the car -----------------------------------------------

    def obd_request(self, cmd: str) -> str:
        s, t = self.sim.scenario, self.sim.now()
        if not s.running(t):
            return "NO DATA" if self.bus_up else "SEARCHING...\rUNABLE TO CONNECT"
        payload = self.answer(cmd[:2], cmd[2:4], t)
        if payload is None:
            return "NO DATA"
        self.bus_up = True
        return "\r".join(self.frames(payload))

    def answer(self, mode: str, pid_hex: str, t: float) -> list[int] | None:
        s = self.sim.scenario
        cleared = self.sim.cleared_at
        if mode == "01" and len(pid_hex) == 2:
            pid = int(pid_hex, 16)
            supported = {PIDS[n][0] for n in s.values} | {PID_STATUS, PID_FREEZE_DTC, PID_VOLTAGE}
            if pid % 0x20 == 0:  # supported-PID bitmaps; one block points at the next
                more = {base + 0x20 for base in range(0, 0xE0, 0x20) if any(p > base + 0x20 for p in supported)}
                return [0x41, pid] + bitmap(supported | more, pid) if pid <= max(supported) else None
            if pid == PID_STATUS:
                stored = s.codes(t, "stored", cleared)
                return [0x41, pid, (0x80 if stored else 0) | min(len(stored), 0x7F), 0x07, 0x65, 0x00]
            if pid == PID_FREEZE_DTC:
                stored = s.codes(t, "stored", cleared)
                return [0x41, pid] + (dtc_bytes(stored[0]) if stored else [0, 0])
            if pid == PID_VOLTAGE:
                return [0x41, pid] + _word(s.voltage["running"] * 1000)
            for name, (p, encode) in PIDS.items():
                if p == pid and name in s.values:
                    return [0x41, pid] + encode(s.value(name, t))
            return None
        if mode in ("03", "07", "0A"):
            kind = {"03": "stored", "07": "pending", "0A": "permanent"}[mode]
            codes = s.codes(t, kind, cleared)
            body = [b for code in codes for b in dtc_bytes(code)]
            return [0x40 + int(mode, 16), len(codes)] + body
        if mode == "04":
            self.sim.cleared_at = t
            return [0x44]
        if mode == "09" and len(pid_hex) == 2:
            info = int(pid_hex, 16)
            if info == 0x00:
                return [0x49, 0x00] + bitmap({INFO_VIN}, 0)
            if info == INFO_VIN:
                return [0x49, 0x02, 0x01] + list(s.vin.encode("ascii")[:17].ljust(17, b"0"))
        return None

    def frames(self, payload: list[int]) -> list[str]:
        """ISO-TP frames as the ELM prints them: header, PCI byte, data."""
        if len(payload) <= 7:
            frames = [[len(payload)] + payload]
        else:
            n = len(payload)
            frames = [[0x10 | (n >> 8), n & 0xFF] + payload[:6]]
            for seq, i in enumerate(range(6, n, 7), start=1):
                frames.append([0x20 | (seq & 0x0F)] + payload[i:i + 7])
        sep = " " if self.spaces else ""
        lines = []
        for f in frames:
            data = sep.join(f"{b:02X}" for b in (f if self.headers else f[1:]))
            lines.append(f"{ENGINE_RX}{sep}{data}" if self.headers else data)
        return lines


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scenario", default=os.environ.get("OBD_SIM_SCENARIO") or DEFAULT_SCENARIO)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("OBD_SIM_PORT") or 35000))
    ap.add_argument("--drop-every", type=float, default=float(os.environ.get("OBD_SIM_DROP_EVERY") or 0),
                    help="close each connection after this many seconds, like a pulled adapter")
    ap.add_argument("--quiet", action="store_true", help="don't log each request to stdout")
    args = ap.parse_args(argv)

    scenario = Scenario.load(Path(args.scenario))
    sim = Simulator(scenario, args.host, args.port, args.drop_every, None if args.quiet else sys.stdout)
    print(f"elm327-sim: scenario {scenario.name} on {args.host}:{sim.port}", flush=True)
    try:
        sim.server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
