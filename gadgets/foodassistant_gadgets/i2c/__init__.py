"""I2C (STEMMA QT / Qwiic) support for the gadgets agent (FoodAssistant-etsc).

Adafruit STEMMA QT and SparkFun Qwiic are one ecosystem electrically: 3.3V
I2C on a 4-pin JST-SH connector, which a Pi speaks natively on /dev/i2c-1.
This package gives the agent the bus, the discovery sweep, the shared seesaw
driver, and one module per device family.

The agent owns the bus because the agent already owns everything an I2C
device needs: config pulled from the app, readings pushed back, discovery
reporting, and a Settings surface. The one carve-out is the host bridge,
which keeps reading the accelerometer itself because its job is the physical
display, not the app's data.

Nothing here is imported at agent start unless the module is turned on, and
smbus2 is imported lazily inside the bus wrapper, so a machine with no I2C
runs the agent exactly as before.
"""
from .bus import BusUnavailable, I2CBus
from .discovery import ADDRESS_TABLE, DRIVERS, choose, sweep
from .module import I2CModule

__all__ = [
    "ADDRESS_TABLE",
    "BusUnavailable",
    "DRIVERS",
    "I2CBus",
    "I2CModule",
    "choose",
    "sweep",
]
