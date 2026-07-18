"""Known-address discovery for STEMMA QT / Qwiic boards (FoodAssistant-etsc).

The sweep only ever touches addresses we have a reason to touch. A blind 0x03
to 0x77 probe is how i2cdetect works and it is the wrong tool here: it uses
quick-write, which some parts read as a malformed write, and there is nothing
to gain from finding a device nobody has written a driver for.

Several launch addresses are shared across families, so an ACK is never proof
of identity on its own:

* 0x29 is both the VL53L0X and the VL53L1X ToF sensor.
* 0x30 to 0x33 is the NeoKey, but the range is not exclusively Adafruit's.
* 0x60 is the NeoDriver, PCA9685 boards, and the DRV8830 motor driver.
* 0x77 is the BMP390 and the DPS310.

So the table maps each address to the families that COULD be there, and the
answer comes from each family's probe(): a chip-id register read, or for a
seesaw board the hardware-id handshake plus its module inventory. A device
that answers but identifies as nothing we drive is reported with
supported=False, the same "we saw it, we just cannot use it" courtesy the BLE
scanner extends to a Meater.
"""
from __future__ import annotations

import logging

from .bus import BusUnavailable
from .drivers import neokey

log = logging.getLogger("foodassistant.gadgets.i2c")

# Families with a working driver: kind -> the module implementing the
# ADDRESSES / probe() contract. Phase 1 drives the NeoKey; the presence,
# temperature, encoder, and NeoPixel drivers slot in here as they land, and
# nothing else in this file has to change.
DRIVERS = {
    neokey.KIND: neokey,
}

# Every address worth probing, and what could be answering. The order inside
# a tuple is the order probe() runs, so the more specific family goes first.
# Entries whose families have no driver yet are here on purpose: a plugged-in
# AHT20 showing as "seen, not supported yet" is far better than a QT board
# that seems invisible.
ADDRESS_TABLE: dict[int, tuple[str, ...]] = {
    0x10: ("veml7700",),
    0x18: ("mcp9808",),
    0x19: ("mcp9808",),
    0x1A: ("mcp9808",),
    0x1B: ("mcp9808",),
    0x1C: ("mcp9808",),
    0x1D: ("mcp9808",),
    0x1E: ("mcp9808",),
    0x1F: ("mcp9808",),
    0x23: ("bh1750",),
    0x29: ("vl53l1x", "vl53l0x"),
    0x30: ("neokey",),
    0x31: ("neokey",),
    0x32: ("neokey",),
    0x33: ("neokey",),
    0x38: ("aht20",),
    0x39: ("apds9960",),
    0x44: ("sht4x",),
    0x45: ("sht4x",),
    0x49: ("ano_encoder",),
    0x53: ("adxl343",),
    0x5A: ("drv2605",),
    0x60: ("neodriver", "pca9685", "drv8830"),
    0x6A: ("lsm6dsox",),
    0x6B: ("lsm6dsox",),
    0x76: ("bmp390", "dps310"),
    0x77: ("bmp390", "dps310"),
}

# Friendly names for the discovered list, so a user reads "AHT20 temperature
# and humidity" rather than a hex address.
MODEL_NAMES = {
    "neokey": "NeoKey 1x4",
    "vl53l1x": "VL53L1X distance sensor",
    "vl53l0x": "VL53L0X distance sensor",
    "apds9960": "APDS9960 proximity sensor",
    "aht20": "AHT20 temperature and humidity",
    "sht4x": "SHT4x temperature and humidity",
    "mcp9808": "MCP9808 temperature sensor",
    "ano_encoder": "ANO rotary encoder",
    "neodriver": "NeoDriver NeoPixel adapter",
    "pca9685": "PCA9685 driver board",
    "drv8830": "DRV8830 motor driver",
    "adxl343": "ADXL343 accelerometer",
    "lsm6dsox": "LSM6DSOX accelerometer",
    "veml7700": "VEML7700 light sensor",
    "bh1750": "BH1750 light sensor",
    "drv2605": "DRV2605L haptic driver",
    "bmp390": "BMP390 pressure sensor",
    "dps310": "DPS310 pressure sensor",
}


def candidates_for(address: int) -> tuple[str, ...]:
    """The families that could be at an address ((), if we never probe it)."""
    return ADDRESS_TABLE.get(int(address), ())


def model_name(model: str) -> str:
    return MODEL_NAMES.get(str(model or ""), "Unknown accessory")


def device_id(bus_number: int, address: int) -> str:
    """The stable id the app registry keys on. Bus plus address, because QT
    addresses are strap-pinned: the same board in the same jumper setting is
    the same id across replugs and reboots, which is what lets a NeoKey keep
    its key mapping when the kitchen loses power."""
    return f"i2c:{int(bus_number)}:0x{int(address):02x}"


def choose(address: int, results: dict) -> dict | None:
    """Resolve one address from its probe results. Pure.

    ``results`` maps a candidate family to what its probe() returned (its
    kind, or None for "not me"). A family with no driver has no entry, which
    is how an ACK with nothing to identify it still gets reported: as the
    address's first known candidate, marked unsupported.

    None means nothing answered at all.
    """
    for model in candidates_for(address):
        if results.get(model):
            return {"model": model, "supported": True,
                    "name": model_name(model)}
    known = candidates_for(address)
    if not known:
        return {"model": "", "supported": False, "name": "Unknown accessory"}
    # Something ACKed but identified as none of the drivers we have. Name the
    # likeliest candidate for the address so the user has a thread to pull,
    # and be honest that we cannot drive it.
    return {"model": known[0], "supported": False, "name": model_name(known[0])}


def sweep(bus, bus_number: int = 1) -> list[dict]:
    """Probe every known address and report what is really there.

    Returns discovered-entry dicts in the shape the app's ingest takes:
    ``{"id", "kind": "stemma", "model", "name", "address", "supported"}``.
    Never raises: a bus that dies mid-sweep returns what it found so far, and
    the caller reports the bus as unhealthy from its own flag.
    """
    found: list[dict] = []
    for address in sorted(ADDRESS_TABLE):
        try:
            if not bus.ping(address):
                continue
        except BusUnavailable:
            break
        results = {}
        for model in candidates_for(address):
            driver = DRIVERS.get(model)
            if not driver:
                continue
            try:
                results[model] = driver.probe(bus, address)
            except BusUnavailable:
                return found
            except Exception:  # noqa: BLE001 - one odd board never stops a sweep
                log.debug("Probe of %s at 0x%02x failed", model, address,
                          exc_info=True)
                results[model] = None
        answer = choose(address, results)
        if not answer:
            continue
        found.append({
            "id": device_id(bus_number, address),
            "kind": "stemma",
            "model": answer["model"],
            "name": answer["name"],
            "address": f"0x{address:02x}",
            "supported": answer["supported"],
        })
    return found
