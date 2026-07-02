# Recipes: do you need Mealie?

Pantry Raider always uses Grocy for inventory. Recipes, meal planning, and
shopping lists are a separate, optional layer that runs on **Mealie**. This page
explains what you get when you add Mealie, what works without it, and how to
decide whether it is worth the extra container for your setup.

**Short answer:** if you want the recipe library, "what can I cook from what I
have" suggestions, the week meal plan, or the shopping list, run Mealie. If you
only care about tracking what's in your fridge and when it expires, you can skip
it. Mealie is one more Docker container and a little more RAM, nothing more.

## What you get with each setup

### Grocy only (no Mealie)

This is the lean setup: Pantry Raider plus Grocy, no second recipe service.

You get everything inventory-related: the storage panels, drag-and-drop moves,
expiry tracking and badges, barcode scanning, photo/receipt import, the expiry
defaults table, and the Home Assistant sensors. The whole "reduce waste" side of
the app works fully.

What you do **not** get: the Recipes, Cook ("what can I cook?"), Meal plan, and
Shopping list pages. Those tabs depend on Mealie and stay dark until it is
configured. The Current Recipe tab and timers still work for an imported or
AI-generated recipe, but there is no recipe library to browse.

Pick this if you want the smallest footprint, you already plan meals elsewhere,
or you are running on a tight board (a Pi 4 with 2 GB is comfortable for
Grocy-only).

### Grocy plus Mealie

Add Mealie and the recipe layer lights up:

- **Recipes.** A real recipe manager with a good editor, a URL importer, photo
  and file import, cookbooks, tags, and nutrition. This is where your recipe
  library lives.
- **Cook ("What Can I Cook?").** Ranks your Mealie recipes by how much of each
  one is already in your Grocy stock, and floats items expiring soon to the top.
  This is the headline feature for a spoilage tracker: it turns "this is about to
  go off" into "here is what to make with it".
- **Meal plan.** A week view you can fill from your recipe library.
- **Shopping list.** Including a one-click "add the ingredients I am missing"
  from a recipe, with check-off as you shop.

How the inventory awareness works is worth knowing, because it shapes the
tradeoffs below. Mealie itself has no idea what's in your fridge; by design it
does not track pantry inventory. Pantry Raider bridges that gap itself: it reads
your Grocy stock, matches it against Mealie's recipe ingredients by name, and
does the "ready / needs staples / needs shopping" tiering and the "consume stock
when you cook" step in its own Python code (see
`service/app/services/mealie.py`). So the matching is by ingredient name, which
is good but approximate: "unsalted butter" in a recipe and "butter" in your
stock are matched on shared words, not on a shared product record.

Pick this (the recommended setup for most people) if you want the cooking and
planning features, which are a big part of why the app exists. Plan for 4 GB of
RAM once Mealie is in the mix.

## How to choose

| If you want... | Run |
|---|---|
| Just inventory and expiry tracking | Grocy only |
| Recipe library and a good recipe editor | Add Mealie |
| "What can I cook from what's expiring?" | Add Mealie |
| Meal planning and shopping lists | Add Mealie |
| The smallest possible footprint / a 2 GB board | Grocy only |
| The full app as advertised | Add Mealie |

You are not locked in. Start Grocy-only and add Mealie later whenever you want
the recipe features; the recipe tabs light up as soon as Mealie is configured in
the setup wizard, and your inventory data is untouched.

## Adding Mealie

Mealie ships as an opt-in Docker Compose profile, so you only run it if you ask
for it.

On a server or Docker host, add the profile to your `up` command:

```bash
docker compose --profile with-grocy --profile with-mealie up -d
```

On a Raspberry Pi appliance, Mealie is installed by default for the Pi Hosted
mode. To add it later to a running device:

```bash
cd /opt/foodassistant
docker compose --profile with-mealie up -d
```

Mealie comes up on port 9285. Create an API token inside Mealie, then paste the
Mealie URL and token into the Pantry Raider setup wizard at `/setup` and test the
connection. The Recipes, Cook, Meal plan, and Shopping tabs become available once
the connection succeeds.

See [Platforms](platforms.md) for the full profile and port reference, and the
[README](../README.md) install section for the one-line installers.

## A note on Grocy's own recipe feature

Grocy ships its own recipe and meal-plan module, with native stock-fulfillment
checking and a one-click "add missing ingredients to the shopping list". Because
the ingredients are linked to real Grocy products, Grocy's fulfillment is exact
rather than name-matched. That is genuinely appealing for a single-box appliance:
recipes, stock, meal plan, and shopping list would all live in one service with
one backup and one product catalog, and no second container.

Pantry Raider does **not** use Grocy's recipe module today. The entire recipe,
cook, meal-plan, and shopping experience in the app is built on Mealie's API, and
`service/app/services/grocy.py` implements no recipe support. Running the recipe
features therefore means running Mealie. Using Grocy as the recipe backend
instead would be a real feature to build, not a setting to flip; it is tracked as
possible future work, not something you can turn on now. If a Grocy-native recipe
mode would make your setup simpler, that is useful feedback to raise on the
[issue tracker](https://github.com/Syracuse3DPrintingOrg/PantryRaider/issues).
