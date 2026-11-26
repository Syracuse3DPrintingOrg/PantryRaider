# Status icons on kitchen screens

Your kitchen screens can show small, see-through icons in a top corner while
something in the kitchen is on: the fridge door is open, a thermometer is
reporting, the oven is heating, the sous vide is running. Each icon shows only
while its condition is true and goes away when it is not. Tap an icon to see
what it is, for example "Fridge door: open" or "Sous vide: 812 W".

The icons show on every kitchen display (a Pantry Raider screen in kiosk mode,
a Bandit Remote) and on tablets and wall panels connected as
[screens](screens.md). An ordinary browser on a phone or laptop does not show
them.

## Turn them on

Open **Settings, Home Assistant** and find **Status icons on kitchen screens**.

1. Switch **Show status icons** on.
2. Pick the corner under **Where**: top right (the default) or top left.
3. Add an item with one of the buttons: **Fridge door**, **Thermometer**, or
   **Appliance or other device**.
4. Press **Save status icons**.

Screens that already show status icons pick up a change within a few seconds.
A screen that had them off shows them from its next page.

## What each item watches

Every item has a name, an icon (fridge, door, thermometer, oven, sous vide or
pot, plug, fan, or a plain dot) and a condition.

**A thermometer reports.** The icon shows while a kitchen thermometer is
sending readings, whether it is a Bluetooth thermometer or one read from Home
Assistant. Choose **Any thermometer** or one in particular. Tapping it shows
the temperature.

**A Home Assistant device.** Type or pick the entity, then choose when the icon
shows:

| Shows while it | Use it for |
| --- | --- |
| is on, open or heating | A door or window sensor, a switch or plug, an oven or water heater that reports heating |
| is off or closed | Something you want to know is off, such as a freezer plug that lost power |
| is in a state I type | Anything that reports its own words, for example `preheating, cooking` |
| reads above a number | An appliance on a power-metering plug: a sous vide or slow cooker counts as on above a few watts |

Home Assistant's "unavailable" and "unknown" never count, so a sensor that
drops off the network hides its icon rather than showing a false alarm.

### The fridge door

Most fridge door sensors appear in Home Assistant as a `binary_sensor` that is
`on` while the door is open. Press **Fridge door**, pick that sensor from the
entity list (or type its id, such as `binary_sensor.fridge_door`), leave
**Shows while it** on **is on, open or heating**, and save. The fridge icon
then shows in the corner whenever the door is open.

### An appliance on a smart plug

For a sous vide, slow cooker or kettle plugged into a plug that measures power,
pick the plug's power sensor (its unit is W). The item switches to **reads
above a number**; set a number a little above what the plug reads while the
appliance is idle, usually 5 to 10 W.

## Home Assistant and Bandit Remotes

Status icons read Home Assistant through the connection set up on the same
page, so connect Home Assistant first. Pantry Raider only reads the entities
you add here, along with the ones your keys, cameras and thermometers already
use.

The list is shared across the kitchen. A Bandit Remote shows the same icons as
its main server: the server reads Home Assistant and the thermometers, and the
Bandit asks the server what to show. Set the icons up on the main server; on a
Bandit the section is read-only. If the Bandit cannot reach its server, it
shows no icons until it can.
