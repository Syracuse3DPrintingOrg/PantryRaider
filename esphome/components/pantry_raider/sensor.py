"""Numeric sensors fed by the Pantry Raider hub.

Every sensor here is a plain ESPHome entity, so the stock ``api:`` block in
the Cub YAML exposes them to Home Assistant with zero extra code.
"""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import sensor
from esphome.const import (
    DEVICE_CLASS_TEMPERATURE,
    STATE_CLASS_MEASUREMENT,
    UNIT_CELSIUS,
    UNIT_SECOND,
)

from . import CONF_PANTRY_RAIDER_ID, PantryRaiderHub

CONF_EXPIRED = "expired"
CONF_EXPIRING_TODAY = "expiring_today"
CONF_EXPIRING_SOON = "expiring_soon"
CONF_PENDING = "pending"
CONF_ACTIVE_TIMERS = "active_timers"
CONF_NEXT_TIMER_SECONDS = "next_timer_seconds"
CONF_PROBE_TEMPERATURE = "probe_temperature"

_COUNT_SCHEMA = sensor.sensor_schema(
    accuracy_decimals=0,
    state_class=STATE_CLASS_MEASUREMENT,
    icon="mdi:food-apple",
)

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(CONF_PANTRY_RAIDER_ID): cv.use_id(PantryRaiderHub),
        cv.Optional(CONF_EXPIRED): _COUNT_SCHEMA,
        cv.Optional(CONF_EXPIRING_TODAY): _COUNT_SCHEMA,
        cv.Optional(CONF_EXPIRING_SOON): _COUNT_SCHEMA,
        cv.Optional(CONF_PENDING): sensor.sensor_schema(
            accuracy_decimals=0,
            state_class=STATE_CLASS_MEASUREMENT,
            icon="mdi:barcode-scan",
        ),
        cv.Optional(CONF_ACTIVE_TIMERS): sensor.sensor_schema(
            accuracy_decimals=0,
            state_class=STATE_CLASS_MEASUREMENT,
            icon="mdi:timer-outline",
        ),
        cv.Optional(CONF_NEXT_TIMER_SECONDS): sensor.sensor_schema(
            unit_of_measurement=UNIT_SECOND,
            accuracy_decimals=0,
            icon="mdi:timer-sand",
        ),
        cv.Optional(CONF_PROBE_TEMPERATURE): sensor.sensor_schema(
            unit_of_measurement=UNIT_CELSIUS,
            accuracy_decimals=1,
            device_class=DEVICE_CLASS_TEMPERATURE,
            state_class=STATE_CLASS_MEASUREMENT,
        ),
    }
)

_SETTERS = {
    CONF_EXPIRED: "set_expired_sensor",
    CONF_EXPIRING_TODAY: "set_today_sensor",
    CONF_EXPIRING_SOON: "set_soon_sensor",
    CONF_PENDING: "set_pending_sensor",
    CONF_ACTIVE_TIMERS: "set_active_timers_sensor",
    CONF_NEXT_TIMER_SECONDS: "set_next_timer_seconds_sensor",
    CONF_PROBE_TEMPERATURE: "set_probe_temperature_sensor",
}


async def to_code(config):
    hub = await cg.get_variable(config[CONF_PANTRY_RAIDER_ID])
    for key, setter in _SETTERS.items():
        if key in config:
            sens = await sensor.new_sensor(config[key])
            cg.add(getattr(hub, setter)(sens))
