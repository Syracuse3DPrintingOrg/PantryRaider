# Home Assistant integration

Pantry Raider has a native Home Assistant integration. Add your install once and
it shows up as a device with your food counts, kitchen timers, thermometer
readings, printer status, and screen settings as Home Assistant entities. No
templates or REST sensors to hand-edit.

If you run a server or a Pi appliance, any bandits (satellite screens) it knows
about appear as their own devices underneath it, so a wall screen in the garage
and the main pantry box each get their own controls in Home Assistant.

## No key copying needed

When you add the integration, leave the API key field blank: Home Assistant
asks your install to pair, a four digit code appears on the kitchen screen,
and one tap of Approve there (or in Pantry Raider under Settings, Bandit
Remotes) hands Home Assistant a key of its own, limited to what the
integration needs. It is listed, and can be revoked, under Settings, Security.
Pair even when your install has no password: Home Assistant without a key
stops working once a password is set, and until it is paired Home Assistant
shows a Repairs notice saying so. Device pairing is turned on in Pantry Raider
under Settings, Security (Allow devices on your home network to request
access). Paste a key manually only if you have turned device pairing off.

Already set up from an earlier version? Update the integration in HACS and
restart Home Assistant. An entry that used the install's shared key moves to
a key of its own by itself; one that cannot asks you to approve a pairing code
on the kitchen screen.

## It finds itself

Installs on your network announce themselves, so a running Pantry Raider
usually shows up in Home Assistant on its own under **Settings, Devices &
Services** as a discovered device waiting to be set up. Click it, confirm the
install it names, and you are done (if the install uses a password, you are sent
to approve pairing on the kitchen screen, exactly as below). A discovered
install that later changes its IP address is updated for you, so nothing breaks
when your router hands it a new lease.

Autodiscovery needs Home Assistant and the install to share a network that
carries these announcements. A server running in a Docker bridge network (a
common setup on a NAS) does not broadcast onto your LAN, so add it by hand with
the steps below; the address is the only thing you need.

## Add it

### With HACS (recommended)

1. In HACS, open the three-dot menu and choose **Custom repositories**.
2. Add `https://github.com/Syracuse3DPrintingOrg/PantryRaider` as an
   **Integration**.
3. Search HACS for **Pantry Raider**, install it, and restart Home Assistant.
4. Open **Settings, Devices & Services, Add Integration**, search for
   **Pantry Raider**, and fill in your address.

### By hand

1. Copy the `custom_components/pantry_raider` folder from the project's
   `homeassistant` directory into your Home Assistant `config/custom_components`
   folder.
2. Restart Home Assistant.
3. Add the integration from **Settings, Devices & Services**.

## What to enter

- **Host**: the local address of your install, such as `192.168.1.50`. Use the
  local address, not a public web address. Home Assistant talks to the app
  directly, and a public login page would get in the way.
- **Port**: `9284` unless you changed it.
- **API key**: leave it blank to pair, and approve the code on the kitchen
  screen. To paste a key instead, create one in Pantry Raider under
  **Settings, Security**, Create a key, choosing The Home Assistant
  integration.

You can change how often Home Assistant checks in (every 30 seconds by default)
from the integration's **Configure** button later.

## What appears, by install

The integration only offers what a given install can actually do.

A **server** or **Pi appliance** gives you:

- Counts for expired, expiring today, expiring within 3 days, and expiring
  within 7 days.
- Pending scans waiting for review, and open action items.
- Timers running (a count), a plain-language **Timers** readout that lists every
  running timer at a glance ("Pasta 7:42, Soft egg 2:10", or "none"), and the
  next timer with its label and countdown. The Timers running and Timers
  sensors both carry the full list of running timers as attributes, so a
  dashboard card can show each countdown.
- A temperature sensor for each Bluetooth thermometer probe, which goes
  unavailable when the thermometer drops out or its battery dies.
- The label printer queue and the app version.
- Presence, when the screen reports it, and an "expiring attention" alert that
  turns on when anything is already expired or expires today.
- Display sleep and screensaver delay in minutes (as sliders), the screensaver
  style, and how presence wakes the screen.
- On an appliance with its own screen, **Sleep screen** and **Wake screen**
  buttons.

A **bandit** (a satellite screen, whether you add it directly or it is
discovered under a server) gives you just its own device controls: presence,
display sleep, screensaver delay and style, wake on presence, **Sleep screen**
and **Wake screen** buttons, its printer queue, and version.

## The screen controls

The sleep and screensaver settings can fight each other, so it helps to know how
they line up:

- **Sleep screen** and **Wake screen** are buttons: they blank or wake the panel
  right now, which is handy in an automation (sleep the kitchen screen at
  bedtime, wake it when the morning motion sensor trips). A press that cannot
  act, for example on a server with no screen of its own, reports why rather
  than failing silently.
- **Display sleep** is how many minutes of no activity before the panel goes
  fully dark, and **Screensaver delay** is how many minutes before the
  screensaver starts. Here is the catch: if sleep fires first, the panel is
  already dark and the screensaver never gets a chance to appear. If you want to
  see the screensaver, set its delay shorter than the sleep time, or set
  Display sleep to 0 to turn sleeping off entirely.

Bandits under a server are added on their own as they check in. If a bandit
drops off the network for a moment its entities stay put and simply go
unavailable, so your automations are not disturbed; they come back when it
returns. To drop a bandit that is truly gone for good, reload the integration.

## Notifications and camera pop-ups

Every Pantry Raider device gets a **Notify** entity, so putting a message on a
kitchen screen is a plain notify action, with no YAML anywhere. Send a message
(and an optional title) to a device's notify entity and it appears as an
on-screen toast on that device: the main install's entity targets the main
screen, and each bandit's entity targets that bandit's screen.

```yaml
automation:
  - alias: "Laundry done -> tell the kitchen"
    trigger:
      - platform: state
        entity_id: sensor.washer_status
        to: "complete"
    action:
      - action: notify.send_message
        target:
          entity_id: notify.pr_notify
        data:
          message: "The laundry is done."
          title: "Washer"
```

The entity id follows the device's name, so an install whose hostname is `pr`
gets `notify.pr_notify` and a bandit named `kitchen` gets something like
`notify.pantry_raider_bandit_kitchen_notify`; pick the exact one from the
device's page in Home Assistant.

The integration adds three services. Two of them cover the camera events,
and both take a Pantry Raider device (pick the screen the pop-up should
appear on; with a single install you can leave it empty), the camera's name as configured on Pantry Raider's Cameras page (empty
means the first one), and an optional length in seconds (0 uses the length set
in Pantry Raider):

- **`pantry_raider.camera_popup`** pops the camera feed up right away, for
  example when someone rings the doorbell.
- **`pantry_raider.camera_detect`** reports what a camera saw (`person`,
  `vehicle`, `animal`, or `visitor` for a doorbell press) and lets each
  camera's own "pop up on" checkboxes in Pantry Raider decide whether the
  camera actually appears. One automation can send every detection type it
  sees; turning a type on or off happens on the Cameras page, not in Home
  Assistant.

```yaml
automation:
  - alias: "Person at door -> pop up camera"
    trigger:
      - platform: state
        entity_id: binary_sensor.front_door_person
        to: "on"
    action:
      - action: pantry_raider.camera_popup
        data:
          device_id: YOUR_KITCHEN_SCREEN_DEVICE
          camera: "Front Door"
          seconds: 20
```

### Probe alerts

When a thermometer probe reaches its target, Pantry Raider calls the
integration's **`pantry_raider.probe_alert`** service, which fires the
`pantry_raider_probe_alert` event with the alert's details. Automations that
listen for that event keep working exactly as before; you do not call the
service yourself. Settings, Home Assistant in Pantry Raider shows whether
probe alerts are getting through. Without the updated integration, Pantry
Raider still sends the event the older way until at least 0.23.

Notifications and pop-ups show when **Show notifications** is on in Pantry
Raider under Settings, Home Assistant (it is on by default, and each device
can override it for its own screen).

### Advanced: without the integration

Installs without the integration can still receive notifications the original
way: a `rest_command` in Home Assistant's `configuration.yaml` posting to the
device's `/events/notify`, `/events/camera-popup`, and `/events/camera-detect`
endpoints with your Pantry Raider API key. Make the key for it in Pantry Raider
under Settings, Security, Create a key, choosing Home Assistant sensors,
buttons and ESPHome panels; on an install with no password you can create it
from any device at home without a password. The ready-to-paste YAML for that
route lives in Pantry Raider under Settings, Home Assistant, in the "Advanced
setup (manual rest_command + automation)" section, with this device's address
already filled in. With the integration installed you never need it.

## Cameras and Stream Deck connect on their own

Pantry Raider needs a link back to Home Assistant for Stream Deck and
on-screen keys, camera pictures, and thermometers. Setting up this integration
wires that up for you, so you do not have to paste a token into Pantry Raider
by hand.

With **Let Pantry Raider connect back to Home Assistant** turned on (the
default, under the integration's **Configure** screen), the integration gives
Pantry Raider a limited Home Assistant account of its own:

- It creates a Home Assistant user named after your install, for Pantry
  Raider only. That user is never an administrator, and it is read-only unless
  one of your Pantry Raider keys controls a device (a light, a switch, a
  scene), in which case it may control devices too. The integration adjusts
  it on its own as you add or remove such keys.
- It hands Pantry Raider that user's token and your Home Assistant address,
  and replaces the token once a year. Pantry Raider shows the account in use
  and when its token expires under **Settings, Home Assistant**.
- Bandits do not need a token of their own: they reach Home Assistant through
  their main server, which passes along only the entities and actions your
  keys, cameras and sensors use. A Bandit with its own Home Assistant
  thermometers or Bluetooth probe targets still receives the token.
- A token you pasted into Pantry Raider yourself is left alone, and a Repairs
  notice explains how to switch to the limited account if you want to.
- An install too old for a limited account gets a token for your owner
  account, as before, and Home Assistant shows a notice asking you to update
  Pantry Raider. While your owner account is in use, Home Assistant reminds
  you once a week. Once every Pantry Raider install in this Home Assistant
  uses a limited account, the integration removes the owner token it created
  earlier. It never removes a token another entry or you created.

Keys that unlock, open or disarm something (locks, alarms, garage doors,
covers) run only after you allow them on that key in Pantry Raider under
Settings, Start Page, whatever account Home Assistant gave it.

Turning connect-back off asks Pantry Raider to forget the connection and
removes only the tokens this integration created. Deleting the integration
entry also removes the user it made. If Home Assistant makes the token but the
install cannot reach the address it was given (for example a container that
cannot see your internal URL), the log says so and you can adjust the address
in Pantry Raider under Settings, Home Assistant.

## When something needs your attention

- **Re-authenticate.** If Pantry Raider turns the integration's key away (for
  example after you revoked it), Home Assistant shows Re-authenticate on the
  integration. Leave the key empty to pair again, or paste a new key made
  under Settings, Security.
- **Repairs notices** in Home Assistant explain what to do when the install
  still accepts Home Assistant without a key (pair now), when device pairing is
  turned off, when Pantry Raider needs an update to use a limited account, and
  when an install without a password is counting down to the day it stops
  answering through a proxy or by a name it does not know.

## Moving from the old YAML sensors

Earlier setups pasted REST sensors from `configuration.yaml` and, for wall
panels, `espcontrol.yaml`. Those still work and remain the manual alternative if
you prefer to hand-manage everything. The integration replaces them for exposing
entities: it discovers your install, keeps unique IDs stable, groups everything
under proper devices, and adds bandits for you. You can run the integration and
retire the `sensor.food_*` REST sensors at your own pace.

The YAML sensors and ESPHome panels send a key in an `X-API-Key` header. Make
one in Pantry Raider under Settings, Security, Create a key, choosing Home
Assistant sensors, buttons and ESPHome panels, and keep it in Home Assistant's
`secrets.yaml` as `foodassistant_api_key`. On an install with no password the
key needs no password to create from a device at home, and it keeps the
sensors working once you set one.

The on-screen event toasts no longer need their own YAML either: with the
integration installed, the notify entities and the camera pop-up services
described under "Notifications and camera pop-ups" replace the
`rest_command` snippets. And Pantry Raider's own link to Home Assistant for
camera feeds and Stream Deck keys is handled for you, as described under
"Cameras and Stream Deck connect on their own" above.

## Home Assistant version and the integration icon

The integration needs **Home Assistant 2025.2.0 or newer**. It uses parts of
Home Assistant that only exist from that release on, so on anything older it
will not load. Update Home Assistant first if you are behind.

The Pantry Raider icon ships inside the integration itself and appears
automatically on Home Assistant 2026.3.0 or newer. On older Home Assistant
versions the integration shows a generic placeholder icon until you update
Home Assistant; everything else works the same.
