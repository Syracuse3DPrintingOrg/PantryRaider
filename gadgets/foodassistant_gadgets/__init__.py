"""foodassistant_gadgets: host-side Bluetooth kitchen thermometer reader.

Runs on the host (the BLE radio is not reachable from inside the app
container), reads probe temperatures from supported Bluetooth thermometers,
and posts them to the Pantry Raider app over HTTP on localhost. The same
package layout and systemd pattern as foodassistant_streamdeck.
"""

__version__ = "0.1.0"
