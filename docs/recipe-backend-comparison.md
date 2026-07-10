# Recipes: built in, Mealie optional

Pantry Raider always uses Grocy for inventory. Recipes, the meal plan, and
the shopping list are built into Pantry Raider itself, so a standard install
(Pantry Raider plus Grocy) gives you the whole app: nothing else to run, no
extra container, and everything fits comfortably on a 2 GB board.

**Short answer:** you do not need Mealie. Every recipe feature works out of
the box. Mealie support remains for people who already use it: connect yours
and it keeps working exactly as before, or copy your library into Pantry
Raider with one click and stop running the extra service.

## What the standard install includes

- **Recipes.** Your own library, stored in Pantry Raider. Import from a web
  address, a photo, a PDF, a file, the external catalogs, or the Forager
  community, generate a recipe with AI, or write one by hand. Pictures ride
  along and everything is covered by the normal app backup.
- **Cook ("What can I cook?").** Ranks your recipes by how much of each one
  is already in your Grocy stock and floats items expiring soon to the top,
  turning "this is about to go off" into "here is what to make with it".
- **Meal plan.** A week view you fill from your library or with free-text
  lines like "Leftovers".
- **Shopping list.** Kept in Grocy, right next to your inventory, so "things
  you buy" live with "things you stock". One click adds a recipe's missing
  ingredients; check items off as you shop, from the page, a barcode
  scanner in shopping mode, or the Stream Deck.
- **On the Line, timers, printing, Home Assistant sensors.** All of it works
  against the built-in library and list.

The stock matching is by ingredient name: "unsalted butter" in a recipe and
"butter" in your stock match on shared words, with your staples list filling
the gaps. That is the same matching the app has always used, and it works
for imported and AI recipes that will never be linked to a product record.

## If you already use Mealie

Connecting a Mealie you already run is fully supported:

- Add its address and API token in Settings under Recipes. Your Mealie
  library, meal plan, and shopping list keep working through Pantry Raider
  exactly as before. Nothing about your Mealie is changed.
- Whenever you are ready, use **Copy recipes into Pantry Raider** (Settings,
  Recipes pane, or the button on the Recipes page). It copies every recipe,
  pictures included, switches the library to Pantry Raider, and moves the
  shopping list to Grocy. It is safe to run more than once (recipes you
  already have are skipped), and your Mealie is left untouched, so you can
  keep it around or retire the container on your own schedule.
- Mealie export files import directly on the Recipes page too, so even a
  Mealie that Pantry Raider never connected to can hand over its library.

## Running Mealie alongside (optional)

New installs do not set up Mealie. If you want it anyway, it ships as an
opt-in Docker Compose profile:

```bash
docker compose --profile with-grocy --profile with-mealie up -d
```

On a Raspberry Pi appliance, set `ENABLE_MEALIE=true` before installing, or
add it later on a running device:

```bash
cd /opt/foodassistant
docker compose --profile with-mealie up -d
```

Mealie comes up on port 9285 and needs about 4 GB of total RAM to run
comfortably alongside the rest of the stack. Connect it in Settings under
Recipes.

See [Platforms](platforms.md) for the full profile and port reference, and
the [README](https://github.com/Syracuse3DPrintingOrg/PantryRaider#install)
install section for the one-line installers.

## A note on Grocy's own recipe feature

Grocy ships its own recipe module with product-linked, exact fulfillment.
Pantry Raider does not use it: Grocy requires every ingredient to be a real
product in its catalog, which would fill your inventory (the heart of the
app) with one-off phantom products every time you import a web recipe. The
built-in library keeps recipes in their own store and leaves your Grocy
product list clean; only the shopping list lives in Grocy, where free-text
lines are first-class.
