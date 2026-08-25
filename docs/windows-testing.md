# Windows test status

The first native Windows 11 lab sweep used firmware release 2.21, the verified
Renode 1.16.1 build from firmware.ardupilot.org, native Python 3.12, and a
64-bit MinGW-built `am32sim.dll`. Each cell was run with both the published ELF
and Intel HEX image; the two formats produced the same result.

| MCU family | Representative target | PWM | DShot600 | BDShot |
|---|---|---:|---:|---:|
| A153 | FRDM_A153 | pass | pass | fail |
| E230 | RHINO40A_E230 | fail | fail | fail |
| F031 | CRAWLMASTER_F031 | pass | pass | pass |
| F051 | AGFRC_V2_F051 | pass | pass | pass |
| F415 | AT32DEV_F415 | pass | pass | pass |
| F421 | AIKON_55A_F421 | pass | pass | pass |
| G031 | GEN_G031 | pass | pass | pass |
| G071 | AIKON_04_G071 | pass | pass | pass |
| G431 | AS_G431 | pass | pass | fail |
| L431 | NEUTRON_L431 | pass | pass | pass |
| V203 | AIRBOT_V203 | pass | pass | pass |

This is 56 passing cells out of 66. E230 PWM also fails on Linux with the same
firmware and target, so that row is an existing family-model issue rather than
a Windows port failure. A153 and G431 BDShot remain explicit gaps; they are not
excluded from the comprehensive report.

Windows-specific validation also covers:

- 89 native Python/Qt tests passing, with only the POSIX-mode and external-GCC
  comparison tests skipped;
- native DLL build and smoke executable;
- usbip-win2 0.9.7.7 attach, exact serial identity, COM-port enumeration,
  28-byte serial echo, and exact owned-port detach;
- PyInstaller application build, packaged CLI and offscreen GUI startup;
- a 52 MiB per-user Inno Setup installer, silent installation, artifact and
  Renode discovery, and target generation from the installed application.

Run the comprehensive matrix and USB/IP check with the Makefile commands in
[packaging.md](packaging.md). The scheduled Windows workflow uploads the full
JSON report even while known model gaps make its parity step non-blocking.
