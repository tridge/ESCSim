"""UI-independent lifetime wrapper for an ESCSim Renode process."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

from escsim.renode.process import ProcessTree


def generator_command() -> list[str]:
    """Return the internal generator entry point for source and frozen builds."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--internal-generator"]
    return [sys.executable, "-m", "escsim.renode.generator"]


def generator_environment() -> dict[str, str]:
    """Environment in which the internal generator can import this package."""
    environment = os.environ.copy()
    if not getattr(sys, "frozen", False):
        source_root = str(Path(__file__).resolve().parents[2])
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            source_root if not existing else source_root + os.pathsep + existing
        )
    return environment


@dataclass(frozen=True)
class SessionSpec:
    target: str
    firmware: Path
    eeprom: Path
    model: Path
    bootloader: Path | None = None
    renode: Path | None = None
    gui_port: int = 57733
    state_port: int = 57734
    monitor_port: int = 57735
    can_bus: int = -1
    dshot_frame_us: int = 250

    def command(self) -> list[str]:
        command = generator_command() + [
            self.target,
            "--link",
            "--gui-port",
            str(self.gui_port),
            "--gui-state-port",
            str(self.state_port),
            "--monitor-port",
            str(self.monitor_port),
            "--gui-dshot-us",
            str(self.dshot_frame_us),
            "--elf",
            str(self.firmware),
            "--eeprom",
            str(self.eeprom),
            "--model",
            str(self.model),
        ]
        if self.bootloader is not None:
            command.extend(("--bootloader-elf", str(self.bootloader)))
        if self.renode is not None:
            command.extend(("--renode", str(self.renode)))
        if self.can_bus >= 0:
            command.extend(("--can-bus", str(self.can_bus)))
        return command

    def validate(self) -> None:
        for label, path in (
            ("firmware", self.firmware),
            ("eeprom", self.eeprom),
            ("motor model", self.model),
        ):
            if not Path(path).is_file():
                raise ValueError(f"no {label} at {path}")
        if self.bootloader is not None and not Path(self.bootloader).is_file():
            raise ValueError(f"no bootloader at {self.bootloader}")
        if self.renode is not None and not Path(self.renode).is_file():
            raise ValueError(f"no Renode executable at {self.renode}")
        for label, port in (
            ("GUI", self.gui_port),
            ("state", self.state_port),
            ("monitor", self.monitor_port),
        ):
            if not 1 <= port <= 65535:
                raise ValueError(f"{label} port must be 1..65535")
        if not -1 <= self.can_bus <= 9:
            raise ValueError("CAN bus must be -1..9")


class RenodeSession:
    """Own one generator/Renode process tree and its line-oriented log."""

    def __init__(self, spec: SessionSpec) -> None:
        self.spec = spec
        self.logs: queue.Queue[str] = queue.Queue()
        self.process: ProcessTree | None = None
        self._pump: threading.Thread | None = None

    def start(self) -> None:
        if self.running():
            raise RuntimeError("Renode session is already running")
        self.spec.validate()
        self.process = ProcessTree(
            self.spec.command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            env=generator_environment(),
        )
        self._pump = threading.Thread(target=self._pump_output, daemon=True)
        self._pump.start()

    def _pump_output(self) -> None:
        tree = self.process
        if tree is None or tree.process.stdout is None:
            return
        for line in tree.process.stdout:
            self.logs.put(line.rstrip("\n"))
        status = tree.process.wait()
        self.logs.put(f"[emulator exited, status {status}]")

    def running(self) -> bool:
        return self.process is not None and self.process.running()

    def stop(self) -> None:
        if self.process is not None:
            self.process.stop()
            self.process = None

    def drain_logs(self) -> list[str]:
        lines = []
        while True:
            try:
                lines.append(self.logs.get_nowait())
            except queue.Empty:
                return lines

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, _type, _value, _traceback):
        self.stop()
