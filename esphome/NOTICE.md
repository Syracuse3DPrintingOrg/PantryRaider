# Bandit Cub firmware: licensing

The rest of this repository is under the PolyForm Noncommercial License 1.0.0
(see the root `LICENSE`). The firmware in this folder is different, and this
note explains why and what it means for you.

## The short version

Bandit Cub firmware is built with [ESPHome](https://esphome.io), which is dual
licensed. Its C++ and runtime sources (`.c`, `.cpp`, `.h`, `.hpp`, `.tcc`,
`.ino`) are GPL-3.0; its Python tooling and everything else is MIT. The
authoritative terms are in the ESPHome repository's own `LICENSE`.

The Pantry Raider component in `components/pantry_raider/` includes ESPHome's
C++ headers and is compiled into the same binary as ESPHome's runtime, so the
C++ parts of that component and every Cub firmware image built from this
folder are GPL-3.0 works. They are licensed to you under GPL-3.0-or-later, not
under the repository's PolyForm license, and the noncommercial restriction
does not apply to them. A copy of the GPL is in
`components/pantry_raider/COPYING`.

The component's Python files (`__init__.py`, `sensor.py`, `text_sensor.py`,
`check_vectors.py`) build on ESPHome's MIT-licensed tooling. They are offered
under GPL-3.0-or-later as well, so the whole component travels under one
license and nobody has to reason about which file is which.

## What this covers, and what it does not

This applies to the firmware only: the contents of `esphome/` and the compiled
`.bin` images published for the Bandit Cubs flasher page. The Pantry Raider
server, the web app, the host agents and the cloud service are separate
programs that talk to a Cub over the network. They are not linked with ESPHome
and stay under the repository's PolyForm Noncommercial License.

## Source

The complete corresponding source for any Cub firmware image is this folder,
in the public repository at
<https://github.com/Syracuse3DPrintingOrg/PantryRaider>, at the tag matching
the version the firmware reports. The version is on the Cub's own screen and
in the Bandit Cubs page in Settings.

## Third party pieces in the build

The build also pulls in LVGL (MIT), ESP-IDF and the Espressif managed
components (Apache-2.0), and the Arduino ESP32 core where a profile uses it.
Each ships its own license text in the build tree. `THIRD_PARTY_NOTICES.md` in
the repository root carries the full list.
