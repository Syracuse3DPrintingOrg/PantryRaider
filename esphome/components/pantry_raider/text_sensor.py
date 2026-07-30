"""Text sensors fed by the Pantry Raider hub.

``view`` is the state string a Cub is rendering right now (``expiring``,
``timers``, ``probe``, ``clock``, plus the local ``pairing`` and ``offline``
states), ``next_timer`` is the featured timer as "Label M:SS", and
``pairing_code`` carries the code while a pairing request is on screen.
"""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import text_sensor

from . import CONF_PANTRY_RAIDER_ID, PantryRaiderHub

CONF_VIEW = "view"
CONF_NEXT_TIMER = "next_timer"
CONF_PAIRING_CODE = "pairing_code"

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(CONF_PANTRY_RAIDER_ID): cv.use_id(PantryRaiderHub),
        cv.Optional(CONF_VIEW): text_sensor.text_sensor_schema(
            icon="mdi:television-guide"
        ),
        cv.Optional(CONF_NEXT_TIMER): text_sensor.text_sensor_schema(
            icon="mdi:timer-outline"
        ),
        cv.Optional(CONF_PAIRING_CODE): text_sensor.text_sensor_schema(
            icon="mdi:key-link"
        ),
    }
)

_SETTERS = {
    CONF_VIEW: "set_view_text_sensor",
    CONF_NEXT_TIMER: "set_next_timer_text_sensor",
    CONF_PAIRING_CODE: "set_pairing_code_text_sensor",
}


async def to_code(config):
    hub = await cg.get_variable(config[CONF_PANTRY_RAIDER_ID])
    for key, setter in _SETTERS.items():
        if key in config:
            sens = await text_sensor.new_text_sensor(config[key])
            cg.add(getattr(hub, setter)(sens))
