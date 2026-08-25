#!/usr/bin/env python3
"""Attach ESCSim's USB/IP serial device on Windows and verify an echo."""

from __future__ import annotations

import argparse
import json
import os
import threading

import serial

from escsim.control import usbip


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default=f"ESCSIM-WINDOWS-{os.getpid()}")
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args(argv)
    if not usbip.IS_WINDOWS:
        parser.error("this integration test requires native Windows Python")

    version = usbip.windows_usbip_version()
    server = usbip.UsbipServer(port=0, serial=args.serial)
    attached_port = None
    tty = None
    stop = threading.Event()

    def echo() -> None:
        while not stop.is_set():
            data = server.read(0.1)
            if data:
                server.write(data)

    thread = threading.Thread(target=echo, daemon=True)
    thread.start()
    try:
        attached_port = usbip.attach(host=server.host, port=server.port)
        tty = usbip.find_tty(args.serial, timeout=args.timeout)
        if tty is None:
            raise RuntimeError(f"USB/IP serial {args.serial} did not enumerate")
        payload = b"ESCSim Windows USB/IP echo\r\n"
        with serial.Serial(tty, 115200, timeout=args.timeout) as stream:
            stream.reset_input_buffer()
            stream.write(payload)
            stream.flush()
            reply = stream.read(len(payload))
        if reply != payload:
            raise RuntimeError(f"USB/IP echo mismatch: {reply!r}")
        print(
            json.dumps(
                {
                    "status": "passed",
                    "usbip_version": ".".join(map(str, version)),
                    "serial": args.serial,
                    "port": tty,
                    "owned_ude_port": attached_port,
                    "echo_bytes": len(reply),
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        stop.set()
        if attached_port is not None:
            usbip.detach(attached_port)
        server.close()
        thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
