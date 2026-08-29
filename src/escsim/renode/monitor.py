"""Renode monitor protocol and metrics parsing."""

from __future__ import annotations

import re
import socket
import time


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
PROMPT_RE = re.compile(r"(?m)^\([^\r\n]+\)\s*$")


def parse_elapsed(value: str) -> float:
    """Convert Renode's ``[days.]HH:MM:SS.s`` elapsed-time format."""

    fields = value.strip().split(":")
    if len(fields) != 3:
        raise ValueError(f"bad elapsed time {value}")
    days = 0
    hours = fields[0]
    if "." in hours:
        days_text, hours = hours.split(".", 1)
        days = int(days_text)
    return days * 86400 + int(hours) * 3600 + int(fields[1]) * 60 + float(fields[2])


def clean_monitor_text(data: bytes | bytearray | str) -> str:
    text = (
        bytes(data).decode("utf-8", errors="replace")
        if isinstance(data, (bytes, bytearray))
        else data
    )
    text = ANSI_RE.sub("", text).replace("\r", "")
    return text.replace("\ufffd", "")


def parse_metrics(text: str) -> dict[str, float | int]:
    values = re.findall(r"(?m)^\s*(0x[0-9A-Fa-f]+)\s*$", text)
    virtual = re.search(r"(?m)^Elapsed Virtual Time:\s*(\S+)\s*$", text)
    host = re.search(r"(?m)^Elapsed Host Time:\s*(\S+)\s*$", text)
    if len(values) not in (3, 6, 9, 11) or virtual is None or host is None:
        raise ValueError("incomplete Renode monitor metrics")
    result = {
        "pc": int(values[0], 16),
        "mips": int(values[1], 16),
        "instructions": int(values[2], 16),
        "virtual_seconds": parse_elapsed(virtual.group(1)),
        "host_seconds": parse_elapsed(host.group(1)),
    }
    if len(values) >= 6:
        result.update(
            dshot_frames=int(values[3], 16),
            dshot_replies=int(values[4], 16),
            dshot_injected=int(values[5], 16),
        )
    if len(values) >= 9:
        result.update(
            dshot_last_frame=int(values[6], 16),
            dshot_bidir_frames=int(values[7], 16),
            dshot_type=int(values[8], 16),
        )
    if len(values) == 11:
        result.update(
            serial_requests=int(values[9], 16),
            serial_replies=int(values[10], 16),
        )
    return result


def startup_error(text: bytes | str) -> str | None:
    clean = clean_monitor_text(text)
    if "There was an error executing command" not in clean:
        return None
    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    errors = [
        line for line in lines if line.startswith(("Error ", "Could not ", "An error "))
    ]
    return errors[0] if errors else "Renode setup command failed"


class MonitorClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.socket: socket.socket | None = None

    def connect(self, timeout: float = 45) -> str:
        self.close()
        self.socket = socket.create_connection((self.host, self.port), timeout=2)
        self.socket.settimeout(0.5)
        return self._read_to_prompt(timeout)

    def close(self) -> None:
        if self.socket is not None:
            try:
                self.socket.close()
            except OSError:
                pass
        self.socket = None

    def command(self, command: str, timeout: float = 5) -> str:
        if self.socket is None:
            raise OSError("monitor is not connected")
        self.socket.sendall((command + "\n").encode("ascii"))
        return self._read_to_prompt(timeout, expected=command)

    def _read_to_prompt(self, timeout: float, expected: str | None = None) -> str:
        if self.socket is None:
            raise OSError("monitor is not connected")
        # Keep the socket object local so another thread can call close() to
        # interrupt a pending monitor read without turning this into a
        # None.recv() race. Closing the local object wakes recv() with OSError.
        sock = self.socket
        deadline = time.monotonic() + timeout
        data = bytearray()
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise OSError("Renode monitor disconnected")
            data.extend(chunk)
            text = clean_monitor_text(data)
            if (expected is None or expected in text) and PROMPT_RE.search(text):
                return text
        raise TimeoutError("timed out waiting for the Renode monitor")

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()
