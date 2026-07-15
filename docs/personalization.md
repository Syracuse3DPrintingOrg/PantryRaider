# Personalization and On-screen Features

The Settings page has two menus behind a toggle at the top. Personalization
(the default) holds the things you change often: Appearance, Screen & Sleep,
the Start Page & Stream Deck editors, and Recipe Preferences. Settings holds
the set-and-forget administration: connections, security, backups, and the
like. A search box above the menu filters both menus as you type and jumps
to the match. This page covers the appearance and on-screen features and
where each lives.

## Themes

Settings, Personalization, Appearance. Pick a colour theme for the whole app; it
applies on every page and device because it is stored on the server.

- **Built-in themes:** a default dark and light, plus several bundled
  "fun" themes (Darkly, Cyborg, Flatly, Synthwave, Solarized, Midnight, Forest,
  iOS Light/Dark, Outrun, Vaporwave). The fun themes are vendored locally under
  `service/app/static/vendor/themes/`, so they make no external requests.
- **Custom themes:** build your own palette (a light or dark base plus primary,
  accent, background, surface, and text colours), give it a name, and Save. It
  joins the Theme dropdown and applies immediately. You can keep several named
  custom themes and switch between them, and delete one from the builder.

All bundled themes are contrast-checked so the selected menu item, the Save
buttons, and the status badges stay legible.

## Background image

Settings, Personalization, Appearance, Background image. Paint an optional photo
behind the whole UI. Upload a JPG, PNG, WebP, or GIF (up to 8 MB) or paste an
image URL, and use the opacity slider so the interface stays readable over it.
It applies on every page.

## Navigation

The menu is a short list of top-level areas (Glance, Inventory, Manage,
Review, Cook, Shopping, Kitchen Guide, Time & Temp, Home Hub), and opening an
area shows its sub-pages as a full-width row of tabs in the header, so Cook
holds Recipes, On the Line, and Meal Plan, and Time & Temp holds Timers and
Thermometers. On a phone or a narrow panel the top row tucks into the menu
button, which lists every area, so navigation works one-handed. All of it can
be rearranged.

Settings, Personalization, Appearance holds the tab editor; the on-screen
navigation bar lives under Personalization, Screen & Sleep.

- **Tab editor:** drag a row to reorder it, or use the up/down buttons. Drop a
  tab onto a folder (or onto any tab already inside one) to nest it; the parent
  becomes a top-level area with its own sub-page row. Add a heading to create a
  folder that groups tabs but has no page of its own. Hide tabs you do not use
  with the switch. Tabs for services that are not configured (for example the
  Camera page) hide automatically, and every page stays reachable by direct
  URL. A Reset to defaults button restores the original order, grouping, and
  visibility.
- **Home page:** visiting the app (`/ui`) opens whatever page leads the
  navigation menu. By default that is Glance, the self-building home screen
  (see below); hide it or move another page to the top and that page becomes
  home instead.
- **On-screen navigation bar:** an optional fixed bar of nav icons docked to a
  screen edge (bottom, left, or right), handy on touchscreens. It reserves
  layout space so it never overlaps content. The dock position is a per-device
  choice that overrides the server default.

## Home screen: Glance, or a custom Start Page

Settings, Personalization, Start Page & Stream Deck. The home screen is served
at `/ui/start` and comes in two styles, picked with the Home style switch:

- **Glance** (the default) builds itself: big square buttons for your main
  pages (Inventory, Manage, Review, Cook, Shopping, and the rest) plus a row
  of small pills with live counts of what needs you, items to review,
  on-screen alerts, and food expiring this week. Each pill jumps straight to
  its page. Glance follows your navigation, so hiding or reordering pages
  reshapes the home too.
- **Custom layout** is a hand-arranged launcher that works like an on-screen
  Stream Deck. If you had already built a custom Start Page, it stays exactly
  as you left it.

The custom layout offers:

- Choose 6, 15, or 32 keys (the Stream Deck grid sizes); the keys scale to fill
  the screen without scrolling.
- The editor uses the same key catalog, the same shared custom-key library, and
  the same key style and icon options as the physical Stream Deck. Build or edit
  a custom key (Home Assistant action, timer, weather, camera, media, macro) on
  either side and it appears on both; Stream Deck key placements are preserved.
- The Glance tab leads the navigation by default, so either home style acts as
  the device's home screen (including the kiosk).
- Built-in keys open the matching app page. Custom keys that map to a page
  (timer, shopping, weather, camera) open it.
- Action keys fire from the screen, no deck required: a Home Assistant toggle,
  a media key, a macro, and any legacy HA slot keys (ha_1 to ha_5) still on a
  saved layout call Home Assistant through the server using the shared HA
  settings, and show the result in a small toast. When building a Home
  Assistant key, the editor lists your actual devices from Home Assistant:
  pick one and the right action and label are filled in for you. A macro runs its HA slot and preset kitchen-timer
  steps (the timers become shared server timers); steps that need deck hardware
  (paging, brightness) are skipped and named in the toast. Purely hardware-bound
  keys still note that they run on a connected Stream Deck.

On a Pi appliance the section shows a toggle at the top to switch between
the on-screen Start Page and the physical deck's editor.

## Screen & Sleep

Settings, Personalization, Screen & Sleep gathers everything about the screen:
interface scale, rotation, display sleep, the screensaver, the on-screen
navigation bar and keyboard, quiet mode, and (on a Pi appliance) the scheduled
reboot.

- **Display sleep** switches a kiosk panel off after the idle minutes; a touch,
  key press, or Stream Deck button wakes it. On kits with the built-in
  accelerometer, **Wake on motion** also wakes the screen when the device is
  moved or bumped (Auto turns it on exactly when the sensor is fitted).
- **Scheduled reboot** (Pi appliances) restarts the device automatically:
  Off, Nightly, or Weekly with a day-of-week picker, at the time you choose.
- **Return to home when idle** drifts a kiosk back to its home page after a
  set time without a touch, so the next person starts fresh instead of on
  someone else's recipe or forecast. Pages you actively watch (Cook, On the
  Line, Weather, Cameras, Timers) are left alone so the screen never jumps
  away mid-cook. Off by default, and a per-device choice.
- **Quiet mode** silences the timer chime on this device, leaving the
  highlighted timer row as the only signal.
- **On-screen keyboard**: in kiosk mode a touch keyboard slides up whenever a
  text field is tapped, with shift, a digits row, and Enter, so names,
  barcodes, and searches can be typed without a physical keyboard. On by
  default; turn it off on a kiosk with a keyboard attached (per device).

A kiosk also plays a short branded intro when it boots: the raccoon fades in,
holds a moment, and dissolves into the app. It plays once per boot and a touch
or key press skips it.

## Screensaver

The screensaver is the softer counterpart to Display sleep: after the idle
minutes the page dims to a moving clock instead of powering the panel off,
which suits panels that wake slowly or misbehave when switched off. Any touch
brings the page right back. It is configured per device in Screen & Sleep:

- **Style**: the bouncing Pantry Raider logo (with a slow, normal, or fast
  glide speed), a retro canvas saver (flying toasters or a starfield), or a
  **photo slideshow**. The pictures fill the screen with a slow pan and
  crossfade; with no photos to show the saver falls back to the logo, so the
  setting is always safe to leave on.
- **Screensaver photos**: the slideshow can draw from several sources. Point it
  at a USB flash drive (put images in a folder named photos or pictures at the
  top of the drive), a folder on the device itself, an **Immich** album (give
  its address, an API key, and the album id), or a plain list of direct image
  links. Google Photos and iCloud do not offer reliable access for third-party
  apps, so they are not on the list, and the settings say so plainly rather
  than pretending otherwise.
- **Running timers float along**: each timer drifts around as a pill with its
  name, live countdown, and a food icon picked from the name. A pill in its
  last minute breathes a pulsing pink glow, and a finished timer pulses red
  and amber, reads Done, and spins until it is dismissed.
- **Screensaver on every browser** extends the saver beyond the kiosk: any
  browser viewing the install (a desktop or a phone included) dims after the
  same idle minutes. Because of this, the screensaver settings also appear on
  server installs.
- **The Stream Deck rests with the display**: while the display is asleep,
  an attached Stream Deck shows the Pantry Raider logo across its keys
  instead of the buttons (a switch in the Stream Deck settings, on by
  default). Pressing any key or touching the screen wakes both surfaces.
- **Test screensaver** starts the saver immediately with the options picked in
  the form, no waiting for the idle timeout. There is also a Screensaver
  button on the Timers page for the same jump.
- The saver stays out of the way of cameras: it never starts while the camera
  page is open or a Home Assistant camera pop-up is on screen.

## Kitchen timers

Timers are shared: they live on the main server, so the Timers page, the
floating timer window, the Stream Deck keys, the Start Page keys, and every
satellite screen show the same countdowns. Timers lives under Time & Temp in
the navigation, alongside Thermometers.

- The Timers page has one-tap presets (1 to 60 minutes) and a custom timer
  with an optional name. A **Common foods** row adds one-tap named timers for
  everyday staples too: a soft or hard egg, pasta, rice, steamed veg, a baked
  potato, or a tea steep. Each running timer has a **+1 min** button and a
  Cancel button (Dismiss once it finishes), and a **Clear all** button stops
  every timer at once after a confirmation.
- On a Stream Deck or the Start Page, a timer key shows the live countdown on
  its face: a press starts it, each press while it runs adds a minute, a press
  on a finished timer dismisses it, and holding the key resets it.
- Step durations in the active recipe ("simmer 20 minutes") become ready-to-
  start named timers on the On the Line page and the deck's timer keys.
- Bluetooth kitchen thermometers share the page: a connected probe shows its
  live temperature in big numbers with its battery state, and a target you set
  pops an on-screen alert when it is reached. The Time & Temp tabs in the
  header switch between Timers, Thermometers, and the combined view, and in
  the combined view the timers area folds down when nothing is running so the
  temperatures get the screen. See
  [Bluetooth kitchen thermometers](thermometers.md) for setup.

## Weather

The Weather page (`/ui/weather`, under Home Hub in the navigation) shows the current
conditions and a multi-day forecast. Set the location, units, and (under
Advanced) the weather server with the gear button on the page itself; there is
no separate weather settings menu. The location is a city, a ZIP, or
`lat,lon`; leave it empty to auto-detect. The same values drive any Stream Deck
weather keys. The forecast uses Open-Meteo by default (point the Weather server
at a self-hosted Open-Meteo if you run one) and falls back to wttr.in. The page
also shows a short, food-themed line based on the forecast, hot enough to fry
an egg on a scorcher, soup weather on a cold day, a nudge that the grill might
get rained on. There is always a line, calm mild days included, and it keeps a
few takes for each kind of weather and rotates through them day by day.

## On-screen Home Assistant events and camera pop-ups

Settings, Connections, On-screen notifications and camera pop-ups. A Home
Assistant automation can push notification toasts and full-screen camera
pop-ups to a device's display. Whether a device shows them is a per-device
choice (follow the server default, always show, or never show), which is useful
for a headless server or for picking which Pi Remote displays pop-ups. The
Home Assistant Connection section shows a status badge (configured, connected,
or not reachable) on both the server and satellites.

A camera can also pop itself up on a detection without any Home Assistant
automation to write. Turn it on per camera to show an on-screen pop-up when a
person, vehicle, animal, or doorbell visitor is detected, from a Home
Assistant camera's own detection entities or from a Reolink camera checked
automatically in the background. Reolink doorbells and other two-way-talk
models are flagged as such, since a doorbell visitor is worth a pop-up in a
way a passing car usually is not.
