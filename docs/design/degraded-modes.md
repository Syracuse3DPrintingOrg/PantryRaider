# Degraded modes: every page during an outage

Pantry Raider depends on services that can be down while the app itself is
fine: Grocy (inventory), Mealie (recipes, meal plan, shopping), and, on a
satellite, the main server that fronts both. This page records how every
user-facing surface behaves during each outage shape, and the pattern the
fixes follow (FoodAssistant-2cmm).

## The pattern

- The service clients (`services/grocy.py`, `services/mealie.py`) convert a
  dead connection into a typed `GrocyError` / `MealieError` carrying honest,
  user-forward copy ("Grocy is not reachable. Inventory will return when it
  is."). On a satellite, where both clients talk through the main server's
  proxy, the copy names the main server instead. One wrap point means every
  route that already handles the typed error degrades the same way, and no
  raw `httpx` connection error can bubble up as a 500 page.
- JSON endpoints answer an outage with a 502 whose `detail` is that copy (or
  an `error` field on endpoints whose pages parse the body directly), never a
  stack trace.
- Pages render the copy in a warning banner (the shared
  `templates/_upstream_down.html` partial for server-rendered pages, the
  fetch error path for JS-driven ones) and keep the page shell navigable.
  Nothing pretends to be empty: an outage never reads as "all clear", "no
  lists yet", or "no locations found".
- No caching layers were added in this pass. Deliberately: this pass is about
  banner honesty only.

The satellite forwarders (pending, audit, timers, action items) answer a dead
main server with the same copy: "The main server is not reachable. This will
work again when it is."

`tests/test_degraded_modes.py` pins the behavior with a TestClient and a
monkeypatched connection that always refuses.

## Audit: page by outage

Legend for the before column: **graceful** (a designed message), **raw** (a
5xx, a stack trace, or technical text on screen), **silent** (spinner forever
or a false empty state).

| Page | Grocy down | Mealie down | Main server unreachable (satellite) |
|---|---|---|---|
| Inventory (`/ui/inventory`) | Was **raw** (red "Failed to load:" with a raw 500 body in every panel). Now a warning banner with the honest reason; panels read "Unavailable right now". | n/a | Same as Grocy down, banner names the main server. |
| Expiring (`/ui/expiring`) | Was **silent** (empty list rendered as the celebratory "Nothing expiring" state). Now the outage banner, and the celebration is suppressed. | n/a (Use-it-up tips stay; AI ideas depend on the provider, not Mealie) | Same as Grocy down, banner names the main server. |
| Manage Pantry (`/ui/add`), Stock up tab | Graceful by design: scans queue locally, the duplicate hint is simply omitted. Committing a queued item reports the honest reason per row. | n/a | Scans forward to the server; a failure now reports "The main server is not reachable" instead of a raw exception string. |
| Manage Pantry, Use stock tab | Was **raw-ish** (an outage was reported as "no product linked to this barcode"). Now the outage reason is reported as itself. | n/a | Same, names the main server. |
| Manage Pantry, Shopping tab | n/a | Was **silent** ("no shopping list in Mealie" during an outage). Quick-add and scans now report the honest reason. | Same, names the main server. |
| Manage Pantry, Audit tab | Status stays local and works; starting a count reports the honest reason. | n/a | Forwarded; honest 502 copy. |
| Pending (`/ui/pending`) | Graceful: the list is local; only the duplicate badge is omitted. Commit errors carry the honest reason. | Auto-check of shopping items is skipped silently by design (never blocks a commit). | Was **silent** (a 502 body parsed as an empty queue: "all caught up"). Now a banner with the honest reason and Commit disabled. |
| Cook (`/ui/cook`) | Was **silent** (stock quietly treated as empty, every recipe filed under "worth shopping for"). Now suggestions still render plus a banner saying inventory matching is paused. | Was **raw-ish** (alert with "Mealie 0 on /recipes..." style text). Now the alert carries the honest copy. | Both banners name the main server. |
| Recipes (`/ui/recipes`) | n/a | Alert now carries the honest copy instead of technical text. | Same, names the main server. |
| Meal plan (`/ui/mealplan`) | n/a | Alert now carries the honest copy. | Same, names the main server. |
| Shopping (`/ui/shopping`) | Restock suggestions section hides quietly (an extra, not the page's point). | Was **silent** ("No shopping lists in Mealie yet: create one first"). Now a warning banner with the honest reason. | Same, names the main server. |
| Timers (`/ui/timers`) | n/a | n/a | Graceful by design: the page keeps the last snapshot and counts down locally from `deadline_epoch`; starts/cancels answer with the honest 502 copy. |
| Start Page (`/ui/start`) | n/a | n/a | Timer key faces keep ticking locally; a key press that needs the server toasts "Could not reach the server." |
| On the Line (`/ui/current-recipe`) | Marking cooked reports the honest reason. | Loading a recipe from Mealie reports the honest copy. | Forwarded timer actions answer with the honest 502 copy. |
| Weather (`/ui/weather`) | n/a | n/a | Graceful by design: fetched directly from Open-Meteo with a wttr.in fallback and reason faces; no dependency on the three outage shapes. |
| Cameras (`/ui/camera`) | n/a | n/a | Graceful by design: designed 502 messages per feed ("Camera unreachable", diagnostics page). Depends on Home Assistant, not the three outage shapes. |
| Nutrition (`/ui/nutrition`) | n/a | n/a | Local intake log, no upstream dependency. Estimates need the AI provider, which is out of scope here. |
| Audit (`/ui/audit`) | Was **silent** ("No stocked locations found" during an outage, from a raw 500). Now the honest reason shows in the location picker. | n/a | Forwarded; honest 502 copy. |
| Journal (`/ui/journal`) | Alert now carries the honest copy instead of technical text. | n/a | Same, names the main server. |
| Convert (`/ui/convert`) | n/a | n/a | Static, no upstream dependency. |
| Kitchen guide (`/ui/kitchen-guide`) | n/a | n/a | Static, no upstream dependency. |
| Settings (`/setup`) | Service status rows show the connection test result; the page itself renders. | Same. | Graceful by design: a satellite keeps the last synced backend config (`services/satellite.py`) and re-syncs when the server returns. |
| HA / deck endpoints (`/expiring/summary`, `/mealie/*/summary`, counts) | Summary answers 502 with honest copy; the deck counts keep their designed soft zeros. | Counts degrade to `{"count": 0}` by design; the meal plan summary reports `error: unreachable`. | Same paths through the proxy. |

## Follow-ups deliberately out of scope

- Cached last-known-good data (inventory, meal plan) so a banner can sit above
  yesterday's list instead of an empty page. That is a separate pass with its
  own staleness rules; this one is banner honesty only.
