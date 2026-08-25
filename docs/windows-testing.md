# Windows test status

The native Windows 11 lab sweep uses firmware release 2.21, the verified
Renode 1.16.1 build from firmware.ardupilot.org, native Python 3.12, and a
64-bit MinGW-built `am32sim.dll`. Each cell was run with both the published ELF
and Intel HEX image; the two formats produced the same result.

| MCU family | Representative target | PWM | DShot600 | BDShot |
|---|---|---:|---:|---:|
| A153 | FRDM_A153 | pass | pass | pass |
| E230 | GD32DEV_A_E230 | pass | pass | pass |
| F031 | CRAWLMASTER_F031 | pass | pass | pass |
| F051 | AGFRC_V2_F051 | pass | pass | pass |
| F415 | AT32DEV_F415 | pass | pass | pass |
| F421 | AIKON_55A_F421 | pass | pass | pass |
| G031 | GEN_G031 | pass | pass | pass |
| G071 | AIKON_04_G071 | pass | pass | pass |
| G431 | AS_G431 | pass | pass | pass |
| L431 | NEUTRON_L431 | pass | pass | pass |
| V203 | AIRBOT_V203 | pass | pass | pass |

This is 66 passing cells out of 66. Every cell starts a fresh Renode process,
waits for the firmware's reported armed state, advances the throttle, and for
BDShot also requires decoded telemetry replies.

Windows-specific validation also covers:

- 91 native Python/Qt tests passing, with only the POSIX-mode and external-GCC
  comparison tests skipped;
- native DLL build and smoke executable;
- usbip-win2 0.9.7.7 attach, exact serial identity, COM-port enumeration,
  28-byte serial echo, and exact owned-port detach;
- PyInstaller application build, packaged CLI and offscreen GUI startup;
- a per-user Inno Setup installer with the verified usbip-win2 prerequisite,
  silent non-driver installation, artifact and Renode discovery, and target
  generation from the installed application.

Run the comprehensive matrix and USB/IP check with the Makefile commands in
[packaging.md](packaging.md). The scheduled Windows workflow fails if any
matrix cell fails and still uploads the full JSON report for diagnosis.
