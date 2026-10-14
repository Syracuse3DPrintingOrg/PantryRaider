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

## A richer dashboard with Beszel (optional)

The live snapshot above always works with nothing to set up, but it only
shows the current moment. If you want history and graphs, an app called
[Beszel](https://github.com/henrygd/beszel) can run alongside Pantry Raider
and give you that. It is a separate, self-hosted monitoring dashboard, not
something Pantry Raider builds itself, so it is entirely optional.

Turn it on by starting the extra `with-beszel` piece of the stack:

```bash
docker compose --profile with-beszel up -d
```

That starts a Beszel hub and a reader for this device's own hardware. The
reader waits for a key from the hub before it starts, so pair the two once:

1. Open the hub at `http://<this device>:8090` and create an admin account.
2. Choose **Add System**, give it a name, and enter
   `/beszel_socket/beszel.sock` as the **Host / IP**. There is no port to
   fill in: the hub and the reader talk through a shared folder on this
   device, not over the network.
3. Copy the **Public Key** the dialog shows into a `BESZEL_AGENT_KEY=` line
   in the `.env` file next to your compose file, then add the system.
4. Run `docker compose --profile with-beszel up -d` again so the reader
   starts with that key.

Within a minute the system shows as up in the hub. Then paste the hub's
address into Settings, Resources, in the Beszel section, turn the toggle on,
and save. An "Open the Beszel dashboard" button appears there from then on,
right above the live snapshot.

If the system stays down, open it in the hub and check that the Host / IP is
exactly `/beszel_socket/beszel.sock`, and that the key in `.env` matches the
one the hub shows.

The reader needs a closer look at the host than most of Pantry Raider's
pieces do, since that is how it measures real hardware use rather than just
what happens inside its own container. It is entirely opt-in: skip the
`with-beszel` profile and nothing changes.

A satellite device (Pi Remote) can point at the same hub as the main server;
set it once there and every device offers the same dashboard link.
