# Third-party material

ESCSim was extracted from the AM32 firmware repository and retains AM32's
GPL-3.0-only license.

Some base `.repl` platform descriptions were derived from Renode platform
files published by Antmicro under the MIT license. The affected files retain
their origin comments; see `licenses/Renode-MIT.txt`. Renode itself is
downloaded separately from `firmware.ardupilot.org` and is not part of this
source distribution.

The native motor simulator contains the small `jsmn` JSON parser originally
published under the MIT license; see `licenses/jsmn-MIT.txt`.

The SpeedyBeeF405Mini platform and ArduPilot-oriented STM32/sensor peripheral
models were adapted from the GPL-3.0 ArduPilot Renode environment. Individual
Renode-derived MIT files retain their SPDX notices and are also covered by
`licenses/Renode-MIT.txt`.

The bundled `SPEEDYBEEF405V5.hex` flight-controller image is Betaflight
2026.6.1 revision `6dbc4218f`, distributed under GPL-3.0. Its corresponding
source is available from the Betaflight repository at
<https://github.com/betaflight/betaflight/tree/6dbc4218fd6bc33bf16ea32c670304d4f89321d5>.
