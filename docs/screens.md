# Screens: tablets and wall panels

Any tablet or wall panel in your kitchen can show the recipe you are cooking
and your running timers, even if it is not a Bandit. That covers a tablet
running a kiosk app such as Kiosk Satellite or Fully Kiosk, a View Assist
display, or a spare phone with a browser open.

Once a screen is connected, Home Assistant (or anything else that talks to
Pantry Raider) can send a recipe, a timer, a notification or a camera pop-up
to that one screen. The other screens stay on whatever they were showing.

## What a screen shows

A connected screen has four pages, built for touch and for older, slower
tablets:

- **Home**: **Continue cooking** with the recipe and the step you are on,
  and your running timers.
- **Timers**: every timer with its time left, **+1 min** and **Cancel**,
  and buttons to start a quick timer. When a timer runs out the whole screen
  turns red and says which one is done and, for a recipe step's timer, what
  to do next, with **Dismiss** and **+1 min**.
- **Recipe**: the whole recipe in large type. A row at the top has a button
  for every timer the recipe calls for ("Start 10 min · Bake crust"), and
  each step has its own. Once tapped, a button shows that timer counting
  down, so a second tap never starts a second timer. Tap a step to mark it
  as the one you are on; **Back** and **Next** along the bottom move the
  marker. On a narrow portrait screen the steps come first and the
  ingredients wait behind **Show ingredients**.
- **Cook mode** (**Start cooking** on the recipe): one step at a time, in
  type big enough to read across the kitchen, with that step's timers,
  your running timers, what comes next, and an **Ingredients** button that
  starts with the ones this step uses. **Back**, **Next** or a swipe moves
  between steps. After the last step, **Mark as cooked** uses up the
  matching items in your pantry, the same as **Cooked** on the recipe page.
  **Exit** at the top puts the screen back.

### The step you are on

Each screen remembers the recipe it is showing and the step you are on, so
two tablets can cook two different things. Touch and voice move the same
marker: tap a step, press **Next**, or say "next step" to the voice device
for that screen, and the screen follows. "Start the timer" means the timer
of the step you are on.

### The whole of Pantry Raider on a tablet

A connected tablet can also switch into the whole Pantry Raider interface,
starting at the start page: inventory, scanning, shopping, recipes, the meal
plan, everything a household member sees. Settings stay out of reach, since a
wall tablet is touched by anyone who walks past.

Say "open Pantry Raider" to a voice device that has that screen, or use the
button your kiosk app offers. It leaves by itself after five minutes on the
start page with nobody touching it; on any other page it stays until you
press **Exit** in the top right corner or say "close Pantry Raider". A finished timer still takes over the
screen with **Dismiss** while it is open.

While the whole interface is open, a running timer shows as a bar across
the top of every page: it drains as the time runs down and flashes when the
timer is done. The same bar appears on any kitchen display that has no key
pad.

A timers page that opened because a timer was set closes itself ten seconds
after the last timer is done or dismissed.

Timers are shared by your whole kitchen, so a timer started by voice, on a
Bandit, or from the Timers page shows on every screen, and dismissing it on
one screen dismisses it everywhere.

## Connect a screen

Each screen gets its own key, which can only show recipes and timers. It
cannot open your inventory, shopping list or Settings.

1. In **Settings, Security**, under keys, create a key and choose **A tablet
   or wall screen that shows recipes and timers**. Copy it when it is shown;
   it is not shown again.
2. Enter your kitchen's address and that key in the screen's app. A kiosk
   app with Pantry Raider support registers itself and appears in the list
   of screens under the name you gave it.

A kiosk app can also ask to pair instead: it shows up in **Settings,
Bandit Remotes** as a pairing request, and approving it there gives it a screen key
without anything to type.

To turn a screen off, revoke its key in **Settings, Security**. The screen
is signed out at once.

### A plain browser

On a kitchen with no password, open `http://<your kitchen>/screen/` on the
tablet and it works straight away. Add `?screen=<a name>` after registering
that name if you want the page to follow what is sent to it.

## Which screens there are

Three kinds of screen can be sent something:

| Screen | How it gets there |
|---|---|
| The main kitchen display | The display attached to your main Pantry Raider, in kiosk mode |
| A Bandit | Each Bandit's own display, once the Bandit is on this release or later |
| A tablet or wall panel | Any screen connected with a screen key, as above |

A Bandit on an older release still appears in the list, marked as needing an
update before anything can be sent to it.

Only the display itself moves. Sending a recipe to the main kitchen display
does not move a phone or laptop that happens to have Pantry Raider open.

## For developers

Everything is plain JSON over HTTP with the key in an `X-API-Key` header.

| Call | What it does |
|---|---|
| `GET /api/screens` | The list of screens. A screen key sees only its own |
| `POST /api/screens/register` | `{"screen_id", "label", "kind", "width", "height"}`. The id is yours to choose, 1 to 40 of `a-z 0-9 - _`, and must stay the same for that tablet |
| `GET /api/screens/poll?screen=<id>&since=<n>&wait=25` | Events for one screen. The request is held until one arrives. Ask first with `since=999999999` to read `last_id` without old events |
| `POST /api/screens/session` | `{"screen", "page"}` returns a one-use address, good for a minute, that signs the screen's web view in and opens the page (`home`, `recipe`, `cook`, `timers`, or `app` for the whole interface). For `app` and `cook` the reply also has `"exit": "page"`: the page draws its own Exit, so the app needs no close button over it. A missing, used or expired ticket answers 401 with the header `X-PR-Screen: reconnect` and a page with no form; ask for a new ticket |
| `POST /api/screens/show-recipe` | `{"query", "screen", "page"}` finds a recipe by what was said, makes it that screen's recipe (and the kitchen's current one) and moves the screen to it. `page` is `recipe` (the whole recipe) or `cook` (cook mode at step 1, read out in `speech`). The reply has `result` (`shown`, `ambiguous` with `candidates`, or `not_found`) and a `speech` line. After `ambiguous`, send `choice` with the answer ("the second one", "the best-ever one") or `slug`; the answer keeps the page the question was asked for |
| `GET /api/screens/recipe?screen=<id>` | The recipe open on that screen: `{"ok": true, "recipe": {"title"}, "title", "servings", "ingredients" (lines with scaled amounts), "steps" ([{"number", "text", "timers"}]), "step" (the step you are on, from 1, or null), "current", "timers", "speech"}`, or `{"ok": false, "result": "none", "speech"}` when nothing is open there. With no screen, the kitchen's current recipe. A screen key always names one of its own screens: with none it is refused (400), and another key's screen answers 403. The step, timer and cooked routes below work the same way |
| `POST /api/screens/recipe/step` | `{"screen", "step"}` moves the step you are on: `first`, `next`, `back`, `repeat`, `last`, a number, or `none`. The reply is the recipe as above, with `current` and a `speech` line that reads the step. The screen follows at once |
| `POST /api/screens/recipe/timer` | `{"screen", "step", "query", "origin", "show"}` starts one of the recipe's timers, picked by step number, by words ("the crust", "baking it"), or with neither, the timer of the step you are on. `result` is `started` or `running` (with `timer`), `ambiguous` (with `choices`, each `{"step", "label", "seconds", "speech"}`: ask, then call again with the step), `no_recipe` or `not_found`. A timer that is already counting down is never started twice. `show` moves the screen to its timers |
| `POST /api/screens/recipe/cooked` | `{"screen"}` marks that screen's recipe cooked, the same as **Cooked** on the recipe page: it uses up the matching items in your pantry. Home Assistant's key cannot call it |
| `POST /events/navigate`, `/events/notify`, `/events/camera-popup`, `/events/camera-detect`, `/events/confirm` | Each takes an optional `"screen"` to reach one screen. `navigate` also takes `"page"` in place of a path |
| `POST /events/close` | `{"screen"}` puts a screen back where it rests: a tablet app drops the page, a kitchen display goes home |
| `POST /timers` | `{"label", "seconds", "origin", "screen"}`. `origin` records who set the timer; `screen` moves that screen to its timers |
| `POST /timers/{id}/dismiss`, `POST /timers/dismiss` | Dismiss one finished timer, or all of them |
| `GET /timers/events?since=<n>&wait=25` | What happened to timers (`started`, `extended`, `finished`, `cancelled`, `dismissed`) plus the list as it stands |

Events for a screen are kept for two minutes. If `last_id` comes back lower
than the number you sent, Pantry Raider restarted: read the list again and
carry on from the new number.
