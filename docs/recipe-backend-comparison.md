# Recipe backend: Grocy vs Mealie

Bead: FoodAssistant-0o5

## Summary

FoodAssistant already requires Grocy for inventory, and Grocy ships a full
recipes feature: a recipe data model with scalable ingredient lists, a meal
planner, native stock fulfillment checking, one-click "add missing ingredients
to shopping list", a stock-aware "Due Score" that ranks recipes by what is about
to expire, and a "consume recipe" action that decrements stock. Mealie is a
nicer recipe manager (better editor, URL scraper, OCR import, cookbooks, tags,
nutrition) but it has no inventory awareness at all. The current app papers over
that gap by reading Grocy stock itself and running token matching
(`classify_recipes`) and a manual consume loop in Python.

Recommendation: make Grocy the default recipe backend so a stock install needs
no extra container, and keep Mealie as an optional upgrade for households that
want the richer recipe-authoring experience. This is worth a follow-up
implementation bead; the setting and the surfaces it touches are sketched at the
end. Do not implement it under this bead.

## What each integration does today (in this codebase)

Grocy (`service/app/services/grocy.py`): inventory only. Stock add/consume/move,
product and location management, expiring lookup, shopping list CRUD, restock
suggestions, stock log. The client never calls any Grocy recipe or meal-plan
endpoint, even though Grocy exposes them.

Mealie (`service/app/services/mealie.py` plus `service/app/routers/mealie.py`):
everything recipe-shaped. Recipe search and detail, URL/photo/LLM import, recipe
create, meal plan CRUD, shopping list CRUD, plus the
`classify_recipes` inventory matcher. Crucially, the stock-aware behavior is not
Mealie's: the router fetches `GrocyClient().get_full_stock()` and the Python code
does the token matching, tiering (ready / staples / shopping), the
"add missing to shopping list" diff (`/suggest/add-missing`), and the
"mark cooked -> consume Grocy stock" loop (`/cooked`). Mealie is just the recipe
store; Grocy stock plus local Python provides the intelligence.

So the app has already re-implemented, in Python against Mealie data, the three
things Grocy does natively: fulfillment checking, add-not-fulfilled to shopping
list, and consume-on-cook.

## Capability comparison

### Data model

Grocy: recipes are first-class objects (`/objects/recipes`) with ingredient rows
(`/objects/recipes_pos`) that reference real Grocy products and quantity units,
plus `base_servings` / `desired_servings` for scaling, per-ingredient "only
check if in stock" / "don't add to shopping list" flags, and recipe nesting.
Ingredients are linked to inventory products, which is exactly why fulfillment is
exact rather than fuzzy. Instructions are a single rich-text/markdown field
rather than structured steps.

Mealie: richer authoring model. Structured ingredient objects (food, unit, note,
display), structured instruction steps, description, nutrition, tags, categories,
cookbooks, ratings, images, source URL. Ingredients are free text or linked to
Mealie's own "foods", which are not the same entities as Grocy products, so
matching back to inventory is always approximate.

Verdict: Mealie wins on authoring richness; Grocy wins on inventory linkage.

### Meal planning

Grocy: meal planner with day slots (breakfast/lunch/dinner and custom sections),
recipe entries, product entries, and free-text notes; meal-plan recipes feed the
same stock-fulfillment and shopping-list machinery.

Mealie: calendar view, plan rules for random/templated planning, household
sharing. More polished as a standalone planner.

Verdict: both cover the appliance use case. Mealie's planner UI is nicer; Grocy's
is tied directly to stock.

### Shopping-list integration

Grocy: native and exact. `POST /recipes/{id}/add-not-fulfilled-products-to-shoppinglist`
adds precisely the missing quantities (recipe need minus current stock) as real
products. The shopping list and the inventory share the same product catalog.

Mealie: has shopping lists, but they are recipe-driven, not stock-driven. The app
currently computes "missing" itself by token-diffing Mealie ingredients against
Grocy stock and pushing free-text notes (`/suggest/add-missing`). It works but is
approximate and the items are notes, not catalog products.

Verdict: Grocy wins clearly. Its shopping list closes the loop with stock; this
is the feature the app most laboriously reconstructs for Mealie.

### Stock-aware suggestions / fulfillment

Grocy: native. `GET /recipes/{id}/fulfillment` and `GET /recipes/fulfillment`
return whether each recipe is cookable from stock, the missing product count, and
costs. The "Due Score" ranks recipes by how well they use up items that are due
soon or overdue, which is the same goal as this app's expiring-first tiering and
`expiring_items_used` scoring.

Mealie: explicitly out of scope. The maintainers have stated they do not intend
to track pantry inventory because of the upkeep burden, and community requests
for "suggest recipes from what I have" remain unimplemented. Any stock awareness
on top of Mealie must be built outside Mealie, which is exactly what
`classify_recipes` is.

Verdict: Grocy wins decisively, and this is the single most relevant axis for a
spoilage-tracking appliance. Grocy's Due Score is conceptually what this project
built by hand.

### UI quality

Grocy: functional, dense, dated. Server-rendered, works offline, fine but not
pretty.

Mealie: modern Vue SPA, attractive recipe cards, good mobile experience, markdown
editor. Clearly the better cooking-and-browsing surface.

Verdict: Mealie wins. This is the main reason to keep it available as an upgrade.

### API ergonomics

Grocy: one REST API with Swagger UI, a generic `/objects/{entity}` CRUD layer
plus purpose-built recipe verbs (fulfillment, add-not-fulfilled, consume, copy).
Single API key. Same base URL the app already talks to for stock, so no new
service, port, or credential.

Mealie: clean documented REST API, bearer token auth, but version drift (v1
`/groups` vs v2 `/households`, moved scraper paths) which the client already has
to probe and work around. Recipe POST then PATCH two-step. Separate service, URL,
and token to configure.

Verdict: roughly even on raw quality; Grocy wins on "it is already wired up and
authenticated".

### Operational cost

Grocy: zero additional cost. Already required and running for inventory.

Mealie: an extra Docker container (port 9285, `--profile with-mealie`), its own
database, memory and disk, plus a second URL and API token in `/setup`. On a Pi
appliance that is real overhead.

Verdict: Grocy wins. No extra container is the headline benefit.

### Offline / local appliance fit

Both are self-hosted and run fully offline. Grocy's advantage is that recipes,
stock, meal plan, and shopping list live in one service with one backup, one
auth, and one catalog of products, which suits a single-box appliance. Mealie
adds a second moving part for a second backup and a second failure mode.

Verdict: Grocy wins for the appliance default; Mealie remains fine for users who
accept the extra container.

## Recommendation

Make Grocy the default recipe backend. A fresh install should get working
recipes, meal planning, stock-aware suggestions, and shopping-list integration
with no second container, using Grocy endpoints the app does not call yet
(`/objects/recipes`, `/objects/recipes_pos`, `/objects/meal_plan`,
`/recipes/{id}/fulfillment`, `/recipes/fulfillment`,
`/recipes/{id}/add-not-fulfilled-products-to-shoppinglist`,
`/recipes/{id}/consume`). Keep Mealie as an explicit opt-in upgrade for
households that want the better editor, URL/OCR import, cookbooks, and nicer UI.

The fit is unusually clean because the app already does, in Python, the exact
work Grocy does natively. With the Grocy backend, fulfillment tiering, "add
missing to list", and "mark cooked / consume" can be delegated to Grocy verbs
instead of being recomputed by token matching, which should be both simpler and
more accurate (real quantities and real products rather than fuzzy name tokens).

One caveat to record for the implementation bead: external recipe suggestions
(TheMealDB / Spoonacular) and LLM import/extraction currently save into Mealie.
With a Grocy backend, "save this external/LLM recipe" must map to Grocy's recipe
plus recipe_pos model, and free-text ingredients have to be resolved or created
as Grocy products to get real fulfillment. That mapping is the main new work; it
is not hard, but it is the piece that does not already exist.

### What a `recipe_backend = grocy | mealie` setting would touch

Scoping notes for a follow-up implementation bead. Do not build this here.

Config (`service/app/config.py`): add a `recipe_backend` field (default
`grocy`), add it to `_SAVEABLE` and to the satellite-synced key lists alongside
the existing `mealie_*` and `recipe_source` keys. Mealie keys stay, used only
when `recipe_backend == "mealie"`. Update `mealie_configured()` callers so the
recipe/meal-plan/shopping pages light up when either backend is ready, not only
when Mealie is set.

Service layer: introduce a recipe-backend abstraction (a small interface like the
existing `VisionProvider` pattern) with two implementations. Add the missing
recipe/meal-plan methods to `GrocyClient` (list/get/create recipe and positions,
meal-plan CRUD, fulfillment, add-not-fulfilled-to-list, consume). The Mealie
implementation already exists in `services/mealie.py`. The
`classify_recipes` tiering can stay as the Mealie path's stock matcher, and the
Grocy path can use native fulfillment / Due Score instead.

Router (`service/app/routers/mealie.py`): this is where most change concentrates.
Today every endpoint instantiates `MealieClient()` directly. Each route
(`/mealplan`, `/recipes`, `/suggest`, `/suggest/add-missing`, `/cooked`,
`/shopping*`, the import/create/generate routes, and the
`/mealplan/summary` and `/shopping/summary` HA sensor routes) needs to branch on
the configured backend, or call through the abstraction. Consider renaming the
router prefix from `/mealie` to something backend-neutral like `/recipes`, with a
redirect for compatibility, since the path name leaks the backend.

UI templates (`service/app/templates/recipes.html`, `mealplan.html`,
`shopping.html`, and the nav in `base.html`): mostly unchanged if they keep
hitting the same JSON routes, but any hardcoded "open in Mealie" link
(`mealie_link_url()`) must become a backend-aware deep link (Grocy recipe URL vs
Mealie recipe URL), and the setup wizard copy should explain the Grocy default
versus the Mealie upgrade.

Setup (`service/app/routers/setup.py`): add the `recipe_backend` choice to the
wizard, show the Mealie URL/token fields only when Mealie is selected, and make
the default path require no Mealie input.

Home Assistant: the `mealplan/summary` and `shopping/summary` sensor endpoints
keep their response shape, so HA config is unaffected as long as the routes stay
available regardless of backend.

## Sources

- Grocy product overview (recipes, meal planner, stock fulfillment, due score,
  one-click shopping list): https://grocy.info/
- Grocy cooking tutorial (recipe fulfillment, base/desired servings,
  per-ingredient stock-check toggle, consume recipe):
  https://github.com/grocy/grocy-docs/blob/master/tutorials/cooking.md
- Grocy OpenAPI spec (recipe and meal-plan endpoints:
  `/recipes/{id}/fulfillment`, `/recipes/fulfillment`,
  `/recipes/{id}/add-not-fulfilled-products-to-shoppinglist`,
  `/recipes/{id}/consume`):
  https://github.com/grocy/grocy/blob/master/grocy.openapi.json
- Grocy API reference overview:
  https://deepwiki.com/grocy/grocy/3.4-api-reference
- Grocy changelog (recipe cost calculation, fulfillment fixes, due score):
  https://grocy.info/changelog
- Mealie features (scraper, OCR/AI import, cookbooks, tags, nutrition, Vue UI,
  meal planner, plan rules): https://docs.mealie.io/documentation/getting-started/features/
- Mealie repository overview: https://github.com/mealie-recipes/mealie
- Mealie maintainers on not tracking pantry inventory; community requests for
  inventory-based recipe suggestions:
  https://github.com/mealie-recipes/mealie/discussions/2448
