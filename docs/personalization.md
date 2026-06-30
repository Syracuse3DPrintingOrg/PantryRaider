# Personalization and On-screen Features

FoodAssistant separates everyday, taste-level settings (Personalization) from
the set-and-forget configuration (Settings). Open Settings and use the toggle at
the top of the page to switch between the two. This page covers the
Personalization features and the on-screen surfaces they drive.

## Themes

Settings, Personalization, Theme. Pick a colour theme for the whole app; it
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

Settings, Personalization, Theme, Background image. Paint an optional photo
behind the whole UI. Upload a JPG, PNG, WebP, or GIF (up to 8 MB) or paste an
image URL, and use the opacity slider so the interface stays readable over it.
It applies on every page.

## Navigation

Settings, Personalization, Navigation.

- **Tab editor:** drag a row to reorder it, or use the up/down buttons. Drop a
  tab onto a folder (or onto any tab already inside one) to nest it; the parent
  becomes a dropdown. Add a heading to create a folder that groups tabs but has
  no page of its own. Hide tabs you do not use with the switch. Tabs for
  services that are not configured (for example Mealie pages) hide
  automatically, and every page stays reachable by direct URL. A Reset to
  defaults button restores the original order, grouping, and visibility.
- **First page:** visiting the app (`/ui`) opens whatever page leads the
  navigation menu, so moving a page to the top makes it the home screen.
- **On-screen navigation bar:** an optional fixed bar of nav icons docked to a
  screen edge (bottom, left, or right), handy on touchscreens. It reserves
  layout space so it never overlaps content. The dock position is a per-device
  choice that overrides the server default.

## Start Page (on-screen Stream Deck)

Settings, Personalization, Start & Stream Deck. The Start Page is an optional
full-screen launcher that works like an on-screen Stream Deck, served at
`/ui/start`. It is off by default.

- Choose 6, 15, or 32 keys (the Stream Deck grid sizes); the keys scale to fill
  the screen without scrolling.
- The editor uses the same key catalog, the same shared custom-key library, and
  the same key style and icon options as the physical Stream Deck. Build or edit
  a custom key (Home Assistant action, timer, weather, camera, media, macro) on
  either side and it appears on both; Stream Deck key placements are preserved.
- When enabled, the Start tab is added to the navigation and defaults to the top,
  so the Start Page can act as the device's home screen (including the kiosk).
- Built-in keys open the matching app page. Custom keys that map to a page
  (timer, shopping, weather, camera) open it; deck-only actions (Home Assistant
  entity, brightness) render but note that they run on a connected Stream Deck.

On a Pi appliance, the Start & Stream Deck menu shows a toggle at the top to
switch between configuring the on-screen Start Page and the physical deck.

## Weather

The Weather page (`/ui/weather`, also a navigation tab) shows the current
conditions and a multi-day forecast. Set the location, units, and (under
Advanced) the weather server with the gear button on the page itself; there is
no separate weather settings menu. The location is a city, a ZIP, or
`lat,lon`; leave it empty to auto-detect. The same values drive any Stream Deck
weather keys. The forecast uses Open-Meteo by default (point the Weather server
at a self-hosted Open-Meteo if you run one) and falls back to wttr.in.

## On-screen Home Assistant events

Settings, Home Assistant, On-screen notifications and camera pop-ups. A Home
Assistant automation can push notification toasts and full-screen camera
pop-ups to a device's display. Whether a device shows them is a per-device
choice (follow the server default, always show, or never show), which is useful
for a headless server or for picking which Pi Remote displays pop-ups. The
Home Assistant Connection section shows a status badge (configured, connected,
or not reachable) on both the server and satellites.
