# What needs the internet

Pantry Raider is built for people who want to run their own kitchen software on
their own hardware, so this page is deliberately blunt about where the app stays
on your local network and where it reaches out to the internet. Nothing here is
hidden behind marketing language: if a feature phones home, it is listed below
with what it contacts, why, and how to turn it off.

**The short version:** the core of the app runs with no internet at all.
Inventory, recipes, expiry tracking, the kiosk, and the Stream Deck all work on
a network with no route to the outside world. The features that need the
internet are the ones that inherently do (looking up a product from its barcode,
fetching a weather forecast, sending a photo to a cloud AI provider, reaching
your kitchen from outside the house), and each of those is optional.

There is no always-on telemetry. Pantry Raider does not report your usage,
inventory, or activity to the developer or to any analytics service. The only
things it contacts are the specific services you configure, when a feature that
needs them runs.

## The matrix

| Capability | Works fully offline? | What it contacts | Why | How to avoid the internet |
|---|---|---|---|---|
| Inventory (Grocy) | Yes | Nothing external; the Grocy container runs on your box | Grocy is the local database of what you have in stock | Nothing to do; it is local by design |
| Recipes, meal plan, and shopping list | Yes | Nothing external | Your recipes and meal plan are stored in Pantry Raider itself and the shopping list in Grocy, all on your box | Nothing to do; it is local by design |
| Recipe suggestions from your library | Yes | Nothing external | Ranking your own recipes by what is in stock is pure local matching | Nothing to do; it is local |
| Adding items by hand | Yes | Nothing external | Manual entry and editing are entirely local | Nothing to do; it is local |
| Barcode lookup | No | Open Food Facts (`world.openfoodfacts.org`) | To turn a scanned barcode into a product name and category | Enter the item by hand; scanning still records the barcode, only the name lookup needs the internet |
| Recipe search from public catalogs | No | TheMealDB and, if you add a key, Spoonacular | Optional discovery of new recipes beyond your own library | Skip external recipe search; your own library and suggestions stay local |
| Community recipes (Forager) | No | Forager (`forager.pantryraider.app`) | Browse, download, and share recipes with other Pantry Raider kitchens; included free with any Forager account | It is opt-in; leave Forager unlinked and use your own library and external recipe search |
| AI photo scanning, local | Yes | Your own Ollama instance on the LAN | Reads a photo of groceries or a receipt into items using a local model | Use Ollama as the AI provider; the image never leaves your network |
| AI photo scanning, cloud provider | No | The provider you choose: Google Gemini, OpenAI, or Anthropic | Higher-accuracy vision when you would rather not run a local model | Switch the AI provider to Ollama, or leave AI unconfigured and enter items by hand |
| AI photo scanning, Forager | No | Forager (`forager.pantryraider.app`) | The managed AI proxy for people who want cloud accuracy without holding their own provider key | It is opt-in; do not link Forager, and use Ollama or manual entry instead |
| Weather panel | No | Open-Meteo, with wttr.in as a fallback | The kiosk forecast needs live weather data and geocoding | Turn the weather panel off; the rest of the kiosk is unaffected |
| Home Assistant integration | Yes, on your LAN | Your own Home Assistant instance | REST sensors, camera pop-ups, and on-screen event toasts | It talks only to your HA server; keep HA on your LAN and nothing leaves it |
| Cameras | Yes, on your LAN | Your own cameras or Home Assistant | Live snapshots and streams on the kiosk and Stream Deck | Point it at cameras on your own network; feeds stay local |
| Remote access (tunnel) | No | Forager's tunnel, or your own Cloudflare Tunnel | To reach your kitchen from outside the house | It is off by default; leave the tunnel disabled and use the app on your LAN |
| Software updates | No | GitHub (source) and the GitHub container registry (`ghcr.io`) | To pull new app code and container images | Updates run only when you trigger one or enable auto-update; disable auto-update to control exactly when it reaches out |
| Kiosk and Start Page | Yes | Nothing external | The on-screen control surface is served by your own app | Nothing to do; it is local |
| Stream Deck | Yes | Nothing external; talks to your app over the LAN | The physical deck drives the local app | Nothing to do; it is local |
| Bluetooth kitchen thermometers | Yes | Nothing external; the probes talk over Bluetooth, or through your own Home Assistant | Live probe temperatures and target alerts | Nothing to do; readings stay on your device or your LAN |
| Label and document printing | Yes | Nothing external; your own printers on the LAN or Bluetooth | Printing food labels, spice labels, and recipes | Nothing to do; printing stays on your network |
| Screensaver photos | Depends | A USB drive or a device folder is local; an Immich album is on your LAN; a list of image links reaches wherever those links point | The photo slideshow's pictures | Use a USB drive, a device folder, or a local Immich; skip remote image links |

## Notes on a few of these

**Barcode scanning is offline; the name lookup is not.** When you scan a
barcode, recording the code and consuming or adding stock is entirely local.
What needs the internet is turning that number into a product name, which comes
from the community Open Food Facts database. If you are offline, or you just
prefer it, type the item name in and everything else works.

**AI scanning is offline only with Ollama.** If you configure a local Ollama
model as the AI provider, photos and receipts are read on your own hardware and
the image never leaves your network. If you choose Gemini, OpenAI, or Anthropic,
the image is sent to that provider so it can do the reading, the same as any
other app that uses those APIs. Forager is a third path: a managed proxy that
holds the provider key for you, so the image goes to Forager and on to the
upstream model. You pick which of these you want, and you can leave AI turned
off entirely and enter items by hand.

**Community recipes are free.** With a Forager account you can browse and
download recipes shared by other Pantry Raider kitchens, and share your own, at
no cost; browsing and submitting are part of the free tier, not a paid add-on.
It is entirely optional: if you do not connect Forager, your own Mealie library
and the public recipe search still work exactly as before.

**Weather always needs the internet.** A forecast is live outside data, so the
weather panel fetches it from Open-Meteo (falling back to wttr.in if that is
rate-limited). If you would rather the kiosk stay fully local, turn the weather
panel off in settings; nothing else on the kiosk depends on it.

**Home Assistant and cameras stay on your LAN.** These integrations talk to
servers and devices you run, not to anything on the internet. As long as your
Home Assistant and cameras live on your own network, none of this traffic leaves
the house.

**Remote access is opt-in.** Out of the box the app is reachable only on your
local network. If you want to open the fridge from the grocery store, you can
enable a tunnel, either through Forager or your own Cloudflare Tunnel. It is off
until you turn it on.

**Updates reach out only when they run.** Applying an update pulls new code from
GitHub and new container images from the GitHub container registry. That happens
when you trigger an update, or on a schedule if you enable auto-update. Turn
auto-update off if you want to decide exactly when the app contacts those
services.
