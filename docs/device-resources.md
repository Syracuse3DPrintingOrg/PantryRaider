# Device resources

Settings has a Resources section that shows, live, what the machine running
Pantry Raider is doing. It refreshes every few seconds while you watch, so you
can see at a glance whether the device is comfortable or under strain.

It reports:

- **Processor.** Overall use and a per-core breakdown.
- **Memory.** How much is in use.
- **Storage.** Space used for the app's data and for the system.
- **Temperature.** The device's current temperature.
- **Uptime.** How long the device has been running.
- **Power and throttling (Raspberry Pi).** On a Pi, whether the board has hit
  under-voltage or slowed itself down to cope. This is the single most useful
  thing to check when a Pi acts up: under-voltage almost always means the power
  supply or cable cannot keep up.

If the kitchen screen ever pops a warning about a hot or underpowered device,
tapping it takes you straight to this section, where the details and controls
live. For the power-supply and cabling advice behind those warnings, see
[Hardware](hardware.md#power-and-cabling).
